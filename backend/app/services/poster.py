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


async def fetch_poster(p: str) -> tuple[bytes, str]:
    """回源拉取海报（Task 2 实现完整逻辑；此处占位保证路由可测试）。"""
    raise NotImplementedError
