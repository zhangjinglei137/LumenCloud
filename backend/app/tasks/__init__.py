"""
任务包：调度器各业务任务模块 + 共用 helper

覆盖：scan（巡检）/ transfer（转存）/ cleanup（兜底清理）/ nastools_sync（目录同步）/
notification_scan（通知扫描）/ recovery（启动恢复）。

注意：
- 任务执行状态持久化到 task_run 表（不依赖 APScheduler jobstore，设计文档 §4.1）。
- app.models / app.services 由并行 lane 创建，集成验证时统一接入；
  当前阶段这些模块尚不存在时 import 失败属预期。
"""
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import async_session
from app.models import SystemConfig, TaskRun


def _now() -> datetime:
    """统一时间源（naive UTC）。

    与 models lane 的 server_default=func.now()（无时区 UTC 字符串）保持一致，
    避免 aware/naive 两种表示在 SQLite 中字符串比较不一致。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def get_config_value(session, key: str, default=None):
    """
    读取 system_config（非敏感配置表，§3.3）。键不存在返回 default。
    """
    row = await session.get(SystemConfig, key)
    if row is None:
        return default
    return row.value


async def record_task_run(session, task_type: str, status: str, message: str,
                          media_id: int | None = None, *,
                          duration_seconds: float | None = None,
                          started_at: datetime | None = None) -> int | None:
    """写入 task_run 执行记录（设计文档 §3.1）并返回记录 id。

    Q8①：job 入口以 time.monotonic() 计时后传入 duration_seconds（真实耗时）；
    started_at 可选（entry 级起表时间，缺省即写入时刻）。旧调用（不传新参数）
    行为完全不变。仅 add+flush，由调用方统一 commit。
    """
    ts = started_at or _now()
    run = TaskRun(
        task_type=task_type,
        media_id=media_id,
        status=status,
        message=message,
        started_at=ts,
        # started_at 缺省时与 finished_at 共用同一时刻（精确保持旧行为「同取当前时间」）；
        # 传入 started_at（entry 级起表时间）时 finished_at 为真实写入时刻
        finished_at=ts if started_at is None else _now(),
        duration_seconds=duration_seconds,
    )
    session.add(run)
    await session.flush()
    return run.id


def as_bool(raw) -> bool:
    """宽松布尔解析（system_config 值为字符串）。"""
    if raw is None:
        return False
    return str(raw).strip().lower() in ("true", "1", "yes", "on")


# 子模块在 helper 定义之后导入，避免部分初始化循环依赖。
from app.tasks import (  # noqa: E402,F401
    capacity_alert,
    cleanup,
    nastools_sync,
    notification_scan,
    recovery,
    scan,
    transfer,
)

__all__ = [
    "capacity_alert",
    "scan",
    "transfer",
    "cleanup",
    "nastools_sync",
    "notification_scan",
    "recovery",
    "record_task_run",
    "get_config_value",
    "as_bool",
]
