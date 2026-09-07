"""队列 API（L8 影视任务树）：JWT 鉴权 + 树结构 + 人工重试。

- GET  /api/queue           影视任务树列表（登录用户）：
                              父级 = 影视任务（按 media 分组，aggregate_status 聚合
                              all_done/partial_failed/running/waiting，
                              total_count / done_count），
                              子级 = 分集五节点状态机（node/node_attempt/node_error）。
                              §9.1 网盘凭据（stoken/fids/share_code 等）一律不返回，
                              guest/admin 同构。
- POST /api/queue/{id}/retry admin 人工重试 failed 子任务：
                              task_id 优先为 episode_state.id（新树结构子任务 id），
                              兼容旧扁平调用按 transfer_queue.id 传入；
                              仅 node='failed'（或旧数据 state='failed'）可重试；
                              重置节点字段 + 双表联动回 pending/queued +
                              清空 transfer_queue 幂等标记（防盲等）；
                              非 failed → 409，不存在 → 404，条件更新防并发，
                              commit 后触发转存消费（失败仅告警不阻断）。
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EpisodeState, Media, TaskRun, TransferQueue, User
from app.routers.deps import get_current_admin, get_current_user, get_session

router = APIRouter()

logger = logging.getLogger(__name__)

# 父级聚合状态判定中的「进行中」节点集合（L8 oracles 决策）：idle 归「等待」。
_RUNNING_NODES = frozenset({"transfer", "download", "downloading", "scrape", "library"})


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt: datetime | None) -> str | None:
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


def _child_dto(es: EpisodeState) -> dict:
    """子任务 DTO（分集五节点视图，§9.1 白名单）：仅返回非敏感字段。"""
    return {
        "id": es.id,  # 重试接口 POST /queue/{id}/retry 用（episode_state.id）
        "episode": es.episode,
        "node": es.node,
        "node_attempt": es.node_attempt,
        "node_error": es.node_error,
        "file_name": es.file_name,
        "file_size": es.file_size,
        "updated_at": _iso(es.updated_at),
        "node_started_at": _iso(es.node_started_at),
        "node_finished_at": _iso(es.node_finished_at),
    }


def _scan_task_dto(run: TaskRun) -> dict:
    """父级挂载的最近巡检摘要（scan_tasks 元素，非敏感字段）。"""
    return {
        "id": run.id,
        "status": run.status,
        "message": run.message,
        "started_at": _iso(run.started_at),
        "duration_seconds": run.duration_seconds,
    }


def _aggregate_status(children: list[dict]) -> str:
    """父级聚合状态规则（L8 oracles 决策，按优先级依次判定）：
    - 空                                  → waiting
    - 全部子任务 node='done'              → all_done
    - 任一子任务 node='failed'            → partial_failed
    - 任一进行中（transfer/download/downloading/scrape/library）→ running
    - 其余（全部 idle / 未知节点）        → waiting
    （idle 视为「等待开始」计入 waiting；「进行中」集合不含 idle，
      否则「全部等待 → waiting」分支将永不命中。）
    """
    if not children:
        return "waiting"
    if all(c["node"] == "done" for c in children):
        return "all_done"
    if any(c["node"] == "failed" for c in children):
        return "partial_failed"
    if any(c["node"] in _RUNNING_NODES for c in children):
        return "running"
    return "waiting"


def _parent_dto(group: dict) -> dict:
    """父级 DTO（影视任务）：分组内子任务 + 聚合指标。"""
    children = group["children"]
    done_count = sum(1 for c in children if c["node"] == "done")
    return {
        "media_id": group["media_id"],
        "title": group["title"],
        "media_type": group["media_type"],
        "aggregate_status": _aggregate_status(children),
        "total_count": len(children),
        "done_count": done_count,
        "children": children,
    }


@router.get("/queue")
async def list_queue(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    """影视任务树列表（L8 树结构契约，替代旧扁平 QueueItem[]）。

    一次取回分页的 episode_state（LEFT JOIN media 取 title/media_type），按其
    media_id 分组为父级；孤儿 es（media 记录已不存在，FK 保护下少见）归入合成
    父级 media_id=null，title 取首个子任务 file_name（缺省「未关联影视」）。
    父级按子任务最近 updated_at 倒序，子任务内按 updated_at 倒序。

    巡检可见性改造：每个真实 media 父级附加 scan_tasks（该 media 最近巡检记录
    摘要，task_type='scan_media'）。一次批量查询（media_ids IN）取最近 1 条/影视，
    避免 N+1；孤儿父级（media 不存在）scan_tasks=[]。
    """
    rows = (
        await session.execute(
            select(EpisodeState, Media)
            .outerjoin(Media, Media.id == EpisodeState.media_id)
            .order_by(EpisodeState.updated_at.desc(), EpisodeState.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    # 按 media_id 分组（保序）；孤儿用统一哨兵 key 合成一个父级
    orphan_key: object = object()
    groups: dict[object, dict] = {}
    for es, media in rows:
        key = es.media_id if media is not None else orphan_key
        g = groups.get(key)
        if g is None:
            if media is not None:
                media_id, title, media_type = media.id, media.title, media.media_type
            else:
                media_id = None
                title = es.file_name or "未关联影视"
                media_type = None
            g = {
                "media_id": media_id,
                "title": title,
                "media_type": media_type,
                "children": [],
                # 组内最近 updated_at（父级排序用；es.updated_at 可能为 None → 兜底最小）
                "latest": es.updated_at or datetime.min,
            }
            groups[key] = g
        if es.updated_at and es.updated_at > g["latest"]:
            g["latest"] = es.updated_at
        g["children"].append(_child_dto(es))

    # 巡检可见性：批量取各真实 media 最近 1 条巡检记录（task_type='scan_media'），
    # 一次 IN 查询 + 按 (media_id, started_at) 分组取每组第一条（started_at 倒序），
    # 避免每父级一次查询的 N+1；孤儿父级（media 不存在）不在 media_ids 中 → 空列表。
    real_ids = [g["media_id"] for g in groups.values()
                if g["media_id"] is not None]  # 真实 media 父级（孤儿哨兵键无 media_id）
    scan_by_media: dict[int, list[dict]] = {}
    if real_ids:
        scan_rows = (
            await session.execute(
                select(TaskRun)
                .where(
                    TaskRun.task_type == "scan_media",
                    TaskRun.media_id.in_(real_ids),
                )
                .order_by(TaskRun.media_id.asc(), TaskRun.started_at.desc(), TaskRun.id.desc())
            )
        ).scalars().all()
        for run in scan_rows:
            if run.media_id is not None and run.media_id not in scan_by_media:
                scan_by_media[run.media_id] = [_scan_task_dto(run)]

    # 父级按子任务最近 updated_at 倒序
    ordered = sorted(groups.values(), key=lambda g: g["latest"], reverse=True)
    result: list[dict] = []
    for g in ordered:
        dto = _parent_dto(g)
        if isinstance(g["media_id"], int):
            dto["scan_tasks"] = scan_by_media.get(g["media_id"], [])
        else:
            dto["scan_tasks"] = []
        result.append(dto)
    return result


@router.post("/queue/{task_id}/retry")
async def retry_task(
    task_id: int,
    admin: User = Depends(get_current_admin),  # §9.1 写操作鉴权
    session: AsyncSession = Depends(get_session),
) -> dict:
    """admin 人工重试 failed 子任务（L8 节点语义，契约见五节点状态机）：

    - task_id 优先为 episode_state.id（树结构子任务 id，即子级返回的 id）；
      兼容旧扁平接口按 transfer_queue.id 调用（旧调用方/旧测试经
      (media_id, episode) 关联回 episode_state 后同等处理）。
    - 可重试：node='failed'（新契约）或旧数据 state='failed'；重置
      node='idle' / node_attempt=0 / node_error=None / state='queued' /
      retry_count=0 / error=None，并同步 transfer_queue failed/done → pending +
      清空 save_task_id/save_attempt_at（P0-1 防幂等盲等）。P1-3：scrape/library
      失败终态下 tq 已是 'done'（_complete_download 置的），故联动条件放宽为
      status IN ('failed','done')，以 es 失败定位守卫不误伤正常完成项。
    - 非 failed → 409；任务不存在 → 404。条件更新防并发（行数=0 → 409，
      未 commit 自动回滚保持原状）；commit 后延迟导入触发转存消费，
      失败仅告警不阻断。
    """
    tq = None
    # 1) 优先按 episode_state.id 定位（新语义）
    es = await session.get(EpisodeState, task_id)
    if es is None:
        # 2) 回退旧语义：task_id 指向 transfer_queue.id → 经双键关联回 es
        tq = await session.get(TransferQueue, task_id)
        if tq is None:
            raise HTTPException(status_code=404, detail="队列任务不存在")
        es = (
            await session.execute(
                select(EpisodeState).where(
                    EpisodeState.media_id == tq.media_id,
                    EpisodeState.episode == tq.episode,
                )
            )
        ).scalars().first()
        if es is None:
            raise HTTPException(status_code=404, detail="队列任务不存在或状态不允许重试")
    else:
        # 3) 新语义取对应 transfer_queue（若存在）以便联动清空幂等标记
        tq = (
            await session.execute(
                select(TransferQueue).where(
                    TransferQueue.media_id == es.media_id,
                    TransferQueue.episode == es.episode,
                )
            )
        ).scalars().first()

    # 4) 可重试判定：node='failed'（新语义）或 state='failed'（旧数据兼容）
    if es.node != "failed" and es.state != "failed":
        raise HTTPException(
            status_code=409,
            detail="episode_state 状态不一致，任务不可重试（仅 failed 状态可人工重试）",
        )

    # 5) 重置节点与执行流状态（WHERE 保留 failed 条件防并发，行数=0 → 409）
    now = _now()
    es_result = await session.execute(
        update(EpisodeState)
        .where(
            EpisodeState.id == es.id,
            or_(EpisodeState.node == "failed", EpisodeState.state == "failed"),
        )
        .values(
            node="idle",
            node_attempt=0,
            node_error=None,
            node_started_at=None,
            node_finished_at=None,
            state="queued",
            retry_count=0,
            error=None,
            updated_at=now,
        )
    )
    if es_result.rowcount == 0:
        raise HTTPException(status_code=409, detail="episode_state 状态不一致，请稍后重试")

    # 6) 双表联动（§3.1）：transfer_queue failed/done → pending + 清空幂等标记防盲等。
    #    P1-3：scrape/library 失败终态（es.node='failed'）下 tq.status 已是 'done'
    #    （_complete_download 置的），原 WHERE status='failed' 不命中 → es 已重置但
    #    tq 未联动 → _process_one_pending 取件（tq.pending）永不命中而卡死。放宽为
    #    IN ('failed','done')：es 已在上方按 node/state='failed' 定位（409 判定守卫），
    #    正常完成项（es.node='done'）不会走到此分支，不误伤。
    if tq is not None:
        await session.execute(
            update(TransferQueue)
            .where(TransferQueue.id == tq.id, TransferQueue.status.in_(("failed", "done")))
            .values(
                status="pending",
                quota_reject_count=0,
                error=None,
                save_task_id=None,
                save_attempt_at=None,
                updated_at=now,
            )
        )
    await session.commit()

    # 7) 重试成功后触发转存消费（延迟导入 + 兜底，与 scan 触发同模式；
    #    状态已改 pending/queued，触发后由队列消费续跑）
    try:
        from app.tasks.transfer import trigger_transfer

        await trigger_transfer()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[queue] retry 后触发转存消费失败（不阻断重试）: %s", exc)
    return {"ok": True}