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
import logging
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
        return {
            "tmdb_id": str(row.tmdb_id),
            "title": row.title or "",
            "media_type": row.media_type,
            "poster_path": row.poster_path,
            "year": year,
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
) -> None:
    """tmdb_cache 幂等 upsert（命中更新 / 未命中新增，updated_at=now）。

    - tmdb_id 字符串化（P3 契约）；
    - year 转 int（可空）：int("2023") → 2023，缺失/非法 → None；
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
            row.updated_at = now
            await session.commit()
    except Exception as exc:  # noqa: BLE001 缓存落盘失败降级（不阻断返回）
        logger.warning("tmdb_cache upsert 失败（忽略，不影响返回）: %s", exc)


async def get_by_tmdb_id(tmdb_id: str | int, media_type: str) -> dict[str, Any]:
    """按 TMDB id 取单条元数据（P3 元数据缓存逻辑）。

    流程：
    1. 查 tmdb_cache（tmdb_id + media_type 命中且 updated_at 距今 < 7 天）
       → 直接返回缓存（不回源）；
    2. 未命中 / 超 7 天 → 回源 GET /3/{movie|tv}/{id}（media_type 决定路径，
       复用 _client_kwargs 出口代理与 config_store api_key 读取）→ 归一化
       title / poster_path / year → upsert 缓存（updated_at=now）→ 返回。

    返回 dict：{tmdb_id, title, media_type, poster_path, year}。

    异常:
        TMDBUnavailable: 未配置 key / 请求失败 / 响应异常
    """
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
    await _upsert_cache(tmdb_id, media_type, title, poster_path, year)

    return {
        "tmdb_id": str(tmdb_id),
        "title": title,
        "media_type": media_type,
        "poster_path": poster_path,
        "year": year,
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
        # P3 元数据缓存：搜索命中即 upsert（tmdb_id 字符串化；id 缺失的异常条目跳过）
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
