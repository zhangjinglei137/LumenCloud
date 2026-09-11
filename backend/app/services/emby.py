"""
Emby 防重基线 / 遗漏集 / 已有集 / 影视库展示服务。

- find_emby_id      ：按 TMDB id 定位 Emby 条目（/Items + AnyProviderIdEquals）
                      未命中时做 P11 二次模糊查询兜底（需传入 title）
- get_missing_episodes：查剧集遗漏集（/emby/Shows/Missing），作为防重基线
- list_episodes     ：查已有集（/Shows/{id}/Episodes），供防重基线
- list_library      ：查 Emby 影视库（/Items 按 library_id 单库 Recursive 查询），供 des-3
                      展示页；library_id 必选（Views ViewId → ParentId），支持
                      item_type / status（SeriesStatus 在更/完结）；series 条目连载判定
                      TMDB 优先（有 tmdb_id → /3/tv/{id} status 字段，无 → Emby
                      SeriesStatus 兜底，见 _attach_tmdb_series_status）
- list_library_folders ：查 Emby 媒体库列表（/Users/{UserId}/Views），返回
                       [{id, name, collection_type, is_anime}]，供媒体库分类 Tab
- refresh_library   ：触发 Emby 全库扫描（POST /Library/Refresh），NasTools 转移/刮削
                      完成后调用，加速新文件入库（library_check 轮询兜底确认）

契约参照 n8n 旧流程（docs/新系统设计.md §10）：
    GET {base}/Items?api_key=...&Recursive=true&HasTmdbId=true&Fields=ProviderIds
        &AnyProviderIdEquals=tmdb.<id>
    GET {base}/emby/Shows/Missing?ParentId=<id>&api_key=...&IncludeUnaired=true&IncludeSpecials=false
故障（超时 / 5xx / 网络异常）统一抛 EmbyUnavailable，由调用方按 fail-safe 处理
（docs/新系统设计.md §4.3：Emby 故障时不进入新缺集发现）。
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models import Media
from app.services import config_store
from app.services.tmdb import TMDBUnavailable, get_by_tmdb_id

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = httpx.Timeout(30.0)   # Emby 为慢端点（/Shows/Missing 实测 7s+），超时须充足
# Task 8：全库扫描触发端点（/Library/Refresh）扫描异步执行，请求本身应快速返回；
# 但 Emby 调度扫描时可能短暂阻塞，超时放宽到 60s（对比常规 30s）。
REFRESH_TIMEOUT = httpx.Timeout(60.0)

# 动漫库名称关键词（大小写不敏感）：Emby 没有 CollectionType=anime，
# 动漫库只能靠 VirtualFolderInfo.Name 匹配或库白名单判定（des-3 增强 C）
ANIME_LIBRARY_KEYWORDS = ("动漫", "动画", "anime")
# VirtualFolderInfo.CollectionType 白名单：仅保留影视类媒体库（movies/tvshows/mixed 或 null；
# null 由过滤逻辑的 is not None 分支放行），music/homevideos/book 等非影视库不进入列表
LIBRARY_COLLECTION_TYPES = ("movies", "tvshows", "mixed")

# 连载判定 TMDB 优先：get_by_tmdb_id 返回的 tv status 原值 → 库页 series_status
# 小写约定（与 Emby SeriesStatus 归一化口径一致）。
# - Returning Series → continuing（在更）
# - Ended / Canceled → ended（完结 / 被砍均不再连载）
# - Pilot / None / 其它 → 无有效判定，保留 Emby SeriesStatus 原值
_TMDB_TV_STATUS_MAP = {
    "Returning Series": "continuing",
    "Ended": "ended",
    "Canceled": "ended",
}
# TMDB 批量回源并发上限：list_library 一次可能返回数百条剧集，逐条直连会触发
# TMDB 免费 API 限流（~40 req/10s）；信号量限并发，缓存命中不占并发额度。
_TMDB_BATCH_CONCURRENCY = 5
# P2-8：Emby /Items 分页拉取（list_library 大库完整）。单页 500（原硬编码 Limit）；
# 当前页满单页即 StartIndex 翻页（模式对齐 alist.list_dir），直到少于单页或达到
# 页数上限防御（防 Emby 恒满页导致死循环拉爆）。
_LIST_PAGE_SIZE = 500
_LIST_MAX_PAGES = 40  # 500 × 40 = 20000 条，远超影视库实际规模，仅作异常兜底


class EmbyUnavailable(Exception):
    """Emby 服务不可用：配置缺失 / 网络故障 / 非 2xx 响应 / 响应格式异常。"""


def _base_url() -> str:
    # Phase 8 配置入库：DB 优先、env fallback；函数内读取，每次调用读最新值
    base = (config_store.get("emby_base_url", settings.EMBY_BASE_URL) or "").strip().rstrip("/")
    if not base:
        raise EmbyUnavailable("EMBY_BASE_URL 未配置")
    return base


def _check_config() -> None:
    if not config_store.get("emby_api_key", settings.EMBY_API_KEY):
        raise EmbyUnavailable("EMBY_API_KEY 未配置")


def _episode_code(season: Any, episode: Any) -> str:
    """SxxExx 归一化。季/集缺失时退化为 'S??E??'，由调用方忽略无效码。"""
    try:
        s = int(season)
        e = int(episode)
        return f"S{s:02d}E{e:02d}"
    except (TypeError, ValueError):
        return ""


def _normalize_episode(item: dict[str, Any]) -> dict[str, Any]:
    """把 Emby 的 Item 归一化为服务层通用结构（已有集 / 遗漏集共用）。"""
    season = item.get("ParentIndexNumber")
    episode = item.get("IndexNumber")
    return {
        "emby_id": item.get("Id"),
        "season": season,
        "episode": episode,
        "name": item.get("Name"),
        "code": _episode_code(season, episode),
        # C-1：Emby 原始 PremiereDate（ISO 8601 字符串或缺失/None），供 aired-only 过滤
        "premiere_date": item.get("PremiereDate"),
    }


async def _get(path: str, params: dict[str, Any], timeout: httpx.Timeout = REQUEST_TIMEOUT) -> dict[str, Any]:
    """GET 封装：鉴权 + 故障归一（网络异常/非 2xx/JSON 异常 → EmbyUnavailable）。"""
    _check_config()
    url = f"{_base_url()}{path}"
    params = dict(params)
    params.setdefault("api_key", config_store.get("emby_api_key", settings.EMBY_API_KEY))

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            logger.warning("Emby 请求失败 %s: %s", path, exc)
            raise EmbyUnavailable(f"Emby 请求失败: {exc}") from exc

    if resp.status_code >= 400:
        logger.warning("Emby 非 2xx 响应 %s: %s", path, resp.status_code)
        raise EmbyUnavailable(f"Emby 返回 HTTP {resp.status_code}")

    try:
        return resp.json()
    except ValueError as exc:
        raise EmbyUnavailable("Emby 响应不是合法 JSON") from exc


# D-1（P1）：Emby serverId（/System/Info/Public 的 Id）——web 详情链接必需参数，
# 缺失时前端打开空白页。恒定不变，模块级惰性缓存（获取一次全局复用）。
_SERVER_ID: Optional[str] = None
_SERVER_ID_LOADED = False


async def _get_server_id() -> Optional[str]:
    """惰性获取 Emby serverId：成功/失败均只尝试一次并缓存结果（恒定值）。

    Public 端点（无需 api_key）；失败或响应无 Id → None（调用方降级处理）。
    """
    global _SERVER_ID, _SERVER_ID_LOADED
    if _SERVER_ID_LOADED:
        return _SERVER_ID
    _SERVER_ID_LOADED = True
    try:
        payload = await _get("/System/Info/Public", {})
        _SERVER_ID = payload.get("Id") or None
    except EmbyUnavailable as exc:
        logger.warning("[emby] 获取 serverId 失败，详情链接降级为 None: %s", exc)
        _SERVER_ID = None
    return _SERVER_ID


# 生产回归修复：媒体库列表数据源从 /Library/MediaFolders 改为 /Users/{UserId}/Views。
# MediaFolders 项的 Id 不是 /Items 接受的 ParentId（ViewId）；Views 项的 Id ==
# VirtualFolders ItemId == ViewId，才是 list_library 作为 ParentId 的合法来源。
# UserId 恒定不变，模块级惰性缓存（获取一次全局复用，失败降级 None）。
_USER_ID: Optional[str] = None
_USER_ID_LOADED = False


async def _get_user_id() -> Optional[str]:
    """惰性获取 Emby UserId（/Users 首个用户的 Id）。

    系统 api_key 具备管理员权限，/Users 返回用户数组（或 dict 包装的 Items）；
    取首个用户的 Id 作为 /Users/{UserId}/Views 查询目标。成功/非配置类失败均只
    尝试一次并缓存结果（恒定值）；「未配置」错误（EMBY_BASE_URL/EMBY_API_KEY
    缺失）原样 re-raise（保持前端「未配置空态」），并重置缓存标记允许配置修复
    后重试；其他失败或响应无 Id → None（调用方降级为空列表）。
    """
    global _USER_ID, _USER_ID_LOADED
    if _USER_ID_LOADED:
        return _USER_ID
    _USER_ID_LOADED = True
    try:
        payload = await _get("/Users", {})
        raw: Any = payload if isinstance(payload, list) else payload.get("Items")
        users: list[Any] = raw or []
        first = users[0] if users else {}
        _USER_ID = first.get("Id") or None
    except EmbyUnavailable as exc:
        if "未配置" in str(exc):
            # 配置缺失可恢复：不固化缓存，配置修复后允许重试（Finding-1 修复）
            _USER_ID_LOADED = False
            raise
        logger.warning("[emby] 获取 UserId 失败，媒体库列表降级为空: %s", exc)
        _USER_ID = None
    return _USER_ID


async def refresh_library() -> None:
    """触发 Emby 全库扫描（POST /Library/Refresh，管理端 Admin 认证）。

    Task 8：NasTools 转移/刮削完成后调用，让 Emby 尽快入库新文件；
    library_check 轮询仍是收录确认的兜底。

    /Library/Refresh 为官方唯一全库扫描端点（无按目录扫描端点，经 dev.emby.media
    验证）；扫描异步执行，成功/失败均返回。认证沿用本模块 api_key 查询参数约定
    （与 _get 的 _check_config/_base_url 逻辑一致）；非 2xx / 网络异常 → EmbyUnavailable
    （调用方降级为告警，不阻断主链路）。

    异常:
        EmbyUnavailable: 配置缺失 / 请求失败 / 非 2xx 响应
    """
    _check_config()
    url = f"{_base_url()}/Library/Refresh"
    params = {"api_key": config_store.get("emby_api_key", settings.EMBY_API_KEY)}

    async with httpx.AsyncClient(timeout=REFRESH_TIMEOUT) as client:
        try:
            resp = await client.post(url, params=params)
        except httpx.HTTPError as exc:
            logger.warning("Emby 全库扫描触发失败 %s: %s", url, exc)
            raise EmbyUnavailable(f"Emby 请求失败: {exc}") from exc

    if resp.status_code >= 400:
        logger.warning("Emby 全库扫描非 2xx 响应: %s", resp.status_code)
        raise EmbyUnavailable(f"Emby 返回 HTTP {resp.status_code}")

    logger.info("Emby 全库扫描已触发（%s）", url)


async def find_emby_id(tmdb_id: int, title: Optional[str] = None) -> Optional[str]:
    """按 TMDB id 定位 Emby 条目 id。

    P11 兜底（docs/新系统设计.md §10 / 问题矩阵 P11）：
        精确匹配（AnyProviderIdEquals=tmdb.<id>）未命中且传入 title 时，
        二次模糊查询（searchTerm=title）避免 Emby 实际已存在却被误判为不在库。

    参数:
        tmdb_id: TMDB id
        title:   影视名称（可选；用于未精确命中时的模糊兜底）
    返回:
        Emby Item Id；未找到返回 None
    异常:
        EmbyUnavailable: 配置缺失 / 请求失败
    """
    _check_config()
    items = (await _get("/Items", {
        "Recursive": "true",
        "HasTmdbId": "true",
        "Fields": "ProviderIds",
        "AnyProviderIdEquals": f"tmdb.{tmdb_id}",
    })).get("Items", []) or []

    if items:
        logger.info("Emby 精确命中 tmdb=%s → %s", tmdb_id, items[0].get("Id"))
        return items[0].get("Id")

    # P11：精确未命中 → 标题模糊查询兜底
    fuzzy_title = (title or "").strip()
    if fuzzy_title:
        logger.info("Emby 精确未命中 tmdb=%s，进行标题模糊兜底查询「%s」", tmdb_id, fuzzy_title)
        fuzzy = (await _get("/Items", {
            "Recursive": "true",
            "searchTerm": fuzzy_title,
            "IncludeItemTypes": "Series,Movie",
            "Limit": "5",
            "Fields": "ProviderIds",
        })).get("Items", []) or []
        if fuzzy:
            logger.info("Emby 模糊兜底命中「%s」→ %s", fuzzy_title, fuzzy[0].get("Id"))
            return fuzzy[0].get("Id")

    return None


async def get_missing_episodes(emby_id: str) -> list[dict[str, Any]]:
    """查询剧集遗漏集（防重基线 / 扫描入口）。

    C-1 aired-only 语义：请求 Fields=PremiereDate 显式获取播出日期，
    返回前剔除 PremiereDate 在未来（未播出）的集（预告/未来集入漏判定），
    避免追更场景把未播出集误入队转存（下载空文件/预告致失败）。

    参数:
        emby_id: Emby 剧集条目 id
    返回:
        遗漏集列表（仅已播出），每项含 emby_id/season/episode/name/code/
        premiere_date（Emby 原始值原样保留，缺失或未播出时的过滤见内部逻辑）
    异常:
        EmbyUnavailable: 配置缺失 / 请求失败
    """
    payload = await _get("/emby/Shows/Missing", {
        "ParentId": emby_id,
        "IncludeUnaired": "true",
        "IncludeSpecials": "false",
        "Fields": "PremiereDate",
    })
    items = payload.get("Items", []) or []
    result = [_normalize_episode(item) for item in items]
    result.sort(key=lambda ep: (ep["season"] or 0, ep["episode"] or 0))
    # C-1（P1）：aired-only——PremiereDate 在未来（未播出）的集剔除，防追更误入队预告/空文件。
    # 缺失/无法解析 PremiereDate 的集保守保留（宁可多余不去，不误杀信息不全的集）。
    now = datetime.now(timezone.utc)
    filtered: list[dict[str, Any]] = []
    for ep in result:
        raw = ep.get("premiere_date")
        if raw:
            try:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                dt = None
            if dt is not None and dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt is not None and dt > now:
                continue  # 未播出集
        filtered.append(ep)
    dropped = len(result) - len(filtered)
    result = filtered
    logger.info(
        "Emby 遗漏集（emby_id=%s）: %d 集（过滤未播出 %d 集）",
        emby_id, len(result), dropped,
    )
    return result


async def list_episodes(emby_id: str) -> list[dict[str, Any]]:
    """查询已有集（供防重基线，与遗漏集互补）。

    参数:
        emby_id: Emby 剧集条目 id
    返回:
        已有集列表，每项含 emby_id/season/episode/name/code
    异常:
        EmbyUnavailable: 配置缺失 / 请求失败
    """
    payload = await _get(f"/Shows/{emby_id}/Episodes", {
        "Fields": "ProviderIds",
        "IncludeSpecials": "false",
    })
    items = payload.get("Items", []) or []
    result = [_normalize_episode(item) for item in items]
    result.sort(key=lambda ep: (ep["season"] or 0, ep["episode"] or 0))
    logger.info("Emby 已有集（emby_id=%s）: %d 集", emby_id, len(result))
    return result


def _normalize_library_item(
    item: dict[str, Any], base: str, api_key: Optional[str], server_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """把 Emby Item 归一化为影视库 DTO（des-3 Emby 展示页）。

    仅保留 Movie / Series；其余类型（Folder 等纯目录）返回 None 跳过；
    另过滤 tmdb_id 与海报均为空的目录性质条目。

    D-1（P1）：emby_web_url 依赖 server_id（web 详情路由必带 serverId 定位后端
    实例，缺参打开空白页）；server_id 为空时 emby_web_url 降级 None。
    """
    item_type = item.get("Type")
    if item_type == "Movie":
        kind = "movie"
    elif item_type == "Series":
        kind = "series"
    else:
        return None

    item_id = item.get("Id")
    provider_ids = item.get("ProviderIds") or {}
    tmdb_id = provider_ids.get("Tmdb")
    image_tags = item.get("ImageTags") or {}
    has_poster = bool(image_tags.get("Primary"))

    # 过滤 tmdb_id 与海报均为空的纯目录条目（无任何可展示信息）
    if not tmdb_id and not has_poster:
        return None

    poster_url = None
    if has_poster:
        poster_url = f"{base}/Items/{item_id}/Images/Primary?api_key={api_key}"

    # D-1（P1）：Emby web 详情路由依赖 serverId 定位后端实例（缺参 → 空白页）；
    # serverId 获取失败时降级 None（前端隐藏「在 Emby 中打开」入口）
    emby_web_url = None
    if server_id:
        emby_web_url = f"{base}/web/index.html#!/item?id={item_id}&serverId={server_id}"

    return {
        "emby_id": item_id,
        "title": item.get("Name"),
        "type": kind,
        "year": item.get("ProductionYear"),
        "poster_url": poster_url,
        "community_rating": item.get("CommunityRating"),
        # Q12：连载状态（Emby Series 条目为 "continuing"/"ended"，Movie 或无该字段
        # 时缺失 → None，原样透传；用于库页「仅在更」展示）
        "series_status": item.get("SeriesStatus"),
        "tmdb_id": str(tmdb_id) if tmdb_id else None,  # str 返回，避免大整数精度问题
        "emby_web_url": emby_web_url,
    }


async def list_library_folders() -> list[dict[str, Any]]:
    """查媒体库列表（/Users/{UserId}/Views），返回 [{id, name, collection_type, is_anime}]。

    数据源说明（生产回归修复）：/Library/MediaFolders 项的 Id 不是 /Items 接受的
    ParentId，以其作为 library_id 会导致 Emby 4xx；/Users/{UserId}/Views 项的 Id
    即 ViewId（== VirtualFolders ItemId），可直接作为 /Items 的 ParentId，故改用
    该端点（UserId 经 _get_user_id 惰性获取，失败降级空列表）。
    is_anime：仅对 tvshows 库按 Name 含 ANIME_LIBRARY_KEYWORDS 判定（大小写不敏感）。
    emby_series_library_ids 白名单非空时，仅过滤 tvshows 库（其他类型不受影响）；
    白名单值为原 VirtualFolders ItemId，与 Views 的 Id 相同（无需迁移）。
    Views 调用失败时记 warn 返回空列表，不阻断主流程；配置缺失（「未配置」）仍抛
    EmbyUnavailable（保持前端「未配置空态」）；UserId 获取失败降级空列表。
    """
    user_id = await _get_user_id()
    if user_id is None:
        return []
    try:
        payload = await _get(f"/Users/{user_id}/Views", {})
    except EmbyUnavailable as exc:
        if "未配置" in str(exc):
            raise
        logger.warning("Emby 媒体库列表获取失败（分类降级为空）: %s", exc)
        return []
    # Views 返回 dict 包装（Items 键）；防御性兼容裸数组
    raw_folders: Any = payload if isinstance(payload, list) else payload.get("Items")
    folders: list[Any] = raw_folders or []
    # 白名单：仅影响 tvshows 库
    whitelist_raw = (config_store.get("emby_series_library_ids") or "").strip()
    whitelist = {x.strip() for x in whitelist_raw.split(",") if x.strip()}

    result: list[dict[str, Any]] = []
    for folder in folders:
        collection_type = folder.get("CollectionType")
        # 仅保留影视类媒体库（movies/tvshows/mixed 或 null）；music/homevideos/book 等不进入
        if collection_type is not None and collection_type not in LIBRARY_COLLECTION_TYPES:
            continue
        folder_id = folder.get("Id")
        name = folder.get("Name") or ""
        is_anime = False
        if collection_type == "tvshows":
            if whitelist and folder_id not in whitelist:
                continue  # 白名单过滤剧集库
            lower_name = name.lower()
            is_anime = any(keyword in lower_name for keyword in ANIME_LIBRARY_KEYWORDS)
        result.append({
            "id": folder_id,
            "name": name,
            "collection_type": collection_type,
            "is_anime": is_anime,
        })
    logger.info("Emby 媒体库列表: %d 个（影视类）", len(result))
    return result


async def _attach_in_media_flag(items: list[dict[str, Any]]) -> None:
    """为库条目附加本地收录标记（增强 B：in_media / media_id）。

    收集全部非空 tmdb_id 后单次 IN 查询 Media 表，避免 N+1；
    media.tmdb_id 为 int，Emby ProviderIds.Tmdb 为字符串，比对前转换。
    DB 异常降级为全部 in_media=False（仅记 warn，不阻断 Emby 展示）。
    """
    tmdb_ids: set[int] = set()
    for item in items:
        tmdb_id = item.get("tmdb_id")
        if tmdb_id:
            try:
                tmdb_ids.add(int(tmdb_id))
            except (TypeError, ValueError):
                continue  # 非法 id 忽略，保持未收录

    id_by_tmdb: dict[int, int] = {}
    if tmdb_ids:
        try:
            async with async_session() as session:
                rows = (
                    await session.execute(
                        select(Media.id, Media.tmdb_id).where(Media.tmdb_id.in_(tmdb_ids))
                    )
                ).all()
            id_by_tmdb = {row.tmdb_id: row.id for row in rows if row.tmdb_id is not None}
        except Exception as exc:  # noqa: BLE001 DB 不可用降级，不阻断 Emby 展示
            logger.warning("本地 Media 收录标记查询失败，降级为全部未收录: %s", exc)

    for item in items:
        tmdb_id = item.get("tmdb_id")
        media_id = None
        if tmdb_id:
            try:
                media_id = id_by_tmdb.get(int(tmdb_id))
            except (TypeError, ValueError):
                pass
        item["in_media"] = media_id is not None
        item["media_id"] = media_id


async def _attach_tmdb_series_status(items: list[dict[str, Any]]) -> None:
    """TMDB 优先连载判定：就地覆盖 series 条目的 series_status。

    规则（oracle 决策）：Emby 条目有 tmdb_id → 用 TMDB /3/tv/{id} 的 status
    字段判定；无 tmdb_id → 保留 Emby SeriesStatus 原值。

    批量策略（成本控制——list_library 一次返回数百条 series，逐条查 TMDB 是
    N+1 网络调用，必须收敛请求量）：
    1. 候选过滤：仅「Emby SeriesStatus 缺失或为 continuing」且 tmdb_id 非空的
       series 条目进入候选。Emby 已判 ended 的条目直接信任 Emby 值——完结剧集
       极少复活，即便 TMDB 先行报 Returning Series，Emby 检测到新集也会自行
       更新，接受该延迟一致，从而省掉大部分请求；
    2. 命中有效缓存跳过：候选逐条走 get_by_tmdb_id，内部命中 7 天 TTL 的
       tmdb_cache 直接返回（零网络调用）；仅未命中/过期的条目回源 TMDB；
    3. 并发收敛：回源并发受 _TMDB_BATCH_CONCURRENCY 信号量约束（TMDB 免费 API
       限流保护）；
    4. 静默降级：TMDB 未配置 / 调用失败 / 响应无 status（含 Pilot）→ 保留
       Emby SeriesStatus 原值，不抛异常、不阻塞 list_library 主流程。
    """
    candidates: list[tuple[dict[str, Any], str]] = []  # (item, tmdb_id 字符串)
    for item in items:
        if item.get("type") != "series":
            continue  # movie 不判定连载
        tmdb_id = item.get("tmdb_id")
        if not tmdb_id:
            continue  # 无 tmdb_id → Emby SeriesStatus 兜底
        if item.get("series_status") == "ended":
            continue  # Emby 已判完结 → 信任 Emby 值，省一次网络调用
        candidates.append((item, str(tmdb_id)))

    if not candidates:
        return

    sem = asyncio.Semaphore(_TMDB_BATCH_CONCURRENCY)

    async def _fetch(tmdb_id: str) -> Optional[str]:
        async with sem:
            try:
                meta = await get_by_tmdb_id(tmdb_id, "tv")
            except TMDBUnavailable as exc:
                # TMDB 未配置 / 请求失败 → 静默回退 Emby SeriesStatus
                logger.info("TMDB 连载判定降级（回退 Emby SeriesStatus）tmdb_id=%s: %s", tmdb_id, exc)
                return None
            return meta.get("tv_status")

    results = await asyncio.gather(*(_fetch(tmdb_id) for _, tmdb_id in candidates))
    for (item, _), tv_status in zip(candidates, results):
        mapped = _TMDB_TV_STATUS_MAP.get(tv_status or "")
        if mapped is not None:
            item["series_status"] = mapped


async def _fetch_items(params: dict[str, Any]) -> list[dict[str, Any]]:
    """分页拉取 /Items（StartIndex+Limit 翻页，含 _LIST_MAX_PAGES 防御）。

    P2-8 分页逻辑抽为独立辅助：list_library 主流程与 Q2 媒体库白名单
    （逐库 ParentId 查询）两条路径共用，保证分页口径一致。
    """
    items: list[dict[str, Any]] = []
    start_index = 0
    page = 0
    while True:
        page += 1
        if page > _LIST_MAX_PAGES:
            logger.warning(
                "Emby 影视库分页超过 %d 页上限，提前停止（已收集 %d 条）",
                _LIST_MAX_PAGES, len(items),
            )
            break
        page_params = dict(params)
        page_params["StartIndex"] = str(start_index)
        payload = await _get("/Items", page_params)
        chunk = payload.get("Items", []) or []
        items.extend(chunk)
        # 当前页条数 < 单页 → 已到最后一页（含 0 条），停止分页
        if len(chunk) < _LIST_PAGE_SIZE:
            break
        start_index += len(chunk)
    return items


def _build_library_params(
    item_type: Optional[str],
    status: Optional[str],
    parent_id: Optional[str] = None,
) -> dict[str, Any]:
    """构造 /Items 查询参数：IncludeItemTypes 映射 + SeriesStatus + 分页 + 可选 ParentId。

    单库（list_library）与全部聚合（list_all_library）共用，保证口径一致。
    """
    include_item_types = {"movie": "Movie", "series": "Series"}.get(item_type or "", "Movie,Series")
    if status and "Series" not in include_item_types:
        include_item_types = f"{include_item_types},Series"
    params: dict[str, Any] = {
        "Recursive": "true",
        "IncludeItemTypes": include_item_types,
        "Fields": "ProviderIds,CommunityRating,ProductionYear,SeriesStatus",
        "Limit": str(_LIST_PAGE_SIZE),
    }
    if parent_id:
        params["ParentId"] = parent_id
    if status:
        params["SeriesStatus"] = status
    return params


async def list_library(
    library_id: str,
    item_type: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict[str, Any]]:
    """查 Emby 影视库（des-3 Emby 展示页 / GET /api/emby/library）。

    参数:
        library_id: 必选，目标媒体库 Id（/Users/{UserId}/Views 的 ViewId），作为 /Items 的 ParentId
        item_type:  "movie" 电影 / "series" 剧集 / None 全部（Movie,Series）
        status:     "continuing" 仅在更 / "ended" 已完结；非空时加 SeriesStatus 并确保 IncludeItemTypes 含 Series
    返回:
        归一化条目列表（同现有字段，含 series_status/in_media/media_id/emby_web_url）
    异常:
        EmbyUnavailable: 配置缺失 / 请求失败
    """
    params = _build_library_params(item_type, status, parent_id=library_id)

    items = await _fetch_items(params)

    base = _base_url()
    api_key = config_store.get("emby_api_key", settings.EMBY_API_KEY)
    # D-1（P1）：详情链接的 serverId 一次获取，批量复用（惰性缓存，失败降级 None）
    server_id = await _get_server_id()
    result: list[dict[str, Any]] = []
    for item in items:
        normalized = _normalize_library_item(item, base, api_key, server_id)
        if normalized is not None:
            result.append(normalized)

    # TMDB 优先连载判定：series 条目按「SeriesStatus 缺失或为 continuing + 缓存未命中」
    # 批量查 TMDB /3/tv/{id}，失败静默回退 Emby SeriesStatus（不阻塞展示）
    await _attach_tmdb_series_status(result)

    # 本地已收录标记（in_media/media_id）：按 tmdb_id 批量查 Media 表（单次 IN 查询）
    await _attach_in_media_flag(result)

    logger.info(
        "Emby 影视库（library_id=%s, item_type=%s, status=%s）: %d 条",
        library_id, item_type, status, len(result),
    )
    return result


# 全部聚合逐库并发上限：Emby 单实例，并发过高无收益且可能打爆服务端（低于 TMDB 批处理 5）
_LIBRARY_FETCH_CONCURRENCY = 3


async def list_all_library(
    item_type: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict[str, Any]]:
    """聚合全部影视类媒体库条目（GET /api/emby/library/all）。

    1. 复用 list_library_folders() 取全部影视类库（movies/tvshows/mixed/null，
       tvshows 白名单过滤沿用）；配置缺失抛 EmbyUnavailable（emby_not_configured）；
       无影视库 → 返回 []。
    2. 逐库并发 _fetch_items（信号量 _LIBRARY_FETCH_CONCURRENCY 限并发）。
    3. 归一化 + 按 emby_id 去重（保留先到者；Emby ItemId 全局唯一，去重仅防御）。
    4. 复用 _attach_tmdb_series_status + _attach_in_media_flag。
    5. 错误语义：全部库失败（含库列表获取失败）→ 抛 EmbyUnavailable（emby_unreachable，
       取首个失败原因）；部分失败 → 返回成功部分 + warn 日志。
    """
    folders = await list_library_folders()
    if not folders:
        # list_library_folders 对非配置类失败（网络/5xx）静默降级为空列表，无法与
        # 「确无影视库」区分；全部聚合入口必须区分「空」与「失败」（静默空会被前端
        # 误判为库内无内容）。以 /System/Info/Public（Public 端点）探针确认可达性：
        # 可达 → 确为无影视库，返回 []；不可达 → 全部库失败，异常上抛（emby_unreachable）。
        await _get("/System/Info/Public", {})
        return []

    sem = asyncio.Semaphore(_LIBRARY_FETCH_CONCURRENCY)

    async def _fetch_one(folder: dict[str, Any]) -> list[dict[str, Any]]:
        async with sem:
            params = _build_library_params(item_type, status, parent_id=folder["id"])
            return await _fetch_items(params)

    errors: list[Exception] = []
    results = await asyncio.gather(
        *(_fetch_one(f) for f in folders), return_exceptions=True
    )
    for exc in results:
        if isinstance(exc, Exception):
            errors.append(exc)

    base = _base_url()
    api_key = config_store.get("emby_api_key", settings.EMBY_API_KEY)
    seen: dict[str, dict[str, Any]] = {}
    for chunk in results:
        if isinstance(chunk, Exception):
            continue
        for raw in chunk:
            # 注意：当前 _normalize_library_item 为 4 参签名（item/base/api_key/server_id）；
            # Task 5 改为 3 参（删除 api_key）后，此处同步改为 _normalize_library_item(raw, base, server_id=None)
            normalized = _normalize_library_item(raw, base, api_key, server_id=None)
            if normalized is not None:
                seen.setdefault(normalized["emby_id"], normalized)

    # D-1：serverId 批量获取（惰性缓存），一次性为条目附加 emby_web_url
    server_id = await _get_server_id()
    items = list(seen.values())
    if server_id:
        base = _base_url()
        for item in items:
            item["emby_web_url"] = f"{base}/web/index.html#!/item?id={item['emby_id']}&serverId={server_id}"

    await _attach_tmdb_series_status(items)
    await _attach_in_media_flag(items)

    if len(errors) == len(folders):
        raise errors[0]  # 全部库失败 → 上抛（路由映射 emby_unreachable）
    if errors:
        logger.warning("Emby 全部聚合部分库失败: %d/%d", len(errors), len(folders))
    logger.info("Emby 全部影视库聚合: %d 条（去重后）", len(items))
    return items
