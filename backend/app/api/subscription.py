from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models import User, Media, Subscription, Vote
from app.api.deps import get_current_user
from app.schemas.user import SubscriptionInfo

router = APIRouter(prefix="/api/subscriptions", tags=["subscriptions"])


@router.post("/{media_id}")
async def subscribe_media(
    media_id: str,
    auto_download: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    existing = await db.execute(
        select(Subscription).where(
            Subscription.user_id == current_user.id, Subscription.media_id == media_id
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Already subscribed")

    sub = Subscription(user_id=current_user.id, media_id=media_id, auto_download=auto_download)
    db.add(sub)
    await db.flush()
    return {"id": sub.id, "message": "Subscribed"}


@router.delete("/{media_id}")
async def unsubscribe_media(
    media_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Subscription).where(
            Subscription.user_id == current_user.id, Subscription.media_id == media_id
        )
    )
    sub = result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404, detail="Not subscribed")
    await db.delete(sub)
    return {"message": "Unsubscribed"}


@router.get("/", response_model=list[SubscriptionInfo])
async def my_subscriptions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Subscription).where(Subscription.user_id == current_user.id)
        .order_by(Subscription.created_at.desc())
    )
    subs = []
    for s in result.scalars():
        media_result = await db.execute(select(Media).where(Media.id == s.media_id))
        media = media_result.scalar_one_or_none()
        # Check if user voted
        vote_result = await db.execute(
            select(Vote).where(Vote.user_id == current_user.id, Vote.media_id == s.media_id)
        )
        has_voted = vote_result.scalar_one_or_none() is not None
        subs.append(SubscriptionInfo(
            id=s.id, media_id=s.media_id,
            media_title=media.title if media else "Unknown",
            media_poster=media.poster_path if media else None,
            voted=has_voted,
            created_at=s.created_at,
        ))
    return subs


@router.post("/{media_id}/vote")
async def vote_media(
    media_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    existing = await db.execute(
        select(Vote).where(Vote.user_id == current_user.id, Vote.media_id == media_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Already voted")

    vote = Vote(user_id=current_user.id, media_id=media_id)
    db.add(vote)
    await db.flush()

    count = (await db.execute(
        select(func.count(Vote.id)).where(Vote.media_id == media_id)
    )).scalar()
    return {"vote_count": count}


@router.delete("/{media_id}/vote")
async def unvote_media(
    media_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Vote).where(Vote.user_id == current_user.id, Vote.media_id == media_id)
    )
    vote = result.scalar_one_or_none()
    if vote is None:
        raise HTTPException(status_code=404, detail="Not voted")
    await db.delete(vote)
    return {"message": "Vote removed"}
