"""
兜底清理任务（设计文档 §4.2 release_space_cleanup，每 12h / 交付 D 兜底）。

正常流程靠「下载完成即释放」（transfer 阶段 A complete 即删夸克残留）；
此处仅兜底清理夸克残留孤儿文件：alist /quark 中不在「受引用集合」的文件
（download_task **仅 downloading 状态**的 quark_path 末段文件名 + transfer_queue 中
transferring/downloading 状态的 file_name）→ 一次批量 alist.remove。

P1-3（Oracle 审查）：引用集合只含进行中任务——complete/failed 的 download_task
不再保护其 quark_path，下载完成未删干净的残留可由本兜底清理（而非永久滞留）。

保守原则：只删无引用孤儿文件（必要时宁可保留，绝不误删进行中任务）；
alist 故障 → task_run(error) 记录，不向外抛异常。
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.database import async_session
from app.models import DownloadTask, QuarkCapacityLog, TaskRun, TransferQueue
from app.services import alist
from app.tasks import record_task_run

logger = logging.getLogger(__name__)

_IMPLEMENTED = True

_QUARK_ROOT = "/quark"
# 执行流「进行中」态的文件视为被引用（转存中/下载中都占用 /quark 空间，不可删）
_TQ_REFERENCED_STATES = ("transferring", "downloading")


def _basename(path) -> str | None:
    """取夸克路径末段文件名（null/空 → None）。"""
    if not path:
        return None
    return str(path).rstrip("/").rsplit("/", 1)[-1] or None


async def release_space_cleanup_job() -> None:
    """夸克残留孤儿文件兜底清理（注册：IntervalTrigger(hours=12)）。"""
    # Q8①：计时起点（待 record_task_run 支持 duration_seconds 后补结束计时）
    t0 = time.monotonic()
    # 1) 列 /quark（alist 故障 → task_run(error)，不抛异常）
    try:
        entries = await alist.list_dir(_QUARK_ROOT)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cleanup] alist 列目录失败: %s", exc)
        async with async_session() as s:
            # Q8①：真实耗时（entry 级 t0 计时）
            await record_task_run(
                s, "cleanup", "error", f"alist 列目录失败: {exc}",
                duration_seconds=time.monotonic() - t0,
            )
            await s.commit()
        return
    present: set[str] = {str(e.get("name")) for e in entries if e.get("name")}
    if not present:
        async with async_session() as s:
            # Q8①：真实耗时（entry 级 t0 计时）
            await record_task_run(
                s, "cleanup", "skipped", "夸克中转目录为空",
                duration_seconds=time.monotonic() - t0,
            )
            await s.commit()
        return

    # 2) 受引用集合（P1-3：download_task 仅 downloading 状态引用其 quark_path；
    #    complete/failed 不再保护，残留可被兜底清理；tq 进行中态 file_name）
    async with async_session() as s:
        dl_paths = (
            (
                await s.execute(
                    select(DownloadTask.quark_path).where(
                        DownloadTask.status == "downloading",
                        DownloadTask.quark_path.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        tq_names = (
            (
                await s.execute(
                    select(TransferQueue.file_name).where(
                        TransferQueue.status.in_(_TQ_REFERENCED_STATES)
                    )
                )
            )
            .scalars()
            .all()
        )
    referenced = {b for b in (_basename(p) for p in dl_paths) if b}
    referenced.update(n for n in tq_names if n)

    # 3) 孤儿文件 → 一次批量删除
    orphans = sorted(present - referenced)
    if not orphans:
        async with async_session() as s:
            # Q8①：真实耗时（entry 级 t0 计时）
            await record_task_run(
                s, "cleanup", "skipped", "无孤儿文件（无需清理）",
                duration_seconds=time.monotonic() - t0,
            )
            await s.commit()
        return

    try:
        await alist.remove(orphans, f"{_QUARK_ROOT}/")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cleanup] 删除孤儿文件失败: %s", exc)
        async with async_session() as s:
            # Q8①：真实耗时（entry 级 t0 计时）
            await record_task_run(
                s, "cleanup", "error", f"删除孤儿文件失败: {exc}",
                duration_seconds=time.monotonic() - t0,
            )
            await s.commit()
        return

    preview = ", ".join(orphans[:10]) + ("…" if len(orphans) > 10 else "")
    async with async_session() as s:
        # Q8①：真实耗时（entry 级 t0 计时）
        await record_task_run(
            s, "cleanup", "success",
            f"清理夸克孤儿文件 {len(orphans)} 个: {preview}",
            duration_seconds=time.monotonic() - t0,
        )
        await s.commit()
    logger.info("[cleanup] 清理孤儿文件 %d 个: %s", len(orphans), preview)


# D-1（P2）：task_run / quark_capacity_log 定期清理（保留天数可经 system_config
# task_run_retention_days 覆盖，缺省 30 天）。注册：IntervalTrigger(days=1)。
_RETENTION_DAYS = 30
_RETENTION_CONFIG_KEY = "task_run_retention_days"


async def prune_history_job() -> None:
    """清理超期历史记录：task_run（按 started_at）与 quark_capacity_log（按 checked_at），
    各保留最近 retention_days 天；一次批量 delete。失败/异常 → task_run(error) 不抛。
    清理任务自身的记录在次日清理中自然过期，无需特殊处理。"""
    retention = _RETENTION_DAYS
    try:
        async with async_session() as s:
            from app.models import SystemConfig
            row = await s.get(SystemConfig, _RETENTION_CONFIG_KEY)
            if row and row.value:
                retention = max(1, int(str(row.value).strip()))
    except Exception as exc:
        logger.warning("[prune] 读取 %s 失败，用默认 %d 天: %s",
                       _RETENTION_CONFIG_KEY, retention, exc)

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=retention)
    try:
        async with async_session() as s:
            r1 = await s.execute(
                delete(TaskRun).where(TaskRun.started_at.is_not(None), TaskRun.started_at < cutoff)
            )
            r2 = await s.execute(
                delete(QuarkCapacityLog).where(
                    QuarkCapacityLog.checked_at.is_not(None), QuarkCapacityLog.checked_at < cutoff
                )
            )
            n1, n2 = r1.rowcount or 0, r2.rowcount or 0
            await s.commit()
        async with async_session() as s:
            await record_task_run(s, "prune_history", "success",
                                  f"清理历史记录 task_run {n1} 条 / 容量快照 {n2} 条（保留 {retention} 天）")
            await s.commit()
        logger.info("[prune] 清理 task_run %d 条、容量快照 %d 条（保留 %d 天）", n1, n2, retention)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[prune] 清理历史记录失败: %s", exc)
        async with async_session() as s:
            await record_task_run(s, "prune_history", "error", f"清理历史记录失败: {exc}")
            await s.commit()