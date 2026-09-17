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
P2-1（Oracle 审查）演进（D3/C9）：模块级互斥锁只护「冷却检查 + 在途标志 +
时间戳更新」短临界区；登录/重启/sleep(30)/同步在锁外执行。并发防重（防 NasTools
双重启，替代原整链路串行）由 _sync_in_progress 在途标志承担——该标志经
try/finally 保证在成功/失败/任何异常路径清除（review R1）。
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
from app.services.notify_templates import flow_error_nastools_sync
from app.tasks import get_config_value, record_task_run

logger = logging.getLogger(__name__)

_IMPLEMENTED = True

_COOLDOWN_KEY = "nastools_last_sync_at"
_COOLDOWN_MIN_KEY = "nastools_sync_cooldown_minutes"

# P2-1 演进（D3/C9）：模块级互斥锁只保护「冷却检查 + 在途标志 + 时间戳更新」
# 等短临界区；登录/重启/sleep(30)/同步等长耗时段在锁外执行——冷却 sleep 期间
# 其它入口（L3 刮削 force 触发）不被兜底同步长阻塞。
# 并发防重（防 NasTools 双重启）由 _sync_in_progress 在途标志替代原整链路串行。
_sync_lock = asyncio.Lock()
_sync_in_progress = False  # 是否有同步在途（执行中，含重启等待）

# T8.4（council）：flow_error 通知节流窗（秒）。同步失败由兜底 job / 下载完成
# 事件重复触发，同一失败 10 分钟内只 notify 一次，防通知刷屏（task_run(error)
# 仍每次记录，仅通知节流）。对齐 transfer._alert_cooldown 模式（模块级 dict + TTL）。
_ALERT_COOLDOWN_SECONDS = 600.0
# T8.4：告警节流表。key = f"sync:{bucket[:40]}"；值 = (最近 notify 的
# monotonic 时间戳, 上次消息指纹)。同一 key 在窗口内重复触发且指纹相同 → 跳过
# notify（消息根因变化视为新告警，必须通知）。
_sync_alert_cooldown: dict[str, tuple[float, str]] = {}


def _sync_alert_bucket(message: str) -> str:
    """告警节流指纹：消息固定前缀（去掉 ': <exc>' 变量尾巴）。

    对齐 transfer._alert_bucket（M4）：exc 文本随网络抖动变化（超时/拒连/解析
    失败…），直接整条比较会让节流对变量尾巴失效（每次失败都算「新消息」刷屏）；
    取冒号前固定前缀作比较指纹，使「NasTools 同步失败」这类同根因消息共享同一
    指纹；不同前缀 = 不同根因，照常放行通知。
    """
    return (message or "").split(": ", 1)[0]


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def nastools_sync(force: bool = False) -> None:
    """NasTools 目录同步（下载完成事件触发；APScheduler job 兜底同样走此入口）。

    force=True（L3 刮削执行器专用）：跳过冷却检查，直接执行登录/重启/等待/
    重新登录/全部分目录同步——下载完成的集需立即刮削入库，不受冷却制动；
    同步失败（任一环节抛异常）时除 notify + task_run(error) 外**向上 re-raise**
    ——刮削执行器依赖异常感知失败以推进 node_attempt（L3 节点级重试计数）。
    force=False（默认）：冷却 → 冷启动放行 → 同步 → 更新时间戳 + task_run(success)；
    失败仅记录，不向调用方抛（旧语义）。

    D3（审查 C9，锁粒度）：`_sync_lock` 只保护「冷却检查 + 在途标志 + 时间戳更新」
    等短临界区；登录/重启/`asyncio.sleep(30)`/同步在锁外执行——冷却 sleep 期间
    刮削触发（force）不被兜底同步长阻塞。并发防重（防 NasTools 双重启）由
    `_sync_in_progress` 在途标志替代原整链路串行：任一同步在途时，普通同步
    等效冷却跳过、force 立即跳过（不重复执行，调用方凭异常感知/重试兜底）。
    """
    global _sync_in_progress  # 函数内绑定写入模块级在途标志（须显式声明）
    t0 = time.monotonic()  # Q8①：真实耗时

    # 1) 短临界区（锁内）：在途判定 + 冷却检查 + 在途置位
    async with _sync_lock:
        if _sync_in_progress:
            # 有同步在途（正在重启等待/执行）——防 NasTools 双重启：普通同步
            # 等效冷却跳过；force（刮削触发）也立即跳过、不被阻塞（数据由在途
            # 同步入库，不并发重启）。
            # 注意（review 发现 3）：此处为正常 return（不 raise）——force 调用方
            # （library_check.scrape_runner）依赖异常感知同步失败（except →
            # _count_scrape_failure 节点级重试计数）；在途跳过窗口内，本轮的刮削
            # 数据依赖在途同步成功入库；若在途同步因故失败，由下一轮 force
            # （library_check 轮询加速）或兜底 job 重新同步兜底。
            async with async_session() as s:
                await record_task_run(  # Q8①：真实耗时
                    s, "sync_nastools", "skipped",
                    "同步在途（另一任务正在执行），跳过本次同步",
                    duration_seconds=time.monotonic() - t0,
                )
                await s.commit()
            logger.info("[sync_nastools] 同步在途，跳过本次同步（force=%s）", force)
            return
        if not force:
            # 冷却检查（锁内重读，与「时间戳更新」互斥——首个同步完成后时间戳
            # 已更新，后续并发调用锁内重读直接冷却跳过）
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
        _sync_in_progress = True  # 无在途 → 置位（锁内），随后锁外执行

    # 2) 执行同步（锁外——D3 C9）+ 结果处理——外层 try/finally 保证任意退出路径
    #    （成功 / 同步失败 / record_task_run / notify / 冷却时间戳 DB 写自身抛异常）
    #    都清除在途标志，杜绝 `_sync_in_progress` 泄漏导致后续同步（含 force 刮削
    #    触发）被永久「在途跳过」（review R1，Important）。
    try:
        # 2.1) 执行同步（登录 → 重启 → 等待 → 重新登录 → 全部分目录同步）
        try:
            await nastools.client.login()
            await nastools.client.restart()
            await asyncio.sleep(30)  # NasTools 重启等待（n8n 契约），低频任务可接受
            await nastools.client.login()  # 重启后重新登录
            await nastools.client.run_directory_sync([])  # [] = 全部分目录
        except Exception as exc:  # noqa: BLE001  NasToolsUnavailable 统一失败路径（N2）
            message = f"NasTools 同步失败: {exc}"
            logger.error("[sync_nastools] %s", message)
            async with async_session() as s:
                await record_task_run(  # Q8①：真实耗时
                    s, "sync_nastools", "error", message,
                    duration_seconds=time.monotonic() - t0,
                )
                await s.commit()
            # T8.4：失败通知节流（复用 transfer._record_alert 模式）——task_run(error)
            # 每次记录；notify 仅按指纹（冒号前固定前缀，_sync_alert_bucket）在
            # _ALERT_COOLDOWN_SECONDS 窗口内去重，防兜底 job/事件重复触发刷屏。
            # 无 media 维度 → 类别级指纹 key（"sync:..."）；不同根因消息（前缀不同）
            # 照常放行。notify 为 best-effort：发送失败只记日志，不阻断主流程
            # （task_run 已落库；force 路径仍向上 re-raise，刮削执行器靠异常感知失败）。
            bucket = _sync_alert_bucket(message)
            key = f"sync:{bucket[:40]}"
            now_m = time.monotonic()
            # TTL 清理（对齐 transfer._record_alert）：停留超 2 倍窗口的条目不可能
            # 再被命中（此后任何触发都走「新告警」分支重写时间戳），遍历删除防
            # dict 随异常类别变化长期无界增长。每次入口 O(n) 清理一次。
            for _key, (_ts, _b) in list(_sync_alert_cooldown.items()):
                if now_m - _ts > 2 * _ALERT_COOLDOWN_SECONDS:
                    _sync_alert_cooldown.pop(_key, None)
            last_ts, last_bucket = _sync_alert_cooldown.get(key, (0.0, None))
            if last_bucket == bucket and (now_m - last_ts) < _ALERT_COOLDOWN_SECONDS:
                logger.info(
                    "[sync_nastools] flow_error 通知节流（%ds 内同类重复告警 %s）",
                    _ALERT_COOLDOWN_SECONDS, key,
                )
            else:
                _sync_alert_cooldown[key] = (now_m, bucket)
                try:
                    # 失败通知文案经 flow_error_nastools_sync 工厂
                    # （fix-notification-templates）：title=「NasTools 目录同步失败」，
                    # body 含 exc 文本（str(exc)，与节流指纹前缀无关）。
                    ns_title, ns_body = flow_error_nastools_sync(str(exc))
                    await notifier.notify(NotifyEvent(
                        event_type=EVENT_FLOW_ERROR,
                        title=ns_title,
                        body=ns_body,
                        recipient=None,
                    ))
                except Exception as notify_exc:  # noqa: BLE001  best-effort 通知
                    logger.warning(
                        "[sync_nastools] flow_error 通知发送失败: %s", notify_exc,
                    )
            if force:
                # L3（刮削执行器专用）：force 路径（下载完成立即刮削）失败必须向上
                # 暴露——调用方（library_check.scrape_runner）凭异常做节点级重试
                # 计数（node_attempt++，<3 重试 / ≥3 failed）。内部已 notify +
                # task_run(error)，re-raise 不重复通知。
                raise
            return

        # 3) 成功：更新冷却时间戳（upsert system_config）+ task_run(success)——锁内
        #    执行（与冷却检查互斥；后续并发调用锁内重读即命中冷却跳过）
        now = _now()
        async with _sync_lock:
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
    finally:
        async with _sync_lock:
            _sync_in_progress = False  # 释放在途（成功/失败/任何异常均清除，review R1）


async def nastools_sync_job() -> None:
    """APScheduler 兜底 job（IntervalTrigger(hours=1)；正式为下载完成事件触发）。"""
    try:
        await nastools_sync()
    except Exception:  # noqa: BLE001
        logger.exception("[sync_nastools] nastools_sync_job 异常")