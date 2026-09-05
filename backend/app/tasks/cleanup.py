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

from sqlalchemy import select

from app.database import async_session
from app.models import DownloadTask, TransferQueue
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
    # 1) 列 /quark（alist 故障 → task_run(error)，不抛异常）
    try:
        entries = await alist.list_dir(_QUARK_ROOT)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cleanup] alist 列目录失败: %s", exc)
        async with async_session() as s:
            await record_task_run(s, "cleanup", "error", f"alist 列目录失败: {exc}")
            await s.commit()
        return
    present: set[str] = {str(e.get("name")) for e in entries if e.get("name")}
    if not present:
        async with async_session() as s:
            await record_task_run(s, "cleanup", "skipped", "夸克中转目录为空")
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
            await record_task_run(s, "cleanup", "skipped", "无孤儿文件（无需清理）")
            await s.commit()
        return

    try:
        await alist.remove(orphans, f"{_QUARK_ROOT}/")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cleanup] 删除孤儿文件失败: %s", exc)
        async with async_session() as s:
            await record_task_run(s, "cleanup", "error", f"删除孤儿文件失败: {exc}")
            await s.commit()
        return

    preview = ", ".join(orphans[:10]) + ("…" if len(orphans) > 10 else "")
    async with async_session() as s:
        await record_task_run(
            s, "cleanup", "success",
            f"清理夸克孤儿文件 {len(orphans)} 个: {preview}",
        )
        await s.commit()
    logger.info("[cleanup] 清理孤儿文件 %d 个: %s", len(orphans), preview)