"""
Emby 防重基线 / 遗漏集 / 已有集查询服务。

- find_emby_id      ：按 TMDB id 定位 Emby 条目（/Items + AnyProviderIdEquals）
                      未命中时做 P11 二次模糊查询兜底（需传入 title）
- get_missing_episodes：查剧集遗漏集（/emby/Shows/Missing），作为防重基线
- list_episodes     ：查已有集（/Shows/{id}/Episodes），供防重基线

契约参照 n8n 旧流程（docs/新系统设计.md §10）：
    GET {base}/Items?api_key=...&Recursive=true&HasTmdbId=true&Fields=ProviderIds
        &AnyProviderIdEquals=tmdb.<id>
    GET {base}/emby/Shows/Missing?ParentId=<id>&api_key=...&IncludeUnaired=true&IncludeSpecials=false
故障（超时 / 5xx / 网络异常）统一抛 EmbyUnavailable，由调用方按 fail-safe 处理
（docs/新系统设计.md §4.3：Emby 故障时不进入新缺集发现）。
"""
import logging
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = httpx.Timeout(30.0)   # Emby 为慢端点（/Shows/Missing 实测 7s+），超时须充足


class EmbyUnavailable(Exception):
    """Emby 服务不可用：配置缺失 / 网络故障 / 非 2xx 响应 / 响应格式异常。"""


def _base_url() -> str:
    base = (settings.EMBY_BASE_URL or "").strip().rstrip("/")
    if not base:
        raise EmbyUnavailable("EMBY_BASE_URL 未配置")
    return base


def _check_config() -> None:
    if not settings.EMBY_API_KEY:
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
    }


async def _get(path: str, params: dict[str, Any], timeout: httpx.Timeout = REQUEST_TIMEOUT) -> dict[str, Any]:
    """GET 封装：鉴权 + 故障归一（网络异常/非 2xx/JSON 异常 → EmbyUnavailable）。"""
    _check_config()
    url = f"{_base_url()}{path}"
    params = dict(params)
    params.setdefault("api_key", settings.EMBY_API_KEY)

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

    参数:
        emby_id: Emby 剧集条目 id
    返回:
        遗漏集列表，每项含 emby_id/season/episode/name/code（code 为 SxxExx，空串表示无法解析）
    异常:
        EmbyUnavailable: 配置缺失 / 请求失败
    """
    payload = await _get("/emby/Shows/Missing", {
        "ParentId": emby_id,
        "IncludeUnaired": "true",
        "IncludeSpecials": "false",
    })
    items = payload.get("Items", []) or []
    result = [_normalize_episode(item) for item in items]
    result.sort(key=lambda ep: (ep["season"] or 0, ep["episode"] or 0))
    logger.info("Emby 遗漏集（emby_id=%s）: %d 集", emby_id, len(result))
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
