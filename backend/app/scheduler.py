"""
APScheduler（AsyncIOScheduler）—— 单进程内嵌调度器（设计文档 §4.1/§4.2、实施计划 §3.3）

- 单 uvicorn 进程（--workers 1）内嵌，MemoryJobStore；任务状态持久化在业务表
  （task_run / episode_state / transfer_queue），重启后按表内状态恢复（recover_on_boot），
  不依赖 jobstore 持久化。
- 注册 6 个 job（id 固定）：
  | job_id                   | 触发器                                | 阶段 3 行为 |
  |--------------------------|---------------------------------------|-------------|
  | scan_all_media           | 不注册定时（add 后 paused，手动触发） | API /api/media/{id}/scan 调用 scan.scan_media |
  | process_transfer_queue   | IntervalTrigger(minutes=1)            | 阶段 3 已实现；定时默认关闭（事件 + 手动触发） |
  | nastools_sync            | IntervalTrigger(hours=1) 兜底（正式事件触发） | 阶段 3 已实现；定时默认关闭（事件触发） |
  | release_space_cleanup    | IntervalTrigger(hours=12)             | 阶段 3 已实现；定时默认关闭 |
  | notification_scan        | IntervalTrigger(minutes=5)            | 阶段 3 已实现；定时默认关闭 |
  | recover_stale            | IntervalTrigger(hours=1)              | P2-2 新增：运行期超时回退（recover 不只 boot）；定时默认关闭 |
- system_config 双层开关：scheduler_enabled（全局，默认开）+ scheduler.<job_id>（job 级，
  未配置时按阶段 3 默认：定时全部关闭，阶段 4 经 system_config 启用，§12.2 冷切换）。
"""
import logging

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.database import async_session
from app.models import SystemConfig
from app.tasks import cleanup, nastools_sync, notification_scan, recovery, scan, transfer

logger = logging.getLogger(__name__)

# ---- job 固定 id ----
JOB_SCAN_ALL_MEDIA = "scan_all_media"
JOB_PROCESS_TRANSFER_QUEUE = "process_transfer_queue"
JOB_NASTOOLS_SYNC = "nastools_sync"
JOB_RELEASE_SPACE_CLEANUP = "release_space_cleanup"
JOB_NOTIFICATION_SCAN = "notification_scan"
JOB_RECOVER_STALE = "recover_stale"

JOB_IDS = [
    JOB_SCAN_ALL_MEDIA,
    JOB_PROCESS_TRANSFER_QUEUE,
    JOB_NASTOOLS_SYNC,
    JOB_RELEASE_SPACE_CLEANUP,
    JOB_NOTIFICATION_SCAN,
    JOB_RECOVER_STALE,
]

# job 级开关默认值（阶段 3：定时默认全部关闭——只用手动触发与事件触发；
# 阶段 4 上线时才经 system_config 启用，见设计文档 §12.2 冷切换铁律）
_JOB_DEFAULT_ENABLED = {
    JOB_SCAN_ALL_MEDIA: False,
    JOB_PROCESS_TRANSFER_QUEUE: False,
    JOB_NASTOOLS_SYNC: False,
    JOB_RELEASE_SPACE_CLEANUP: False,
    JOB_NOTIFICATION_SCAN: False,
    JOB_RECOVER_STALE: False,
}

# MemoryJobStore：任务状态持久化走业务表，jobstore 仅承载调度
scheduler = AsyncIOScheduler(jobstores={"default": MemoryJobStore()})


# ---------------------------------------------------------------------------
# system_config 双层开关
# ---------------------------------------------------------------------------

async def _get_config_value(key: str, default=None):
    """读取 system_config，键不存在返回 default。"""
    async with async_session() as session:
        row = await session.get(SystemConfig, key)
        return row.value if row else default


def _as_bool(raw) -> bool:
    if raw is None:
        return False
    return str(raw).strip().lower() in ("true", "1", "yes", "on")


async def get_job_enabled(job_id: str) -> bool:
    """双层开关（实施计划 §3.3）：

    - 全局总开关：scheduler_enabled，未配置默认开启；
    - job 级开关：scheduler.<job_id>，未配置按阶段 2 默认（scan 手动、其余 guard）。
    """
    global_raw = await _get_config_value("scheduler_enabled", "true")
    if not _as_bool(global_raw):
        return False
    job_raw = await _get_config_value(f"scheduler.{job_id}", None)
    if job_raw is None:
        return _JOB_DEFAULT_ENABLED.get(job_id, True)
    return _as_bool(job_raw)


# ---------------------------------------------------------------------------
# 注册与启动
# ---------------------------------------------------------------------------

def register_jobs() -> None:
    """注册 6 个 job（id 固定，幂等 replace_existing）。

    scan_all_media 阶段 2 不注册定时：add_job(paused=True) 仅保留 run 函数
    （scan.scan_all_media_job → scan_all_media）供 API 手动触发；
    阶段 4 通过 system_config scheduler.scan_all_media=true 启用定时。
    """
    scheduler.add_job(
        scan.scan_all_media_job,
        IntervalTrigger(hours=1),
        id=JOB_SCAN_ALL_MEDIA,
        paused=True,  # 阶段2 手动触发：POST /api/media/{id}/scan → app.tasks.scan.scan_media
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        transfer.process_transfer_queue_job,
        IntervalTrigger(minutes=1),
        id=JOB_PROCESS_TRANSFER_QUEUE,
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        nastools_sync.nastools_sync_job,
        IntervalTrigger(hours=1),  # 正式为下载完成事件触发（§4.2），阶段2 以低频兜底注册
        id=JOB_NASTOOLS_SYNC,
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        cleanup.release_space_cleanup_job,
        IntervalTrigger(hours=12),
        id=JOB_RELEASE_SPACE_CLEANUP,
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        notification_scan.notification_scan_job,
        IntervalTrigger(minutes=5),
        id=JOB_NOTIFICATION_SCAN,
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    # P2-2：运行期超时回退兜底（recover 不再只 boot；阶段 4 经 system_config 启用）
    scheduler.add_job(
        recovery.recover_stale_tasks,
        IntervalTrigger(hours=1),
        id=JOB_RECOVER_STALE,
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )


async def _apply_job_switches() -> None:
    """双层开关落地：按 system_config 决定各 job 激活/暂停（读失败保持注册默认）。"""
    for job_id in JOB_IDS:
        job = scheduler.get_job(job_id)
        if job is None:
            continue
        try:
            enabled = await get_job_enabled(job_id)
        except Exception as exc:
            logger.warning("[scheduler] 读取 job=%s 开关失败: %s，保持注册默认", job_id, exc)
            continue
        if enabled and job.next_run_time is None:
            job.resume()
        elif not enabled and job.next_run_time is not None:
            job.pause()


async def start() -> None:
    """启动调度器：注册 job → start → 应用双层开关。"""
    register_jobs()
    scheduler.start()
    await _apply_job_switches()
    logger.info("[scheduler] 已启动 %d 个 job", len(scheduler.get_jobs()))
