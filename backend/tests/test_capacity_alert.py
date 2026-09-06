"""capacity 容量使用率阈值告警单测（阶段 4 生产化 / E，交付 1+2）。

mock quark_capacity_log 快照行 + notifier，验证 check_capacity_alert：
- 连续 2 次快照超阈值 → flow_error 通知（title「夸克容量使用率过高」）
- 去抖：单条超阈值 / 快照不足 / 数据缺失 → 不通知
- 冷却：30min 窗口内不重复通知（模块级 _last_capacity_alert_at）
- 回落重置：最近快照低于阈值后不触发，恢复后可再次触发
- 阈值可经 system_config capacity_alert_threshold 覆盖（非法值回退默认 0.90）
- capacity_alert_job 编排：先主动 get_usage（快照落库）→ 再告警评估 → 记 task_run

不连真实服务/数据库：mock async_session / notifier / provider.get_usage。
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import QuarkCapacityLog
from app.services import capacity as cap_mod
from app.services.capacity import check_capacity_alert
from app.services.notifier import EVENT_FLOW_ERROR


def run(coro):
    return asyncio.run(coro)


def _log(total_gb: float, used_gb: float | None, offset_hours: float = 0.0):
    """构造快照 mock 行（used_gb=None 表示数据缺失场景）。"""
    log = MagicMock(spec=QuarkCapacityLog)
    log.total_gb = total_gb
    log.used_gb = used_gb
    log.source = "alist"
    log.checked_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        hours=offset_hours
    )
    return log


def _session_maker(rows, threshold_value=None):
    """mock async_session：session.get(阈值配置) + session.execute(快照查询)。"""
    session = MagicMock()
    session.get = AsyncMock(
        return_value=None if threshold_value is None else MagicMock(value=str(threshold_value))
    )
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    session.execute = AsyncMock(return_value=result)
    maker = MagicMock()
    maker.return_value.__aenter__ = AsyncMock(return_value=session)
    maker.return_value.__exit__ = AsyncMock(return_value=False)
    return maker, session


@pytest.fixture(autouse=True)
def _reset_capacity_alert_cooldown():
    """每个测试前重置 capacity 模块级告警冷却戳（conftest 只重置 transfer 的，这里补）。

    冷却戳是进程级共享状态；不重置会让多个测试在 30min 真实时间窗内命中同一
    「已冷却」分支，互相吞掉断言依赖的 flow_error 通知。
    """
    cap_mod._last_capacity_alert_at = 0.0
    yield
    cap_mod._last_capacity_alert_at = 0.0


def _aligned_check(rows, threshold_value=None, notifier_mock=None):
    """在 async_session + notifier mock 上下文内执行一次 check_capacity_alert。

    返回 (alerted, session, notifier_mock)。
    """
    maker, session = _session_maker(rows, threshold_value)
    n = notifier_mock if notifier_mock is not None else AsyncMock()
    with (
        patch("app.services.capacity.async_session", maker),
        patch("app.services.capacity.notifier", n),
    ):
        return run(check_capacity_alert()), session, n


# ---- 交付 1：阈值触发 ----

def test_alert_fires_when_two_consecutive_snapshots_over_threshold():
    # 最近 2 条快照：95% / 92%（最新在前）→ 连续 2 次超阈值 → flow_error 通知
    rows = [_log(10.0, 9.5), _log(10.0, 9.2)]
    alerted, session, n = _aligned_check(rows)

    assert alerted is True
    n.notify.assert_awaited_once()
    event = n.notify.await_args.args[0]
    assert event.event_type == EVENT_FLOW_ERROR
    assert event.title == "夸克容量使用率过高"
    assert "95.0%" in event.body
    assert session.execute.await_count == 1  # 快照查询一次
    assert session.get.await_count == 1  # 阈值配置一次


# ---- 交付 1：去抖（连续 2 次才告警）----

def test_alert_not_fired_when_recent_snapshot_below_threshold():
    # 最新已回落（80%），仅上一条超阈值 → 不满足连续 2 次 → 不通知
    rows = [_log(10.0, 8.0), _log(10.0, 9.5)]
    alerted, session, n = _aligned_check(rows)

    assert alerted is False
    n.notify.assert_not_called()


def test_alert_not_fired_when_only_one_snapshot():
    alerted, session, n = _aligned_check([_log(10.0, 9.5)])

    assert alerted is False
    n.notify.assert_not_called()


def test_alert_not_fired_when_no_snapshot():
    alerted, session, n = _aligned_check([])

    assert alerted is False
    n.notify.assert_not_called()


def test_alert_skipped_when_usage_data_missing():
    # 最新快照 used_gb 缺失 → 无法计算使用率 → 不判定（fail-safe）
    rows = [_log(10.0, None), _log(10.0, 9.5)]
    alerted, session, n = _aligned_check(rows)

    assert alerted is False
    n.notify.assert_not_called()


# ---- 交付 1：冷却（30min 内不重复告警）----

def test_alert_throttled_within_cooldown():
    rows = [_log(10.0, 9.5), _log(10.0, 9.2)]
    n = AsyncMock()
    alerted1, _, _ = _aligned_check(rows, notifier_mock=n)
    assert alerted1 is True

    alerted2, _, _ = _aligned_check(rows, notifier_mock=n)  # 30min 窗口内再次
    assert alerted2 is False
    n.notify.assert_awaited_once()


# ---- 交付 1：回落重置（使用率低于阈值后不再触发，恢复后可再次触发）----

def test_alert_recovers_after_usage_drops():
    n = AsyncMock()
    # ① 连续超阈值 → 首次告警
    alerted1, _, _ = _aligned_check([_log(10.0, 9.5), _log(10.0, 9.2)], notifier_mock=n)
    assert alerted1 is True
    # ② 最新快照回落（80%）→ 去抖不满足 → 不告警
    alerted2, _, _ = _aligned_check([_log(10.0, 8.0), _log(10.0, 9.5)], notifier_mock=n)
    assert alerted2 is False
    # ③ 模拟冷却窗口已过（30min），再次连续超阈值 → 可再次告警
    cap_mod._last_capacity_alert_at = 0.0
    alerted3, _, _ = _aligned_check([_log(10.0, 9.6), _log(10.0, 9.4)], notifier_mock=n)
    assert alerted3 is True

    assert n.notify.await_count == 2


# ---- 交付 1：阈值可配置（system_config capacity_alert_threshold）----

def test_alert_uses_config_threshold():
    # 阈值配置 0.5（50%）：两条 60% 快照 → 触发（默认 90% 下不触发）
    rows = [_log(10.0, 6.0), _log(10.0, 6.2)]
    alerted, session, n = _aligned_check(rows, threshold_value=0.5)

    assert alerted is True
    n.notify.assert_awaited_once()


def test_alert_uses_default_threshold_when_config_invalid():
    # 阈值配置非数值 → 回退默认 0.90：60% 快照不触发
    rows = [_log(10.0, 6.0), _log(10.0, 6.2)]
    alerted, session, n = _aligned_check(rows, threshold_value="abc")

    assert alerted is False
    n.notify.assert_not_called()


# ---- 交付 2：容量巡检 job 编排（主动 get_usage 落快照 → 告警评估 → task_run）----

def test_capacity_alert_job_runs_usage_then_check_and_records_run():
    from app.tasks import capacity_alert as job_mod

    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    maker = MagicMock()
    maker.return_value.__aenter__ = AsyncMock(return_value=session)
    maker.return_value.__exit__ = AsyncMock(return_value=False)

    with (
        patch("app.services.capacity.provider.get_usage", new=AsyncMock()),
        patch("app.services.capacity.check_capacity_alert", new=AsyncMock(return_value=True)),
        patch("app.tasks.capacity_alert.async_session", maker),  # job 内直接使用
        patch("app.tasks.async_session", maker),  # record_task_run 命名空间
    ):
        run(job_mod.capacity_alert_job())

    assert session.commit.await_count == 1
    added = [c.args[0] for c in session.add.call_args_list]
    assert any(getattr(r, "status", None) == "success" for r in added)  # 告警已发送 → success


def test_capacity_alert_job_swallows_get_usage_failure():
    """get_usage 失败（容量不可用）→ 记 error 不外泄，不评估告警。"""
    from app.tasks import capacity_alert as job_mod

    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    maker = MagicMock()
    maker.return_value.__aenter__ = AsyncMock(return_value=session)
    maker.return_value.__exit__ = AsyncMock(return_value=False)

    with (
        patch("app.services.capacity.provider.get_usage", new=AsyncMock(side_effect=RuntimeError("alist 不可用"))),
        patch("app.services.capacity.check_capacity_alert", new=AsyncMock(return_value=False)),
        patch("app.tasks.capacity_alert.async_session", maker),
        patch("app.tasks.async_session", maker),
    ):
        run(job_mod.capacity_alert_job())  # 不应抛

    assert session.commit.await_count == 1
    added = [c.args[0] for c in session.add.call_args_list]
    assert any(getattr(r, "status", None) == "error" for r in added)
