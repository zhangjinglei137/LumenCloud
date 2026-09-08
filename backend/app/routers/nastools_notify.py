"""NaSTools Webhook 集成端点（影视下载两队列重设计 §12）。

接收 NaSTools 内建 Webhook 插件（作者 jxxghp）的事件推送，把「NasTools 整理完成」
这一信号用于驱动本系统入库链路的即时推进：

- `transfer.finished`（整理完成）：NasTools 已把文件整理进 Emby 媒体库目录 →
  该 media 的 download_queue `scrape` 行推进为 `library` → fire-and-forget 触发
  `library_check()`（Emby 入库确认 → done + 删夸克 + 释放容量预留）。
- `transfer.fail` / `download.fail`：flow_error 通知（节流沿用）。
- 其它事件：记录后忽略（返回 ok）。

鉴权：静态 token（设计 §12.2，旧版插件 POST 不支持自定义 Header）：
- query `?token=`（旧版插件 Webhook 地址带 token）
- header `X-NaSTools-Token` / `Authorization`（新版「消息通知→Webhook」渠道）
secret 来源：system_config `internal_nastools_webhook_token` 优先，env/settings
`NASTOOLS_WEBHOOK_SECRET` fallback；未配置 → 503（fail-closed）。

幂等/兜底：scrape→library 用条件更新（仅 status='scrape' 命中）；事件丢失不影响主链路
（library_check 轮询照旧 + recovery 超时回退照旧），回调仅加速「整理完成→入库确认」。
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import hmac

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, update

from app.config import settings
from app.database import async_session
from app.models import DownloadQueue, Media, SystemConfig
from app.services.notifier import EVENT_FLOW_ERROR, NotifyEvent, notifier

logger = logging.getLogger(__name__)

router = APIRouter(tags=["nastools-webhook"])

# 关注的事件类型白名单（其余忽略，设计 §12.3）
_EVENT_TRANSFER_FINISHED = "transfer.finished"
_EVENT_TRANSFER_FAIL = "transfer.fail"
_EVENT_DOWNLOAD_FAIL = "download.fail"

# 后台任务强引用集合（防 GC，与 tasks 模块同模式）
_background: set[asyncio.Task] = set()


def _now() -> datetime:
    """统一时间源（naive UTC，与 tasks/__init__._now 一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _secret() -> Optional[str]:
    """systen_config internal_nastools_webhook_token 优先，env/settings fallback。"""
    try:
        async with async_session() as s:
            row = await s.get(SystemConfig, "internal_nastools_webhook_token")
        if row is not None and row.value:
            return str(row.value)
    except Exception as exc:  # noqa: BLE001  DB 异常 → 回退 env
        logger.warning("[nastools] 读取 webhook token 失败，回退 env: %s", exc)
    return settings.NASTOOLS_WEBHOOK_SECRET


def _token_from_request(request: Request) -> Optional[str]:
    """query `?token=` → header `X-NaSTools-Token` → header `Authorization`。"""
    token = request.query_params.get("token")
    if token:
        return token
    token = request.headers.get("X-NaSTools-Token")
    if token:
        return token
    auth = request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[len("bearer "):].strip()
    return None


def _seconds_compare(a: str, b: str) -> bool:
    """常数时间比较（防时序侧信道）。"""
    return hmac.compare_digest(str(a or ""), str(b or ""))


async def _advance_scrape_to_library(media_id: int) -> int:
    """该 media 的 download_queue `scrape` 行推进为 `library`（条件更新，幂等）。"""
    now = _now()
    async with async_session() as s:
        async with s.begin():
            r = await s.execute(
                update(DownloadQueue)
                .where(
                    DownloadQueue.media_id == media_id,
                    DownloadQueue.status == "scrape",
                )
                .values(
                    status="library",
                    node_started_at=now,
                    updated_at=now,
                )
            )
            return r.rowcount


async def _check_library_background(media_id: int) -> None:
    """后台触发入库确认（library_check）：NasTools 整理完成即检查一次，加速入库。"""
    try:
        from app.tasks import library_check as lc

        # 优先触发全局入库轮询（会处理该 media 已推进到 library 的行）；
        # library_check 内部 Emby 命中 → done + 删夸克 + 释放容量。
        await lc.library_check()
    except Exception as exc:  # noqa: BLE001  事件驱动失败不阻断（轮询兜底）
        logger.warning("[nastools] media=%s 触发 library_check 失败（轮询兜底）: %s", media_id, exc)


async def _handle_transfer_finished(data: dict) -> "tuple[int, str]":
    """处理整理完成事件：反查本系统 media → 推进 scrape→library → 后台触发入库确认。"""
    media_info = (data or {}).get("media_info") or {}
    tmdb_id = media_info.get("tmdb_id")
    title = str(media_info.get("title") or "").strip()
    if not tmdb_id:
        # 无 tmdb_id（识别失败）→ 无法关联本系统影视，记录后忽略
        return 0, f"缺少 tmdb_id（title={title or '未知'}），无法关联影视，忽略"

    async with async_session() as s:
        media = (
            await s.execute(select(Media.id).where(Media.tmdb_id == tmdb_id))
        ).scalars().first()
        if media is None:
            return 0, f"tmdb_id={tmdb_id} 不在本系统影视库（title={title}），忽略"

    advanced = await _advance_scrape_to_library(media)
    if advanced:
        task = asyncio.create_task(_check_library_background(media))
        _background.add(task)
        task.add_done_callback(_background.discard)
        return advanced, f"推进 {advanced} 条 scrape→library 并触发入库确认"
    return 0, "无 scrape 状态任务待推进（可能已推进/已在库）"


async def _notify_flow_error(title: str, body: str, media_id: Optional[int] = None) -> None:
    """整理失败/下载失败 → flow_error 通知（站内 + PushPlus，节流沿用 notifier）。"""
    try:
        await notifier.notify(NotifyEvent(
            event_type=EVENT_FLOW_ERROR,
            title=title,
            body=body,
            recipient=None,
            extra={"media_id": media_id} if media_id is not None else {},
        ))
    except Exception as exc:  # noqa: BLE001  通知失败不阻断应答
        logger.warning("[nastools] 失败事件通知发送失败: %s", exc)


@router.post("/internal/nastools/notify")
async def nastools_notify(request: Request) -> JSONResponse:
    """接收 NaSTools Webhook 事件推送（§12.3）。

    body: {"type": "<event_type>", "data": {...}}（旧版插件 POST 原文）
    鉴权：query/header 静态 token；secret 未配置 503，不匹配 401。
    """
    secret = await _secret()
    if not secret:
        return JSONResponse(
            status_code=503,
            content={"detail": "NaSTools webhook token 未配置（internal_nastools_webhook_token）"},
        )
    provided = _token_from_request(request)
    if not provided or not _seconds_compare(provided, secret):
        return JSONResponse(status_code=401, content={"detail": "webhook token 无效"})

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"detail": "请求体不是合法 JSON"})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"detail": "请求体须为 JSON 对象"})

    event_type = str(payload.get("type") or "").strip()
    data = payload.get("data") or {}
    if event_type == _EVENT_TRANSFER_FINISHED:
        advanced, msg = await _handle_transfer_finished(data)
        logger.info("[nastools] %s: %s", event_type, msg)
        return JSONResponse(content={"ok": True, "advanced": advanced, "message": msg})

    if event_type in (_EVENT_TRANSFER_FAIL, _EVENT_DOWNLOAD_FAIL):
        media_info = (data or {}).get("media_info") or {}
        title = str(media_info.get("title") or data.get("name") or event_type)
        tmdb_id = media_info.get("tmdb_id")
        media_id = None
        if tmdb_id:
            async with async_session() as s:
                media_id = (
                    await s.execute(select(Media.id).where(Media.tmdb_id == tmdb_id))
                ).scalars().first()
        await _notify_flow_error(
            f"NaSTools {event_type}: {title}",
            f"NaSTools 报告 {event_type}（文件整理/下载失败），请人工核查。",
            media_id,
        )
        return JSONResponse(content={"ok": True})

    # 其它事件：记录后忽略
    logger.debug("[nastools] 忽略事件 %s", event_type)
    return JSONResponse(content={"ok": True})