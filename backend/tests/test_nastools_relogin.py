"""NasTools 客户端会话失效自动重登单测（fix-audit-issues Task D3 / 审查 C10）。

验证（对应 design D11「NasTools 会话失效自动重登」）：
- `_do` 收到 401/403 → 清除 `_session_cookie` 并自动重登一次 → 重试请求成功
  （重登对调用方透明：返回正常 JSON，cookie 换新）
- 只重试一次（防 401→重登→401 无限循环）：401 后重登再 401 → 不再重登，
  按既有 `NasToolsUnavailable` 语义上抛
- 重登本身失败（login 抛）→ `NasToolsUnavailable` 上抛（既有语义）

测试方式：monkeypatch `config_store.get` 注入配置、`_get_client()` 返回可编程
fake HTTP client（按序弹出预置响应），纯单元级验证（无真实网络 / DB）。
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.services import nastools


# ---------------------------------------------------------------------------
# fake HTTP 层
# ---------------------------------------------------------------------------

class FakeResp:
    """最小响应：status_code / headers(.get) / text / json()。"""

    def __init__(self, status_code, headers=None, text="", body=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self._body = body

    def json(self):
        if self._body is not None:
            return self._body
        raise ValueError("not json")


class FakeHTTPClient:
    """可编程 fake HTTP client：按序弹出预置响应，记录 post 调用。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []  # [(url, data)]
        self.cookies = SimpleNamespace(
            set=lambda *a, **k: None,
            delete=lambda *a, **k: None,
        )

    async def post(self, url, data=None):
        self.calls.append((url, data))
        return self._responses.pop(0)


def _login_ok(cookie="session=s1"):
    """登录成功：302 + Set-Cookie 含 session=（Q4 实证契约）。"""
    return FakeResp(302, {"set-cookie": cookie})


def _login_fail():
    """登录失败：非 302 / 无 session cookie → login() 抛 NasToolsUnavailable。"""
    return FakeResp(403, text="forbidden")


def _do_ok(body=None):
    return FakeResp(200, body=body or {"ok": True})


def _do_unauthorized(status=401):
    return FakeResp(status, text="unauthorized")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


def _configured_client(monkeypatch, responses):
    """注入配置 + fake HTTP client 的 NasToolsClient 实例。"""
    monkeypatch.setattr(
        nastools.config_store, "get",
        lambda key, default=None: {
            "nastools_base_url": "http://nas.test",
            "nastools_username": "user",
            "nastools_password": "pass",
        }.get(key, default),
    )
    cli = nastools.NasToolsClient()
    fake = FakeHTTPClient(responses)
    monkeypatch.setattr(cli, "_get_client", lambda: fake)
    return cli, fake


def _split_calls(fake):
    """按 URL 区分 login（base/）与 do（base/do）调用，返回 (login_calls, do_calls)。"""
    login_calls = [c for c in fake.calls if c[0] == "http://nas.test/"]
    do_calls = [c for c in fake.calls if c[0] == "http://nas.test/do"]
    return login_calls, do_calls


# ---------------------------------------------------------------------------
# 会话失效自动重登（审查 C10）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [401, 403])
def test_do_relogin_on_session_invalid_and_retry_succeeds(monkeypatch, status):
    """`_do` 收到 401/403（会话失效）→ 清除 cookie 自动重登一次 → 重试请求成功。

    重登对调用方透明：返回正常 JSON；`_session_cookie` 更新为新会话。
    """
    cli, fake = _configured_client(monkeypatch, [
        _login_ok("session=s1"),
        _do_unauthorized(status),
        _login_ok("session=s2"),
        _do_ok({"ok": True}),
    ])

    result = run(cli._do("restart"))

    assert result == {"ok": True}
    assert cli._session_cookie == "session=s2"  # 已换新会话（重登成功）
    login_calls, do_calls = _split_calls(fake)
    assert len(do_calls) == 2  # 原始请求 + 重登后重试各一次
    assert len(login_calls) == 2  # 首次登录 + 自动重登各一次


def test_do_relogin_retries_only_once(monkeypatch):
    """只重试一次：401 后重登再 401 → 不再重登，按既有语义抛 NasToolsUnavailable。

    防 401→重登→401 无限循环（重登限一次，审查 C10 边界）。
    """
    cli, fake = _configured_client(monkeypatch, [
        _login_ok("session=s1"),
        _do_unauthorized(401),
        _login_ok("session=s2"),
        _do_unauthorized(401),
    ])

    with pytest.raises(nastools.NasToolsUnavailable):
        run(cli._do("restart"))

    login_calls, do_calls = _split_calls(fake)
    assert len(do_calls) == 2  # 原始 + 一次重试；无第三次（循环终止）
    assert len(login_calls) == 2  # 首次 + 一次重登；无第二次重登


def test_do_relogin_failure_propagates(monkeypatch):
    """重登本身失败（login 抛）→ NasToolsUnavailable 上抛（既有语义）。

    会话已清除：下次调用将从登录重新开始（不再持续持失效 cookie 失败）。
    """
    cli, fake = _configured_client(monkeypatch, [
        _login_ok("session=s1"),
        _do_unauthorized(401),
        _login_fail(),
    ])

    with pytest.raises(nastools.NasToolsUnavailable, match="登录失败"):
        run(cli._do("restart"))

    assert cli._session_cookie is None  # 失效会话已被清除
