"""
recover —— 超时任务恢复（设计文档 §4.1 / §3.1 / §4.5）。

- recover_on_boot()      启动恢复（lifespan startup 阶段：init_db 之后、scheduler.start 之前）
- recover_stale_tasks()  核心逻辑（P2-2 抽出）：运行期兜底（scheduler job recover_stale，
                         每 1h）与启动恢复共用同一实现

扫描 episode_state 中超过 episode_state_timeout_hours（默认 2h）无进展的
transferring/downloading 记录 → 回退 queued + retry_count++，并清理夸克残留
（调用 alist；服务未就绪/调用失败 try/except 包裹，记录 task_run，不阻塞回退）；
同步 transfer_queue 对应记录回退 pending。

幂等：回退后记录变为 queued，不再命中 transferring/downloading 条件，可重复执行。
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update

from app.config import settings
from app.database import async_session
from app.models import DownloadTask, EpisodeState, SystemConfig, TransferQueue
from app.services import alist, aria2  # noqa: F401  服务层由另一 lane 创建，集成时统一验证
from app.tasks import record_task_run

logger = logging.getLogger(__name__)

# 状态机进行中态（§3.1）：超时回退候选
_PROGRESS_STATES = ("transferring", "downloading")
# transfer_queue 中与进行中态对应的执行流状态
_TQ_PROGRESS_STATES = ("transferring", "downloading")

# system_config 中的超时阈值键（「配置双源统一」：system_config 优先，env 仅 fallback）
EPISODE_TIMEOUT_CONFIG_KEY = "episode_state_timeout_hours"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _load_timeout_hours() -> float:
    """读取超时阈值（system_config episode_state_timeout_hours，缺失/非法/异常 → env fallback）。

    settings.py PATCH 白名单可写该键，运行时此处读 system_config 使配置生效。
    """
    default = settings.EPISODE_STATE_TIMEOUT_HOURS
    try:
        async with async_session() as session:
            row = await session.get(SystemConfig, EPISODE_TIMEOUT_CONFIG_KEY)
    except Exception as exc:
        logger.warning("读取 %s 失败，用默认超时 %.1fh: %s",
                       EPISODE_TIMEOUT_CONFIG_KEY, default, exc)
        return default
    if row is None or not row.value:
        return default
    try:
        return float(row.value)
    except (TypeError, ValueError):
        logger.warning("%s 非数值 %r，用默认超时 %.1fh",
                       EPISODE_TIMEOUT_CONFIG_KEY, row.value, default)
        return default


def _split_quark_path(path: str) -> tuple[str, list[str]]:
    """把夸克完整路径拆为 (dir, [name])，适配 alist.remove(names, dir) 契约。"""
    path = (path or "").strip()
    if not path:
        return "/", []
    path = path.rstrip("/")
    if "/" in path:
        dir_part, name = path.rsplit("/", 1)
        return (dir_part or "/") + "/", [name]
    return "/", [path]


async def _cleanup_quark(quark_path: str) -> None:
    """清理夸克残留（设计文档 §4.5：重试前清理残留）。

    抛出的任何异常（含 alist 未就绪）由调用方 try/except 兜底，不阻塞回退。
    """
    dir_part, names = _split_quark_path(quark_path)
    if names:
        await alist.remove(names, dir_part)


async def recover_stale_tasks() -> int:
    """超时未完成 transferring/downloading → 回退 queued + retry_count++。

    两阶段法（避免事务内网络 IO 长事务，council P2）：
      阶段① 事务内只查超时记录（快照），显式覆盖 NULL updated_at（council P5）
      阶段② 事务外逐条清理夸克残留（网络 IO，失败仅记录 task_run 不阻塞）
      阶段③ 新事务批量回退状态 + 双表联动
    幂等可重复执行（回退后不再命中进行中态条件）。返回回退条数。
    """
    timeout_hours = await _load_timeout_hours()
    cutoff = _now() - timedelta(hours=timeout_hours)

    # 阶段①：只读快照查询（NULL updated_at 显式覆盖，P5）
    async with async_session() as session:
        rows = (
            (
                await session.execute(
                    select(EpisodeState).where(
                        EpisodeState.state.in_(_PROGRESS_STATES),
                        or_(
                            EpisodeState.updated_at < cutoff,
                            EpisodeState.updated_at.is_(None),
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )

    if not rows:
        logger.info("[recover] 无超时任务")
        return 0

    # 阶段②：事务外清理夸克残留 + 尝试移除 aria2 任务（网络 IO 不放事务内；
    #          aria2 移除失败仅 warning——不必要的外呼失败不得阻断回退，P0-3a）
    cleaned = 0
    cleanup_failures: list[str] = []
    for row in rows:
        if row.aria2_gid:
            try:
                await aria2.client.remove(row.aria2_gid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[recover] aria2 移除失败 %s: %s", row.aria2_gid, exc)
        if not row.quark_path:
            continue
        try:
            await _cleanup_quark(row.quark_path)
            cleaned += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("[recover] 清理夸克残留失败 %s: %s", row.quark_path, exc)
            cleanup_failures.append(f"{row.episode}: {exc}")

    # 阶段③：新事务批量回退状态 + 双表联动（bulk update——rows 为上一 session 快照，
    # detached 对象赋属性不落库，必须用 execute(update) 按 id 批量更新）
    now = _now()
    error = f"超时回退（{timeout_hours}h 无进展）"
    async with async_session() as session:
        async with session.begin():
            await session.execute(
                update(EpisodeState)
                .where(
                    EpisodeState.state.in_(_PROGRESS_STATES),
                    EpisodeState.id.in_([r.id for r in rows]),
                )
                .values(
                    state="queued",
                    retry_count=EpisodeState.retry_count + 1,
                    error=error,
                    updated_at=now,
                )
            )
            for row in rows:
                await session.execute(
                    update(TransferQueue)
                    .where(
                        TransferQueue.media_id == row.media_id,
                        TransferQueue.episode == row.episode,
                        TransferQueue.status.in_(_TQ_PROGRESS_STATES),
                    )
                    .values(status="pending", error=error, updated_at=now)
                )
                # P0-3a（council）：同步终结进行中的 download_task（防阶段 A 轮询
                # 重复计数/虚假完成）；DownloadTask 无 updated_at/error 列，仅置终态
                await session.execute(
                    update(DownloadTask)
                    .where(
                        DownloadTask.media_id == row.media_id,
                        DownloadTask.episode == row.episode,
                        DownloadTask.status == "downloading",
                    )
                    .values(status="failed")
                )

            message = f"恢复 {len(rows)} 条超时任务（回退 queued，清理残留 {cleaned}）"
            if cleanup_failures:
                message += f"；清理失败 {len(cleanup_failures)} 条: {'; '.join(cleanup_failures)}"
            await record_task_run(session, "recover", "success", message, None)

    logger.info(
        "[recover] 恢复完成：回退 %d 条超时任务（清理残留 %d 条）",
        len(rows), cleaned,
    )
    return len(rows)


async def recover_on_boot() -> None:
    """启动恢复：lifespan startup 阶段调用核心逻辑（保持原行为，无返回值）。"""
    await recover_stale_tasks()
