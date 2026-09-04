"""
APScheduler（AsyncIOScheduler）—— 单进程内嵌调度器
骨架阶段仅注册占位任务，业务任务（巡检/转存/兜底清理/通知扫描）按
docs/新系统设计.md §4.2 在实施阶段注册。

注意：使用 MemoryJobStore + 业务表持久化任务状态（task_run 表），
重启后按表内状态恢复，不依赖 jobstore 持久化。
"""
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.tasks import placeholder

scheduler = AsyncIOScheduler()


def register_jobs() -> None:
    # 占位任务：每分钟心跳，用于验证调度器在容器内正常运行
    if not scheduler.get_job("heartbeat"):
        scheduler.add_job(
            placeholder.heartbeat,
            IntervalTrigger(minutes=1),
            id="heartbeat",
            max_instances=1,
            coalesce=True,
        )


def start() -> None:
    register_jobs()
    scheduler.start()