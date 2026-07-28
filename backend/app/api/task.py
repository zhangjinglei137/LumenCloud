from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models import User, DownloadTask, Media
from app.api.deps import get_admin_user
from app.schemas.task import DownloadTaskInfo

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("/", response_model=list[DownloadTaskInfo])
async def list_tasks(
    status: str = None,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    query = select(DownloadTask).order_by(DownloadTask.created_at.desc())
    if status:
        query = query.where(DownloadTask.status == status)
    query = query.limit(50)
    result = await db.execute(query)
    tasks = []
    for t in result.scalars():
        media_result = await db.execute(select(Media).where(Media.id == t.media_id))
        media = media_result.scalar_one_or_none()
        tasks.append(DownloadTaskInfo(
            id=t.id, media_id=t.media_id,
            media_title=media.title if media else None,
            episode_range=t.episode_range, file_name=t.file_name,
            status=t.status, created_at=t.created_at, completed_at=t.completed_at,
        ))
    return tasks


@router.get("/{task_id}", response_model=DownloadTaskInfo)
async def get_task(
    task_id: str,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(DownloadTask).where(DownloadTask.id == task_id))
    t = result.scalar_one_or_none()
    if t is None:
        raise HTTPException(status_code=404, detail="Task not found")
    media_result = await db.execute(select(Media).where(Media.id == t.media_id))
    media = media_result.scalar_one_or_none()
    return DownloadTaskInfo(
        id=t.id, media_id=t.media_id,
        media_title=media.title if media else None,
        episode_range=t.episode_range, file_name=t.file_name,
        status=t.status, created_at=t.created_at, completed_at=t.completed_at,
    )
