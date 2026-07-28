from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models import User, Rating, UserMediaInteraction, UserMediaStatus
from app.api.deps import get_current_user
from app.schemas.user import RatingRequest, RatingInfo, UserInteractionStatus

router = APIRouter(prefix="/api/interactions", tags=["interactions"])


@router.post("/rating/{media_id}")
async def rate_media(
    media_id: str, req: RatingRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if req.score < 1 or req.score > 10:
        raise HTTPException(status_code=400, detail="Score must be 1-10")

    result = await db.execute(
        select(Rating).where(Rating.user_id == current_user.id, Rating.media_id == media_id)
    )
    rating = result.scalar_one_or_none()
    if rating:
        rating.score = req.score
    else:
        rating = Rating(user_id=current_user.id, media_id=media_id, score=req.score)
        db.add(rating)
    await db.flush()
    return RatingInfo(media_id=media_id, score=rating.score, updated_at=rating.updated_at)


@router.get("/rating/{media_id}", response_model=RatingInfo)
async def get_my_rating(
    media_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Rating).where(Rating.user_id == current_user.id, Rating.media_id == media_id)
    )
    rating = result.scalar_one_or_none()
    if rating is None:
        raise HTTPException(status_code=404, detail="Not rated")
    return RatingInfo(media_id=media_id, score=rating.score, updated_at=rating.updated_at)


@router.put("/status/{media_id}")
async def set_media_status(
    media_id: str, status: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if status not in [s.value for s in UserMediaStatus]:
        raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    result = await db.execute(
        select(UserMediaInteraction).where(
            UserMediaInteraction.user_id == current_user.id,
            UserMediaInteraction.media_id == media_id,
        )
    )
    interaction = result.scalar_one_or_none()
    if interaction:
        interaction.status = UserMediaStatus(status)
    else:
        interaction = UserMediaInteraction(
            user_id=current_user.id, media_id=media_id, status=UserMediaStatus(status)
        )
        db.add(interaction)
    await db.flush()
    return UserInteractionStatus(media_id=media_id, status=status)


@router.get("/status/{media_id}", response_model=UserInteractionStatus)
async def get_my_status(
    media_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(UserMediaInteraction).where(
            UserMediaInteraction.user_id == current_user.id,
            UserMediaInteraction.media_id == media_id,
        )
    )
    interaction = result.scalar_one_or_none()
    if interaction is None:
        return UserInteractionStatus(media_id=media_id, status="none")
    return UserInteractionStatus(media_id=media_id, status=interaction.status.value)
