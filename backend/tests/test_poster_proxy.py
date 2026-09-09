"""海报代理端点单测（路由函数直调，user 传 fake；鉴权在 test_poster_auth.py）。"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

import app.services.poster as poster_mod
from app.routers import poster as poster_router

_USER = SimpleNamespace(id=1, role="user", username="u")


def test_router_rejects_invalid_path(monkeypatch):
    """端点对非法路径返回 400，且不发起对外请求。"""
    fetch = AsyncMock()
    monkeypatch.setattr(poster_mod, "fetch_poster", fetch)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(poster_router.get_poster(p="/t/p/../../etc/passwd", user=_USER))
    assert exc_info.value.status_code == 400
    fetch.assert_not_awaited()


# ---- 回源 ----

def _make_client_factory(resp, calls):
    """构造返回 FakeClient 的工厂；FakeClient.get 记录 url 并返回预设 resp。"""
    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def get(self, url):
            calls.append(url)
            return resp
    return lambda *a, **kw: FakeClient()


def test_fetch_poster_success(monkeypatch):
    calls: list[str] = []
    resp = SimpleNamespace(status_code=200, content=b"\xff\xd8jpg", headers={"content-type": "image/jpeg"})
    monkeypatch.setattr(poster_mod, "_client_factory", _make_client_factory(resp, calls))
    monkeypatch.setattr(poster_mod, "_POSTER_CACHE", {})
    monkeypatch.setattr(poster_mod, "_ALERT_COOLDOWN", {})
    content, ctype = asyncio.run(poster_mod.fetch_poster("/t/p/w500/x.jpg"))
    assert content == b"\xff\xd8jpg"
    assert ctype == "image/jpeg"
    assert calls == ["https://image.tmdb.org/t/p/w500/x.jpg"]


def test_fetch_poster_upstream_5xx_raises(monkeypatch):
    calls: list[str] = []
    resp = SimpleNamespace(status_code=500, content=b"", headers={})
    monkeypatch.setattr(poster_mod, "_client_factory", _make_client_factory(resp, calls))
    monkeypatch.setattr(poster_mod, "_POSTER_CACHE", {})
    monkeypatch.setattr(poster_mod, "_ALERT_COOLDOWN", {})
    with pytest.raises(Exception):
        asyncio.run(poster_mod.fetch_poster("/t/p/w500/x.jpg"))


def test_fetch_poster_network_error_raises(monkeypatch):
    class BoomClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def get(self, url):
            raise httpx.ConnectError("conn refused")
    monkeypatch.setattr(poster_mod, "_client_factory", lambda *a, **kw: BoomClient())
    monkeypatch.setattr(poster_mod, "_POSTER_CACHE", {})
    monkeypatch.setattr(poster_mod, "_ALERT_COOLDOWN", {})
    with pytest.raises(Exception):
        asyncio.run(poster_mod.fetch_poster("/t/p/w500/x.jpg"))


# ---- 节流告警 ----

def test_alert_cooldown_suppresses_repeat_warning(monkeypatch):
    """同 path 连续失败（时间未推进）只告警一次。"""
    calls: list[tuple] = []

    class Recorder:
        def warning(self, *a, **kw):
            calls.append(a)

    monkeypatch.setattr(poster_mod, "logger", Recorder())
    monkeypatch.setattr(poster_mod, "_ALERT_COOLDOWN", {})
    poster_mod._alert("/t/p/w500/x.jpg")
    poster_mod._alert("/t/p/w500/x.jpg")
    assert len(calls) == 1


def test_alert_fires_again_after_cooldown_expired(monkeypatch):
    """cooldown 超过 60s 后同 path 再次失败会再次告警。"""
    calls: list[tuple] = []

    class Recorder:
        def warning(self, *a, **kw):
            calls.append(a)

    monkeypatch.setattr(poster_mod, "logger", Recorder())
    monkeypatch.setattr(poster_mod, "_ALERT_COOLDOWN", {})
    poster_mod._alert("/t/p/w500/x.jpg")
    # 手动把 cooldown 推进到超过 60s（写入过去时间戳）
    poster_mod._ALERT_COOLDOWN["/t/p/w500/x.jpg"] = (
        time.monotonic() - poster_mod._POSTER_ALERT_TTL - 1
    )
    poster_mod._alert("/t/p/w500/x.jpg")
    assert len(calls) == 2
