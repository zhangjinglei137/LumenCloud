"""
通知扫描任务（设计文档 §4.2 notification_scan，每 5min 兜底 / 交付 E）。

检查待审批 watch_requests / 失败 transfer_queue → Notifier 推送（§7）。
站内去重：notifications.body 以 "wr#<id> " / "tq#<id> " 前缀标记（P2-4：带空格，
避免 wr#1% 误命中 wr#10），已推送过的不重复推送
（body like "wr#{id} %" / "tq#{id} %"，SQLite/PG 均可）。
空跑无待推送 → task_run(skipped)，不推送（消灭 P1 噪音）。
"""
import logging
import time

from sqlalchemy import select

from app.database import async_session
from app.models import Notification, TransferQueue, WatchRequest
from app.services.notifier import (
    EVENT_APPROVAL_PENDING,
    EVENT_FLOW_ERROR,
    NotifyEvent,
    notifier,
)
from app.tasks import record_task_run

logger = logging.getLogger(__name__)

_IMPLEMENTED = True

# body 去重前缀：wr#<id>（watch_requests）/ tq#<id>（transfer_queue）
# P2-4：查询/写入统一带空格分隔，杜绝 "wr#1%" 误命中 "wr#10" 的前缀串扰
_WR_PREFIX = "wr#"
_TQ_PREFIX = "tq#"


async def _already_notified(session, prefix: str, obj_id: int) -> bool:
    """按 body 前缀查重（notifications 已有 "wr#{id} " / "tq#{id} " 开头记录，带空格）。"""
    row = (
        await session.execute(
            select(Notification.id).where(
                Notification.body.like(f"{prefix}{obj_id} %")
            ).limit(1)
        )
    ).first()
    return row is not None


async def notification_scan_job() -> None:
    """通知扫描（注册：IntervalTrigger(minutes=5)）：待审批 + 失败任务 → Notifier 推送。"""
    t0 = time.monotonic()  # Q8①：真实耗时
    sent = 0
    async with async_session() as s:
        # 1) 待审批看剧请求（§7 approval_pending → 全体/admin 通知）
        wrs = (
            (await s.execute(select(WatchRequest).where(WatchRequest.status == "pending")))
            .scalars().all()
        )
        for wr in wrs:
            if await _already_notified(s, _WR_PREFIX, wr.id):
                continue
            sent += 1
            await notifier.notify(NotifyEvent(
                event_type=EVENT_APPROVAL_PENDING,
                title=f"新的想看请求: {wr.title}",
                body=f"{_WR_PREFIX}{wr.id} {wr.title}",
                recipient=None,  # 全体（站内铃铛）；前端按角色过滤
                extra={"watch_request_id": wr.id},
            ))

        # 2) 失败转存任务（§7 flow_error）
        fails = (
            (await s.execute(select(TransferQueue).where(TransferQueue.status == "failed")))
            .scalars().all()
        )
        for tq in fails:
            if await _already_notified(s, _TQ_PREFIX, tq.id):
                continue
            sent += 1
            await notifier.notify(NotifyEvent(
                event_type=EVENT_FLOW_ERROR,
                title="转存任务失败",
                body=f"{_TQ_PREFIX}{tq.id} {tq.file_name}: {tq.error or '未知原因'}",
                recipient=None,
                extra={"transfer_queue_id": tq.id, "media_id": tq.media_id},
            ))

        # 3) 记录本次扫描：有推送 → success；空跑 → skipped（P1 不推送）
        status = "success" if sent else "skipped"
        message = f"推送 {sent} 条通知" if sent else "无待推送（空跑）"
        await record_task_run(  # Q8①：真实耗时
            s, "notify", status, message,
            duration_seconds=time.monotonic() - t0,
        )
        await s.commit()
        logger.info("[notify] 通知扫描完成: %s", message)