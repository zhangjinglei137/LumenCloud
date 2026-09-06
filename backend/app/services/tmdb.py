"""
TMDB 元数据搜索服务。

- 端点：GET /3/search/multi（docs/新系统设计.md §10）
- API key 来自 settings.TMDB_API_KEY（环境变量，敏感凭据不进数据库）
- TMDB_PROXY 支持：配置后以该地址作为 API 镜像根地址（旧 config 代理字段语义，
  即替换官方 api.themoviedb.org 主机名，镜像域名需提供同路径 /3/... 接口）
- 全部使用 httpx.AsyncClient（每请求创建，不阻塞事件循环）
"""
import logging
from typing import Any, Optional

import httpx

from app.config import settings
from app.services import config_store

logger = logging.getLogger(__name__)

TMDB_DEFAULT_BASE_URL = "https://api.themoviedb.org"
SEARCH_MULTI_PATH = "/3/search/multi"
REQUEST_TIMEOUT = httpx.Timeout(10.0)


class TMDBUnavailable(Exception):
    """TMDB 服务不可用：配置缺失 / 网络故障 / 非 2xx 响应 / 响应格式异常。"""


def _base_url() -> str:
    """TMDB API 根地址：优先 settings.TMDB_PROXY（镜像/反向代理），否则官方地址。"""
    # Phase 8 配置入库：DB 优先、env fallback；函数内读取，每次调用读最新值
    proxy = (config_store.get("tmdb_proxy", settings.TMDB_PROXY) or "").strip().rstrip("/")
    return proxy or TMDB_DEFAULT_BASE_URL


async def search_multi(q: str) -> list[dict[str, Any]]:
    """多类型影视搜索（movie/tv/person 混合结果）。

    参数:
        q: 搜索关键词
    返回:
        归一化结果列表，每项含:
            title:      影视名称（movie 用 title，tv 用 name）
            tmdb_id:    TMDB id
            media_type: movie / tv / person
            poster_path: 海报相对路径（可为 None，前端拼图床完整地址）
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

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
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
        results.append(
            {
                "title": item.get("title") or item.get("name") or "",
                "tmdb_id": item.get("id"),
                "media_type": item.get("media_type"),
                "poster_path": item.get("poster_path"),
            }
        )
    logger.info("TMDB 搜索「%s」命中 %d 条", keyword, len(results))
    return results
