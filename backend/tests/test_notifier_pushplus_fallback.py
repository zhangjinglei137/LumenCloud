"""PushPlus 失败降级站内告警单测（task C8：PushPlus 失败降级 + 防递归 + 节流）。

覆盖 brief 三点：
- PushPlus.send 抛错 → 补发一条 flow_error 站内通知（InAppNotifier 直发）
- 连续失败窗口内不重复刷屏（节流生效）
- 降级通知不触发 PushPlus 重推（防递归）
"""
import asyncio

import pytest

from app.services import notifier as notifier_mod
from app.services.notifier import EVENT_FLOW_ERROR, NotifyEvent, PushPlusNotifier


class _FakeInAppNotifier:
    """记录 notify 事件的 InAppNotifier 桩。"""

    def __init__(self) -> None:
        self.events: list[NotifyEvent] = []

    async def notify(self, event: NotifyEvent) -> None:
        self.events.append(event)


class _FailingPushPlusClient:
    """send 必抛异常的 PushPlus 客户端桩，并记录调用次数（防递归判定）。"""

    def __init__(self) -> None:
        self.send_calls = 0

    async def send(self, title: str, content: str, template: str = "txt") -> dict:
        self.send_calls += 1
        raise RuntimeError("pushplus 通道故障")


@pytest.fixture(autouse=True)
def _reset_pushplus_fallback_cooldown():
    """每个测试前重置 notifier 模块的 PushPlus 降级节流表（进程级共享状态）。"""
    table = getattr(notifier_mod, "_pushplus_alert_cooldown", None)
    if table:
        table.clear()
    yield
    table = getattr(notifier_mod, "_pushplus_alert_cooldown", None)
    if table:
        table.clear()


def _build_failing_notifier(monkeypatch) -> tuple[PushPlusNotifier, _FailingPushPlusClient, _FakeInAppNotifier]:
    """构造 PushPlusNotifier：client 必抛错、注入 InAppNotifier 桩。"""
    notifier = PushPlusNotifier()
    client = _FailingPushPlusClient()
    inapp = _FakeInAppNotifier()
    monkeypatch.setattr(notifier, "_client", client)
    monkeypatch.setattr(notifier, "_refresh_client", lambda: None)
    monkeypatch.setattr(notifier, "_fallback", inapp)
    return notifier, client, inapp


class TestPushPlusFallback:
    def test_send_failure_notifies_inapp_flow_error(self, monkeypatch):
        """PushPlus.send 抛错 → 站内补发一条 flow_error 通知。"""
        notifier, _, inapp = _build_failing_notifier(monkeypatch)
        event = NotifyEvent(EVENT_FLOW_ERROR, "下载完成", "正文")
        asyncio.run(notifier.notify(event))

        assert len(inapp.events) == 1
        ev = inapp.events[0]
        assert ev.event_type == EVENT_FLOW_ERROR
        assert "PushPlus" in ev.title

    def test_cooldown_suppresses_repeat_failures(self, monkeypatch):
        """窗口内连续失败只补发一次，不重复刷屏。"""
        notifier, _, inapp = _build_failing_notifier(monkeypatch)
        event = NotifyEvent(EVENT_FLOW_ERROR, "下载完成", "正文")
        asyncio.run(notifier.notify(event))
        asyncio.run(notifier.notify(event))

        assert len(inapp.events) == 1

    def test_no_recursive_pushplus_retry(self, monkeypatch):
        """降级告警不触发 PushPlus 重推（防递归）：send 只被调 1 次。"""
        notifier, client, inapp = _build_failing_notifier(monkeypatch)
        event = NotifyEvent(EVENT_FLOW_ERROR, "下载完成", "正文")
        asyncio.run(notifier.notify(event))

        assert len(inapp.events) == 1
        assert client.send_calls == 1
