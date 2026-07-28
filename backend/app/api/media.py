from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models import Media, Season, Episode, Subscription, Vote
from app.schemas.media import MediaDetail, MediaListItem
from app.services.tmdb import tmdb_service
from app.services.emby import emby_service

router = APIRouter(prefix="/api/media", tags=["media"])


@router.get("/search")
async def search_media(keyword: str, page: int = Query(1, ge=1)):
    tmdb_data = await tmdb_service.search_multi(keyword, page)
    results = []
    for item in tmdb_data.get("results", []):
        media_type = item.get("media_type")
        if media_type not in ("movie", "tv"):
            continue
        tmdb_id = item["id"]
        results.append({
            "tmdb_id": tmdb_id,
            "title": item.get("title") or item.get("name", ""),
            "original_title": item.get("original_title") or item.get("original_name"),
            "media_type": media_type,
            "overview": item.get("overview"),
            "poster_path": item.get("poster_path"),
            "release_date": item.get("release_date") or item.get("first_air_date"),
            "vote_average": item.get("vote_average"),
        })
    return {"results": results, "total_results": tmdb_data.get("total_results", 0), "page": page}


@router.get("/{media_id}", response_model=MediaDetail)
async def get_media_detail(media_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Media).where(Media.id == media_id))
    media = result.scalar_one_or_none()
    if media is None:
        raise HTTPException(status_code=404, detail="Media not found")

    sub_count = (await db.execute(
        select(func.count(Subscription.id)).where(Subscription.media_id == media_id)
    )).scalar() or 0

    watch_count = await emby_service.get_play_count(media.tmdb_id)

    season_result = await db.execute(
        select(Season).where(Season.media_id == media_id).order_by(Season.season_number)
    )
    seasons = []
    for s in season_result.scalars():
        ep_result = await db.execute(
            select(Episode).where(Episode.season_id == s.id).order_by(Episode.episode_number)
        )
        episodes = [
            {"id": e.id, "episode_number": e.episode_number, "name": e.name,
             "air_date": e.air_date, "in_emby": e.in_emby}
            for e in ep_result.scalars()
        ]
        seasons.append({
            "id": s.id, "season_number": s.season_number, "name": s.name, "episodes": episodes,
        })

    return MediaDetail(
        id=media.id, tmdb_id=media.tmdb_id, title=media.title,
        original_title=media.original_title, media_type=media.media_type.value,
        overview=media.overview, poster_path=media.poster_path, backdrop_path=media.backdrop_path,
        release_date=media.release_date, vote_average=media.vote_average,
        status=media.status.value, subscription_count=sub_count, watch_count=watch_count,
        seasons=seasons,
    )


@router.get("/", response_model=list[MediaListItem])
async def list_media(
    status: str = None, media_type: str = None,
    db: AsyncSession = Depends(get_db),
):
    query = select(Media)
    if status:
        query = query.where(Media.status == status)
    if media_type:
        query = query.where(Media.media_type == media_type)
    query = query.order_by(Media.created_at.desc()).limit(100)
    result = await db.execute(query)
    items = []
    for m in result.scalars():
        sub_count = (await db.execute(
            select(func.count(Subscription.id)).where(Subscription.media_id == m.id)
        )).scalar() or 0
        items.append(MediaListItem(
            id=m.id, tmdb_id=m.tmdb_id, title=m.title, media_type=m.media_type.value,
            poster_path=m.poster_path, release_date=m.release_date, vote_average=m.vote_average,
            status=m.status.value, subscription_count=sub_count, watch_count=0,
            created_at=m.created_at,
        ))
    return items
