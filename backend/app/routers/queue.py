"""队列 API（影视下载两队列重设计 §8.2 控制面 + §8.1 树结构契约）。

- GET  /api/queue                       扁平任务列表（登录用户）：TaskQueue(活跃探测态)
                                         ∪ DownloadQueue(活跃执行态) 合一，每行
                                         {id, media_id, title, episode, status, node,
                                         file_name, file_size, updated_at, enqueued_at}；
                                         终态（tq done / dq done|skipped|failed）剔除；
                                         同集已有 DQ 活跃行时去重 task_queue 探测快照。
                                         ?type=download → 下载队列扁平列表（DownloadQueueItem[]，
                                         §8.1 下载队列 Tab：按准入顺序、支持仅看活跃/取消/排序）。
- POST /api/queue/download/pause|resume 整条下载队列暂停/恢复（system_config 开关，
                                         语义：暂停=不取新+在途继续）。
- GET  /api/queue/download/state        暂停状态 + 在途任务数（横幅文案数据源）。
- POST /api/queue/{id}/cancel           取消单任务：清 aria2 任务 + 删夸克残留 +
                                         status='failed'（reserved 由 DB 聚合自动释放）；
                                         task_queue 对应行同步 done。
- POST /api/queue/{id}/prioritize       置顶/优先（pending/quota_wait 的 enqueued_at 提前）。
- POST /api/queue/{id}/skip             跳过某集（写 DownloadQueue(status='skipped') 防重终态
                                         + TaskQueue done，防 scan 重新入队）。
- POST /api/queue/{id}/retry            重试（兼容 DownloadQueue failed/skipped 与
                                         TaskQueue error/unmatched；旧三表语义兼容回退）。
- POST /api/queue/{id}/promote          task_queue ready → 手动产出 download_queue(pending)。
- POST /api/queue/probe/{media_id}      手动触发单影视探测（scan_media 后台）。
- POST /api/queue/tasks                 手动加集（body {media_id, episode} → task_queue pending）。
- POST /api/queue/{id}/sort             调整 pending 顺序（body {direction: up|down|top}，
                                         enqueued_at 交换）。
- GET  /api/queue/download/progress     downloading 行实时进度（aria2.tellStatus 聚合；
                                         失败行降级返回 null 字段，局部轮询用）。

权限：除 GET /queue 树外均需 admin（§8.2）；网盘凭据（stoken/fids 等）一律不返回。
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DownloadQueue,
    EpisodeState,  # 旧三表兼容回退分支使用（只读归档）
    Media,
    SystemConfig,
    TaskQueue,
    User,
)
from app.routers.deps import get_current_admin, get_current_user, get_session
from app.services import alist, aria2
from app.tasks import as_bool

router = APIRouter()

logger = logging.getLogger(__name__)

# 下载队列在途状态（暂停横幅 in_flight / 取消释放 reserved / progress 轮询集合）
_IN_FLIGHT_STATUSES = ("transferring", "downloading", "scrape", "library")
# 全局暂停开关 system_config 键（§8.2：暂停=不取新+在途继续）
_PAUSE_CONFIG_KEY = "download_queue_paused"

# 扁平显示列表活跃态集合（终态剔除：tq done / dq done|skipped|failed 一律不返回）
_TQ_ACTIVE = ("pending", "probing", "ready", "unmatched", "error")
_DQ_ACTIVE = ("pending", "transferring", "downloading", "scrape", "library", "quota_wait")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


# 兼容旧模块名（其他 lane / capacity.py 仍可能按此名导入该 helper）
def _load_transfer_queue_model():
    try:
        from app.models.transfer_queue import TransferQueue
    except ImportError:
        try:
            from app.models import TransferQueue
        except ImportError:
            TransferQueue = None
    return TransferQueue


async def _list_flat(session: AsyncSession, limit: int, offset: int) -> list[dict]:
    """扁平任务列表（Task 9 契约）：TaskQueue(活跃探测态) ∪ DownloadQueue(活跃执行态)
    合一，join Media 取 title，终态（tq done / dq done|skipped|failed）剔除；同
    (media_id, episode) 已有 DQ 活跃行时不展示 task_queue 探测快照（promote 遗留快照
    去重，延续树视图 Playwright 修复）。跨表按 updated_at 倒序 + id 倒序决胜，
    limit/offset 分页作用于扁平行切片。
    """
    dq_rows = (
        (
            await session.execute(
                select(DownloadQueue)
                .where(DownloadQueue.status.in_(_DQ_ACTIVE))
                .order_by(DownloadQueue.updated_at.desc(), DownloadQueue.id.desc())
            )
        )
        .scalars()
        .all()
    )
    tq_rows = (
        (
            await session.execute(
                select(TaskQueue)
                .where(TaskQueue.status.in_(_TQ_ACTIVE))
                .order_by(TaskQueue.updated_at.desc(), TaskQueue.id.desc())
            )
        )
        .scalars()
        .all()
    )

    # media 一次性批量查询（避免 N+1）；行内 title 缺失（media 已删）降级 None
    media_ids = {r.media_id for r in dq_rows} | {r.media_id for r in tq_rows}
    media_map: dict[int, Media] = {}
    if media_ids:
        media_map = {
            m.id: m for m in (
                await session.execute(select(Media).where(Media.id.in_(media_ids)))
            ).scalars().all()
        }

    # 去重：该集已有 DQ 活跃行（执行视图）→ 不重复展示 task_queue 探测快照
    dq_key_set = {(dq.media_id, dq.episode) for dq in dq_rows}

    rows: list[dict] = []
    for tq in tq_rows:
        if (tq.media_id, tq.episode) in dq_key_set:
            continue
        media = media_map.get(tq.media_id)
        rows.append({
            "id": tq.id,
            "media_id": tq.media_id,
            "title": media.title if media is not None else None,
            "episode": tq.episode,
            "status": tq.status,
            "node": None,  # 探测视图无节点概念
            "file_name": tq.file_name,
            "file_size": tq.file_size,
            "updated_at": _iso(tq.updated_at),
            "enqueued_at": _iso(tq.created_at),
        })
    for dq in dq_rows:
        media = media_map.get(dq.media_id)
        rows.append({
            "id": dq.id,
            "media_id": dq.media_id,
            "title": media.title if media is not None else None,
            "episode": dq.episode,
            "status": dq.status,
            "node": dq.status,  # 执行视图：node=status（与树子节点同口径）
            "file_name": dq.file_name,
            "file_size": dq.file_size,
            "updated_at": _iso(dq.updated_at),
            "enqueued_at": _iso(dq.enqueued_at),
        })

    # 跨表合并排序：最新 updated_at 倒序（空值置后），id 倒序决胜
    rows.sort(key=lambda r: (r["updated_at"] or "", r["id"]), reverse=True)
    return rows[offset: offset + limit]


async def _list_download(
    session: AsyncSession, limit: int, offset: int,
) -> list[dict]:
    """下载队列扁平列表（§8.1，?type=download）：按准入顺序（enqueued_at 倒序）。"""
    rows = (
        await session.execute(
            select(DownloadQueue, Media)
            .outerjoin(Media, Media.id == DownloadQueue.media_id)
            .order_by(DownloadQueue.enqueued_at.desc(), DownloadQueue.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    result = []
    for dq, media in rows:
        result.append({
            "id": dq.id,
            "media_id": dq.media_id,
            "media_title": media.title if media is not None else None,
            "episode": dq.episode,
            "file_name": dq.file_name,
            "file_size": dq.file_size,
            "share_code": dq.share_code,
            "status": dq.status,
            "node_attempt": dq.node_attempt,
            "node_error": dq.node_error,
            "retry_count": dq.retry_count,
            "quota_hint": dq.error,  # quota_wait 排队原因（后端直出，前端可读）
            "aria2_gid": dq.aria2_gid,
            "enqueued_at": _iso(dq.enqueued_at),
            "updated_at": _iso(dq.updated_at),
        })
    return result


@router.get("/queue")
async def list_queue(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    type: Annotated[Optional[str], Query()] = None,
) -> list[dict]:
    """扁平任务列表（Task 9 契约：两队列活跃行合一 + 终态剔除）或 ?type=download 下载队列扁平列表。"""
    if type == "download":
        return await _list_download(session, limit, offset)
    return await _list_flat(session, limit, offset)


# ---------------------------------------------------------------------------
# 整条下载队列暂停 / 恢复 / 状态
# ---------------------------------------------------------------------------

async def _set_pause(value: bool, session: AsyncSession) -> bool:
    now = _now()
    await session.merge(SystemConfig(key=_PAUSE_CONFIG_KEY, value="true" if value else "false",
                                     updated_at=now))
    await session.commit()
    return value


@router.post("/queue/download/pause")
async def pause_download_queue(
    admin: User = Depends(get_current_admin),  # noqa: B008  §9.1 写操作鉴权
    session: AsyncSession = Depends(get_session),
) -> dict:
    """暂停整条下载队列（§8.2：暂停=不取新+在途继续；不调 aria2.pause）。"""
    return {"paused": await _set_pause(True, session)}


@router.post("/queue/download/resume")
async def resume_download_queue(
    admin: User = Depends(get_current_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),
) -> dict:
    """恢复整条下载队列。"""
    return {"paused": await _set_pause(False, session)}


@router.get("/queue/download/state")
async def download_queue_state(
    admin: User = Depends(get_current_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),
) -> dict:
    """暂停状态 + 在途任务数（§8.1 横幅「队列已暂停，在途 n 个继续完成」）。"""
    row = await session.get(SystemConfig, _PAUSE_CONFIG_KEY)
    paused = as_bool(row.value) if row is not None else False
    in_flight = (
        await session.scalar(
            select(func.count())
            .select_from(DownloadQueue)
            .where(DownloadQueue.status.in_(_IN_FLIGHT_STATUSES))
        )
    ) or 0
    return {"paused": paused, "in_flight": in_flight}


# ---------------------------------------------------------------------------
# 单任务控制面
# ---------------------------------------------------------------------------

async def _cleanup_cancel_side_effects(dq: DownloadQueue) -> None:
    """取消任务的事务提交后 best-effort 清理副作用（B-3；失败仅记录不阻断）。"""
    if dq.aria2_gid:
        try:
            await aria2.client.remove(dq.aria2_gid)
        except Exception as exc:  # noqa: BLE001  已失效/已 complete 时静默跳过（§8.2 幂等兜底）
            logger.warning("[queue] cancel 移除 aria2 任务失败 %s: %s", dq.aria2_gid, exc)
    if dq.quark_path:
        try:
            path = (dq.quark_path or "").strip().rstrip("/")
            if "/" in path:
                dir_part, name = path.rsplit("/", 1)
                await alist.remove([name], (dir_part or "/") + "/")
            elif path:
                await alist.remove([path], "/")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[queue] cancel 删除夸克残留失败 %s: %s", dq.quark_path, exc)


async def _trigger_consume() -> None:
    """重试/手动入队成功后触发下载队列消费（事件触发，失败仅告警不阻断）。"""
    try:
        from app.tasks.transfer import trigger_transfer

        await trigger_transfer()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[queue] 触发下载队列消费失败（不阻断）: %s", exc)


@router.post("/queue/{task_id}/cancel")
async def cancel_task(
    task_id: int,
    admin: User = Depends(get_current_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),
) -> dict:
    """取消单任务（§8.2，不可逆）：DownloadQueue 进行中/排队 → status='failed' +
    error='人工取消'；事务提交后清 aria2 任务 + 删夸克残留（reserved 由 DB 聚合
    自动释放）；task_queue 对应行同步 done（探测视图不再入队）。task_id 也可指
    task_queue 行（探测视图取消 → done，不再探测）。"""
    now = _now()
    dq = await session.get(DownloadQueue, task_id)
    if dq is not None:
        if dq.status in ("done", "skipped", "failed"):
            raise HTTPException(status_code=409, detail="终态任务不可取消")
        err = "人工取消"
        r = await session.execute(
            update(DownloadQueue)
            .where(DownloadQueue.id == dq.id, DownloadQueue.status == dq.status)  # 快照门控
            .values(status="failed", node_error=err, error=err,
                    node_finished_at=now, updated_at=now)
        )
        if r.rowcount == 0:
            raise HTTPException(status_code=409, detail="任务状态已变化，请刷新后重试")
        await session.execute(
            update(TaskQueue)
            .where(TaskQueue.media_id == dq.media_id, TaskQueue.episode == dq.episode)
            .values(status="done", updated_at=now)
        )
        await session.commit()
        await _cleanup_cancel_side_effects(dq)
        return {"ok": True}

    # 探测视图取消：task_queue → done（不再参与探测），防重不涉及
    tq = await session.get(TaskQueue, task_id)
    if tq is None:
        raise HTTPException(status_code=404, detail="队列任务不存在")
    await session.execute(
        update(TaskQueue).where(TaskQueue.id == tq.id).values(status="done", updated_at=now)
    )
    await session.commit()
    return {"ok": True}


@router.post("/queue/{task_id}/prioritize")
async def prioritize_task(
    task_id: int,
    admin: User = Depends(get_current_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),
) -> dict:
    """置顶/优先（§8.2）：pending/quota_wait 的 enqueued_at 提前至同队列最小值的
    「-1s」（SQLite CURRENT_TIMESTAMP 秒级精度，与最小并列时再逐秒前移保证排到最前）。"""
    now = _now()
    dq = await session.get(DownloadQueue, task_id)
    if dq is not None:
        if dq.status not in ("pending", "quota_wait"):
            raise HTTPException(status_code=409, detail="仅 pending/quota_wait 任务可置顶")
        candidates = ("pending", "quota_wait")
    else:
        # 探测视图置顶：task_queue 无独立顺序列，用 created_at 前移（探测顺序不敏感，
        # 兼容前端 tree 对 pending 子行的置顶按钮）
        tq = await session.get(TaskQueue, task_id)
        if tq is None:
            raise HTTPException(status_code=404, detail="队列任务不存在")
        min_ts = await session.scalar(
            select(func.min(TaskQueue.created_at)).where(
                TaskQueue.created_at.is_not(None),
            )
        )
        new_ts = (min_ts or now) - timedelta(seconds=1)
        await session.execute(
            update(TaskQueue).where(TaskQueue.id == tq.id).values(created_at=new_ts, updated_at=now)
        )
        await session.commit()
        return {"ok": True}

    min_ts = await session.scalar(
        select(func.min(DownloadQueue.enqueued_at)).where(
            DownloadQueue.status.in_(candidates),
            DownloadQueue.enqueued_at.is_not(None),
        )
    )
    new_ts = (min_ts or now) - timedelta(seconds=1)
    while (await session.scalar(
        select(func.count())
        .select_from(DownloadQueue)
        .where(DownloadQueue.enqueued_at == new_ts, DownloadQueue.id != dq.id)
    )) or 0:
        new_ts -= timedelta(seconds=1)
    r = await session.execute(
        update(DownloadQueue)
        .where(DownloadQueue.id == dq.id, DownloadQueue.status == dq.status)
        .values(enqueued_at=new_ts, updated_at=now)
    )
    if r.rowcount == 0:
        raise HTTPException(status_code=409, detail="任务状态已变化，请刷新后重试")
    await session.commit()
    return {"ok": True}


@router.post("/queue/{task_id}/skip")
async def skip_task(
    task_id: int,
    admin: User = Depends(get_current_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),
) -> dict:
    """跳过某集（§8.2）：写 DownloadQueue(status='skipped') 作防重终态 + TaskQueue done，
    防 scan 重新入队。task_id 可为 DownloadQueue 行（当前任务跳过）或 TaskQueue 行
    （探测视图 skipped → 无 dq 时按快照补写防重终态，兜底 NOT NULL 字段）。"""
    now = _now()
    dq = await session.get(DownloadQueue, task_id)
    if dq is not None:
        if dq.status in ("done", "skipped", "failed"):
            raise HTTPException(status_code=409, detail="终态任务不可跳过")
        r = await session.execute(
            update(DownloadQueue)
            .where(DownloadQueue.id == dq.id, DownloadQueue.status == dq.status)
            .values(status="skipped", node_error=None, error=None,
                    node_finished_at=now, updated_at=now)
        )
        if r.rowcount == 0:
            raise HTTPException(status_code=409, detail="任务状态已变化，请刷新后重试")
        await session.execute(
            update(TaskQueue)
            .where(TaskQueue.media_id == dq.media_id, TaskQueue.episode == dq.episode)
            .values(status="done", updated_at=now)
        )
        await session.commit()
        return {"ok": True}

    tq = await session.get(TaskQueue, task_id)
    if tq is None:
        raise HTTPException(status_code=404, detail="队列任务不存在")
    # 探测视图跳过：同 (media, episode) 已有 dq（任意状态）→ 直接置 tq done；
    # 无 dq 但已探测到快照（ready / 有 file_name/fids）→ 补写 skipped 防重终态
    # （防 scan 重新入队）；无 dq 且未探测（pending/probing，无快照）→ 仅置 tq
    # done（放弃当前探测，scan 下次仍可重新探测——议会验证 P1：勿补 0 字节任务）
    has = (
        await session.execute(
            select(DownloadQueue.id).where(
                DownloadQueue.media_id == tq.media_id,
                DownloadQueue.episode == tq.episode,
            )
        )
    ).first()
    has_probe_snapshot = bool(
        (tq.file_size or 0) > 0 or (tq.fids or None) or tq.status == "ready"
    )
    if has is None and has_probe_snapshot:
        session.add(DownloadQueue(
            media_id=tq.media_id, episode=tq.episode,
            file_name=tq.file_name or "", file_size=tq.file_size or 0,
            share_code=tq.share_code or "",
            pwd_id=tq.pwd_id, stoken=tq.stoken, receive_code=tq.receive_code,
            fids=tq.fids, fid_tokens=tq.fid_tokens, folder_id=tq.folder_id,
            status="skipped", error="人工跳过",
            enqueued_at=now, updated_at=now,
        ))
    await session.execute(
        update(TaskQueue).where(TaskQueue.id == tq.id).values(status="done", updated_at=now)
    )
    await session.commit()
    return {"ok": True}


@router.post("/queue/{task_id}/retry")
async def retry_task(
    task_id: int,
    admin: User = Depends(get_current_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),
) -> dict:
    """admin 人工重试（§8.2）：兼容 DownloadQueue（failed/skipped → pending，重置
    retry_count=0/node_attempt=0/save_task_id=None）与 TaskQueue（error/unmatched →
    pending，重置 probe_attempt/silent_until）。旧三表（episode_state/transfer_queue）
    语义保留为兼容回退（旧前端/旧测试调用按 transfer_queue.id 或 episode_state.id）。
    条件更新防并发；commit 后触发下载队列消费，失败仅告警不阻断。"""
    now = _now()
    # 1) 执行视图：DownloadQueue failed/skipped → pending
    dq = await session.get(DownloadQueue, task_id)
    if dq is not None:
        if dq.status not in ("failed", "skipped"):
            raise HTTPException(status_code=409, detail="仅 failed/skipped 任务可重试")
        r = await session.execute(
            update(DownloadQueue)
            .where(DownloadQueue.id == dq.id, DownloadQueue.status == dq.status)
            .values(
                status="pending",
                retry_count=0,
                node_attempt=0,
                node_error=None,
                node_started_at=None,
                node_finished_at=None,
                save_task_id=None,   # 防「已受理未落盘」盲目幂等
                save_attempt_at=None,
                error=None,
                enqueued_at=now,
                updated_at=now,
            )
        )
        if r.rowcount == 0:
            raise HTTPException(status_code=409, detail="任务状态已变化，请稍后重试")
        await session.commit()
        await _trigger_consume()
        return {"ok": True}

    # 2) 探测视图：TaskQueue error/unmatched → pending（强制重新探测）
    tq = await session.get(TaskQueue, task_id)
    if tq is not None:
        if tq.status not in ("error", "unmatched"):
            raise HTTPException(status_code=409, detail="仅 error/unmatched 探测任务可重试")
        r = await session.execute(
            update(TaskQueue)
            .where(TaskQueue.id == tq.id, TaskQueue.status == tq.status)
            .values(status="pending", probe_attempt=0, error=None, silent_until=None,
                    updated_at=now)
        )
        if r.rowcount == 0:
            raise HTTPException(status_code=409, detail="任务状态已变化，请稍后重试")
        await session.commit()
        return {"ok": True}

    # 3) 兼容回退：旧三表语义（task_id 可为 episode_state.id 或 transfer_queue.id）
    logger.info("[queue] retry task_id=%s 未命中 DownloadQueue/TaskQueue，回退旧三表语义", task_id)
    es = await session.get(EpisodeState, task_id)
    tq_old = None
    if es is None:
        _TQ = _load_transfer_queue_model()
        if _TQ is None:
            raise HTTPException(status_code=404, detail="队列任务不存在")
        tq_old = await session.get(_TQ, task_id)
        if tq_old is None:
            raise HTTPException(status_code=404, detail="队列任务不存在")
        es = (
            await session.execute(
                select(EpisodeState).where(
                    EpisodeState.media_id == tq_old.media_id,
                    EpisodeState.episode == tq_old.episode,
                )
            )
        ).scalars().first()
        if es is None:
            raise HTTPException(status_code=404, detail="队列任务不存在或状态不允许重试")
    else:
        _TQ = _load_transfer_queue_model()
        if _TQ is not None:
            tq_old = (
                await session.execute(
                    select(_TQ).where(
                        _TQ.media_id == es.media_id,
                        _TQ.episode == es.episode,
                    )
                )
            ).scalars().first()

    if es.node != "failed" and es.state != "failed":
        raise HTTPException(status_code=409,
                            detail="episode_state 状态不一致，任务不可重试（仅 failed 状态可人工重试）")
    es_result = await session.execute(
        update(EpisodeState)
        .where(
            EpisodeState.id == es.id,
            or_(EpisodeState.node == "failed", EpisodeState.state == "failed"),
        )
        .values(
            node="idle", node_attempt=0, node_error=None,
            node_started_at=None, node_finished_at=None,
            state="queued", retry_count=0, error=None, updated_at=now,
        )
    )
    if es_result.rowcount == 0:
        raise HTTPException(status_code=409, detail="episode_state 状态不一致，请稍后重试")
    if tq_old is not None:
        await session.execute(
            update(_TQ)
            .where(_TQ.id == tq_old.id, _TQ.status.in_(("failed", "done")))
            .values(status="pending", quota_reject_count=0, error=None,
                    save_task_id=None, save_attempt_at=None, updated_at=now)
        )
    await session.commit()
    await _trigger_consume()
    return {"ok": True}


@router.post("/queue/{task_id}/promote")
async def promote_task(
    task_id: int,
    admin: User = Depends(get_current_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),
) -> dict:
    """手动入队（§8.2 前端契约）：task_queue(ready) → 手动产出 download_queue(pending)。
    防重：同 (media, episode) 已有 dq → 409。download_name 按 media.title 格式化（复用
    transfer._format_download_name，失败回退 None 不影响入队）。"""
    from app.tasks.transfer import _format_download_name  # noqa: PLC0415 延迟导入

    tq = await session.get(TaskQueue, task_id)
    if tq is None:
        raise HTTPException(status_code=404, detail="探测任务不存在")
    if tq.status != "ready":
        raise HTTPException(status_code=409, detail="仅 ready 探测任务可手动入队")
    has = (
        await session.execute(
            select(DownloadQueue.id).where(
                DownloadQueue.media_id == tq.media_id,
                DownloadQueue.episode == tq.episode,
            )
        )
    ).first()
    if has:
        raise HTTPException(status_code=409, detail="该集已在下载队列中")

    # download_name（aria2 落盘名，§7）按 media.title/media_type 生成；失败回退 None
    download_name = None
    try:
        media = await session.get(Media, tq.media_id)
        if media is not None and media.title:
            # episode_key 兜底：纯数字分享文件名（190.mkv）也规范化为
            # 「剧名 - S01E190 - 第 190 集.mkv」（对齐 n8n 下载落盘带集号标识）
            download_name = _format_download_name(
                tq.file_name or "", media.title, media.media_type, episode_key=tq.episode
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[queue] promote download_name 生成失败（回退 None）: %s", exc)

    now = _now()
    session.add(DownloadQueue(
        media_id=tq.media_id, episode=tq.episode, task_queue_id=tq.id,
        file_name=tq.file_name or "", file_size=tq.file_size or 0,
        share_code=tq.share_code or "",
        pwd_id=tq.pwd_id, stoken=tq.stoken, receive_code=tq.receive_code,
        fids=tq.fids, fid_tokens=tq.fid_tokens, folder_id=tq.folder_id,
        download_name=download_name,
        status="pending", enqueued_at=now, updated_at=now,
    ))
    await session.execute(
        update(TaskQueue).where(TaskQueue.id == tq.id).values(status="done", updated_at=now)
    )
    await session.commit()
    await _trigger_consume()
    return {"ok": True}


@router.post("/queue/probe/{media_id}")
async def probe_media(
    media_id: int,
    admin: User = Depends(get_current_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),
) -> dict:
    """手动触发单影视探测（§8.2）：scan_media 后台 fire-and-forget（该 media 的
    task_queue 由 scan 补集入队并探测）。"""
    media = await session.get(Media, media_id)
    if media is None:
        raise HTTPException(status_code=404, detail="影视不存在")
    try:
        from app.tasks.scan import trigger_scan_background  # noqa: PLC0415 延迟导入

        trigger_scan_background(media_id, manual=True)  # 手动探测：绕过静默期
    except Exception as exc:  # noqa: BLE001
        logger.warning("[queue] probe media=%s 触发失败: %s", media_id, exc)
        raise HTTPException(status_code=500, detail="探测触发失败，请稍后重试") from exc
    return {"ok": True}


class _AddQueueTaskBody(BaseModel):
    media_id: int
    episode: str


@router.post("/queue/tasks")
async def add_queue_task(
    body: _AddQueueTaskBody,
    admin: User = Depends(get_current_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),
) -> dict:
    """手动加集（§8.2）：body {media_id, episode} → 写 task_queue(pending) 触发探测；
    已存在（任意状态）→ 重置 pending 重新探测。随后触发该 media 后台巡检消费。"""
    media = await session.get(Media, body.media_id)
    if media is None:
        raise HTTPException(status_code=404, detail="影视不存在")
    episode = (body.episode or "").strip()
    if not episode:
        raise HTTPException(status_code=422, detail="episode 不能为空")
    now = _now()
    existing = (
        await session.execute(
            select(TaskQueue).where(
                TaskQueue.media_id == body.media_id,
                TaskQueue.episode == episode,
            )
        )
    ).scalars().first()
    if existing is not None:
        await session.execute(
            update(TaskQueue)
            .where(TaskQueue.id == existing.id)
            .values(status="pending", probe_attempt=0, error=None, silent_until=None,
                    updated_at=now)
        )
    else:
        session.add(TaskQueue(
            media_id=body.media_id, episode=episode, status="pending",
            probe_attempt=0, created_at=now, updated_at=now,
        ))
    await session.commit()
    # 触发探测（scan_media 后台；task_queue pending 由巡检消费，防积压）
    try:
        from app.tasks.scan import trigger_scan_background  # noqa: PLC0415 延迟导入

        trigger_scan_background(body.media_id, manual=True)  # 手动加集：绕过静默期
    except Exception as exc:  # noqa: BLE001
        logger.warning("[queue] 加集后触发探测失败（task_queue 已入队，巡检兜底）: %s", exc)
    return {"ok": True}


class _SortBody(BaseModel):
    direction: Literal["up", "down", "top"] = "top"


@router.post("/queue/{task_id}/sort")
async def sort_task(
    task_id: int,
    body: _SortBody,
    admin: User = Depends(get_current_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),
) -> dict:
    """排序（§8.2）：调整 pending 准入顺序（enqueued_at 交换）。同一 media 内 pending
    按 (enqueued_at, id) 升序排队；direction=up/down 与相邻行交换、top 置队首。
    quota_wait 不参与本排序（其准入由容量释放 + 优先级决定）。"""
    dq = await session.get(DownloadQueue, task_id)
    if dq is None:
        raise HTTPException(status_code=404, detail="队列任务不存在")
    if dq.status != "pending":
        raise HTTPException(status_code=409, detail="仅 pending 任务可排序")
    pending = (
        (
            await session.execute(
                select(DownloadQueue)
                .where(
                    DownloadQueue.media_id == dq.media_id,
                    DownloadQueue.status == "pending",
                )
                .order_by(DownloadQueue.enqueued_at.asc(), DownloadQueue.id.asc())
            )
        )
        .scalars()
        .all()
    )
    idx = next((i for i, r in enumerate(pending) if r.id == dq.id), None)
    if idx is None:
        raise HTTPException(status_code=409, detail="任务状态已变化，请刷新后重试")
    now = _now()
    if body.direction == "top":
        # 置顶（前端 top 语义 = 移到队首、其余顺延，非与队首交换）：enqueued_at
        # 前移至同 media pending 最小值 -1s（与 prioritize 同款；秒级精度冲突时逐秒前移）
        min_ts = await session.scalar(
            select(func.min(DownloadQueue.enqueued_at)).where(
                DownloadQueue.media_id == dq.media_id,
                DownloadQueue.status == "pending",
                DownloadQueue.enqueued_at.is_not(None),
            )
        )
        new_ts = (min_ts or now) - timedelta(seconds=1)
        while (await session.scalar(
            select(func.count())
            .select_from(DownloadQueue)
            .where(DownloadQueue.enqueued_at == new_ts, DownloadQueue.id != dq.id)
        )) or 0:
            new_ts -= timedelta(seconds=1)
        r = await session.execute(
            update(DownloadQueue)
            .where(DownloadQueue.id == dq.id, DownloadQueue.status == dq.status)
            .values(enqueued_at=new_ts, updated_at=now)
        )
        if r.rowcount == 0:
            raise HTTPException(status_code=409, detail="任务状态已变化，请刷新后重试")
        await session.commit()
        return {"ok": True}
    # up/down：与相邻行交换 enqueued_at（两行互换，无唯一约束；重复值由 id 排序兜底）
    target = max(0, idx - 1) if body.direction == "up" else min(len(pending) - 1, idx + 1)
    if target == idx:
        return {"ok": True}
    a, b = pending[idx], pending[target]
    ts_a, ts_b = a.enqueued_at, b.enqueued_at
    await session.execute(
        update(DownloadQueue).where(DownloadQueue.id == a.id).values(enqueued_at=ts_b, updated_at=now)
    )
    await session.execute(
        update(DownloadQueue).where(DownloadQueue.id == b.id).values(enqueued_at=ts_a, updated_at=now)
    )
    await session.commit()
    return {"ok": True}


@router.get("/queue/download/progress")
async def download_progress(
    admin: User = Depends(get_current_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),
    gid: Annotated[Optional[str], Query()] = None,
) -> list[dict]:
    """downloading 行实时进度（§8.2，2-3s 局部轮询）：tellStatus 聚合
    [{id, gid, speed, progress, download_name}]；aria2 故障/字段缺失 → 降级 null 字段。"""
    if gid:
        dq_rows = (
            (
                await session.execute(
                    select(DownloadQueue).where(DownloadQueue.aria2_gid == gid)
                )
            )
            .scalars()
            .all()
        )
    else:
        dq_rows = (
            (
                await session.execute(
                    select(DownloadQueue).where(DownloadQueue.status == "downloading")
                )
            )
            .scalars()
            .all()
        )
    result: list[dict] = []
    for dq in dq_rows:
        if not dq.aria2_gid:
            continue
        entry: dict = {
            "id": dq.id,
            "gid": dq.aria2_gid,
            "speed": None,
            "progress": None,
            "download_name": dq.download_name,
        }
        try:
            st = await aria2.client.tell_status(dq.aria2_gid)
            total = int(st.get("totalLength") or 0)
            done = int(st.get("completedLength") or 0)
            if total > 0:
                entry["progress"] = round(done / total * 100, 1)
            speed = st.get("downloadSpeed")
            if speed is not None:
                entry["speed"] = int(speed)
        except Exception as exc:  # noqa: BLE001  失败行降级返回 null 字段
            logger.warning("[queue] progress tell_status 失败（降级 null 字段）: %s", exc)
        result.append(entry)
    return result