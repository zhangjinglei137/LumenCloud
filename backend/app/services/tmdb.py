"""
TMDB 元数据搜索服务。

- 端点：GET /3/search/multi（docs/新系统设计.md §10）、GET /3/{movie|tv}/{id}
- API key 来自 settings.TMDB_API_KEY（环境变量，敏感凭据不进数据库）
- TMDB_PROXY 支持：配置后以该地址作为 API 镜像根地址（旧 config 代理字段语义，
  即替换官方 api.themoviedb.org 主机名，镜像域名需提供同路径 /3/... 接口）
- P2-2 出口代理双模式：TMDB_HTTP_PROXY 作为 httpx 出口代理（proxy= 参数），
  与镜像根地址相互独立可叠加——镜像请求同样可走出口代理；无镜像时配合官方
  地址直连官方；皆空则官方直连
- P3 元数据缓存：tmdb_cache 表（TmdbCache 模型，由并行 lane 提供）做 7 天
  级缓存；缓存层为纯优化——DB/表不可用、upsert 失败一律降级（仅告警），
  不阻断搜索/回源主流程
- 全部使用 httpx.AsyncClient（每请求创建，不阻塞事件循环）
"""
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.services import config_store

logger = logging.getLogger(__name__)

TMDB_DEFAULT_BASE_URL = "https://api.themoviedb.org"
SEARCH_MULTI_PATH = "/3/search/multi"
REQUEST_TIMEOUT = httpx.Timeout(10.0)

# P3 元数据缓存 TTL：命中缓存后 7 天内不回源刷新
_CACHE_TTL_DAYS = 7


class TMDBUnavailable(Exception):
    """TMDB 服务不可用：配置缺失 / 网络故障 / 非 2xx 响应 / 响应格式异常。"""


def _base_url() -> str:
    """TMDB API 根地址：优先 settings.TMDB_PROXY（镜像/反向代理），否则官方地址。"""
    # Phase 8 配置入库：DB 优先、env fallback；函数内读取，每次调用读最新值
    proxy = (config_store.get("tmdb_proxy", settings.TMDB_PROXY) or "").strip().rstrip("/")
    if proxy:
        # 防御校验（ora-6）：TMDB_PROXY 语义是「API 镜像根地址」——形如 http://host:port
        # 的配置极可能是科学上网代理端口被误填（用户实证：192.168.3.31:7897 返回 HTTP 400，
        # 因为该端口只接受代理协议、不支持直接 GET /3/search/multi）。
        # 这类误填在请求阶段暴露为 400，用户难排查；此处尽早以明确错误提示。
        if "://" not in proxy and ":" in proxy:
            # 无 scheme 的 host:port（如 192.168.3.31:7897）→ 一定是误填的代理端口
            raise TMDBUnavailable(
                f"TMDB 镜像地址疑似填了代理端口（{proxy}）。TMDB 镜像应为反代根地址 "
                "如 https://tmdb-mirror.example.com；科学上网代理请填到「TMDB 出口代理」"
                "（tmdb_http_proxy），不要填在这里（会返回 400）。设置页 → 服务凭据配置 "
                "→ 元数据 · TMDB 修改。"
            )
    return proxy or TMDB_DEFAULT_BASE_URL


def _extract_year(item: dict[str, Any]) -> str | None:
    """从 TMDB 条目提取 4 位年份（线上反馈修复 Q1：搜索结果显示年份）。

    - movie → release_date 前 4 位
    - tv    → first_air_date 前 4 位
    - person（及其他类型）→ None
    防御：日期为空 / 格式非法 / 非 4 位数字前缀 → None（不因异常中断单条装配）。
    """
    media_type = item.get("media_type")
    if media_type == "movie":
        raw = item.get("release_date")
    elif media_type == "tv":
        raw = item.get("first_air_date")
    else:
        return None
    text = str(raw or "").strip()
    if len(text) >= 4 and text[:4].isdigit():
        return text[:4]
    return None


def _normalize_aliases(raw: list | None, original_title: str | None) -> list[str]:
    """别名归一化：小写、去空格、去括号版本后缀、去重；上限 20 防膨胀。

    - 首个候选为 original_title（movie 的 original_title / tv 的 original_name，
      详情响应的权威主名），其后拼接 also_known_as 原始数组；
    - 每项 strip + lower + 去空格（如 "Soul Land" → "soulland"）+ 去除
      "(YYYY)" 括号版本后缀（如 "Soul Land (2020)" → "soulland"）；
    - 非 str / 空值跳过；去重保序；上限 20 条（TMDB 别名可能很长，防膨胀）。
    """
    out: list[str] = []
    for name in [original_title, *(raw or [])]:
        if not name or not isinstance(name, str):
            continue
        s = name.strip().lower().replace(" ", "")
        s = re.sub(r"\(\d{4}\)", "", s).strip()
        if s and s not in out:
            out.append(s)
    return out[:20]


def _client_kwargs() -> dict[str, Any]:
    """构造 httpx.AsyncClient 关键字参数（P2-2 出口代理双模式）。

    - requirements.txt 固定 httpx==0.28.1（>=0.26）：出口代理用单数 proxy= 参数
      （0.26 起弃用旧式 proxies= dict，0.28 已移除）；
    - 未配置 tmdb_http_proxy 时省略该参数（零配置直连，且保持对既有
      AsyncClient(timeout=...) 调用形态兼容）。
    """
    proxy = (config_store.get("tmdb_http_proxy", settings.TMDB_HTTP_PROXY) or "").strip()
    kwargs: dict[str, Any] = {"timeout": REQUEST_TIMEOUT}
    if proxy:
        kwargs["proxy"] = proxy
    return kwargs


def _now() -> datetime:
    """naive UTC 当前时间（与 models 的 DateTime 存储口径一致，见 settings._now）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _read_cache(tmdb_id: str | int, media_type: str) -> dict[str, Any] | None:
    """读 tmdb_cache：命中且 updated_at 距今 < 7 天 → 归一化 dict；否则返回 None。

    缓存层纯优化：DB/表不可用（TmdbCache 由并行 lane 提供，可能尚未落盘）、
    字段异常一律降级返回 None（走回源），不阻断主流程。
    """
    try:
        from app.models import TmdbCache  # noqa: PLC0415 延迟导入（并行 lane 提供）

        key = str(tmdb_id)
        now = _now()
        async with async_session() as session:
            row = (
                await session.execute(
                    select(TmdbCache).where(
                        TmdbCache.tmdb_id == key,
                        TmdbCache.media_type == media_type,
                    )
                )
            ).scalar_one_or_none()
        if row is None or row.updated_at is None:
            return None
        if (now - row.updated_at).total_seconds() >= _CACHE_TTL_DAYS * 86400:
            return None  # 超 7 天 → 视为未命中，回源刷新
        year = str(row.year) if row.year is not None else None
        aliases: list[str] = []
        if row.aliases:
            try:
                parsed = json.loads(row.aliases)
                if isinstance(parsed, list):
                    aliases = [str(a) for a in parsed]
            except (TypeError, ValueError):
                aliases = []  # 落库数据异常（手改/旧脏数据）→ 降级空列表
        return {
            "tmdb_id": str(row.tmdb_id),
            "title": row.title or "",
            "media_type": row.media_type,
            "poster_path": row.poster_path,
            "year": year,
            # 影视状态原值（TMDB movie/tv 详情 status 字段；无 → None）
            "tv_status": row.tv_status,
            "status": row.tv_status,
            # TV 总集数（/3/tv/{id} 的 number_of_episodes；movie/缺失 → None）。
            # 全量模式集号范围校验（scan A3）数据基础。
            "number_of_episodes": row.number_of_episodes,
            # 别名集合（JSON 数组字符串列反序列化；无 → 空列表）
            "aliases": aliases,
        }
    except Exception as exc:  # noqa: BLE001 缓存不可用降级回源
        logger.warning("tmdb_cache 读取失败（降级回源）: %s", exc)
        return None


async def _upsert_cache(
    tmdb_id: str | int,
    media_type: str,
    title: str,
    poster_path: str | None,
    year: str | None,
    tv_status: str | None = None,
    number_of_episodes: int | None = None,
    aliases: list[str] | None = None,
) -> None:
    """tmdb_cache 幂等 upsert（命中更新 / 未命中新增，updated_at=now）。

    - tmdb_id 字符串化（P3 契约）；
    - year 转 int（可空）：int("2023") → 2023，缺失/非法 → None；
    - tv_status 参数语义为「影视状态原值」（movie/tv 通用，即 TMDB 详情响应的
      status 字段，列名沿用 tv_status 不动，无需迁移）：仅非 None 时覆盖
      （search_multi 等无 status 来源的调用传 None，不覆盖 get_by_tmdb_id
      已落库的状态，防误清）；
    - number_of_episodes（tv 总集数，movie/无 → None）：仅非 None 时覆盖，
      语义与 tv_status 一致（search_multi 等无来源的调用不覆盖已落库值）；
    - aliases（别名集合，JSON 数组字符串落库）：仅非 None 时覆盖——详情回源
      （get_by_tmdb_id）传归一化结果（空列表也覆盖，详情是权威来源），
      search_multi 等 search 响应无该字段的调用不传（None）不覆盖已落库别名；
    - 缓存层纯优化：失败仅告警（表未建 / DB 不可用等），不阻断调用方。
    """
    try:
        from app.models import TmdbCache  # noqa: PLC0415 延迟导入（并行 lane 提供）

        key = str(tmdb_id)
        try:
            year_int = int(year) if year else None
        except (TypeError, ValueError):
            year_int = None
        now = _now()
        async with async_session() as session:
            row = (
                await session.execute(
                    select(TmdbCache).where(
                        TmdbCache.tmdb_id == key,
                        TmdbCache.media_type == media_type,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = TmdbCache(tmdb_id=key, media_type=media_type, updated_at=now)
                session.add(row)
            row.title = title
            row.poster_path = poster_path
            row.year = year_int
            if tv_status is not None:
                row.tv_status = tv_status
            if number_of_episodes is not None:
                row.number_of_episodes = number_of_episodes
            if aliases is not None:
                row.aliases = json.dumps(aliases, ensure_ascii=False)
            row.updated_at = now
            await session.commit()
    except Exception as exc:  # noqa: BLE001 缓存落盘失败降级（不阻断返回）
        logger.warning("tmdb_cache upsert 失败（忽略，不影响返回）: %s", exc)


async def get_by_tmdb_id(tmdb_id: str | int, media_type: str, force_refresh: bool = False) -> dict[str, Any]:
    """按 TMDB id 取单条元数据（P3 元数据缓存逻辑）。

    流程：
    1. 查 tmdb_cache（tmdb_id + media_type 命中且 updated_at 距今 < 7 天）
       → 直接返回缓存（不回源）；
    2. 未命中 / 超 7 天 → 回源 GET /3/{movie|tv}/{id}（media_type 决定路径，
       复用 _client_kwargs 出口代理与 config_store api_key 读取）→ 归一化
       title / poster_path / year / number_of_episodes → upsert 缓存
       （updated_at=now）→ 返回。

    force_refresh=True：跳过缓存直接回源（scan A3 集号范围校验用——tmdb_cache
    行可能被 search_multi 写入的不完整数据占据（缺 number_of_episodes 等详情
    字段），缓存命中会拿到 None 导致集号范围校验失效；强制回源补全并刷新缓存）。

    返回 dict：{tmdb_id, title, media_type, poster_path, year, status, tv_status,
    number_of_episodes, aliases}。
    status / tv_status 同值：TMDB movie/tv 详情响应的 status 字段原值
    （movie: Released/In Production/Post Production/Rumored/Planned/Canceled；
    tv: Returning Series/Ended/Canceled/Pilot）；无该字段 → None。
    tv_status 键保留，兼容 emby.py _attach_tmdb_series_status 的读取。
    number_of_episodes：仅 tv 详情响应有（movie 响应无该字段 → None）；全量模式
    集号范围校验（scan A3）的数据基础。
    aliases：别名集合（original_name/original_title + also_known_as 归一化
    小写、去重、上限 20）；无别名来源 → 空列表。详情回源是权威来源，落库时
    空列表也会覆盖旧值；缓存命中时从 tmdb_cache.aliases 列反序列化返回。

    异常:
        TMDBUnavailable: 未配置 key / 请求失败 / 响应异常
    """
    if not force_refresh:
        cached = await _read_cache(tmdb_id, media_type)
        if cached is not None:
            logger.info("tmdb_cache 命中 tmdb_id=%s media_type=%s", tmdb_id, media_type)
            return cached

    api_key = config_store.get("tmdb_api_key", settings.TMDB_API_KEY)
    if not api_key:
        raise TMDBUnavailable("TMDB_API_KEY 未配置")

    media_type = "movie" if media_type == "movie" else "tv"
    url = f"{_base_url()}/3/{media_type}/{tmdb_id}"
    params = {"api_key": api_key, "language": "zh-CN"}

    async with httpx.AsyncClient(**_client_kwargs()) as client:
        try:
            resp = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            logger.warning("TMDB 请求失败: %s", exc)
            raise TMDBUnavailable(f"TMDB 请求失败: {exc}") from exc

    if resp.status_code != 200:
        logger.warning("TMDB 非 200 响应: %s", resp.status_code)
        raise TMDBUnavailable(f"TMDB 返回 HTTP {resp.status_code}")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise TMDBUnavailable("TMDB 响应不是合法 JSON") from exc

    title = payload.get("title") or payload.get("name") or ""
    poster_path = payload.get("poster_path")
    year = _extract_year(
        {
            "media_type": media_type,
            "release_date": payload.get("release_date"),
            "first_air_date": payload.get("first_air_date"),
        }
    )
    # 影视状态原值（movie/tv 通用）：TMDB 详情响应的 status 字段。
    # movie: Released/In Production/Post Production/Rumored/Planned/Canceled；
    # tv: Returning Series/Ended/Canceled/Pilot；无该字段 → None。
    status = payload.get("status")
    # TV 总集数（仅 tv 详情响应有；movie 响应无该字段 → None）
    number_of_episodes = payload.get("number_of_episodes")
    # 别名集合：tv 详情用 original_name、movie 用 original_title；+ also_known_as。
    # search 响应无这些字段（search_multi 传 None 不覆盖），仅详情回源落库。
    orig = payload.get("original_name") or payload.get("original_title")
    aliases = _normalize_aliases(payload.get("also_known_as") or [], orig)
    await _upsert_cache(
        tmdb_id, media_type, title, poster_path, year,
        tv_status=status, number_of_episodes=number_of_episodes,
        aliases=aliases,
    )

    return {
        "tmdb_id": str(tmdb_id),
        "title": title,
        "media_type": media_type,
        "poster_path": poster_path,
        "year": year,
        "status": status,
        "tv_status": status,  # 兼容 emby.py _attach_tmdb_series_status 读取
        "number_of_episodes": number_of_episodes,
        "aliases": aliases,
    }


async def search_multi(q: str) -> list[dict[str, Any]]:
    """多类型影视搜索（movie/tv/person 混合结果）。

    参数:
        q: 搜索关键词
    返回:
        归一化结果列表，每项含:
            title:       影视名称（movie 用 title，tv 用 name）
            tmdb_id:     TMDB id
            media_type:  movie / tv / person
            poster_path: 海报相对路径（可为 None，前端拼图床完整地址）
            year:        上映/首播年份（4 位字符串；movie 取 release_date、
                         tv 取 first_air_date；缺失/非法/person → None）
    P3: 每个命中结果 upsert 到 tmdb_cache（幂等；失败仅告警，不阻断返回）。
    异常:
        ValueError:    关键词为空
        TMDBUnavailable: 未配置 key / 请求失败 / 响应异常
    """
    keyword = (q or "").strip()
    if not keyword:
        raise ValueError("搜索关键词不能为空")

    api_key = config_store.get("tmdb_api_key", settings.TMDB_API_KEY)
    if not api_key:
        raise TMDBUnavailable("TMDB_API_KEY 未配置")

    url = f"{_base_url()}{SEARCH_MULTI_PATH}"
    params = {
        "api_key": api_key,
        "query": keyword,
        "language": "zh-CN",
        "page": 1,
    }

    async with httpx.AsyncClient(**_client_kwargs()) as client:
        try:
            resp = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            logger.warning("TMDB 请求失败: %s", exc)
            raise TMDBUnavailable(f"TMDB 请求失败: {exc}") from exc

    if resp.status_code != 200:
        logger.warning("TMDB 非 200 响应: %s", resp.status_code)
        raise TMDBUnavailable(f"TMDB 返回 HTTP {resp.status_code}")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise TMDBUnavailable("TMDB 响应不是合法 JSON") from exc

    results: list[dict[str, Any]] = []
    for item in payload.get("results", []) or []:
        entry = {
            "title": item.get("title") or item.get("name") or "",
            "tmdb_id": item.get("id"),
            "media_type": item.get("media_type"),
            "poster_path": item.get("poster_path"),
            "year": _extract_year(item),
        }
        results.append(entry)
        # P3 元数据缓存：搜索命中即 upsert（tmdb_id 字符串化；id 缺失的异常条目跳过；
        # tv_status 无来源传 None，不覆盖 get_by_tmdb_id 已落库的连载状态）
        if entry["tmdb_id"] is not None:
            await _upsert_cache(
                tmdb_id=entry["tmdb_id"],
                media_type=entry["media_type"],
                title=entry["title"],
                poster_path=entry["poster_path"],
                year=entry["year"],
            )
    logger.info("TMDB 搜索「%s」命中 %d 条", keyword, len(results))
    return results


# ---------------------------------------------------------------------------
# 剧集季内每集首播日期（详情页集数状态 tag 增强字段）
# ---------------------------------------------------------------------------
# 进程内 TTL 缓存：详情页同一剧集多集同季只回源一次；缓存层纯优化，任何
# 读取/回源失败一律降级返回 {}（log warning），绝不把异常抛给详情接口。
_SEASON_AIR_TTL = 6 * 3600  # 6 小时
_SEASON_AIR_CACHE: dict[str, tuple[float, dict[int, str | None]]] = {}


async def _fetch_season_episodes(tmdb_id: str | int, season_number: int) -> list[dict[str, Any]] | None:
    """回源 TMDB 指定季的 episodes 原始列表（内部辅助，不做进程内缓存）。

    返回 list[dict]，每项为 season 接口 episodes 数组的原始条目（含
    episode_number / air_date / name 等字段）；失败（api_key 缺失 / 网络 /
    非 200 / JSON 异常）→ log warning 并返回 None。

    None 与「成功但空季」的 [] 有区分，供调用方决定是否写缓存
    （get_tv_season_air_dates / get_tv_all_episodes 共用本辅助）。
    回源模式沿用 _base_url / _client_kwargs / api_key 读取 / httpx 写法
    （参考 get_by_tmdb_id / 原 get_tv_season_air_dates）。
    """
    api_key = config_store.get("tmdb_api_key", settings.TMDB_API_KEY)
    if not api_key:
        logger.warning("TMDB_API_KEY 未配置，season 回源降级 None（season=%s）", season_number)
        return None

    url = f"{_base_url()}/3/tv/{tmdb_id}/season/{season_number}"
    params = {"api_key": api_key, "language": "zh-CN"}
    try:
        async with httpx.AsyncClient(**_client_kwargs()) as client:
            resp = await client.get(url, params=params)
        if resp.status_code != 200:
            logger.warning("TMDB season 回源非 200 响应: %s", resp.status_code)
            return None
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001  详情页增强字段：任何失败降级，不阻断
        logger.warning("TMDB season 回源失败（降级）: %s", exc)
        return None
    return list(payload.get("episodes") or [])


async def get_tv_season_air_dates(tmdb_id: str | int, season_number: int) -> dict[int, str | None]:
    """获取 TV 指定季每集的 TMDB 首播日期（详情页集数 tag 的 air_date 数据源）。

    返回 `{episode_number(int): air_date}`，air_date 为 "YYYY-MM-DD" 字符串或 None
    （TMDB 该集未提供首播日期）。回源复用 _fetch_season_episodes 辅助
    （`GET /3/tv/{tmdb_id}/season/{season_number}`，language=zh-CN）。

    进程内 TTL 缓存（_SEASON_AIR_TTL=6h，key=f"{tmdb_id}:{season_number}"）：
    命中且未过期直接返回；回源失败/非 200/JSON 异常仅 log warning 并返回 {}
    ——这是详情页增强字段，任何失败都不能抛异常拖垮详情接口。
    """
    key = f"{tmdb_id}:{season_number}"
    hit = _SEASON_AIR_CACHE.get(key)
    if hit is not None and _now().timestamp() - hit[0] < _SEASON_AIR_TTL:
        return hit[1]

    episodes = await _fetch_season_episodes(tmdb_id, season_number)
    if episodes is None:
        return {}

    out: dict[int, str | None] = {}
    for ep in episodes:
        ep_num = ep.get("episode_number")
        if isinstance(ep_num, int):
            out[ep_num] = ep.get("air_date")
    # 仅成功回源才写缓存（失败不缓存空结果，避免临时故障期间长时间拿不到数据）
    _SEASON_AIR_CACHE[key] = (_now().timestamp(), out)
    return out


# ---------------------------------------------------------------------------
# TV 全部正片季的每集信息（详情页「TMDB 全集数 + 首播日期」数据源）
# ---------------------------------------------------------------------------
# 进程内 TTL 缓存：详情页同一剧集只回源一次；缓存层纯优化，任何回源失败一律
# 降级返回 []（log warning），绝不把异常抛给详情接口。
_ALL_EPS_TTL = 6 * 3600  # 6 小时
_ALL_EPS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


async def get_tv_all_episodes(tmdb_id: str | int) -> list[dict[str, Any]]:
    """获取 TV 全部正片季的每集信息（详情页「TMDB 全集数 + 首播日期」数据源）。

    返回 list[dict]，每项:
        {"season": int, "episode": int, "air_date": str|None, "name": str|None}
    按 season 升序、episode 升序排列；air_date / name 可能为 None（TMDB 该集
    未提供首播日期 / 集名）。

    实现路径:
    1. `GET /3/tv/{tmdb_id}?language=zh-CN` 取 seasons 数组，过滤
       season_number==0（特辑/预告不算正片）；
    2. 对每个正片季调用 _fetch_season_episodes（与 get_tv_season_air_dates
       共用回源辅助）聚合 episode_number / air_date / name。

    进程内 TTL 缓存（key=f"{tmdb_id}:all_episodes"，TTL 6h）：仅成功聚合出
    非空结果才写缓存。降级语义与 get_tv_season_air_dates 一致：api_key 缺失 /
    网络失败 / 非 200 / JSON 异常一律 log warning 并返回 []（单季失败仅跳过
    该季）——绝不抛出；movie / 无 tmdb_id 场景由调用方控制，不调用本函数。
    """
    cache_key = f"{tmdb_id}:all_episodes"
    hit = _ALL_EPS_CACHE.get(cache_key)
    if hit is not None and _now().timestamp() - hit[0] < _ALL_EPS_TTL:
        return hit[1]

    api_key = config_store.get("tmdb_api_key", settings.TMDB_API_KEY)
    if not api_key:
        logger.warning("TMDB_API_KEY 未配置，全部季集数降级 []（tmdb_id=%s）", tmdb_id)
        return []

    url = f"{_base_url()}/3/tv/{tmdb_id}"
    params = {"api_key": api_key, "language": "zh-CN"}
    try:
        async with httpx.AsyncClient(**_client_kwargs()) as client:
            resp = await client.get(url, params=params)
        if resp.status_code != 200:
            logger.warning("TMDB tv 详情（全集数）非 200 响应: %s", resp.status_code)
            return []
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001  详情页增强字段：任何失败降级 []，不阻断
        logger.warning("TMDB tv 详情（全集数）回源失败（降级 []）: %s", exc)
        return []

    seasons: list[int] = []
    for s in payload.get("seasons") or []:
        if not isinstance(s, dict):
            continue
        sn = s.get("season_number")
        if isinstance(sn, int) and sn > 0:
            seasons.append(sn)  # season_number==0（特辑/预告）不算正片，直接过滤
    seasons = sorted(set(seasons))
    out: list[dict[str, Any]] = []
    for season in seasons:
        eps = await _fetch_season_episodes(tmdb_id, season)
        if eps is None:
            continue  # 单季失败仅跳过，不影响其余季
        for ep in eps:
            ep_num = ep.get("episode_number")
            if not isinstance(ep_num, int):
                continue
            out.append(
                {
                    "season": season,
                    "episode": ep_num,
                    "air_date": ep.get("air_date"),
                    "name": ep.get("name"),
                }
            )

    out.sort(key=lambda e: (e["season"], e["episode"]))
    if out:
        _ALL_EPS_CACHE[cache_key] = (_now().timestamp(), out)
    return out


# ---------------------------------------------------------------------------
# 集信息持久化缓存（episode-status-cache）：episode_info_cache 表读写
# ---------------------------------------------------------------------------
# 注：此处沿用本模块顶部 import 的 async_session 全局名（测试/替换依赖通过
# monkeypatch tmdb_mod.async_session 生效），不另起 _async_session 别名——
# 模块级别名在 import 时一次性绑定真实 sessionmaker，patch 无法覆盖。
from sqlalchemy import delete as _sa_delete, select as _sa_select, update as _sa_update  # noqa: E402
from app.models import EpisodeInfoCache as _EpisodeInfoCache  # noqa: E402


async def refresh_episode_info(tmdb_id: int) -> int:
    """回源 TV 全部正片季每集信息并 upsert 到 episode_info_cache；返回写入行数。

    复用 get_tv_all_episodes（含 zh-CN / season 过滤 / 降级语义）：回源失败或空 → 返回 0，
    保留旧缓存（upsert 不动旧行）。仅供每日刷新任务 / 详情回源兜底调用。
    """
    episodes = await get_tv_all_episodes(tmdb_id)
    if not episodes:
        return 0
    count = 0
    async with async_session() as s:
        async with s.begin():
            for ep in episodes:
                season = ep.get("season")
                episode = ep.get("episode")
                if season is None or episode is None:
                    continue
                row = await s.execute(
                    _sa_select(_EpisodeInfoCache).where(
                        _EpisodeInfoCache.tmdb_id == tmdb_id,
                        _EpisodeInfoCache.season == season,
                        _EpisodeInfoCache.episode == episode,
                    )
                )
                existing = row.scalar_one_or_none()
                if existing is None:
                    s.add(_EpisodeInfoCache(
                        tmdb_id=tmdb_id, season=season, episode=episode,
                        name=ep.get("name"), air_date=ep.get("air_date"),
                    ))
                else:
                    existing.name = ep.get("name")
                    existing.air_date = ep.get("air_date")
                count += 1
    return count


async def get_episode_info(tmdb_id: int) -> list[dict]:
    """读 episode_info_cache，返回 [{season, episode, name, air_date}] 按 season/episode 升序。

    无缓存返回 []（调用方回退回源或降级）。movie / 无 tmdb_id 场景由调用方控制不调用。
    """
    async with async_session() as s:
        rows = (
            (await s.execute(
                _sa_select(_EpisodeInfoCache)
                .where(_EpisodeInfoCache.tmdb_id == tmdb_id)
                .order_by(_EpisodeInfoCache.season.asc(), _EpisodeInfoCache.episode.asc())
            ))
            .scalars()
            .all()
        )
    return [
        {"season": r.season, "episode": r.episode, "name": r.name, "air_date": r.air_date}
        for r in rows
    ]
