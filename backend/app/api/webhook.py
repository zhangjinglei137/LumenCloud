from datetime import datetime, timezone
from fastapi import APIRouter, Request
from sqlalchemy import select
from app.database import async_session
from app.models import DownloadTask, Media, MediaStatus
from app.services.nastools import nastools_service
from app.services.pushplus import pushplus_service

router = APIRouter(prefix="/api/webhook", tags=["webhook"])


@router.post("/aria2")
async def aria2_callback(request: Request):
    body = await request.json()
    gid = body.get("gid", "")

    if not gid:
        return {"status": "ignored"}

    async with async_session() as db:
        result = await db.execute(
            select(DownloadTask).where(DownloadTask.aria2_gid == gid)
        )
        task = result.scalar_one_or_none()
        if task is None:
            return {"status": "ignored"}

        task.status = "completed"
        task.completed_at = datetime.now(timezone.utc)

        media_result = await db.execute(select(Media).where(Media.id == task.media_id))
        media = media_result.scalar_one_or_none()

        pending = await db.execute(
            select(DownloadTask).where(
                DownloadTask.media_id == task.media_id,
                DownloadTask.status == "downloading",
            )
        )
        if not pending.scalar_one_or_none():
            if media:
                media.status = MediaStatus.COMPLETED

        await db.commit()

    # Trigger NasTools
    try:
        await nastools_service.restart()
        import asyncio
        await asyncio.sleep(45)
        await nastools_service.directory_sync()
    except Exception:
        pass

    # PushPlus notification
    media_title = media.title if media else "\u672a\u77e5\u8d44\u6e90"
    await pushplus_service.send(
        title=f"\u2728 {media_title}",
        content=f"<b>\u4e0b\u8f7d\u5b8c\u6210</b><br>\u6587\u4ef6: {task.file_name}<br>\u96c6\u6570: {task.episode_range}",
    )

    return {"status": "ok"}
