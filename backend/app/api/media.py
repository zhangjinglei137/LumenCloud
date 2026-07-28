from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models import Media, Season, Episode, Subscription, Vote
from app.schemas.media import MediaDetail, MediaListItem
from app.services.tmdb import tmdb_service
from app.services.emby import emby_service

router = APIRouter(prefix="/api/media", tags=["media"])


@router.get("/views")
async def list_emby_views():
    """获取 Emby 媒体库视图分类（电影、剧集、动漫等文件夹）"""
    try:
        resp = await emby_service.get_user_views()
        views = []
        for item in resp.get("Items", []):
            views.append({
                "id": item.get("Id"),
                "name": item.get("Name"),
                "type": item.get("CollectionType", "mixed"),
            })
        return {"views": views}
    except Exception:
        return {"views": []}


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


@router.get("/library")
async def list_emby_library(
    page: int = Query(1, ge=1),
    item_type: str = Query("all", description="movie/tv/all"),
    parent_id: str = Query(None, description="Emby 媒体库视图 ID"),
    db: AsyncSession = Depends(get_db),
):
    """从 Emby 获取全部影视库内容，叠加本地状态"""
    from app.models import Media as MediaModel, Subscription as SubscriptionModel

    all_items = []
    types_to_fetch = []
    if item_type in ("movie", "all"): types_to_fetch.append("Movie")
    if item_type in ("tv", "all"): types_to_fetch.append("Series")

    for etype in types_to_fetch:
        try:
            resp = await emby_service.get_items_by_type(etype, parent_id=parent_id)
            all_items.extend(resp.get("Items", []))
        except Exception:
            pass

    page_size = 24
    start = (page - 1) * page_size
    page_items = all_items[start:start + page_size]

    results = []
    for item in page_items:
        tmdb_id = _extract_tmdb_id(item.get("ProviderIds", {}))
        user_data = item.get("UserData", {})

        entry = {
            "emby_id": item.get("Id"),
            "title": item.get("Name", ""),
            "type": "movie" if item.get("Type") == "Movie" else "tv",
            "year": item.get("ProductionYear"),
            "image_tag": item.get("ImageTags", {}).get("Primary"),
            "play_count": user_data.get("PlayCount", 0),
            "is_favorite": user_data.get("IsFavorite", False),
            "is_played": user_data.get("Played", False),
            "tmdb_id": tmdb_id,
            "community_rating": item.get("CommunityRating"),
            "local_media_id": None,
            "library_status": None,
            "subscription_count": 0,
        }

        if tmdb_id:
            result = await db.execute(select(MediaModel).where(MediaModel.tmdb_id == tmdb_id))
            local = result.scalar_one_or_none()
            if local:
                entry["local_media_id"] = local.id
                entry["library_status"] = local.status.value
                sub_count = (await db.execute(
                    select(func.count(SubscriptionModel.id)).where(SubscriptionModel.media_id == local.id)
                )).scalar() or 0
                entry["subscription_count"] = sub_count

        results.append(entry)

    return {
        "items": results,
        "total": len(all_items),
        "page": page,
        "has_more": len(page_items) == page_size,
    }


@router.get("/emby/{emby_id}")
async def get_emby_detail(emby_id: str, db: AsyncSession = Depends(get_db)):
    """直接从 Emby 获取影视详情（不依赖本地数据库）"""
    emby_item = await emby_service.get_item_by_id(emby_id)
    if not emby_item:
        raise HTTPException(status_code=404, detail="Emby item not found")

    tmdb_id = _extract_tmdb_id(emby_item.get("ProviderIds", {}))
    user_data = emby_item.get("UserData", {})

    # 检查本地数据库
    local_media_id = None
    library_status = None
    subscription_count = 0
    if tmdb_id:
        from app.models import Media as MediaModel, Subscription as SubscriptionModel
        result = await db.execute(select(MediaModel).where(MediaModel.tmdb_id == tmdb_id))
        local = result.scalar_one_or_none()
        if local:
            local_media_id = local.id
            library_status = local.status.value
            sub_count = (await db.execute(
                select(func.count(SubscriptionModel.id)).where(SubscriptionModel.media_id == local.id)
            )).scalar() or 0
            subscription_count = sub_count

    return {
        "emby_id": emby_item.get("Id"),
        "title": emby_item.get("Name", ""),
        "type": "movie" if emby_item.get("Type") == "Movie" else "tv",
        "year": emby_item.get("ProductionYear"),
        "image_tag": emby_item.get("ImageTags", {}).get("Primary"),
        "overview": emby_item.get("Overview", ""),
        "genres": emby_item.get("Genres", []),
        "community_rating": emby_item.get("CommunityRating"),
        "play_count": user_data.get("PlayCount", 0),
        "is_favorite": user_data.get("IsFavorite", False),
        "is_played": user_data.get("Played", False),
        "premiere_date": emby_item.get("PremiereDate"),
        "tmdb_id": tmdb_id,
        "local_media_id": local_media_id,
        "library_status": library_status,
        "subscription_count": subscription_count,
        "seasons": _get_seasons_from_emby(emby_item),
    }


def _get_seasons_from_emby(emby_item: dict) -> list:
    """从 Emby TV Series 中提取季/集结构"""
    if emby_item.get("Type") != "Series":
        return []
    children = emby_item.get("ChildCount", 0)
    if children == 0:
        return []
    # 简单标记有子内容，前端可展开
    return [{"season_number": -1, "name": "剧集列表", "episodes": []}]


@router.get("/{media_id}", response_model=MediaDetail)
async def get_media_detail(media_id: str, db: AsyncSession = Depends(get_db)):
    # 先按 UUID 查
    result = await db.execute(select(Media).where(Media.id == media_id))
    media = result.scalar_one_or_none()
    # 失败时按 tmdb_id 查
    if media is None and media_id.isdigit():
        result = await db.execute(select(Media).where(Media.tmdb_id == int(media_id)))
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


def _extract_tmdb_id(provider_ids: dict) -> int | None:
    """从 Emby ProviderIds 中提取 TMDB ID"""
    if not provider_ids:
        return None
    for key, value in provider_ids.items():
        if key.lower().startswith("tmdb"):
            try:
                return int(value)
            except (ValueError, TypeError):
                pass
    return None
