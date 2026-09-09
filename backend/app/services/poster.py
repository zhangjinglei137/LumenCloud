"""影视海报图床代理服务。

- 校验：_validate_poster_path 仅放行 /t/p/... 形态（防 SSRF/路径穿越）
- 回源：httpx 拉取镜像/官方图床，返回 bytes + content_type
- 缓存：进程内 TTL dict（上限 + 过期），纯优化，任何异常降级直出
- 节流：模块级 {key: ts}，60s 内同键只告警一次
"""
import logging
import posixpath
import time
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.services import config_store

logger = logging.getLogger(__name__)

POSTER_DEFAULT_BASE = "https://image.tmdb.org"
REQUEST_TIMEOUT = httpx.Timeout(10.0)

# 缓存：path → (expire_ts, content_type, bytes)
_POSTER_CACHE_TTL = 600
_POSTER_CACHE_MAX = 100
_POSTER_CACHE: dict[str, tuple[float, str, bytes]] = {}

# 告警节流：path → 上次告警时间戳
_POSTER_ALERT_TTL = 60
_ALERT_COOLDOWN: dict[str, float] = {}


class PosterUnavailable(Exception):
    """海报代理不可用：配置缺失 / 配置误填（防御校验）→ 503。"""


def _validate_poster_path(p: str) -> bool:
    """校验 TMDB 图床相对路径合法性（防 SSRF / 路径穿越）。

    - FastAPI Query 已解码一次 URL 编码，%2e%2e → ..、%2f → /，无需再次解码；
    - posixpath.normpath 消化 ../ 分段后与 /t/p/ 前缀复核，杜绝归一化越界；
    - 协议段（://）与反斜杠、null 字节直接拒绝。
    """
    if not p or not p.strip():
        return False
    if not p.startswith("/"):
        return False
    if "://" in p.lower():
        return False
    if "\\" in p or "\x00" in p:
        return False
    norm = posixpath.normpath(p)
    if not norm.startswith("/t/p/"):
        return False
    if norm == "/t/p" or norm == "/t/p/.." or norm.startswith("/t/p/../"):
        return False
    return True


def _base_url() -> str:
    """海报图床根地址：镜像（tmdb_poster_proxy）优先，否则官方 image.tmdb.org。

    误填防御（复用 tmdb.py _base_url 语义）：无 scheme 的 host:port
    （如 192.168.3.31:7897）一定是误填的科学上网代理端口 → 明确报错。
    """
    mirror = (
        config_store.get("tmdb_poster_proxy", settings.TMDB_POSTER_PROXY) or ""
    ).strip().rstrip("/")
    if mirror and "://" not in mirror and ":" in mirror:
        raise PosterUnavailable(
            f"图床镜像地址疑似填了代理端口（{mirror}）。tmdb_poster_proxy 应为图床"
            "反代根地址（如 https://tmdb-image.example.com）；科学上网代理请填到"
            "「TMDB 出口代理」（tmdb_http_proxy）。设置页 → 服务凭据 → 元数据 · TMDB 修改。"
        )
    return mirror or POSTER_DEFAULT_BASE


def _client_factory():
    """httpx.AsyncClient 实例化入口（测试 monkeypatch 挂点）。"""
    return httpx.AsyncClient(timeout=REQUEST_TIMEOUT)


def _alert(path: str) -> None:
    """节流告警：60s 内同 path 只 warning 一次。"""
    now = time.monotonic()
    last = _ALERT_COOLDOWN.get(path)
    if last is None or now - last >= _POSTER_ALERT_TTL:
        _ALERT_COOLDOWN[path] = now
        logger.warning("海报代理回源失败 path=%s", path)
    # 清理过期键，防无限增长
    if len(_ALERT_COOLDOWN) > _POSTER_CACHE_MAX * 2:
        for k in [k for k, ts in _ALERT_COOLDOWN.items() if now - ts >= _POSTER_ALERT_TTL]:
            _ALERT_COOLDOWN.pop(k, None)


async def fetch_poster(p: str) -> tuple[bytes, str]:
    """回源拉取海报图片。

    返回 (bytes, content_type)。失败：PosterUnavailable（配置误填/缺失）或
    Exception（网络/非 2xx，路由映射 502）。
    """
    now = time.monotonic()
    hit = _POSTER_CACHE.get(p)
    if hit is not None and now < hit[0]:
        return hit[2], hit[1]

    base = _base_url()
    url = f"{base}{p}"
    try:
        async with _client_factory() as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        _alert(p)
        raise Exception(f"网络请求失败: {exc}") from exc
    if resp.status_code != 200:
        _alert(p)
        raise Exception(f"上游返回 HTTP {resp.status_code}")
    ctype = resp.headers.get("content-type", "image/jpeg")
    # 上限未满才写入；失败路径不写缓存（避免临时故障期缓存错误状态）
    if len(_POSTER_CACHE) < _POSTER_CACHE_MAX:
        _POSTER_CACHE[p] = (now + _POSTER_CACHE_TTL, ctype, resp.content)
    return resp.content, ctype
