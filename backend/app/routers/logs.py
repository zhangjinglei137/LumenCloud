"""运行日志 API（admin，docs/新系统设计.md §9.2 Logs）。

- GET /api/logs  task_run 查询：task_type/status/media_id/tmdb_id/title 过滤，按 started_at 倒序
  日志脱敏（§9.1）：task_run 本就不含 token/凭据明文，DTO 白名单直出。
- 线上反馈修复 Q8：支持按 tmdb_id 搜索；返回项附 media_title（影视名称）与 tmdb_id
  （join media 表装配），前端日志不再只看数字 id。
- P1-2：新增 title 影视名称模糊搜索（ilike，join 已有 media 表）。
- 巡检可见性改造：返回项附 phases / scan_detail（TEXT JSON 解析为 dict/None）。
- GET /api/logs/{id}：单条完整巡检/任务记录（含 media_title；media 不存在为 None）。
"""
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Media, TaskRun, User
from app.routers.deps import get_current_admin, get_session

router = APIRouter(prefix="/logs", tags=["logs"])


def _json_or_none(raw: str | None) -> dict | None:
    """task_run.phases/scan_detail TEXT 列 → dict；NULL/非法 JSON 返回 None。"""
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


@router.get("")
async def list_logs(
    admin: User = Depends(get_current_admin),  # 仅 admin（§9.1 日志）
    session: AsyncSession = Depends(get_session),
    task_type: str | None = Query(default=None, max_length=64),
    status: str | None = Query(default=None, max_length=32),
    media_id: int | None = Query(default=None),
    # Q8：按 TMDB id 搜索日志（多个 media 可同 tmdb_id，用子查询覆盖全部）
    tmdb_id: int | None = Query(default=None),
    # P1-2：按影视名称模糊搜索（ilike；空串不生效）
    title: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    """task_run 查询记录（task_type/status/media_id/tmdb_id/title 可选过滤，均 AND；time 倒序分页）。"""
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
    if title:
        # P1-2：影视名称模糊搜索（join 已有 isouter media，直接用 Media.title；空串不生效）
        stmt = stmt.where(Media.title.ilike(f"%{title}%"))
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
            "duration_seconds": r[0].duration_seconds,  # Q8①：真实耗时（job 入口计时；历史为 None）
            "media_title": r[1],  # Q8：影视名称（join media；无关联为 None）
            "tmdb_id": r[2],      # Q8：TMDB id（join media；无关联为 None）
            "phases": _json_or_none(r[0].phases),        # 巡检 5 阶段进度 JSON（dict/None）
            "scan_detail": _json_or_none(r[0].scan_detail),  # 巡检结果摘要 JSON（dict/None）
        }
        for r in rows
    ]


@router.get("/{task_run_id}")
async def get_log(
    task_run_id: int,
    admin: User = Depends(get_current_admin),  # 仅 admin（§9.1 日志）
    session: AsyncSession = Depends(get_session),
) -> dict:
    """单条完整运行记录（巡检可见性：含 phases / scan_detail / media_title）。

    404：记录不存在（media 已删等无关联场景仍返回 200，media_title=None）。
    """
    row = (
        await session.execute(
            select(TaskRun, Media.title)
            .outerjoin(Media, Media.id == TaskRun.media_id)
            .where(TaskRun.id == task_run_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="日志记录不存在")
    run = row[0]
    return {
        "id": run.id,
        "task_type": run.task_type,
        "media_id": run.media_id,
        "status": run.status,
        "message": run.message,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "duration_seconds": run.duration_seconds,
        "phases": _json_or_none(run.phases),
        "scan_detail": _json_or_none(run.scan_detail),
        "media_title": row[1],
    }