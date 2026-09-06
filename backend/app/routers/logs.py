"""运行日志 API（admin，docs/新系统设计.md §9.2 Logs）。

- GET /api/logs  task_run 查询：task_type/status/media_id/tmdb_id 过滤，按 started_at 倒序
  日志脱敏（§9.1）：task_run 本就不含 token/凭据明文，DTO 白名单直出。
- 线上反馈修复 Q8：支持按 tmdb_id 搜索；返回项附 media_title（影视名称）与 tmdb_id
  （join media 表装配），前端日志不再只看数字 id。
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Media, TaskRun, User
from app.routers.deps import get_current_admin, get_session

router = APIRouter(prefix="/logs", tags=["logs"])


@router.get("")
async def list_logs(
    admin: User = Depends(get_current_admin),  # 仅 admin（§9.1 日志）
    session: AsyncSession = Depends(get_session),
    task_type: str | None = Query(default=None, max_length=64),
    status: str | None = Query(default=None, max_length=32),
    media_id: int | None = Query(default=None),
    # Q8：按 TMDB id 搜索日志（多个 media 可同 tmdb_id，用子查询覆盖全部）
    tmdb_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    """task_run 查询记录（task_type/status/media_id/tmdb_id 可选过滤，均 AND；time 倒序分页）。"""
    stmt = select(TaskRun, Media.title, Media.tmdb_id).join(
        Media, TaskRun.media_id == Media.id, isouter=True
    )
    if task_type:
        stmt = stmt.where(TaskRun.task_type == task_type)
    if status:
        stmt = stmt.where(TaskRun.status == status)
    if media_id is not None:
        stmt = stmt.where(TaskRun.media_id == media_id)
    if tmdb_id is not None:
        # 子查询取「该 tmdb_id 对应的全部 media.id」，覆盖同 tmdb 多 media 场景；
        # 缺省 media 的 task_run（media 已删 / 无关联）不命中
        stmt = stmt.where(TaskRun.media_id.in_(select(Media.id).where(Media.tmdb_id == tmdb_id)))
    stmt = (
        stmt.order_by(TaskRun.started_at.desc(), TaskRun.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "id": r[0].id,
            "task_type": r[0].task_type,
            "media_id": r[0].media_id,
            "status": r[0].status,
            "message": r[0].message,
            "started_at": r[0].started_at,
            "finished_at": r[0].finished_at,
            "media_title": r[1],  # Q8：影视名称（join media；无关联为 None）
            "tmdb_id": r[2],      # Q8：TMDB id（join media；无关联为 None）
        }
        for r in rows
    ]