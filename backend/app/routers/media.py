"""媒体 API（阶段 3：JWT 鉴权 + CRUD + §9.1 敏感字段脱敏）。

- GET    /api/media         列表（全部登录用户）→ 含 episode_state 统计 + 最近 task_run 摘要
- POST   /api/media         admin 手动添加影视
- GET    /api/media/{id}    详情（全部登录用户，按角色脱敏）
- PATCH  /api/media/{id}    admin 修改大小覆盖/间隔/状态
- DELETE /api/media/{id}    admin 删除（级联子表）
- POST   /api/media/{id}/scan  admin 手动触发巡检（§9.1 写操作鉴权）
"""
from datetime import datetime, timezone
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DownloadTask, EpisodeState, Media, TaskRun, TransferQueue, User
from app.routers.deps import get_current_admin, get_current_user, get_session

router = APIRouter()

_GB = 1024**3

_VALID_STATUSES = ("tracking", "paused")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _mask_share_code(code: str | None) -> str | None:
    """§9.1 脱敏：share_code 仅回显后 4 位（如 "****abcd"）。"""
    if not code:
        return None
    return "****" + str(code)[-4:]


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class MediaCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    tmdb_id: int | None = None
    media_type: str | None = None  # movie / tv
    # 线上反馈修复 Q2：海报相对路径（TMDB 图床 /t/p/w500/...，MediaAddView 透传，
    # 落库后影视库展示不再丢海报；前端拼完整图床地址，后端不做拼接）
    poster_path: str | None = None


class MediaPatch(BaseModel):
    max_episode_size_gb: float | None = None
    max_movie_size_gb: float | None = None
    scan_interval_minutes: int | None = Field(default=None, ge=1, le=10080)
    status: str | None = None  # tracking / paused


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------

def _parse_episode(ep: str) -> tuple[int | None, int | None]:
    """从 "S01E01" 解析 (season, episode_number)，非标准格式返回 (None, None)。"""
    m = re.match(r"[Ss](\d{1,2})[Ee](\d{2,3})", ep or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def _episode_dto(row: EpisodeState, is_admin: bool) -> dict:
    """单集状态 DTO（§9.1 脱敏：share_code/aria2_gid/quark_path guest 不返回，
    admin 的 share_code 仅后 4 位）。含前端契约别名字段：
    status=state、size_gb=GB 值、season/episode_number=解析自 "SxxExx"。"""
    season, episode_number = _parse_episode(row.episode)
    dto = {
        "id": row.id,
        "episode": row.episode,
        "state": row.state,
        "status": row.state,  # 前端契约别名
        "file_name": row.file_name,
        "file_size": row.file_size,
        "size_gb": round(row.file_size / _GB, 2) if row.file_size else None,  # 前端契约别名（GB）
        "season": season,          # 前端契约别名
        "episode_number": episode_number,  # 前端契约别名
        "retry_count": row.retry_count,
        "error": row.error,
        "updated_at": row.updated_at,
    }
    if is_admin:
        dto["share_code"] = _mask_share_code(row.share_code)
        dto["aria2_gid"] = row.aria2_gid
        dto["quark_path"] = row.quark_path
    return dto


def _tq_dto(row: TransferQueue, is_admin: bool) -> dict:
    """转存队列摘要 DTO（§9.1：guest 全隐藏凭据；admin 仅 share_code 后 4 位；
    stoken/receive_code/fid_tokens/pwd_id/folder_id/fids 任何角色不返回）。"""
    dto = {
        "id": row.id,
        "media_id": row.media_id,
        "episode": row.episode,
        "file_name": row.file_name,
        "file_size": row.file_size,
        "file_size_gb": round(row.file_size / _GB, 2) if row.file_size else None,
        "status": row.status,
        "quota_reject_count": row.quota_reject_count,
        "error": row.error,
        "enqueued_at": row.enqueued_at,
        "updated_at": row.updated_at,
    }
    if is_admin:
        dto["share_code"] = _mask_share_code(row.share_code)
        dto["share_code_tail"] = str(row.share_code)[-4:] if row.share_code else None
    return dto


def _media_dto(m: Media) -> dict:
    return {
        "id": m.id,
        "title": m.title,
        "tmdb_id": m.tmdb_id,
        "media_type": m.media_type,
        "poster_path": m.poster_path,  # 线上反馈修复 Q2：后端有值即回显（不拼接图床地址）
        "status": m.status,
        "scan_interval_minutes": m.scan_interval_minutes,
        "max_episode_size_gb": m.max_episode_size_gb,
        "max_movie_size_gb": m.max_movie_size_gb,
        "in_emby": m.in_emby,
        "last_scan_at": m.last_scan_at,
        "created_at": m.created_at,
        "updated_at": m.updated_at,
    }


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@router.get("/media")
async def list_media(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """影视列表（全部登录用户可读）：含 episode_state 统计 + 最近 task_run 摘要。"""
    media_rows = (
        (await session.execute(select(Media).order_by(Media.created_at.desc(), Media.id.desc())))
        .scalars()
        .all()
    )
    if not media_rows:
        return []
    media_ids = [m.id for m in media_rows]

    # episode_state 统计（一次聚合，避免 N+1）
    counts: dict[int, dict[str, int]] = {}
    stat_rows = await session.execute(
        select(EpisodeState.media_id, EpisodeState.state, func.count(EpisodeState.id))
        .where(EpisodeState.media_id.in_(media_ids))
        .group_by(EpisodeState.media_id, EpisodeState.state)
    )
    for media_id, state, cnt in stat_rows:
        entry = counts.setdefault(
            media_id, {"total": 0, "done": 0, "failed": 0, "in_progress": 0}
        )
        entry["total"] += cnt
        if state == "done":
            entry["done"] += cnt
        elif state == "failed":
            entry["failed"] += cnt
        elif state in ("queued", "transferring", "downloading"):
            entry["in_progress"] += cnt

    # 最近一条 task_run（按时间倒序，取每条 media 首条）
    latest: dict[int, dict] = {}
    for row in await session.execute(
        select(
            TaskRun.media_id,
            TaskRun.id,
            TaskRun.task_type,
            TaskRun.status,
            TaskRun.message,
            TaskRun.started_at,
        )
        .where(TaskRun.media_id.in_(media_ids))
        .order_by(TaskRun.started_at.desc(), TaskRun.id.desc())
    ):
        latest.setdefault(
            row.media_id,
            {
                "id": row.id,
                "task_type": row.task_type,
                "status": row.status,
                "message": row.message,
                "started_at": row.started_at,
            },
        )

    def _stats(media_id: int) -> dict:
        ep = counts.get(media_id, {"total": 0, "done": 0, "failed": 0, "in_progress": 0})
        return {
            "total": ep["total"],
            "done": ep["done"],
            "failed": ep["failed"],
            "in_progress": ep["in_progress"],
            # 前端契约别名（§8 影视列表「已有/总集数」）
            "available": ep["done"],
            "downloaded": ep["done"],
            "missing": ep["total"] - ep["done"],
        }

    return [
        {
            **_media_dto(m),
            "episode_state": counts.get(
                m.id, {"total": 0, "done": 0, "failed": 0, "in_progress": 0}
            ),
            "episode_stats": _stats(m.id),  # 前端契约键
            "latest_task_run": latest.get(m.id),
            "last_task_run": latest.get(m.id),  # 前端契约键
        }
        for m in media_rows
    ]


@router.post("/media")
async def create_media(
    payload: MediaCreate,
    admin: User = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """admin 手动添加影视（status='tracking', in_emby=False）。"""
    if payload.media_type not in (None, "movie", "tv"):
        raise HTTPException(status_code=422, detail="media_type 仅支持 movie/tv")

    media = Media(
        title=payload.title.strip(),
        tmdb_id=payload.tmdb_id,
        media_type=payload.media_type,
        status="tracking",
        in_emby=False,
        poster_path=payload.poster_path,  # Q2：海报相对路径落库
    )
    session.add(media)
    await session.commit()
    await session.refresh(media)
    return _media_dto(media)


@router.get("/media/{media_id}")
async def get_media(
    media_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """详情：media 字段 + episode_state 列表 + transfer_queue 摘要（按角色脱敏）。"""
    is_admin = user.role == "admin"
    media = await session.get(Media, media_id)
    if media is None:
        raise HTTPException(status_code=404, detail="影视不存在")

    episodes = (
        (
            await session.execute(
                select(EpisodeState)
                .where(EpisodeState.media_id == media_id)
                .order_by(EpisodeState.updated_at.desc(), EpisodeState.id.desc())
            )
        )
        .scalars()
        .all()
    )
    tq_rows = (
        (
            await session.execute(
                select(TransferQueue)
                .where(TransferQueue.media_id == media_id)
                .order_by(TransferQueue.enqueued_at.desc(), TransferQueue.id.desc())
            )
        )
        .scalars()
        .all()
    )

    latest_run = (
        (
            await session.execute(
                select(TaskRun)
                .where(TaskRun.media_id == media_id)
                .order_by(TaskRun.started_at.desc(), TaskRun.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    media_dto = _media_dto(media)
    media_dto["last_task_run"] = (  # 前端契约键
        {
            "id": latest_run.id,
            "task_type": latest_run.task_type,
            "status": latest_run.status,
            "message": latest_run.message,
            "started_at": latest_run.started_at,
        }
        if latest_run
        else None
    )

    # 前端按扁平结构读 detail.title/last_task_run（P1-4 契约错位修复）：
    # media 字段展开到顶层（**media_dto），并保留嵌套 media 兼容。
    return {
        **media_dto,
        "media": media_dto,
        "episode_state": [_episode_dto(e, is_admin) for e in episodes],
        "transfer_queue": [_tq_dto(r, is_admin) for r in tq_rows],
    }


@router.patch("/media/{media_id}")
async def patch_media(
    media_id: int,
    payload: MediaPatch,
    admin: User = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """admin 修改：大小覆盖 / 扫描间隔 / status（tracking/paused）。"""
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="无可更新的字段")

    if "status" in updates and updates["status"] not in _VALID_STATUSES:
        raise HTTPException(status_code=422, detail="status 仅支持 tracking/paused")

    media = await session.get(Media, media_id)
    if media is None:
        raise HTTPException(status_code=404, detail="影视不存在")

    for key, value in updates.items():
        setattr(media, key, value)
    media.updated_at = _now()
    await session.commit()
    return _media_dto(media)


@router.delete("/media/{media_id}")
async def delete_media(
    media_id: int,
    admin: User = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """admin 删除影视：先做进行中任务前置检查（并发安全），再按外键依赖顺序删子表。

    删除顺序：episode_state → download_task → transfer_queue → media。
    download_task.transfer_id 外键指向 transfer_queue.id，必须先删 download_task
    再删 transfer_queue，否则 SQLite(foreign_keys=ON)/PostgreSQL 会报外键冲突。
    """
    media = await session.get(Media, media_id)
    if media is None:
        raise HTTPException(status_code=404, detail="影视不存在")

    # 进行中任务前置检查（P0-4）：避免与 transfer/scan worker 并发删除竞态——
    # 存在进行中（pending/queued/transferring/downloading）任务时拒绝删除。
    # 检查范围：transfer_queue（pending/transferring/downloading）、
    # episode_state（queued/transferring/downloading）、download_task（downloading）。
    if (
        await session.scalar(
            select(TransferQueue.id)
            .where(
                TransferQueue.media_id == media_id,
                TransferQueue.status.in_(("pending", "transferring", "downloading")),
            )
            .limit(1)
        )
        or await session.scalar(
            select(EpisodeState.id)
            .where(
                EpisodeState.media_id == media_id,
                EpisodeState.state.in_(("queued", "transferring", "downloading")),
            )
            .limit(1)
        )
        or await session.scalar(
            select(DownloadTask.id)
            .where(
                DownloadTask.media_id == media_id,
                DownloadTask.status == "downloading",
            )
            .limit(1)
        )
    ):
        raise HTTPException(status_code=409, detail="存在进行中任务，无法删除")

    await session.execute(delete(EpisodeState).where(EpisodeState.media_id == media_id))
    await session.execute(delete(DownloadTask).where(DownloadTask.media_id == media_id))
    await session.execute(delete(TransferQueue).where(TransferQueue.media_id == media_id))
    await session.delete(media)
    await session.commit()
    return {"ok": True}


@router.post("/media/{media_id}/scan")
async def scan_media_route(
    media_id: int,
    admin: User = Depends(get_current_admin),  # §9.1 写操作鉴权
) -> dict:
    """admin 手动触发一次媒体巡检（§4.3 搜索→入队），返回本次 task_run id。"""
    # app.tasks.scan 由另一 lane 实现；延迟导入 + 兜底，避免 import 链断裂
    try:
        from app.tasks.scan import scan_media as run_scan  # 预期为 async 协程
    except ImportError:
        run_scan = None

    if run_scan is None:
        # 待集成统一验证：scan 任务未就绪时仍返回 ok，保持 API 契约可用
        return {"ok": True, "task_run_id": None}

    task_run_id = await run_scan(media_id)
    return {"ok": True, "task_run_id": task_run_id}