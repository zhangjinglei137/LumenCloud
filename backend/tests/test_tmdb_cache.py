"""TMDB 元数据缓存（P3）与出口代理双模式（P2-2）单测。

- 命中缓存不回源、超 7 天回源刷新、search_multi 逐条 upsert；
- httpx 出口代理参数形态（httpx==0.28.1 → 单数 proxy=）。
- 不连真实服务/数据库：mock httpx.AsyncClient 与 app.services.tmdb.async_session
  （TmdbCache 模型由并行 lane 提供——若尚未落盘，则整个模块 pytest.skip，
  代码正确性由最终统一验证兜底）。
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.services import config_store, tmdb as tmdb_mod

# TmdbCache 模型由并行 lane 提供（backend/app/models/__init__.py）。
# 未就绪时跳过本模块测试，不造成 import 失败。
try:
    from app.models import TmdbCache  # noqa: F401  (仅探测模型是否就绪)
    _MODEL_READY = True
    _MODEL_ERR = None
except Exception as exc:  # noqa: BLE001
    _MODEL_READY = False
    _MODEL_ERR = exc

if not _MODEL_READY:
    pytest.skip(
        f"TmdbCache 模型尚未落盘（并行 lane 未就绪）：{_MODEL_ERR}",
        allow_module_level=True,
    )


def run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# mock 工具
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


class _FakeClient:
    """兼容 timeout/proxy 关键字的 httpx.AsyncClient mock。"""

    def __init__(self, payload, calls=None):
        self._payload = payload
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None):
        if self._calls is not None:
            self._calls.append(url)
        return _FakeResp(self._payload)


def _make_http_factory(payload, calls=None):
    """构造 AsyncClient mock：收集调用 kwargs（含 proxy）到 calls。"""
    captured = []

    def factory(**kwargs):
        captured.append(kwargs)
        return _FakeClient(payload, calls=calls)

    return factory, captured


def _fake_sessionmaker(row_factory):
    """构造 async_session mock；row_factory() 动态返回 execute 的 scalar_one_or_none。

    SQLAlchemy AsyncSession.execute 是 async 方法，故用 AsyncMock。
    """
    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.side_effect = row_factory
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    maker = MagicMock()
    maker.return_value.__aenter__ = AsyncMock(return_value=session)
    maker.return_value.__exit__ = AsyncMock(return_value=False)
    return maker, session


def _cache_row(tmdb_id="42", media_type="movie", title="缓存标题",
               poster_path="/cached.jpg", year=2023, updated_at=None):
    """构造 tmdb_cache 命中行（SimpleNamespace 模拟 ORM 行）。"""
    return SimpleNamespace(
        tmdb_id=tmdb_id,
        media_type=media_type,
        title=title,
        poster_path=poster_path,
        year=year,
        updated_at=updated_at or _now(),
    )


# ---------------------------------------------------------------------------
# P2-2 出口代理双模式：httpx 参数形态
# ---------------------------------------------------------------------------

def test_client_kwargs_proxy_only_when_configured(monkeypatch):
    """httpx==0.28.1（>=0.26）→ 出口代理用单数 proxy=；未配置时省略该参数。"""
    monkeypatch.setattr(config_store, "_cache", {"tmdb_http_proxy": "http://127.0.0.1:7890"})
    kwargs = tmdb_mod._client_kwargs()
    assert kwargs["proxy"] == "http://127.0.0.1:7890"
    assert kwargs["timeout"] is not None

    monkeypatch.setattr(config_store, "_cache", {})
    monkeypatch.setattr(settings, "TMDB_HTTP_PROXY", "")
    kwargs = tmdb_mod._client_kwargs()
    assert "proxy" not in kwargs  # 零配置直连，保持 AsyncClient(timeout=...) 形态


def test_search_multi_passes_proxy_to_client(monkeypatch):
    """配置出口代理时，search_multi 的 AsyncClient 收到 proxy= 参数。"""
    monkeypatch.setattr(settings, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(config_store, "_cache", {"tmdb_http_proxy": "http://127.0.0.1:7890"})
    payload = {"results": [{"id": 1, "title": "电影A", "media_type": "movie",
                            "release_date": "2023-05-12", "poster_path": "/p1.jpg"}]}
    factory, captured = _make_http_factory(payload)
    maker, _ = _fake_sessionmaker(lambda: None)  # 缓存空 → upsert 新增
    monkeypatch.setattr("app.services.tmdb.httpx.AsyncClient", factory)
    monkeypatch.setattr(tmdb_mod, "async_session", maker)

    results = run(tmdb_mod.search_multi("测试"))
    assert [r["title"] for r in results] == ["电影A"]
    assert captured[0]["proxy"] == "http://127.0.0.1:7890"
    assert captured[0]["timeout"] is not None


# ---------------------------------------------------------------------------
# P3 元数据缓存：get_by_tmdb_id
# ---------------------------------------------------------------------------

def test_get_by_tmdb_id_cache_hit_no_refetch(monkeypatch):
    """命中缓存（updated_at 距今 < 7 天）→ 直接返回，不回源、不 upsert。"""
    monkeypatch.setattr(settings, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(config_store, "_cache", {})
    fresh = _cache_row()
    maker, session = _fake_sessionmaker(lambda: fresh)
    monkeypatch.setattr(tmdb_mod, "async_session", maker)

    # AsyncClient mock：若被调用则直接报错（证明未回源）
    def _explode(**kwargs):
        raise AssertionError("命中缓存不应回源请求 TMDB")
    monkeypatch.setattr("app.services.tmdb.httpx.AsyncClient", _explode)

    result = run(tmdb_mod.get_by_tmdb_id("42", "movie"))
    assert result == {
        "tmdb_id": "42",
        "title": "缓存标题",
        "media_type": "movie",
        "poster_path": "/cached.jpg",
        "year": "2023",
    }
    session.commit.assert_not_called()


def test_get_by_tmdb_id_stale_refetch_and_refresh(monkeypatch):
    """缓存超 7 天 → 回源刷新并 upsert（updated_at=now）。"""
    monkeypatch.setattr(settings, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(config_store, "_cache", {})
    stale = _cache_row(title="旧标题", poster_path="/old.jpg", year=2020,
                       updated_at=_now() - timedelta(days=8))
    maker, session = _fake_sessionmaker(lambda: stale)  # 读取命中 stale → 回源；upsert 更新同行
    monkeypatch.setattr(tmdb_mod, "async_session", maker)

    http_calls = []
    factory, _ = _make_http_factory(
        {"id": 42, "title": "新标题", "release_date": "2024-03-01", "poster_path": "/new.jpg"},
        calls=http_calls,
    )
    monkeypatch.setattr("app.services.tmdb.httpx.AsyncClient", factory)

    result = run(tmdb_mod.get_by_tmdb_id(42, "movie"))
    assert result == {
        "tmdb_id": "42",
        "title": "新标题",
        "media_type": "movie",
        "poster_path": "/new.jpg",
        "year": "2024",
    }
    assert len(http_calls) == 1 and "/3/movie/42" in http_calls[0]  # 确实回源
    session.commit.assert_called()  # upsert 落盘
    assert stale.title == "新标题"  # 缓存行被刷新


def test_get_by_tmdb_id_miss_refetch_and_insert(monkeypatch):
    """缓存未命中 → 回源 + 新增缓存行（tmdb_id 字符串化）。"""
    monkeypatch.setattr(settings, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(config_store, "_cache", {})
    maker, session = _fake_sessionmaker(lambda: None)  # 读取未命中 + upsert 未命中 → 新增
    monkeypatch.setattr(tmdb_mod, "async_session", maker)

    factory, _ = _make_http_factory(
        {"id": 7, "name": "剧集G", "first_air_date": "2021-01-01", "poster_path": None},
    )
    monkeypatch.setattr("app.services.tmdb.httpx.AsyncClient", factory)

    result = run(tmdb_mod.get_by_tmdb_id("7", "tv"))
    assert result["title"] == "剧集G" and result["year"] == "2021"
    assert result["poster_path"] is None
    session.add.assert_called_once()
    new_row = session.add.call_args.args[0]
    assert new_row.tmdb_id == "7" and new_row.media_type == "tv"
    assert new_row.year == 2021  # year 转 int 落库


def test_get_by_tmdb_id_tv_path(monkeypatch):
    """media_type=tv → 回源路径 /3/tv/{id}。"""
    monkeypatch.setattr(settings, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(config_store, "_cache", {})
    maker, _ = _fake_sessionmaker(lambda: None)
    monkeypatch.setattr(tmdb_mod, "async_session", maker)

    http_calls = []
    factory, _ = _make_http_factory(
        {"id": 9, "name": "剧集H", "first_air_date": "2022-06-15", "poster_path": "/h.jpg"},
        calls=http_calls,
    )
    monkeypatch.setattr("app.services.tmdb.httpx.AsyncClient", factory)

    result = run(tmdb_mod.get_by_tmdb_id(9, "tv"))
    assert result["media_type"] == "tv"
    assert "/3/tv/9" in http_calls[0]


# ---------------------------------------------------------------------------
# P3 元数据缓存：search_multi 逐条 upsert
# ---------------------------------------------------------------------------

def test_search_multi_upserts_each_hit(monkeypatch):
    """search_multi 对每个命中结果 upsert 缓存（tmdb_id 字符串化、year 落 int）。"""
    monkeypatch.setattr(settings, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(config_store, "_cache", {})
    maker, session = _fake_sessionmaker(lambda: None)  # 缓存空 → 每条新增
    monkeypatch.setattr(tmdb_mod, "async_session", maker)

    payload = {
        "results": [
            {"id": 1, "title": "电影A", "media_type": "movie",
             "release_date": "2023-05-12", "poster_path": "/p1.jpg"},
            {"id": 2, "name": "剧集B", "media_type": "tv",
             "first_air_date": "2021-01-01", "poster_path": "/p2.jpg"},
        ]
    }
    factory, _ = _make_http_factory(payload)
    monkeypatch.setattr("app.services.tmdb.httpx.AsyncClient", factory)

    results = run(tmdb_mod.search_multi("测试"))
    assert len(results) == 2
    # 每条命中各一次 upsert（新增 + commit）
    assert session.add.call_count == 2
    assert session.commit.call_count == 2
    added = [c.args[0] for c in session.add.call_args_list]
    tmdb_ids = sorted(str(r.tmdb_id) for r in added)
    assert tmdb_ids == ["1", "2"]  # tmdb_id 字符串化
    years = sorted(r.year for r in added)
    assert years == [2021, 2023]  # year 转 int 落库
