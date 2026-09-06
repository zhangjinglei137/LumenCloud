"""
Emby 防重基线 / 遗漏集 / 已有集 / 影视库展示服务。

- find_emby_id      ：按 TMDB id 定位 Emby 条目（/Items + AnyProviderIdEquals）
                      未命中时做 P11 二次模糊查询兜底（需传入 title）
- get_missing_episodes：查剧集遗漏集（/emby/Shows/Missing），作为防重基线
- list_episodes     ：查已有集（/Shows/{id}/Episodes），供防重基线
- list_library      ：查 Emby 影视库（/Items Recursive 全量），供 des-3 展示页；
                      支持 item_type / status（SeriesStatus 在更/完结）/ anime（动漫库）
- list_libraries    ：查 Emby 媒体库列表（/Library/VirtualFolders），供动漫库识别

契约参照 n8n 旧流程（docs/新系统设计.md §10）：
    GET {base}/Items?api_key=...&Recursive=true&HasTmdbId=true&Fields=ProviderIds
        &AnyProviderIdEquals=tmdb.<id>
    GET {base}/emby/Shows/Missing?ParentId=<id>&api_key=...&IncludeUnaired=true&IncludeSpecials=false
故障（超时 / 5xx / 网络异常）统一抛 EmbyUnavailable，由调用方按 fail-safe 处理
（docs/新系统设计.md §4.3：Emby 故障时不进入新缺集发现）。
"""
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models import Media
from app.services import config_store

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = httpx.Timeout(30.0)   # Emby 为慢端点（/Shows/Missing 实测 7s+），超时须充足

# 动漫库名称关键词（大小写不敏感）：Emby 没有 CollectionType=anime，
# 动漫库只能靠 VirtualFolderInfo.Name 匹配或库白名单判定（des-3 增强 C）
ANIME_LIBRARY_KEYWORDS = ("动漫", "动画", "anime")
# VirtualFolderInfo.CollectionType 白名单：仅保留影视类媒体库（movies/tvshows 或 null）
LIBRARY_COLLECTION_TYPES = ("movies", "tvshows")


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


async def list_libraries() -> list[dict[str, Any]]:
    """查 Emby 媒体库列表（/Library/VirtualFolders），供动漫库识别（可选增强 C）。

    归一化每条 {item_id, name, collection_type}，仅保留 CollectionType 为
    movies/tvshows 或 null 的媒体库（Emby 无 CollectionType=anime，动漫库只能靠
    Name 关键词匹配）。VirtualFolders 调用失败时记 warn 返回空列表，不阻断主流程；
    但配置缺失仍抛 EmbyUnavailable（保持前端「未配置空态」四态）。
    """
    try:
        payload = await _get("/Library/VirtualFolders", {})
    except EmbyUnavailable as exc:
        if "未配置" in str(exc):
            raise  # 配置缺失 → 交由 list_library 主流程按四态处理
        logger.warning("Emby 媒体库列表获取失败（动漫筛选降级为空）: %s", exc)
        return []
    # VirtualFolders 返回裸数组；防御性兼容 dict 包装（Items 键）
    raw_folders: Any = payload if isinstance(payload, list) else payload.get("Items")
    folders: list[Any] = raw_folders or []
    result: list[dict[str, Any]] = []
    for folder in folders:
        collection_type = folder.get("CollectionType")
        if collection_type not in LIBRARY_COLLECTION_TYPES and collection_type is not None:
            continue
        result.append({
            "item_id": folder.get("ItemId"),
            "name": folder.get("Name"),
            "collection_type": collection_type,
        })
    logger.info("Emby 媒体库列表: %d 个（影视类）", len(result))
    return result


async def _find_anime_library_item_id() -> Optional[str]:
    """定位首个动漫库的 ItemId（VirtualFolderInfo.ItemId，作 /Items 的 ParentId）。

    Name 含「动漫」「动画」「anime」即判定为动漫库（大小写不敏感）；
    找不到返回 None。
    """
    for library in await list_libraries():
        name = (library.get("name") or "").lower()
        if any(keyword in name for keyword in ANIME_LIBRARY_KEYWORDS):
            return library.get("item_id")
    return None


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


async def list_library(
    item_type: Optional[str] = None,
    status: Optional[str] = None,
    anime: bool = False,
) -> list[dict[str, Any]]:
    """查 Emby 影视库（des-3 Emby 展示页 / GET /api/emby/library）。

    参数:
        item_type: "movie" 电影 / "series" 剧集 / None 全部（Movie,Series）
        status:    "continuing" 仅在更 / "ended" 已完结；非空时 Items 请求加
                   SeriesStatus（注意是 SeriesStatus 而非 Status，Status 需 Fields 才返回），
                   并确保 IncludeItemTypes 含 Series
        anime:     True 时限定动漫库（按 Name 关键词匹配 VirtualFolder，取 ItemId 作
                   ParentId）；忽略 item_type 过滤（动漫库通常为剧集，亦有剧场版电影）；
                   找不到动漫库则返回空列表（前端显示空态，不算错误）
    返回:
        归一化条目列表，每项含 emby_id/title/type/year/poster_url/
        community_rating/tmdb_id/emby_web_url、series_status（Q12：在更/完结，
        "continuing"/"ended"/None），及增强 B 的 in_media/media_id；
        emby_web_url 在 serverId 获取失败/无 Id 时为 None（前端隐藏「在 Emby 中打开」）
    异常:
        EmbyUnavailable: 配置缺失 / 请求失败
    """
    # 动漫模式：定位动漫库（Name 关键词匹配），找不到直接返回空列表
    parent_id: Optional[str] = None
    if anime:
        parent_id = await _find_anime_library_item_id()
        if not parent_id:
            logger.info("Emby 未找到动漫库（Name 含 动漫/动画/anime），返回空列表")
            return []
        item_type = None  # 动漫模式忽略 item_type 过滤（动漫库通常为剧集，亦有剧场版电影）

    # IncludeItemTypes：按 item_type 选择；status 非空时须含 Series（SeriesStatus 只对剧集生效）
    include_item_types = {"movie": "Movie", "series": "Series"}.get(item_type or "", "Movie,Series")
    if status and "Series" not in include_item_types:
        include_item_types = f"{include_item_types},Series"

    params: dict[str, Any] = {
        "Recursive": "true",
        "IncludeItemTypes": include_item_types,
        "Fields": "ProviderIds,CommunityRating,ProductionYear,SeriesStatus",
        "Limit": "500",
    }
    if parent_id:
        params["ParentId"] = parent_id
    if status:
        params["SeriesStatus"] = status

    payload = await _get("/Items", params)
    base = _base_url()
    api_key = config_store.get("emby_api_key", settings.EMBY_API_KEY)
    items = payload.get("Items", []) or []
    # D-1（P1）：详情链接的 serverId 一次获取，批量复用（惰性缓存，失败降级 None）
    server_id = await _get_server_id()
    result: list[dict[str, Any]] = []
    for item in items:
        normalized = _normalize_library_item(item, base, api_key, server_id)
        if normalized is not None:
            result.append(normalized)

    # 本地已收录标记（in_media/media_id）：按 tmdb_id 批量查 Media 表（单次 IN 查询）
    await _attach_in_media_flag(result)

    logger.info(
        "Emby 影视库（item_type=%s, status=%s, anime=%s）: %d 条",
        item_type, status, anime, len(result),
    )
    return result
