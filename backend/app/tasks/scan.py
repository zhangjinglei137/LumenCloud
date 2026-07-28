from datetime import datetime, timezone
from sqlalchemy import select
from app.database import async_session
from app.models import Media, MediaStatus, MediaType
from app.services.emby import emby_service


async def scan_all_media(ctx):
    async with async_session() as db:
        result = await db.execute(
            select(Media).where(Media.status == MediaStatus.TRACKING)
        )
        media_list = result.scalars().all()

    for media in media_list:
        if media.last_scanned_at:
            hours_since = (datetime.now(timezone.utc) - media.last_scanned_at).total_seconds() / 3600
            if hours_since < media.scan_frequency_hours:
                continue
        await ctx["redis"].enqueue_job("app.tasks.scan.scan_single_media", media.id)


async def scan_single_media(ctx, media_id: str):
    async with async_session() as db:
        result = await db.execute(select(Media).where(Media.id == media_id))
        media = result.scalar_one_or_none()
        if media is None:
            return

        emby_items = await emby_service.get_items_by_provider(media.tmdb_id)
        emby_item_list = emby_items.get("Items", [])

        if not emby_item_list:
            media.last_scanned_at = datetime.now(timezone.utc)
            await db.commit()
            return

        emby_item = emby_item_list[0]
        parent_id = emby_item.get("Id")

        if media.media_type == MediaType.TV and parent_id:
            missing = await emby_service.get_missing_episodes(parent_id)
            missing_items = missing.get("Items", [])

            if missing_items:
                missing_codes = []
                for ep in missing_items:
                    season = ep.get("ParentIndexNumber", 1)
                    episode = ep.get("IndexNumber", 1)
                    missing_codes.append(f"S{season:02d}E{episode:02d}")

                await ctx["redis"].enqueue_job(
                    "app.tasks.download.run_download_pipeline",
                    media_id, missing_codes,
                )

        media.last_scanned_at = datetime.now(timezone.utc)
        await db.commit()
