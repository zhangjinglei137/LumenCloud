"""运行日志 API（admin，docs/新系统设计.md §9.2 Logs）。

- GET /api/logs  task_run 查询：task_type/status/media_id 过滤，按 started_at 倒序
  日志脱敏（§9.1）：task_run 本就不含 token/凭据明文，DTO 白名单直出。
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TaskRun, User
from app.routers.deps import get_current_admin, get_session

router = APIRouter(prefix="/logs", tags=["logs"])


@router.get("")
async def list_logs(
    admin: User = Depends(get_current_admin),  # 仅 admin（§9.1 日志）
    session: AsyncSession = Depends(get_session),
    task_type: str | None = Query(default=None, max_length=64),
    status: str | None = Query(default=None, max_length=32),
    media_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    """task_run 查询记录（task_type/status/media_id 可选过滤，time 倒序分页）。"""
    stmt = select(TaskRun)
    if task_type:
        stmt = stmt.where(TaskRun.task_type == task_type)
    if status:
        stmt = stmt.where(TaskRun.status == status)
    if media_id is not None:
        stmt = stmt.where(TaskRun.media_id == media_id)
    stmt = (
        stmt.order_by(TaskRun.started_at.desc(), TaskRun.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": r.id,
            "task_type": r.task_type,
            "media_id": r.media_id,
            "status": r.status,
            "message": r.message,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
        }
        for r in rows
    ]