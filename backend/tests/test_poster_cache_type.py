"""海报缓存淘汰（LRU）与回源响应 content-type 校验单测。

覆盖：
- 缓存满 100 后新条目淘汰最旧并写入（不再「满 100 实质失效」）
- 命中 move_to_end 后最近访问条目不被淘汰
- 上游 200 + text/html（错误页）→ 不写缓存、抛异常（路由映射 502）
- 上游 image/jpeg → 正常缓存返回；二次请求命中缓存
- content-type 带参数（image/jpeg; charset=...）前缀匹配仍视为图片
"""
import asyncio
import time
from collections import OrderedDict
from types import SimpleNamespace

import pytest

import app.services.poster as poster_mod

_MAX = poster_mod._POSTER_CACHE_MAX  # 100


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


def _prefill_full(monkeypatch):
    """预填缓存至满上限：/t/p/w0..w99 → (未过期, image/jpeg, b"x")。"""
    keys = [f"/t/p/w{i}.jpg" for i in range(_MAX)]
    cache = OrderedDict(
        (k, (time.monotonic() + 100, "image/jpeg", b"x")) for k in keys
    )
    monkeypatch.setattr(poster_mod, "_POSTER_CACHE", cache)
    monkeypatch.setattr(poster_mod, "_ALERT_COOLDOWN", {})
    return cache, keys


# ---- 缓存满淘汰（LRU）----


def test_cache_full_evicts_oldest_and_writes_new(monkeypatch):
    """缓存满 100 后新海报仍可写入：淘汰最旧条目，随后请求命中缓存不再回源。"""
    cache, keys = _prefill_full(monkeypatch)
    calls: list[str] = []
    resp = SimpleNamespace(status_code=200, content=b"new", headers={"content-type": "image/jpeg"})
    monkeypatch.setattr(poster_mod, "_client_factory", _make_client_factory(resp, calls))

    new_path = "/t/p/w500/new.jpg"
    content, ctype = asyncio.run(poster_mod.fetch_poster(new_path))
    assert content == b"new"
    assert ctype == "image/jpeg"
    assert len(cache) == _MAX                     # 仍不超上限
    assert cache.get(new_path) is not None        # 新条目已写入
    assert keys[0] not in cache                   # 最旧 /t/p/w0.jpg 被淘汰

    # 二次请求命中缓存，不再回源
    calls.clear()
    content2, _ = asyncio.run(poster_mod.fetch_poster(new_path))
    assert content2 == b"new"
    assert calls == []


def test_lru_recently_accessed_survives_eviction(monkeypatch):
    """命中（move_to_end）后最近访问条目在淘汰时保留，淘汰的是次旧条目。"""
    cache, keys = _prefill_full(monkeypatch)
    # 命中最旧条目 w0 → 移到最近端
    hit_content, _ = asyncio.run(poster_mod.fetch_poster(keys[0]))
    assert hit_content == b"x"
    calls: list[str] = []
    resp = SimpleNamespace(status_code=200, content=b"new", headers={"content-type": "image/jpeg"})
    monkeypatch.setattr(poster_mod, "_client_factory", _make_client_factory(resp, calls))

    asyncio.run(poster_mod.fetch_poster("/t/p/w500/new.jpg"))
    assert len(cache) == _MAX
    assert keys[0] in cache       # 刚被访问 → 不被淘汰
    assert keys[1] not in cache   # 次旧 w1 被淘汰


# ---- 回源响应 content-type 校验 ----


def test_html_response_not_cached_and_raises(monkeypatch):
    """上游 200 + text/html（如劫持/误配错误页）→ 不写缓存、抛异常（路由 502）。"""
    cache = OrderedDict()
    monkeypatch.setattr(poster_mod, "_POSTER_CACHE", cache)
    monkeypatch.setattr(poster_mod, "_ALERT_COOLDOWN", {})
    calls: list[str] = []
    resp = SimpleNamespace(
        status_code=200, content=b"<html>error page</html>",
        headers={"content-type": "text/html"},
    )
    monkeypatch.setattr(poster_mod, "_client_factory", _make_client_factory(resp, calls))

    with pytest.raises(Exception):
        asyncio.run(poster_mod.fetch_poster("/t/p/w500/bad.jpg"))
    assert cache.get("/t/p/w500/bad.jpg") is None  # 非图片不写缓存


def test_image_response_cached_and_returned(monkeypatch):
    """上游 200 + image/jpeg → 正常返回并写入缓存；二次请求命中。"""
    cache = OrderedDict()
    monkeypatch.setattr(poster_mod, "_POSTER_CACHE", cache)
    monkeypatch.setattr(poster_mod, "_ALERT_COOLDOWN", {})
    calls: list[str] = []
    resp = SimpleNamespace(status_code=200, content=b"\xff\xd8jpg", headers={"content-type": "image/jpeg"})
    monkeypatch.setattr(poster_mod, "_client_factory", _make_client_factory(resp, calls))

    content, ctype = asyncio.run(poster_mod.fetch_poster("/t/p/w500/ok.jpg"))
    assert content == b"\xff\xd8jpg"
    assert ctype == "image/jpeg"
    assert cache.get("/t/p/w500/ok.jpg") is not None

    calls.clear()
    content2, _ = asyncio.run(poster_mod.fetch_poster("/t/p/w500/ok.jpg"))
    assert content2 == b"\xff\xd8jpg"
    assert calls == []  # 命中缓存


def test_image_content_type_with_params_ok(monkeypatch):
    """content-type 带参数（image/jpeg; charset=...）前缀匹配仍视为图片并缓存。"""
    cache = OrderedDict()
    monkeypatch.setattr(poster_mod, "_POSTER_CACHE", cache)
    monkeypatch.setattr(poster_mod, "_ALERT_COOLDOWN", {})
    calls: list[str] = []
    resp = SimpleNamespace(
        status_code=200, content=b"\xff\xd8",
        headers={"content-type": "image/jpeg; charset=binary"},
    )
    monkeypatch.setattr(poster_mod, "_client_factory", _make_client_factory(resp, calls))

    content, ctype = asyncio.run(poster_mod.fetch_poster("/t/p/w500/param.jpg"))
    assert content == b"\xff\xd8"
    assert cache.get("/t/p/w500/param.jpg") is not None  # 视为图片写入缓存
