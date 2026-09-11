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


# ---- Task 3：图床镜像（_base_url 镜像优先 + 误填防御）----

def test_base_url_prefers_mirror(monkeypatch):
    monkeypatch.setattr(
        "app.services.config_store._cache",
        {"tmdb_poster_proxy": "https://mirror.example.com"},
    )
    assert poster_mod._base_url() == "https://mirror.example.com"


def test_base_url_falls_back_to_official(monkeypatch):
    monkeypatch.setattr("app.services.config_store._cache", {})
    assert poster_mod._base_url() == "https://image.tmdb.org"


def test_base_url_rejects_proxy_port(monkeypatch):
    """误填科学上网代理端口（无 scheme 的 host:port）→ PosterUnavailable。"""
    monkeypatch.setattr(
        "app.services.config_store._cache",
        {"tmdb_poster_proxy": "192.168.3.31:7897"},
    )
    with pytest.raises(poster_mod.PosterUnavailable):
        poster_mod._base_url()


def test_mirror_path_requests(monkeypatch):
    """配置镜像后回源 URL 使用镜像根地址。"""
    monkeypatch.setattr(
        "app.services.config_store._cache",
        {"tmdb_poster_proxy": "https://mirror.example.com"},
    )
    monkeypatch.setattr(poster_mod, "_POSTER_CACHE", {})
    calls: list[str] = []
    resp = SimpleNamespace(status_code=200, content=b"img", headers={"content-type": "image/png"})
    monkeypatch.setattr(poster_mod, "_client_factory", _make_client_factory(resp, calls))
    asyncio.run(poster_mod.fetch_poster("/t/p/w500/x.png"))
    assert calls == ["https://mirror.example.com/t/p/w500/x.png"]


def test_settings_whitelist_contains_poster_proxy():
    from app.routers.settings import _WHITELIST_EXACT
    assert "tmdb_poster_proxy" in _WHITELIST_EXACT


def test_config_has_poster_proxy_field():
    from app.config import settings as s
    assert hasattr(s, "TMDB_POSTER_PROXY")


# ---- Task 6：内存 TTL 缓存 ----

def _fetch_calls_with_factory(monkeypatch, resp, calls):
    monkeypatch.setattr(poster_mod, "_client_factory", _make_client_factory(resp, calls))


def test_cache_hit_skips_upstream(monkeypatch):
    monkeypatch.setattr(
        poster_mod, "_POSTER_CACHE",
        {"/t/p/w500/x.jpg": (time.monotonic() + 100, "image/jpeg", b"cached")},
    )
    calls: list[str] = []
    resp = SimpleNamespace(status_code=200, content=b"fresh", headers={"content-type": "image/jpeg"})
    monkeypatch.setattr(poster_mod, "_client_factory", _make_client_factory(resp, calls))
    content, ctype = asyncio.run(poster_mod.fetch_poster("/t/p/w500/x.jpg"))
    assert content == b"cached"
    assert calls == []  # 未触发回源


def test_cache_expired_refetches(monkeypatch):
    monkeypatch.setattr(
        poster_mod, "_POSTER_CACHE",
        {"/t/p/w500/x.jpg": (time.monotonic() - 1, "image/jpeg", b"stale")},
    )
    calls: list[str] = []
    resp = SimpleNamespace(status_code=200, content=b"fresh", headers={"content-type": "image/jpeg"})
    monkeypatch.setattr(poster_mod, "_client_factory", _make_client_factory(resp, calls))
    content, ctype = asyncio.run(poster_mod.fetch_poster("/t/p/w500/x.jpg"))
    assert content == b"fresh"
    assert calls == ["https://image.tmdb.org/t/p/w500/x.jpg"]


def test_cache_cap_drops_writes(monkeypatch):
    monkeypatch.setattr(
        poster_mod, "_POSTER_CACHE",
        {f"/t/p/w{k}.jpg": (time.monotonic() + 100, "image/jpeg", b"x") for k in range(poster_mod._POSTER_CACHE_MAX)},
    )
    calls: list[str] = []
    resp = SimpleNamespace(status_code=200, content=b"fresh", headers={"content-type": "image/jpeg"})
    monkeypatch.setattr(poster_mod, "_client_factory", _make_client_factory(resp, calls))
    asyncio.run(poster_mod.fetch_poster("/t/p/w500/overflow.jpg"))
    assert poster_mod._POSTER_CACHE.get("/t/p/w500/overflow.jpg") is None  # 未写入


# ---- Task 4：Emby 封面代理（/emby/<itemId>/Primary 前缀）----


def test_validate_emby_path_ok():
    assert poster_mod._validate_poster_path("/emby/abc-123/Primary") is True
    assert poster_mod._validate_poster_path("/emby/9f2c1a4b-0000-4c3e-8f7d-1234567890ab/Primary") is True


def test_validate_emby_path_rejects():
    # 非法字符 / 路径穿越 / 协议段 / 多余路径段 / 空 id / 非 Primary 后缀
    for bad in [
        "/emby/../x/Primary",
        "/emby/a/b/Primary",        # 多路径段
        "/emby/a/Primary/x",        # 多余后缀
        "/emby/a/Primary/../x",
        "/emby/",                    # 空 id
        "/emby/abc/Backdrop",        # 非 Primary
        "/emby/ab cd/Primary",       # 空白
        "/emby/ab;cd/Primary",       # 分号
        "/emby/ab%2Fcd/Primary",     # 编码斜杠（FastAPI 已解码一次 → %2F → /）
        "emby/a/Primary",            # 缺前导斜杠
        "/emby//Primary",            # 空段
    ]:
        assert poster_mod._validate_poster_path(bad) is False, bad


def test_fetch_poster_emby_success(monkeypatch):
    calls: list[str] = []
    resp = SimpleNamespace(status_code=200, content=b"\xff\xd8jpg", headers={"content-type": "image/jpeg"})
    monkeypatch.setattr(poster_mod, "_client_factory", _make_client_factory(resp, calls))
    monkeypatch.setattr(poster_mod, "_POSTER_CACHE", {})
    monkeypatch.setattr(poster_mod, "_ALERT_COOLDOWN", {})
    monkeypatch.setattr(
        "app.services.config_store._cache",
        {"emby_base_url": "http://emby.test", "emby_api_key": "k123"},
    )
    content, ctype = asyncio.run(poster_mod.fetch_poster("/emby/abc-123/Primary"))
    assert content == b"\xff\xd8jpg"
    assert ctype == "image/jpeg"
    assert calls == ["http://emby.test/Items/abc-123/Images/Primary?api_key=k123"]


def test_fetch_poster_emby_not_configured(monkeypatch):
    # 隔离本机 .env 注入的真实 Emby 配置：_cache 置空后 config_store.get 会回退
    # settings.EMBY_BASE_URL / EMBY_API_KEY，须一并置空才能表达「未配置」语义
    monkeypatch.setattr(poster_mod.settings, "EMBY_BASE_URL", "")
    monkeypatch.setattr(poster_mod.settings, "EMBY_API_KEY", "")
    monkeypatch.setattr("app.services.config_store._cache", {})
    monkeypatch.setattr(poster_mod, "_POSTER_CACHE", {})
    with pytest.raises(poster_mod.PosterUnavailable):
        asyncio.run(poster_mod.fetch_poster("/emby/abc-123/Primary"))


def test_fetch_poster_emby_cache_isolated_from_tmdb(monkeypatch):
    """/emby/ 与 /t/p/ 缓存 key 天然隔离：emby 命中不回源、tmdb 不误伤。"""
    monkeypatch.setattr(
        poster_mod, "_POSTER_CACHE",
        {"/emby/abc-123/Primary": (time.monotonic() + 100, "image/jpeg", b"emby-cached")},
    )
    calls: list[str] = []
    resp = SimpleNamespace(status_code=200, content=b"tmdb-fresh", headers={"content-type": "image/jpeg"})
    monkeypatch.setattr(poster_mod, "_client_factory", _make_client_factory(resp, calls))
    content, ctype = asyncio.run(poster_mod.fetch_poster("/emby/abc-123/Primary"))
    assert content == b"emby-cached"
    assert calls == []


# ---- Task 3（fix-online-issues）：生产构造的 Emby 代理路径须通过校验 ----

def test_production_poster_url_passes_validation():
    """生产构造的 Emby 代理路径必须通过校验（回归：缺前导 / 曾导致全部 400）。"""
    url = f"/api/poster?p=/emby/{'abc-123'}/Primary"
    p = url.split("p=", 1)[1]
    assert poster_mod._validate_poster_path(p) is True


def test_normalized_poster_url_passes_validation():
    """_normalize_library_item 产出的 poster_url 的 p 参数必须通过校验。

    回归：poster_url 曾缺前导斜杠（p=emby/...），而 _validate_poster_path
    要求 p 以 /emby/ 开头 → 全部图片 400。此测试直接调用生产函数，修复前必失败。
    """
    from app.services import emby as emby_mod

    item = {
        "Id": "abc-123",
        "Name": "回归测试",
        "Type": "Movie",
        "ProviderIds": {"Tmdb": "11"},
        "ImageTags": {"Primary": "poster"},
    }
    result = emby_mod._normalize_library_item(item, "http://emby.test", server_id="srv1")
    assert result is not None
    p = result["poster_url"].split("p=", 1)[1]
    assert poster_mod._validate_poster_path(p) is True
