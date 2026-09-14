"""PushPlus HTML 出口单测：纯文本转 HTML + template=html 发送（fix-notification-templates）。

覆盖两层：
- text_to_html 转换：标题加粗、段落分段、媒体高亮、HTML 转义；
- PushPlusNotifier.notify 出口：以 template="html" 发送转换后的 HTML，
  未配置 token（_client is None）时直接返回不发送。
"""
import asyncio

from app.services.notifier import EVENT_DOWNLOAD_COMPLETE, NotifyEvent, PushPlusNotifier
from app.services.notify_templates import text_to_html


class TestTextToHtml:
    def test_html_contains_bold_title(self):
        html = text_to_html("媒体 繁花 · S01E02 已入库完成。", "入库完成：繁花 · S01E02")
        assert "<b>入库完成：繁花 · S01E02</b>" in html

    def test_paragraph_split(self):
        html = text_to_html("第一行\n第二行", "标题")
        assert "<p>第一行</p>" in html
        assert "<p>第二行</p>" in html

    def test_media_highlight_span(self):
        html = text_to_html("媒体 繁花 · S01E02 已入库完成。", "入库完成")
        assert 'style="color:' in html
        assert "S01E02" in html

    def test_escape_html(self):
        html = text_to_html("<script>alert(1)</script>", "标题")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


class _FakePushPlusClient:
    """记录 send 调用的桩客户端（不真正发 HTTP）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def send(self, title: str, content: str, template: str = "txt") -> dict:
        self.calls.append((title, content, template))
        return {"code": 200, "message": "ok"}


class TestPushPlusNotifierHtml:
    """PushPlusNotifier.notify 出口：纯文本 → HTML，且以 template=html 发送。"""

    def _build_notifier(self, monkeypatch, client):
        # 阻止 _refresh_client 重建真实 client（避免读 token 并发 HTTP），
        # 固定注入桩 client，仅验证 notify 的调用参数。
        notifier = PushPlusNotifier()
        monkeypatch.setattr(notifier, "_client", client)
        monkeypatch.setattr(notifier, "_refresh_client", lambda: None)
        return notifier

    def test_notify_sends_html(self, monkeypatch):
        fake = _FakePushPlusClient()
        notifier = self._build_notifier(monkeypatch, fake)
        event = NotifyEvent(
            EVENT_DOWNLOAD_COMPLETE,
            "入库完成：繁花 · S01E02",
            "媒体 繁花 · S01E02 已入库完成。",
        )
        asyncio.run(notifier.notify(event))

        assert len(fake.calls) == 1
        title, content, template = fake.calls[0]
        assert title == event.title
        assert template == "html"
        # 出口内容为 HTML：标题加粗 + 正文分段 + 媒体高亮
        assert "<b>入库完成：繁花 · S01E02</b>" in content
        assert "<p>" in content
        assert 'style="color:' in content

    def test_notify_unconfigured_skips(self, monkeypatch):
        """未配置 token（_client is None）时直接返回，不构建 HTML 也不发送。"""
        notifier = self._build_notifier(monkeypatch, None)
        event = NotifyEvent(EVENT_DOWNLOAD_COMPLETE, "标题", "正文")
        asyncio.run(notifier.notify(event))  # 不抛异常即通过