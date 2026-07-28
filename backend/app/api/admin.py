from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models import User, Media, Subscription, Vote, MediaStatus
from app.api.deps import get_admin_user
from app.schemas.user import AdminSubscriptionItem
from app.schemas.task import ApproveRequest

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/subscriptions", response_model=list[AdminSubscriptionItem])
async def list_subscriptions(
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Subscription).order_by(Subscription.created_at.desc())
    )
    items = []
    for s in result.scalars():
        user_result = await db.execute(select(User).where(User.id == s.user_id))
        user = user_result.scalar_one_or_none()
        media_result = await db.execute(select(Media).where(Media.id == s.media_id))
        media = media_result.scalar_one_or_none()
        vote_count = (await db.execute(
            select(func.count(Vote.id)).where(Vote.media_id == s.media_id)
        )).scalar() or 0
        items.append(AdminSubscriptionItem(
            id=s.id, user_id=s.user_id,
            username=user.username if user else "Unknown",
            media_id=s.media_id,
            media_title=media.title if media else "Unknown",
            media_type=media.media_type.value if media else "unknown",
            vote_count=vote_count, created_at=s.created_at,
        ))
    return items


@router.post("/approve")
async def approve_subscription(
    req: ApproveRequest,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Media).where(Media.id == req.media_id))
    media = result.scalar_one_or_none()
    if media is None:
        raise HTTPException(status_code=404, detail="Media not found")
    media.status = MediaStatus.TRACKING
    media.scan_frequency_hours = req.scan_frequency_hours
    await db.flush()
    return {"message": f"{media.title} approved for tracking"}


@router.delete("/media/{media_id}")
async def delete_media(
    media_id: str,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Media).where(Media.id == media_id))
    media = result.scalar_one_or_none()
    if media is None:
        raise HTTPException(status_code=404, detail="Media not found")
    await db.delete(media)
    return {"message": f"{media.title} deleted"}


from pydantic import BaseModel

class ConfigUpdateRequest(BaseModel):
    key: str
    value: str


@router.get("/configs")
async def get_configs(current_user: User = Depends(get_admin_user)):
    from app.services.config_service import config_service
    return await config_service.get_all()


@router.put("/configs")
async def update_config(req: ConfigUpdateRequest, current_user: User = Depends(get_admin_user)):
    from app.services.config_service import config_service
    await config_service.set(req.key, req.value)
    config_service.clear()
    return {"message": f"{req.key} updated"}
