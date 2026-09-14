"""文案工厂单测：统一通知文案格式（fix-notification-templates）。"""
import pytest

from app.services.notify_templates import (
    approval_pending,
    download_complete,
    flow_error_alert,
    flow_error_capacity,
    flow_error_nastools_event,
    flow_error_nastools_sync,
    flow_error_transfer,
    text_to_html,
)


class TestDownloadComplete:
    def test_episode(self):
        title, body = download_complete("繁花", "S01E02", is_movie=False)
        assert title == "入库完成：繁花 · S01E02"
        assert body == "媒体 繁花 · S01E02 已入库完成。"

    def test_episode_three_digit(self):
        _, body = download_complete("海贼王", "S01E100", is_movie=False)
        assert body == "媒体 海贼王 · S01E100 已入库完成。"

    def test_movie(self):
        title, body = download_complete("长安乱", "movie:长安乱", is_movie=True)
        assert title == "入库完成：长安乱"
        assert body == "媒体 长安乱 已入库完成。"

    def test_no_media_id_or_filename(self):
        _, body = download_complete("繁花", "S01E02", is_movie=False)
        assert "media_id" not in body
        assert ".mkv" not in body


class TestApprovalPending:
    def test_format(self):
        title, body = approval_pending("狂飙")
        assert title == "新的想看请求：狂飙"
        assert body == "狂飙"  # 去重前缀由调用方拼接


class TestFlowError:
    def test_transfer(self):
        title, body = flow_error_transfer("繁花", "S01E02", "磁盘写满", 3)
        assert title == "转存失败：繁花 · S01E02"
        assert "磁盘写满" in body
        assert "3" in body

    def test_capacity(self):
        title, body = flow_error_capacity(82.3, 164.6, 200.0, 2, 80.0)
        assert title == "夸克容量使用率过高"
        assert "82.3%" in body
        assert "164.6G" in body and "200.0G" in body

    def test_nastools_sync(self):
        title, body = flow_error_nastools_sync("连接超时")
        assert title == "NasTools 目录同步失败"
        assert "连接超时" in body

    def test_nastools_event_chinese(self):
        title, body = flow_error_nastools_event("转存失败", "狂飙")
        assert title == "转存失败：狂飙"
        assert "transfer.fail" not in title
        assert body == "NaSTools 报告转存失败，请人工核查。"

    def test_alert(self):
        title, body = flow_error_alert("转存流程告警", "某中转文件失败")
        assert title == "转存流程告警"
        assert body == "某中转文件失败"


class TestTextToHtml:
    def test_bold_title_and_paragraphs(self):
        html = text_to_html("媒体 繁花 · S01E02 已入库完成。", "入库完成：繁花 · S01E02")
        assert "<b>入库完成：繁花 · S01E02</b>" in html
        assert "<p>" in html and "</p>" in html

    def test_media_highlight(self):
        html = text_to_html("媒体 繁花 · S01E02 已入库完成。", "入库完成")
        assert "style=" in html  # 高亮 span 存在

    def test_no_body(self):
        html = text_to_html("", "仅标题")
        assert "<b>仅标题</b>" in html