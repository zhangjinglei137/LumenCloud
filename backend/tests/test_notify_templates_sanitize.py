"""通知文案脱敏单测（task C8：flow_error_nastools_sync 截断脱敏 + 纯函数 sanitize_error_text）。

覆盖 brief 点：
- sanitize_error_text 纯函数：剥 URL userinfo / query token、超长截断
- flow_error_nastools_sync 集成：含凭据 URL 的异常文案入库前已脱敏
"""
from app.services.notify_templates import flow_error_nastools_sync, sanitize_error_text


class TestSanitizeErrorText:
    def test_strips_url_userinfo(self):
        text = "请求 https://user:pass@push.example.com/api 失败"
        out = sanitize_error_text(text)
        assert "user:pass@" not in out
        assert "https://push.example.com/api" in out  # 主机与路径保留

    def test_redacts_query_token_value(self):
        text = "接口 https://host/api?token=abc123&x=1 超时"
        out = sanitize_error_text(text)
        assert "abc123" not in out
        assert "token=" in out  # 参数名保留、值脱敏

    def test_truncates_long_text(self):
        text = "错误" * 500  # 1000 字符 > 500 上限
        out = sanitize_error_text(text)
        assert len(out) <= 500

    def test_short_text_untouched(self):
        text = "连接超时"
        out = sanitize_error_text(text)
        assert out == "连接超时"

    def test_none_or_empty_safe(self):
        assert sanitize_error_text("") == ""


class TestFlowErrorNastoolsSyncSanitized:
    def test_body_sanitizes_url_credentials(self):
        exc = "登录失败 https://admin:p@ss@nastools.local/api?token=topsecret"
        _, body = flow_error_nastools_sync(exc)
        assert "admin:p@ss@" not in body
        assert "topsecret" not in body

    def test_body_truncates_long_exc(self):
        _, body = flow_error_nastools_sync("e" * 800)
        assert len(body) <= 600  # 前缀短文本 + 截断后 ≤ 500 + 前缀

    def test_plain_exc_kept(self):
        _, body = flow_error_nastools_sync("连接超时")
        assert "连接超时" in body