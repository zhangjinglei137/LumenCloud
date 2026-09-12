"""NaSTools Webhook 集成端点（影视下载两队列重设计 §12）。

接收 NaSTools 内建 Webhook 插件（作者 jxxghp）的事件推送，把「NasTools 整理完成」
这一信号用于驱动本系统入库链路的即时推进：

- `transfer.finished`（整理完成）：NasTools 已把文件整理进 Emby 媒体库目录 →
  该 media 的 download_queue `scrape` 行推进为 `library`（按载荷文件名/集号
  定位单行，CAS 推进）→ fire-and-forget 触发 `library_check()`（Emby 入库确认
  → done + 删夸克 + 释放容量预留），并同步触发 Emby 全库 Refresh（Task 8，
  加速新文件入库；失败仅告警，轮询兜底）。
- `transfer.fail` / `download.fail`：flow_error 通知（节流沿用）。
- 其它事件：记录后忽略（返回 ok）。

鉴权：静态 token（设计 §12.2，旧版插件 POST 不支持自定义 Header）：
- query `?token=`（旧版插件 Webhook 地址带 token）
- header `X-NaSTools-Token` / `Authorization`（新版「消息通知→Webhook」渠道）
secret 来源：system_config `internal_nastools_webhook_token` 优先，env/settings
`NASTOOLS_WEBHOOK_SECRET` fallback；未配置 → 503（fail-closed）。

幂等/兜底：scrape→library 用条件更新（仅 status='scrape' 命中）；事件丢失不影响主链路
（library_check 轮询照旧 + recovery 超时回退照旧），回调仅加速「整理完成→入库确认」。
T3 文件级推进：载荷文件名/集号定位 scrape 行（规范化集号 == 行 episode，或
file_name/download_name == 载荷文件名），CAS 单行推进；无法定位 → 零推进，仅触发
library_check 轮询加速（由既有轮询路径推进，不丢任务）。
"""
import asyncio
import logging
import re
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
from app.utils import fmt_episode, parse_episode_num

logger = logging.getLogger(__name__)

router = APIRouter(tags=["nastools-webhook"])

# 关注的事件类型白名单（其余忽略，设计 §12.3）
_EVENT_TRANSFER_FINISHED = "transfer.finished"
_EVENT_TRANSFER_FAIL = "transfer.fail"
_EVENT_DOWNLOAD_FAIL = "download.fail"

# 集号识别模式（与 tasks/library_check.py 同款：SxxExx / 第N集）
_RE_SXXEXX = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")
_RE_CN_EP = re.compile(r"第\s*(\d{1,3})\s*[集话]")

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


def _norm_episode_key(value: str) -> str | None:
    """从文本提取规范化集 key（SxxExx → 'S01E02'）。

    匹配思路对齐 library_check._episode_in_missing：目标文本本身可能是规范
    key，也可能内嵌文件名；统一经 _RE_SXXEXX 提取后 fmt_episode 规范化。
    无法提取 → None。
    """
    m = _RE_SXXEXX.search(value or "")
    if m:
        return fmt_episode(int(m.group(1)), int(m.group(2)))
    return None


def _extract_episode_from_payload(file_name: str) -> str | int | None:
    """从载荷文件名提取集号标识。

    - 'SxxExx' → 规范化集 key（如 'S01E02'）；
    - '第N集'（无季号）→ 纯集号数字 N（跨季按集号匹配，同 _episode_in_missing）；
    - 无法提取 → None（调用方退化精确文件名匹配）。
    """
    m = _RE_SXXEXX.search(file_name)
    if m:
        return fmt_episode(int(m.group(1)), int(m.group(2)))
    m = _RE_CN_EP.search(file_name)
    if m:
        return int(m.group(1))
    return None


def _row_matches_episode(episode: str, target: str | int) -> bool:
    """行 episode 是否命中载荷集号目标（SxxExx 精确 key / 第N集跨季集号数字）。"""
    if isinstance(target, str):
        return _norm_episode_key(episode) == target
    return parse_episode_num(episode) == target


async def _advance_scrape_to_library(media_id: int, file_name: str | None = None) -> int:
    """该 media 的 download_queue `scrape` 行推进为 `library`（文件级定位，CAS 单行）。

    - file_name 为空 → 返回 0（不推进），由调用方仅触发轮询加速。
    - 有文件名：定位该 media 的 status='scrape' 行中匹配的行——匹配优先级：
      ① 规范化集号 == 行 episode（SxxExx / 第N集，同 _episode_in_missing 思路）；
      ② 行 file_name / download_name == 载荷文件名。命中单行 →
      `UPDATE ... WHERE id=? AND status='scrape'`（CAS，幂等）。
    - 返回推进行数（0 或 1）。
    """
    file_name = (file_name or "").strip()
    if not file_name:
        return 0

    ep_target = _extract_episode_from_payload(file_name)

    async with async_session() as s:
        rows = (
            await s.execute(
                select(DownloadQueue.id, DownloadQueue.episode,
                       DownloadQueue.file_name, DownloadQueue.download_name)
                .where(
                    DownloadQueue.media_id == media_id,
                    DownloadQueue.status == "scrape",
                )
            )
        ).all()

    if not rows:
        return 0

    # 匹配优先级：集号规范化匹配（含行 episode 内嵌 SxxExx）→ 精确文件名匹配
    target_id = None
    for dq_id, episode, dq_file_name, dq_download_name in rows:
        if ep_target is not None and _row_matches_episode(episode, ep_target):
            target_id = dq_id
            break
    if target_id is None:
        for dq_id, episode, dq_file_name, dq_download_name in rows:
            if (dq_file_name or "") == file_name or (dq_download_name or "") == file_name:
                target_id = dq_id
                break
    if target_id is None:
        return 0

    now = _now()
    async with async_session() as s:
        async with s.begin():
            r = await s.execute(
                update(DownloadQueue)
                .where(
                    DownloadQueue.id == target_id,
                    DownloadQueue.status == "scrape",  # CAS：仅 scrape 可推进
                )
                .values(
                    status="library",
                    node_started_at=now,
                    node_attempt=0,
                    node_finished_at=now,
                    node_error=None,
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
    """处理整理完成事件：反查本系统 media → 按载荷文件名推进 scrape→library → 轮询兜底。"""
    data = data or {}
    media_info = data.get("media_info") or {}
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

    # 载荷文件名：兼容旧版插件（顶层 file_name/name）与新载荷（media_info 内 file_name）；
    # 无法提取 → None → _advance 不推进，仅轮询兜底
    file_name = (
        data.get("file_name")
        or data.get("name")
        or (media_info.get("file_name") or None)
    )
    advanced = await _advance_scrape_to_library(media, file_name)

    # 无论是否推进，均触发 library_check 轮询加速：无法定位文件名的场景
    # 由既有轮询路径推进（不丢任务），成功推进场景也顺带复查入库。
    task = asyncio.create_task(_check_library_background(media))
    _background.add(task)
    task.add_done_callback(_background.discard)

    if advanced:
        # Task 8：推进成功后同样触发 Emby 全库 Refresh（fire-and-forget + 互斥锁在
        # library_check 内；失败仅告警，Emby 收录由 library_check 轮询兜底确认）
        try:
            from app.tasks import library_check as lc

            lc.trigger_emby_refresh()
        except Exception as exc:  # noqa: BLE001  触发失败不阻断应答（轮询兜底）
            logger.warning("[nastools] media=%s 触发 Emby 全库扫描失败（轮询兜底）: %s", media, exc)
        return advanced, f"推进 {advanced} 条 scrape→library 并触发入库确认"
    return 0, "未定位到匹配的 scrape 任务（无法匹配载荷文件/已推进），已触发入库轮询"


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