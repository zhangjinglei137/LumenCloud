"""转存队列 API（阶段 3）：JWT 鉴权 + §9.1 脱敏 + 人工重试。

- GET  /api/queue           队列列表（登录用户）；DTO 白名单构造，网盘凭据绝不直出：
                              guest 全部隐藏；admin 仅脱敏回显 share_code 后 4 位；
                              stoken/receive_code/fid_tokens/pwd_id/folder_id/fids 任何角色不返回
- POST /api/queue/{id}/retry admin 人工重试 failed 任务（条件更新防并发，行数=0 → 404；
                              episode_state 同步失败 → 409，事务回滚保持原状；
                              commit 后触发转存消费，失败仅告警不阻断）
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EpisodeState, TransferQueue, User
from app.routers.deps import get_current_admin, get_current_user, get_session

router = APIRouter()

logger = logging.getLogger(__name__)

_GB = 1024**3


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _mask_share_code(code: str) -> str:
    """§9.1 脱敏：share_code 仅回显后 4 位（如 "****abcd"）。"""
    return "****" + str(code)[-4:]


# 兼容旧模块名（其他 lane 可能 import _load_transfer_queue_model）
def _load_transfer_queue_model():
    try:
        from app.models.transfer_queue import TransferQueue
    except ImportError:
        try:
            from app.models import TransferQueue
        except ImportError:
            TransferQueue = None
    return TransferQueue


def _to_item(row: TransferQueue, is_admin: bool) -> dict:
    """构造白名单 DTO —— 绝不透出表记录（§9.1）。"""
    dto = {
        "id": row.id,
        "status": row.status,
        "file_name": row.file_name,
        "file_size": row.file_size,
        "file_size_gb": round(row.file_size / _GB, 2) if row.file_size else None,
        "episode": row.episode,
        "media_id": row.media_id,
        "quota_reject_count": row.quota_reject_count,
        "error": row.error,
        "enqueued_at": row.enqueued_at,
        "updated_at": row.updated_at,
    }
    if is_admin:
        dto["share_code"] = _mask_share_code(row.share_code)
        dto["share_code_tail"] = str(row.share_code)[-4:] if row.share_code else None
    return dto


@router.get("/queue")
async def list_queue(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    """转存队列列表，按 enqueued_at 升序（简单分页）。按 admin/guest 分级脱敏。"""
    stmt = (
        select(TransferQueue)
        .order_by(TransferQueue.enqueued_at.asc(), TransferQueue.id.asc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).scalars().all()
    is_admin = user.role == "admin"
    return [_to_item(row, is_admin) for row in rows]


@router.post("/queue/{task_id}/retry")
async def retry_task(
    task_id: int,
    admin: User = Depends(get_current_admin),  # §9.1 写操作鉴权
    session: AsyncSession = Depends(get_session),
) -> dict:
    """admin 人工重试 failed 任务（契约见 §4.5 状态机）：
    transfer_queue: failed → pending + quota_reject_count=0 + error=null
    episode_state : failed 同步回 queued + retry_count=0 + error=null + updated_at
    （retry_count 列仅存在于 episode_state（§3.1），transfer_queue 无此列，故不重置该项）
    条件更新，行数=0（不存在/非 failed）→ 404。
    """
    now = _now()
    row = await session.get(TransferQueue, task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="队列任务不存在")

    result = await session.execute(
        update(TransferQueue)
        .where(TransferQueue.id == task_id, TransferQueue.status == "failed")
        .values(
            status="pending",
            quota_reject_count=0,
            error=None,
            # P0-1（council）：人工重试 = 完整重新走转存链——与状态回退同事务清空
            # save_task_id，防「已受理未落盘」的幂等标记残留导致下一轮跳过 save
            # 盲等死循环；save_attempt_at 与其同生同灭一并清空。
            save_task_id=None,
            save_attempt_at=None,
            updated_at=now,
        )
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="任务不存在或状态不允许重试（仅 failed 可重试）")

    # 双表联动（§3.1）：episode_state 防重权威源同步回 queued
    es_result = await session.execute(
        update(EpisodeState)
        .where(
            EpisodeState.media_id == row.media_id,
            EpisodeState.episode == row.episode,
            EpisodeState.state == "failed",
        )
        .values(state="queued", retry_count=0, error=None, updated_at=now)
    )
    if es_result.rowcount == 0:
        # P2-3（Oracle 审查）：episode_state 非 failed（双表状态不一致）→ 409 中断；
        # 未 commit → tq 已改的 pending 一并回滚，保持 failed 原状（合理）
        raise HTTPException(status_code=409, detail="episode_state 状态不一致，请稍后重试")
    await session.commit()

    # P1-2（Oracle 审查）：重试成功后触发转存消费（延迟导入 + 兜底，与 scan 触发同模式；
    # 状态已改 pending/queued，触发后由队列消费续跑）
    try:
        from app.tasks.transfer import trigger_transfer

        await trigger_transfer()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[queue] retry 后触发转存消费失败（不阻断重试）: %s", exc)
    return {"ok": True}