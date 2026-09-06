"""审批流 API（docs/新系统设计.md §5.3 审批流 + §9.1 写操作鉴权）。

- GET  /api/approvals            admin 看全部；guest 看自己的（requested_by=当前用户）
- POST /api/approvals            guest+admin 提交「想看」→ pending + 通知管理员
- POST /api/approvals/{id}/approve  admin 批准 → 写 media 表(status=tracking) + 通知访客 + 可选触发巡检
- POST /api/approvals/{id}/reject   admin 拒绝 → rejected + reject_reason
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Media, User, WatchRequest
from app.routers.deps import get_current_admin, get_current_user, get_session
from app.services.notifier import (
    EVENT_APPROVAL_PENDING,
    EVENT_DOWNLOAD_STARTED,
    NotifyEvent,
    notifier,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/approvals", tags=["approvals"])


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class WatchRequestCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    tmdb_id: int | None = None
    media_type: str | None = None  # movie / tv
    poster_path: str | None = None


class RejectRequest(BaseModel):
    reject_reason: str = Field(default="", max_length=500)


def _wr_dto(r: WatchRequest, requested_by_username: str | None = None) -> dict:
    """审批 DTO：保留 requested_by(id) 字段，附加 request_by_username（Q10，
    申请人已删除/无关联时 LEFT JOIN 得 None，前端回退 id）。"""
    return {
        "id": r.id,
        "title": r.title,
        "tmdb_id": r.tmdb_id,
        "media_type": r.media_type,
        "poster_path": r.poster_path,
        "status": r.status,
        "reject_reason": r.reject_reason,
        "reviewed_at": r.reviewed_at,
        "created_at": r.created_at,
        "requested_by": r.requested_by,
        "requested_by_username": requested_by_username,
    }


@router.get("")
async def list_approvals(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """审批列表：admin 看全部；guest 只看自己提交的。

    Q10：LEFT JOIN users 取申请人 username（outer join，不留申请人对不上不报错）。
    """
    stmt = (
        select(WatchRequest, User.username)
        .join(User, WatchRequest.requested_by == User.id, isouter=True)
        .order_by(WatchRequest.created_at.desc(), WatchRequest.id.desc())
    )
    if user.role != "admin":
        stmt = stmt.where(WatchRequest.requested_by == user.id)
    rows = (await session.execute(stmt)).all()
    return [_wr_dto(r[0], r[1]) for r in rows]


@router.post("")
async def create_approval(
    payload: WatchRequestCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """提交「想看」请求（guest+admin 均可）→ 通知管理员（§5.3 提交即通知）。"""
    if payload.media_type not in (None, "movie", "tv"):
        raise HTTPException(status_code=422, detail="media_type 仅支持 movie/tv")

    # Q2①（P1）：已存在于影视库的 tmdb_id 拒绝重复提交（应用层去重）
    if payload.tmdb_id is not None and await session.scalar(
        select(Media.id).where(Media.tmdb_id == payload.tmdb_id).limit(1)
    ):
        raise HTTPException(status_code=409, detail="该影视已在影视库，无需重复提交")

    wr = WatchRequest(
        requested_by=user.id,
        title=payload.title.strip(),
        tmdb_id=payload.tmdb_id,
        media_type=payload.media_type,
        poster_path=payload.poster_path,
        status="pending",
    )
    session.add(wr)
    await session.commit()
    await session.refresh(wr)

    # §5.3：访客提交即触发 approval_pending 通知管理员（recipient=None=全体）
    try:
        await notifier.notify(
            NotifyEvent(
                event_type=EVENT_APPROVAL_PENDING,
                title=f"新的想看请求: {wr.title}",
                body=f"wr#{wr.id} {wr.title}",
            )
        )
    except Exception as exc:  # noqa: BLE001  通知失败不阻断提交
        logger.exception("审批待办通知失败: %s", exc)

    return {"id": wr.id}


@router.post("/{approval_id}/approve")
async def approve_approval(
    approval_id: int,
    admin: User = Depends(get_current_admin),  # §9.1 写操作鉴权
    session: AsyncSession = Depends(get_session),
) -> dict:
    """admin 批准：条件更新 pending→approved → 写 media 表 → 通知访客 → 可选触发巡检。"""
    wr = await session.get(WatchRequest, approval_id)
    if wr is None:
        raise HTTPException(status_code=404, detail="审批请求不存在")
    if wr.status != "pending":
        raise HTTPException(status_code=409, detail="该请求已被处理")

    # Q2①（P1）：审批尚未被消费前查重——已存在于影视库的 tmdb_id 拒绝批准，
    # 不产生半提交，管理员可另行 reject
    if wr.tmdb_id is not None and await session.scalar(
        select(Media.id).where(Media.tmdb_id == wr.tmdb_id).limit(1)
    ):
        raise HTTPException(status_code=409, detail="该影视已在影视库，无需重复提交")

    # 条件更新防并发双重审批（§3.1 条件更新约定）；异常未 commit 时整体回滚
    result = await session.execute(
        update(WatchRequest)
        .where(WatchRequest.id == approval_id, WatchRequest.status == "pending")
        .values(status="approved", reviewed_by=admin.id, reviewed_at=_now())
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=409, detail="该请求已被处理")

    title, requester = wr.title, wr.requested_by
    media = Media(
        title=wr.title,
        tmdb_id=wr.tmdb_id,
        media_type=wr.media_type,
        # Q2 配套：访客「想看」携带的 poster_path 在批准入库时透传（此前丢失，
        # 批准后影视无海报；与 MediaCreate 的 poster_path 契约一致）
        poster_path=wr.poster_path,
        status="tracking",
        in_emby=False,
    )
    session.add(media)
    await session.flush()
    media_id = media.id
    await session.commit()

    # ---- 事务外副作用 ----
    # §5.3：批准通过通知访客（download_started）
    if requester:
        try:
            await notifier.notify(
                NotifyEvent(
                    event_type=EVENT_DOWNLOAD_STARTED,
                    title=f"开始入库: {title}",
                    recipient=requester,
                    extra={"media_id": media_id},
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("入库通知失败: %s", exc)

    # 可选：触发该 media 巡检（fire-and-forget，E-1 不再同步等待；故障不影响审批结果）
    try:
        from app.tasks.scan import trigger_scan_background

        trigger_scan_background(media_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("批准后自动巡检触发失败 media=%s: %s", media_id, exc)

    return {"ok": True, "media_id": media_id}


@router.post("/{approval_id}/reject")
async def reject_approval(
    approval_id: int,
    payload: RejectRequest,
    admin: User = Depends(get_current_admin),  # §9.1 写操作鉴权
    session: AsyncSession = Depends(get_session),
) -> dict:
    """admin 拒绝：pending→rejected + reject_reason + reviewed_by/reviewed_at。"""
    result = await session.execute(
        update(WatchRequest)
        .where(WatchRequest.id == approval_id, WatchRequest.status == "pending")
        .values(
            status="rejected",
            reject_reason=payload.reject_reason.strip() or None,
            reviewed_by=admin.id,
            reviewed_at=_now(),
        )
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="请求不存在或已被处理")
    await session.commit()
    return {"ok": True}