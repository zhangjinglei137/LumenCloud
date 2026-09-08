"""媒体 API（阶段 3：JWT 鉴权 + CRUD + §9.1 敏感字段脱敏）。

- GET    /api/media         列表（全部登录用户）→ 含 episode_state 统计 + 最近 task_run 摘要
- POST   /api/media         admin 手动添加影视
- GET    /api/media/{id}    详情（全部登录用户，按角色脱敏）
- PATCH  /api/media/{id}    admin 修改大小覆盖/间隔/状态
- DELETE /api/media/{id}    admin 删除（级联子表）
- POST   /api/media/{id}/scan  admin 手动触发巡检（§9.1 写操作鉴权）
"""
from datetime import datetime, timezone
import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DownloadQueue,
    DownloadTask,
    EpisodeState,
    Media,
    TaskQueue,
    TaskRun,
    TransferQueue,
    User,
)
from app.routers.deps import get_current_admin, get_current_user, get_session
from app.services import emby, tmdb
from app.services.tmdb import TMDBUnavailable

router = APIRouter()

logger = logging.getLogger(__name__)

_GB = 1024**3

_VALID_STATUSES = ("tracking", "paused")


# 两队列重设计状态集：新表「进行中」判定（其余为终态）
_DQ_ACTIVE_STATUSES = ("pending", "transferring", "downloading", "scrape", "library", "quota_wait")
_TQ_ACTIVE_STATUSES = ("pending", "probing", "ready")


# B-1/B-2（P1）：「进行中任务」检查——新表 task_queue(pending/probing/ready) ∪
# download_queue(pending/transferring/downloading/scrape/library/quota_wait) 任一命中即 True；
# 旧三表 transfer_queue / episode_state / download_task 保留（遗留数据兼容）。有任一即 True。
async def _has_in_progress_tasks(session: AsyncSession, media_id: int) -> bool:
    return bool(
        await session.scalar(
            select(TaskQueue.id).where(
                TaskQueue.media_id == media_id,
                TaskQueue.status.in_(_TQ_ACTIVE_STATUSES),
            ).limit(1)
        )
        or await session.scalar(
            select(DownloadQueue.id).where(
                DownloadQueue.media_id == media_id,
                DownloadQueue.status.in_(_DQ_ACTIVE_STATUSES),
            ).limit(1)
        )
        or await session.scalar(
            select(TransferQueue.id).where(
                TransferQueue.media_id == media_id,
                TransferQueue.status.in_(("pending", "transferring", "downloading")),
            ).limit(1)
        )
        or await session.scalar(
            select(EpisodeState.id).where(
                EpisodeState.media_id == media_id,
                EpisodeState.state.in_(("queued", "transferring", "downloading")),
            ).limit(1)
        )
        or await session.scalar(
            select(DownloadTask.id).where(
                DownloadTask.media_id == media_id,
                DownloadTask.status == "downloading",
            ).limit(1)
        )
    )


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


def _episode_dto(row: EpisodeState, is_admin: bool,
                 in_emby_codes: set[str] | None = None,
                 air_map: dict[tuple[int, int], str | None] | None = None) -> dict:
    """单集状态 DTO（§9.1 脱敏：share_code/aria2_gid/quark_path guest 不返回，
    admin 的 share_code 仅后 4 位）。含前端契约别名字段：
    status=state、size_gb=GB 值、season/episode_number=解析自 "SxxExx"。

    详情页集数状态 tag 增强（in_emby / air_date）：
    - in_emby: 该集是否已在 Emby 库（由 in_emby_codes 命中判断；None/空 → False）；
    - air_date: 该集 TMDB 首播日期 "YYYY-MM-DD"（air_map 按 (season, episode_number)
      查表；非标准集号或空 map → None）。非 tv / 无 tmdb_id / 外部服务故障时调用方
      传 None → 恒 False / None，绝不因增强字段阻断详情接口。
    """
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
        "in_emby": bool(in_emby_codes and row.episode in in_emby_codes),
        "air_date": air_map.get((season, episode_number))
        if (season is not None and episode_number is not None and air_map) else None,
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


def _dq_episode_dto(row: DownloadQueue, is_admin: bool,
                    in_emby_codes: set[str] | None = None,
                    air_map: dict[tuple[int, int], str | None] | None = None) -> dict:
    """单集状态 DTO（新表 download_queue 行，两队列重构后详情 episode_state 主数据源）。

    字段对齐旧 _episode_dto 前端契约：state=status（执行层状态直接沿用），
    size_gb/season/episode_number 由 file_size/episode 推导；§9.1 脱敏同旧表：
    share_code/aria2_gid/quark_path guest 不返回，admin 的 share_code 仅后 4 位。

    in_emby / air_date 语义同 _episode_dto（详情页集数状态 tag 增强字段）：
    Emby 库内集 in_emby=True；air_date 取 TMDB 首播日期；非 tv/无 tmdb_id/
    外部服务故障 → 恒 False / None，不阻断详情接口。
    """
    season, episode_number = _parse_episode(row.episode)
    dto = {
        "id": row.id,
        "episode": row.episode,
        "state": row.status,
        "status": row.status,  # 前端契约别名
        "file_name": row.file_name,
        "file_size": row.file_size,
        "size_gb": round(row.file_size / _GB, 2) if row.file_size else None,  # 前端契约别名（GB）
        "season": season,          # 前端契约别名
        "episode_number": episode_number,  # 前端契约别名
        "in_emby": bool(in_emby_codes and row.episode in in_emby_codes),
        "air_date": air_map.get((season, episode_number))
        if (season is not None and episode_number is not None and air_map) else None,
        "retry_count": row.retry_count,
        "error": row.error,
        "updated_at": row.updated_at,
    }
    if is_admin:
        dto["share_code"] = _mask_share_code(row.share_code)
        dto["aria2_gid"] = row.aria2_gid
        dto["quark_path"] = row.quark_path
    return dto


def _task_queue_dto(row: TaskQueue, is_admin: bool) -> dict:
    """探测队列 DTO（新表 task_queue 行，两队列重构后详情 transfer_queue 主数据源）。

    §9.1 脱敏：guest 不返回 share_code；admin 仅回显后 4 位（_mask_share_code）。
    """
    dto = {
        "id": row.id,
        "media_id": row.media_id,
        "episode": row.episode,
        "file_name": row.file_name,
        "file_size": row.file_size,
        "file_size_gb": round(row.file_size / _GB, 2) if row.file_size else None,
        "status": row.status,
        "error": row.error,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    if is_admin:
        dto["share_code"] = _mask_share_code(row.share_code)
    return dto


def _media_dto(m: Media) -> dict:
    return {
        "id": m.id,
        "title": m.title,
        "tmdb_id": m.tmdb_id,
        "media_type": m.media_type,
        "poster_path": m.poster_path,  # 线上反馈修复 Q2：后端有值即回显（不拼接图床地址）
        "series_status": m.series_status,  # Q12：在更/完结（TMDB status 原值）
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

    # episode_state 统计（两队列重构后「新表为主、旧表兼容」）：对每部 media 按
    # (media_id, episode) 键在 task_queue ∪ download_queue ∪ episode_state 三表
    # 去重合并后统计。同键多行以最「重」状态为准（in_progress > failed > done）；
    # download_queue 为执行层权威源，task_queue 探测态计 in_progress，
    # episode_state 保留兼容遗留数据。一次聚合，避免 N+1。
    counts: dict[int, dict[str, int]] = {}
    _rank: dict[int, dict[str, int]] = {}  # media_id -> episode -> 最高状态优先级

    def _bump(media_id: int, episode: str, rank: int) -> None:
        d = _rank.setdefault(media_id, {})
        if episode not in d or rank > d[episode]:
            d[episode] = rank

    dq_agg = await session.execute(
        select(DownloadQueue.media_id, DownloadQueue.episode, DownloadQueue.status)
        .where(DownloadQueue.media_id.in_(media_ids))
    )
    for media_id, episode, status in dq_agg:
        if status == "done":
            _bump(media_id, episode, 1)
        elif status == "failed":
            _bump(media_id, episode, 2)
        elif status in _DQ_ACTIVE_STATUSES:
            _bump(media_id, episode, 3)
        else:
            _bump(media_id, episode, 0)  # skipped 等终态仅计入 total

    tq_agg = await session.execute(
        select(TaskQueue.media_id, TaskQueue.episode, TaskQueue.status)
        .where(TaskQueue.media_id.in_(media_ids))
    )
    for media_id, episode, status in tq_agg:
        if status in _TQ_ACTIVE_STATUSES:
            _bump(media_id, episode, 3)
        else:
            _bump(media_id, episode, 0)  # done/unmatched/error 仅计入 total

    es_agg = await session.execute(
        select(EpisodeState.media_id, EpisodeState.episode, EpisodeState.state)
        .where(EpisodeState.media_id.in_(media_ids))
    )
    for media_id, episode, state in es_agg:
        if state == "done":
            _bump(media_id, episode, 1)
        elif state == "failed":
            _bump(media_id, episode, 2)
        elif state in ("queued", "transferring", "downloading"):
            _bump(media_id, episode, 3)
        else:
            _bump(media_id, episode, 0)

    for media_id, ep_map in _rank.items():
        entry = counts[media_id] = {
            "total": len(ep_map), "done": 0, "failed": 0, "in_progress": 0,
        }
        for rank in ep_map.values():
            if rank == 1:
                entry["done"] += 1
            elif rank == 2:
                entry["failed"] += 1
            elif rank == 3:
                entry["in_progress"] += 1

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
    """admin 手动添加影视（status='tracking'；in_emby 按 Emby 实际收录实时判定，
    不再硬编码 False——Emby 故障时 fail-open 保持 False，由巡检回写兜底）。"""
    if payload.media_type not in (None, "movie", "tv"):
        raise HTTPException(status_code=422, detail="media_type 仅支持 movie/tv")

    # 本地 tmdb_id 去重（应用层防重；Emby 侧防重已移除——需求确认：影视已存在于
    # Emby 媒体库不再阻止订阅）
    if payload.tmdb_id is not None and await session.scalar(
        select(Media.id).where(Media.tmdb_id == payload.tmdb_id).limit(1)
    ):
        raise HTTPException(status_code=409, detail="该影视已在影视库，无需重复提交")

    # series_status 落库：电影/剧集展示 TMDB 状态原值（tmdb_id 非空且 media_type
    # 已知时查一次 TMDB；失败静默降级为 None，绝不阻断添加流程——与 Emby 故障
    # fail-open 风格一致；media_type 为空跳过查询）
    series_status = None
    # poster_path：有传入值优先（TMDB 搜索添加 / 手动添加 / 审批透传）；为空时若
    # 能从 TMDB 元数据取到则回填——Emby 订阅不带海报（Emby 海报是完整 URL，与
    # poster_path 的 TMDB 相对路径语义不同，故订阅时不传，由后端统一回填）；拿不到
    # 保持 None（fail-open，TMDB 无海报的影视本来无图）。
    poster_path = payload.poster_path
    if payload.tmdb_id is not None and payload.media_type is not None:
        try:
            meta = await tmdb.get_by_tmdb_id(payload.tmdb_id, payload.media_type)
        except TMDBUnavailable as exc:
            logger.warning(
                "[media] TMDB 状态查询不可用（series_status=None 放行）tmdb=%s: %s",
                payload.tmdb_id, exc,
            )
        except Exception as exc:  # noqa: BLE001 兜底：任何异常都不阻断添加
            logger.warning(
                "[media] TMDB 状态查询异常（series_status=None 放行）tmdb=%s: %s",
                payload.tmdb_id, exc,
            )
        else:
            series_status = meta.get("status")
            if poster_path is None:
                poster_path = meta.get("poster_path")

    # in_emby 实时判定（影视库「未入库」误显示修复）：创建时按 Emby 实际收录写入，
    # 不再硬编码 False；Emby 故障（未配置/不可达）fail-open 保持 False（由巡检回写
    # 兜底修正，见 scan._emby_missing_codes）。
    in_emby = False
    if payload.tmdb_id is not None:
        try:
            in_emby = (await emby.find_emby_id(payload.tmdb_id, payload.title)) is not None
        except Exception as exc:  # noqa: BLE001  Emby 故障不阻断添加
            logger.warning(
                "[media] Emby 收录检查不可用（in_emby=False 放行）tmdb=%s: %s",
                payload.tmdb_id, exc,
            )

    media = Media(
        title=payload.title.strip(),
        tmdb_id=payload.tmdb_id,
        media_type=payload.media_type,
        status="tracking",
        in_emby=in_emby,
        poster_path=poster_path,  # Q2：海报相对路径落库（Emby 订阅场景由 TMDB 回填）
        series_status=series_status,
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
    """详情：media 字段 + episode_state 列表 + transfer_queue 摘要（按角色脱敏）。

    两队列重构后「新表为主、旧表兼容」：
    - episode_state 以 download_queue 行（DTO state=status）为主，合并旧
      episode_state 中无对应 download_queue 行（同 media+episode）的遗留行；
    - transfer_queue 以 task_queue 行为主，合并旧 transfer_queue 中无对应
      task_queue 行的遗留行。排序沿用 updated_at desc, id desc。
    """
    is_admin = user.role == "admin"
    media = await session.get(Media, media_id)
    if media is None:
        raise HTTPException(status_code=404, detail="影视不存在")

    dq_rows = (
        (
            await session.execute(
                select(DownloadQueue)
                .where(DownloadQueue.media_id == media_id)
                .order_by(DownloadQueue.updated_at.desc(), DownloadQueue.id.desc())
            )
        )
        .scalars()
        .all()
    )
    es_rows = (
        (
            await session.execute(
                select(EpisodeState).where(EpisodeState.media_id == media_id)
            )
        )
        .scalars()
        .all()
    )
    dq_keys = {(r.media_id, r.episode) for r in dq_rows}
    episode_rows = list(dq_rows) + [
        e for e in es_rows if (e.media_id, e.episode) not in dq_keys
    ]
    episode_rows.sort(key=lambda r: (r.updated_at or datetime.min, r.id), reverse=True)

    # 详情页集数状态 tag 增强（in_emby / air_date）：仅 tv + tmdb_id 才查询外部服务。
    # Emby 收录集 code 集（in_emby=True 依据）与 TMDB 每集首播日期（air_date 依据，
    # 进程内 TTL 缓存）。任一外部服务故障 → 对应字段降级（in_emby 全 False /
    # air_date None），绝不阻断详情接口。
    in_emby_codes: set[str] = set()
    air_map: dict[tuple[int, int], str | None] = {}
    if (media.media_type or "").strip().lower() != "movie" and media.tmdb_id is not None:
        try:
            emby_id = await emby.find_emby_id(media.tmdb_id, media.title)
            if emby_id:
                eps = await emby.list_episodes(emby_id)
                in_emby_codes = {str(ep.get("code")) for ep in eps if ep.get("code")}
        except Exception as exc:  # noqa: BLE001  Emby 故障降级（in_emby 全 False）
            logger.warning("[media] detail in_emby 查询降级 media=%s: %s", media_id, exc)
        # TMDB 每集 air_date：只对有标准集号（"SxxExx" 可解析 season）的行涉及季调用
        seasons = {
            season for season in (_parse_episode(r.episode)[0] for r in episode_rows)
            if season is not None
        }
        for season in seasons:
            try:
                dates = await tmdb.get_tv_season_air_dates(media.tmdb_id, season)
                for ep_num, air in (dates or {}).items():
                    air_map[(season, ep_num)] = air
            except Exception as exc:  # noqa: BLE001  理论不触发（函数内已降级），双保险
                logger.warning(
                    "[media] TMDB season air_date 降级 media=%s season=%s: %s",
                    media_id, season, exc,
                )

    tq_rows = (
        (
            await session.execute(
                select(TaskQueue)
                .where(TaskQueue.media_id == media_id)
                .order_by(TaskQueue.updated_at.desc(), TaskQueue.id.desc())
            )
        )
        .scalars()
        .all()
    )
    legacy_tq_rows = (
        (
            await session.execute(
                select(TransferQueue).where(TransferQueue.media_id == media_id)
            )
        )
        .scalars()
        .all()
    )
    tq_keys = {(r.media_id, r.episode) for r in tq_rows}
    transfer_rows = list(tq_rows) + [
        r for r in legacy_tq_rows if (r.media_id, r.episode) not in tq_keys
    ]
    transfer_rows.sort(key=lambda r: (r.updated_at or datetime.min, r.id), reverse=True)

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
        "episode_state": [
            _dq_episode_dto(r, is_admin, in_emby_codes, air_map)
            if isinstance(r, DownloadQueue) else _episode_dto(r, is_admin, in_emby_codes, air_map)
            for r in episode_rows
        ],
        "transfer_queue": [
            _task_queue_dto(r, is_admin) if isinstance(r, TaskQueue) else _tq_dto(r, is_admin)
            for r in transfer_rows
        ],
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

    # B-1（P1）：任务进行中不允许设 paused（防止下载完成后 media 状态无法归位、
    # scan 永久跳过）；paused 仅允许在无进行中任务的良性终态设置。恢复 tracking 不受限。
    if updates.get("status") == "paused" and await _has_in_progress_tasks(session, media_id):
        raise HTTPException(status_code=409, detail="存在进行中任务，无法暂停；请等待任务完成或先处理转存队列")

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

    删除顺序：episode_state → download_task → transfer_queue → download_queue →
    task_queue → media。
    - download_task.transfer_id 外键指向 transfer_queue.id，必须先删 download_task
      再删 transfer_queue，否则 SQLite(foreign_keys=ON)/PostgreSQL 会报外键冲突。
    - 两队列新表：download_queue.task_queue_id 外键指向 task_queue.id，必须先删
      download_queue 再删 task_queue（与旧表无 FK 关联，放现有删除之后）。

    B-2（P1）：检查+删除+commit 整体置于 scan 的 per-media 锁（_media_lock）内，
    与 scan 入队互斥，防「检查通过后、删除前 _enqueue 写入新子表记录」的孤儿数据竞态。
    """
    # B-2（P1）：在 scan 的 per-media 锁内完成「检查+删除+commit」——与 scan 入队
    # 互斥（scan_media 用同一 _media_lock 对象；删除后 scan 持锁时 media 已不存在
    # 会短路跳过），防「检查通过后被 scan 写入子表记录」的孤儿数据竞态。
    from app.tasks.scan import _media_lock  # noqa: PLC0415

    async with _media_lock(media_id):
        media = await session.get(Media, media_id)
        if media is None:
            raise HTTPException(status_code=404, detail="影视不存在")
        if await _has_in_progress_tasks(session, media_id):
            raise HTTPException(status_code=409, detail="存在进行中任务，无法删除")
        await session.execute(delete(EpisodeState).where(EpisodeState.media_id == media_id))
        await session.execute(delete(DownloadTask).where(DownloadTask.media_id == media_id))
        await session.execute(delete(TransferQueue).where(TransferQueue.media_id == media_id))
        # 两队列新表：download_queue.task_queue_id 外键指向 task_queue.id，
        # 必须先删 download_queue 再删 task_queue（与旧表无 FK 关联）。
        await session.execute(delete(DownloadQueue).where(DownloadQueue.media_id == media_id))
        await session.execute(delete(TaskQueue).where(TaskQueue.media_id == media_id))
        await session.delete(media)
        await session.commit()
        return {"ok": True}


@router.post("/media/{media_id}/scan")
async def scan_media_route(
    media_id: int,
    admin: User = Depends(get_current_admin),  # §9.1 写操作鉴权
) -> dict:
    """admin 手动触发一次媒体巡检（§4.3 搜索→入队）。

    E-1（P1）：fire-and-forget 立即返回，不再同步等待完整巡检（防 504）。
    响应 task_run_id=None（结果需前端轮询 /api/logs 获取；scan 完成会写
    task_run 记录）；scan 启动失败静默返回 ok 并告警（无副作用）。
    """
    try:
        from app.tasks.scan import trigger_scan_background  # 延迟导入，防 import 链断裂
    except ImportError:
        logger.warning("[media] trigger_scan_background 未就绪，跳过触发")
        return {"ok": True, "task_run_id": None}

    trigger_scan_background(media_id, manual=True)  # 手动触发：绕过静默期立即重试
    return {"ok": True, "task_run_id": None}