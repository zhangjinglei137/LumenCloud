"""站内通知 API（docs/新系统设计.md §7 前端铃铛）。

- GET  /api/notifications             当前用户站内信（本人 + 全体 recipient=NULL），未读优先，附 unread_count
- POST /api/notifications/{id}/read   标记已读（仅本人或全体消息）
- POST /api/notifications/read-all    全部标记已读
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notification, User
from app.routers.deps import get_current_user, get_session

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _notif_dto(r: Notification) -> dict:
    return {
        "id": r.id,
        "event_type": r.event_type,
        "title": r.title,
        "body": r.body,
        "is_read": r.is_read,
        # 前端契约别名（MainLayout 铃铛渲染字段）
        "read": r.is_read,
        "message": r.body,
        "level": r.event_type,
        "created_at": r.created_at,
    }


def _scope(user_id: int):
    """本人或全体（recipient IS NULL）消息范围。"""
    return or_(Notification.recipient == user_id, Notification.recipient.is_(None))


@router.get("")
async def list_notifications(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """当前用户站内信列表（未读优先）+ unread_count。"""
    rows = (
        (
            await session.execute(
                select(Notification)
                .where(_scope(user.id))
                .order_by(
                    Notification.is_read.asc(),  # 未读在前
                    Notification.created_at.desc(),
                    Notification.id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [_notif_dto(r) for r in rows],
        "unread_count": sum(1 for r in rows if not r.is_read),
    }


@router.post("/read-all")
async def read_all(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """全部标记已读（本人 + 全体）。"""
    result = await session.execute(
        update(Notification)
        .where(_scope(user.id), Notification.is_read.is_(False))
        .values(is_read=True)
    )
    await session.commit()
    return {"ok": True, "updated": result.rowcount}


@router.post("/{notification_id}/read")
async def mark_read(
    notification_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """标记单条已读（仅本人或全体消息）。"""
    result = await session.execute(
        update(Notification)
        .where(Notification.id == notification_id, _scope(user.id))
        .values(is_read=True)
    )
    await session.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="通知不存在或无权操作")
    return {"ok": True}