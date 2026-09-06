"""
容量巡检任务（阶段 4 生产化 / E，交付 1+2）。

注册：scheduler job `capacity_alert`（IntervalTrigger(hours=1)），每小时 tick 依次：

1. 主动 capacity.provider.get_usage() 一次——复用 30s 进程内缓存，每小时调用必过
   缓存与 60s 快照节流，实际每 tick 一次真实 alist 统计并写 quark_capacity_log：
   保证「容量长期趋势」（验证报告 Q6）快照连续落库，不依赖 transfer 活动
   （此前快照只在 get_usage 成功路径写，无转存活动时可能长时间不更新）；
2. capacity.check_capacity_alert() 评估使用率阈值告警（连续 2 次超阈值 + 30min 冷却）。

统计失败（如 alist 不可用）/ 评估异常 → 记 task_run(error)，异常不外泄
（APScheduler job 内捕获，遵循 notification_scan / transfer job 包装模式）。
"""
import logging

from app.database import async_session
from app.services import capacity
from app.tasks import record_task_run

logger = logging.getLogger(__name__)

_IMPLEMENTED = True


async def capacity_alert_job() -> None:
    """容量巡检（APScheduler job）：快照落库 + 告警评估，异常不外泄。"""
    # 1) 主动统计并写快照（fail-closed：统计失败本轮不评估，记 error）
    try:
        await capacity.provider.get_usage()
    except Exception as exc:  # noqa: BLE001  CapacityUnavailable → 本轮跳过评估
        logger.warning("[capacity_alert] 容量统计失败（快照本轮不更新，告警评估跳过）: %s", exc)
        async with async_session() as s:
            await record_task_run(s, "capacity_alert", "error", f"容量统计失败: {exc}")
            await s.commit()
        return

    # 2) 使用率阈值告警评估
    try:
        alerted = await capacity.check_capacity_alert()
    except Exception as exc:  # noqa: BLE001
        logger.exception("[capacity_alert] 告警评估异常")
        async with async_session() as s:
            await record_task_run(s, "capacity_alert", "error", f"告警评估异常: {exc}")
            await s.commit()
        return

    # 3) 记录巡检结果：有告警 → success；无告警 → skipped（空跑不推送，消灭 P1 噪音）
    message = "容量使用率过高告警已发送" if alerted else "容量巡检完成，未触发告警"
    status = "success" if alerted else "skipped"
    async with async_session() as s:
        await record_task_run(s, "capacity_alert", status, message)
        await s.commit()
    logger.info("[capacity_alert] %s", message)
