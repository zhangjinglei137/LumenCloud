"""
APScheduler（AsyncIOScheduler）—— 单进程内嵌调度器（设计文档 §4.1/§4.2、实施计划 §3.3）

- 单 uvicorn 进程（--workers 1）内嵌，MemoryJobStore；任务状态持久化在业务表
  （task_run / episode_state / transfer_queue），重启后按表内状态恢复（recover_on_boot），
  不依赖 jobstore 持久化。
- 注册 8 个 job（id 固定）：
  | job_id                   | 触发器                                | 阶段 4 行为 |
  |--------------------------|---------------------------------------|-------------|
  | scan_all_media           | IntervalTrigger(minutes=1)            | B 定时：每分钟 tick，scan_all_media 按各 media last_scan_at 到期过滤；注册默认 paused，阶段 4 经 system_config 启用 |
  | process_transfer_queue   | IntervalTrigger(minutes=1)            | 阶段 3 已实现；定时默认关闭（事件 + 手动触发） |
  | nastools_sync            | IntervalTrigger(hours=1) 兜底（正式事件触发） | 阶段 3 已实现；定时默认关闭（事件触发） |
  | release_space_cleanup    | IntervalTrigger(hours=12)             | 阶段 3 已实现；定时默认关闭 |
  | notification_scan        | IntervalTrigger(minutes=5)            | 阶段 3 已实现；定时默认关闭 |
  | recover_stale            | IntervalTrigger(hours=1)              | P2-2 新增：运行期超时回退（recover 不只 boot）；定时默认关闭 |
  | capacity_alert           | IntervalTrigger(hours=1)              | 阶段 4（交付 E）：每小时主动统计写容量快照（Q6 趋势连续）+ 使用率阈值告警；定时默认关闭 |
  | prune_history            | IntervalTrigger(days=1)               | D-1：task_run/容量快照历史清理（保留 30 天，可经 task_run_retention_days 覆盖） |
- system_config 双层开关（A-2 方案②）：scheduler_enabled（全局，未配置默认开，false 短路
  全部停用）+ scheduler.<job_id>（job 级，未配置默认跟随总开关——总开关开启即默认启用；
  显式配置 true/false 可单独强制开启 / 单独关闭）。
- 冷切换铁律（P3-4）：注册即暂停（paused=True）——所有 job 默认不空转；仅由
  _apply_job_switches 依据 system_config 恢复。开关读取失败时降级为暂停（fail-closed：
  读不到配置 = 不启用定时，绝不让定时意外开启）。
"""
import logging

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.database import async_session
from app.models import SystemConfig
from app.tasks import capacity_alert, cleanup, nastools_sync, notification_scan, recovery, scan, transfer

logger = logging.getLogger(__name__)

# ---- job 固定 id ----
JOB_SCAN_ALL_MEDIA = "scan_all_media"
JOB_PROCESS_TRANSFER_QUEUE = "process_transfer_queue"
JOB_NASTOOLS_SYNC = "nastools_sync"
JOB_RELEASE_SPACE_CLEANUP = "release_space_cleanup"
JOB_NOTIFICATION_SCAN = "notification_scan"
JOB_RECOVER_STALE = "recover_stale"
JOB_CAPACITY_ALERT = "capacity_alert"
JOB_PRUNE_HISTORY = "prune_history"

JOB_IDS = [
    JOB_SCAN_ALL_MEDIA,
    JOB_PROCESS_TRANSFER_QUEUE,
    JOB_NASTOOLS_SYNC,
    JOB_RELEASE_SPACE_CLEANUP,
    JOB_NOTIFICATION_SCAN,
    JOB_RECOVER_STALE,
    JOB_CAPACITY_ALERT,
    JOB_PRUNE_HISTORY,
]

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
    """双层开关（实施计划 §3.3、A-2 方案②统一）：

    - 全局总开关：scheduler_enabled，未配置默认开启；总开关 false 时全部 job 停用
      （job 级无法越权）；
    - job 级开关：scheduler.<job_id>，未配置默认跟随总开关（总开关开启即默认启用）；
      显式配置 scheduler.<job_id>=true/false 可单独强制开启 / 单独关闭（覆盖跟随默认）。
    """
    global_raw = await _get_config_value("scheduler_enabled", "true")
    if not _as_bool(global_raw):
        return False
    job_raw = await _get_config_value(f"scheduler.{job_id}", None)
    if job_raw is None:
        return True
    return _as_bool(job_raw)


# ---------------------------------------------------------------------------
# 注册与启动
# ---------------------------------------------------------------------------

def register_jobs() -> None:
    """注册 6 个 job（id 固定，幂等 replace_existing）。

    B 定时（阶段 4）：scan_all_media 注册为 IntervalTrigger(minutes=1) 每分钟 tick，
    job 内部按各 media last_scan_at 到期过滤（scan.scan_all_media），到期才巡检；
    API 手动触发（scan.scan_media）不经过期检查，语义不变。

    P3-4 冷切换铁律：全部 job 注册时显式 paused=True（注册即暂停，不因未启用而空转），
    由 _apply_job_switches 依据 system_config 恢复——job 级未配置则跟随总开关
    （scheduler_enabled 未配置默认开 → 恢复后默认全部启用，A-2 方案②）。
    """
    scheduler.add_job(
        scan.scan_all_media_job,
        IntervalTrigger(minutes=1),  # B 定时：每分钟 tick（内部按 last_scan_at 到期过滤）
        id=JOB_SCAN_ALL_MEDIA,
        paused=True,  # P3-4：注册即暂停；阶段 4 经 system_config scheduler.scan_all_media=true 启用
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        transfer.process_transfer_queue_job,
        IntervalTrigger(minutes=1),
        id=JOB_PROCESS_TRANSFER_QUEUE,
        paused=True,  # P3-4：注册即暂停（阶段 3 默认关闭，事件 + 手动触发）
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        nastools_sync.nastools_sync_job,
        IntervalTrigger(hours=1),  # 正式为下载完成事件触发（§4.2），低频兜底注册
        id=JOB_NASTOOLS_SYNC,
        paused=True,  # P3-4：注册即暂停（正式为事件触发）
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        cleanup.release_space_cleanup_job,
        IntervalTrigger(hours=12),
        id=JOB_RELEASE_SPACE_CLEANUP,
        paused=True,  # P3-4：注册即暂停
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        notification_scan.notification_scan_job,
        IntervalTrigger(minutes=5),
        id=JOB_NOTIFICATION_SCAN,
        paused=True,  # P3-4：注册即暂停
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    # P2-2：运行期超时回退兜底（recover 不再只 boot；阶段 4 经 system_config 启用）
    scheduler.add_job(
        recovery.recover_stale_tasks,
        IntervalTrigger(hours=1),
        id=JOB_RECOVER_STALE,
        paused=True,  # P3-4：注册即暂停
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    # 阶段 4 生产化 / E：容量巡检（交付 1+2）——每小时主动统计写快照（Q6 容量趋势
    # 连续落库）+ 使用率阈值告警评估；默认关闭，经 system_config scheduler.capacity_alert=true 启用
    scheduler.add_job(
        capacity_alert.capacity_alert_job,
        IntervalTrigger(hours=1),
        id=JOB_CAPACITY_ALERT,
        paused=True,  # P3-4：注册即暂停（阶段 4 默认关闭，冷切换铁律）
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    # D-1（P2）：task_run / 容量快照历史清理（保留 30 天，每日兜底）
    scheduler.add_job(
        cleanup.prune_history_job,
        IntervalTrigger(days=1),
        id=JOB_PRUNE_HISTORY,
        paused=True,  # P3-4：注册即暂停（冷切换铁律），由 _apply_job_switches 恢复
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )


async def _apply_job_switches() -> None:
    """双层开关落地：按 system_config 决定各 job 激活/暂停。

    P3-4（延后项）：读取 job 开关失败时降级为**暂停**（fail-closed）——冷切换铁律：
    读不到配置 = 不启用定时，绝不让定时意外开启（注册默认已全部 paused，读失败保持暂停）。
    """
    for job_id in JOB_IDS:
        job = scheduler.get_job(job_id)
        if job is None:
            continue
        try:
            enabled = await get_job_enabled(job_id)
        except Exception as exc:
            logger.warning(
                "[scheduler] 读取 job=%s 开关失败: %s，降级为暂停（冷切换铁律：读不到配置=不启用定时）",
                job_id, exc,
            )
            job.pause()
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
