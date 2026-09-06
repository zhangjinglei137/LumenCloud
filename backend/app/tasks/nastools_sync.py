"""
NasTools 目录同步任务（设计文档 §4.2，正式为下载完成事件触发）。

阶段 3：真实实现（交付 D 事件触发）。
- 冷却：nastools_last_sync_at（system_config，ISO 字符串）距今不足
  cooldown（nastools_sync_cooldown_minutes，system_config 覆盖 settings 默认 30min）→ 跳过（N1）
- 冷启动：从未同步过（无该键）→ 允许立即执行
- 执行序列（绕开 NasTools 目录同步 bug，用户确认保留）：
    登录 → 重启 → sleep(30)（重启等待，n8n 契约）→ 重新登录 → run_directory_sync([]) 全部分目录
- 任一步失败（NasToolsUnavailable 等）→ task_run(error) + flow_error 通知（N2 修复）
注意：asyncio.sleep(30) 低频可接受（非转存路径）；job 由 APScheduler max_instances=1 防重入。
P2-1（Oracle 审查）：模块级互斥锁串行化整条同步链路（含冷却检查在锁内重读），
防止事件触发与 job 兜底并发导致 NasTools 双重启。
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.database import async_session
from app.models import SystemConfig
from app.services import nastools
from app.services.notifier import EVENT_FLOW_ERROR, NotifyEvent, notifier
from app.tasks import get_config_value, record_task_run

logger = logging.getLogger(__name__)

_IMPLEMENTED = True

_COOLDOWN_KEY = "nastools_last_sync_at"
_COOLDOWN_MIN_KEY = "nastools_sync_cooldown_minutes"

# P2-1：模块级互斥锁——整条同步链路（冷却检查 + 登录/重启/同步）串行化，
# 并发调用（下载完成事件触发 + job 兜底）不会造成 NasTools 双重启
_sync_lock = asyncio.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def nastools_sync() -> None:
    """NasTools 目录同步（下载完成事件触发；APScheduler job 兜底同样走此入口）。

    冷却 → 冷启动放行 → 登录/重启/等待/重登录/全部分目录同步 → 更新时间戳 + task_run(success)。
    整条流程持 _sync_lock；冷却检查在锁内重读（首个同步完成后时间戳已更新，后续并发直接冷却跳过）。
    """
    async with _sync_lock:
        t0 = time.monotonic()  # Q8①：真实耗时
        # 1) 冷却检查（冷启动放行）
        async with async_session() as s:
            last_raw = await get_config_value(s, _COOLDOWN_KEY, None)
            cooldown_min = await get_config_value(
                s, _COOLDOWN_MIN_KEY, settings.NASTOOLS_SYNC_COOLDOWN_MINUTES
            )
            if last_raw:
                try:
                    last = datetime.fromisoformat(last_raw)
                except (TypeError, ValueError):
                    last = None  # 键存在但格式损坏 → 视为从未同步，立即执行
                effective_cooldown = float(cooldown_min or settings.NASTOOLS_SYNC_COOLDOWN_MINUTES)
                if last is not None and _now() - last < timedelta(minutes=effective_cooldown):
                    await record_task_run(  # Q8①：真实耗时
                        s, "sync_nastools", "skipped",
                        f"冷却中（{effective_cooldown}min 制动），跳过本次同步",
                        duration_seconds=time.monotonic() - t0,
                    )
                    await s.commit()
                    logger.info("[sync_nastools] 冷却中，跳过（last=%s）", last_raw)
                    return

        # 2) 执行同步（登录 → 重启 → 等待 → 重新登录 → 全部分目录同步）
        try:
            await nastools.client.login()
            await nastools.client.restart()
            await asyncio.sleep(30)  # NasTools 重启等待（n8n 契约），低频任务可接受
            await nastools.client.login()  # 重启后重新登录
            await nastools.client.run_directory_sync([])  # [] = 全部分目录
        except Exception as exc:  # noqa: BLE001  NasToolsUnavailable 统一失败路径（N2）
            logger.error("[sync_nastools] NasTools 同步失败: %s", exc)
            await notifier.notify(NotifyEvent(
                event_type=EVENT_FLOW_ERROR,
                title="NasTools 目录同步失败",
                body=f"同步失败，请检查 NasTools 服务与凭据（N2）: {exc}",
                recipient=None,
            ))
            async with async_session() as s:
                await record_task_run(  # Q8①：真实耗时
                    s, "sync_nastools", "error", f"NasTools 同步失败: {exc}",
                    duration_seconds=time.monotonic() - t0,
                )
                await s.commit()
            return

        # 3) 成功：更新冷却时间戳（upsert system_config）+ task_run(success)
        now = _now()
        async with async_session() as s:
            async with s.begin():
                cfg = await s.get(SystemConfig, _COOLDOWN_KEY)
                if cfg is None:
                    s.add(SystemConfig(key=_COOLDOWN_KEY, value=now.isoformat()))
                else:
                    cfg.value = now.isoformat()
                await record_task_run(  # Q8①：真实耗时
                    s, "sync_nastools", "success", "NasTools 目录同步完成（全部分目录）",
                    duration_seconds=time.monotonic() - t0,
                )
        logger.info("[sync_nastools] NasTools 目录同步完成")


async def nastools_sync_job() -> None:
    """APScheduler 兜底 job（IntervalTrigger(hours=1)；正式为下载完成事件触发）。"""
    try:
        await nastools_sync()
    except Exception:  # noqa: BLE001
        logger.exception("[sync_nastools] nastools_sync_job 异常")