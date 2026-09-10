"""TMDB 元数据缓存（P3）与出口代理双模式（P2-2）单测。

- 命中缓存不回源、超 7 天回源刷新、search_multi 逐条 upsert；
- httpx 出口代理参数形态（httpx==0.28.1 → 单数 proxy=）。
- 不连真实服务/数据库：mock httpx.AsyncClient 与 app.services.tmdb.async_session
  （TmdbCache 模型由并行 lane 提供——若尚未落盘，则整个模块 pytest.skip，
  代码正确性由最终统一验证兜底）。
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
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
               poster_path="/cached.jpg", year=2023, tv_status=None,
               number_of_episodes=None, aliases=None, updated_at=None):
    """构造 tmdb_cache 命中行（SimpleNamespace 模拟 ORM 行）。"""
    return SimpleNamespace(
        tmdb_id=tmdb_id,
        media_type=media_type,
        title=title,
        poster_path=poster_path,
        year=year,
        tv_status=tv_status,
        number_of_episodes=number_of_episodes,
        aliases=aliases,
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
        "tv_status": None,
        "status": None,
        "number_of_episodes": None,
        "aliases": [],  # 缓存行无别名 → 空列表
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
        "tv_status": None,  # movie 响应无 status 字段 → None
        "status": None,  # 同 tv_status：movie 无 status → None
        "number_of_episodes": None,  # movie 响应无该字段 → None
        "aliases": [],  # 响应无 original_title/also_known_as → 空列表
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
    assert result["tv_status"] is None  # 响应无 status 字段 → None
    session.add.assert_called_once()
    new_row = session.add.call_args.args[0]
    assert new_row.tmdb_id == "7" and new_row.media_type == "tv"
    assert new_row.year == 2021  # year 转 int 落库
    assert new_row.tv_status is None


def test_get_by_tmdb_id_tv_status_parsed_and_cached(monkeypatch):
    """tv 条目回源解析 status 字段并落缓存（连载判定 TMDB 优先的数据基础）。"""
    monkeypatch.setattr(settings, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(config_store, "_cache", {})
    maker, session = _fake_sessionmaker(lambda: None)  # 缓存未命中 → 回源新增
    monkeypatch.setattr(tmdb_mod, "async_session", maker)

    factory, _ = _make_http_factory(
        {"id": 13, "name": "剧集J", "first_air_date": "2023-01-01",
         "poster_path": "/j.jpg", "status": "Returning Series"},
    )
    monkeypatch.setattr("app.services.tmdb.httpx.AsyncClient", factory)

    result = run(tmdb_mod.get_by_tmdb_id("13", "tv"))
    assert result["tv_status"] == "Returning Series"
    new_row = session.add.call_args.args[0]
    assert new_row.tv_status == "Returning Series"  # tv_status 落库（非 None 才覆盖）


def test_get_by_tmdb_id_number_of_episodes_parsed_and_cached(monkeypatch):
    """tv 条目回源解析 number_of_episodes 并落缓存（全量模式集号范围校验 A3 数据基础）。"""
    monkeypatch.setattr(settings, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(config_store, "_cache", {})
    maker, session = _fake_sessionmaker(lambda: None)  # 缓存未命中 → 回源新增
    monkeypatch.setattr(tmdb_mod, "async_session", maker)

    factory, _ = _make_http_factory(
        {"id": 14, "name": "剧集K", "first_air_date": "2023-01-01",
         "poster_path": "/k.jpg", "number_of_episodes": 48},
    )
    monkeypatch.setattr("app.services.tmdb.httpx.AsyncClient", factory)

    result = run(tmdb_mod.get_by_tmdb_id("14", "tv"))
    assert result["number_of_episodes"] == 48
    new_row = session.add.call_args.args[0]
    assert new_row.number_of_episodes == 48  # 非 None 才覆盖


def test_get_by_tmdb_id_cache_hit_returns_number_of_episodes(monkeypatch):
    """缓存命中 → 返回 number_of_episodes（tv 缓存行有值）。"""
    monkeypatch.setattr(settings, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(config_store, "_cache", {})
    fresh = _cache_row(media_type="tv", number_of_episodes=48)
    maker, _ = _fake_sessionmaker(lambda: fresh)
    monkeypatch.setattr(tmdb_mod, "async_session", maker)

    def _explode(**kwargs):
        raise AssertionError("命中缓存不应回源请求 TMDB")
    monkeypatch.setattr("app.services.tmdb.httpx.AsyncClient", _explode)

    result = run(tmdb_mod.get_by_tmdb_id("42", "tv"))
    assert result["number_of_episodes"] == 48


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


# ---------------------------------------------------------------------------
# get_tv_all_episodes：TV 全部正片季每集信息（详情页「TMDB 全集」数据源）
# ---------------------------------------------------------------------------

class _RoutedFakeClient:
    """按 URL 包含的子串分发 payload 的 AsyncClient mock（tv 详情 + 各季接口）。"""

    def __init__(self, routes, calls=None):
        self._routes = routes
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None):
        if self._calls is not None:
            self._calls.append(url)
        # 季接口优先（URL 含 /season/N）；tv 详情仅当 URL 以路由结尾精确命中，
        # 避免 "/3/tv/42" 子串误匹配到 "/3/tv/42/season/1"。
        for suffix, payload in self._routes.items():
            if "/season/" in suffix and suffix in url:
                return _FakeResp(payload)
        for suffix, payload in self._routes.items():
            if "/season/" not in suffix and url.rstrip("/").endswith(suffix):
                return _FakeResp(payload)
        return _FakeResp({"error": "unexpected"})


def _make_routed_factory(routes, calls=None):
    def factory(**kwargs):
        return _RoutedFakeClient(routes, calls=calls)

    return factory


def _tv_detail_payload(seasons):
    return {
        "id": 42,
        "name": "测试剧",
        "seasons": [{"season_number": sn, "episode_count": 2} for sn in seasons],
    }


def _season_payload(eps):
    return {"id": 42, "episodes": eps}


def test_get_tv_all_episodes_aggregates_filters_and_sorts(monkeypatch):
    """seasons 过滤 season_number==0（特辑/预告）；逐季聚合 episode/air_date/name；
    结果按 season、episode 升序。"""
    monkeypatch.setattr(settings, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(config_store, "_cache", {})
    routes = {
        "/3/tv/42": _tv_detail_payload([0, 1, 2]),
        "/season/1": _season_payload([
            {"episode_number": 2, "air_date": "2026-03-05", "name": "第2集"},
            {"episode_number": 1, "air_date": None, "name": "第1集"},
        ]),
        "/season/2": _season_payload([
            {"episode_number": 1, "air_date": "2026-04-01", "name": None},
        ]),
    }
    calls = []
    factory = _make_routed_factory(routes, calls=calls)
    monkeypatch.setattr("app.services.tmdb.httpx.AsyncClient", factory)

    result = run(tmdb_mod.get_tv_all_episodes(42))
    assert result == [
        {"season": 1, "episode": 1, "air_date": None, "name": "第1集"},
        {"season": 1, "episode": 2, "air_date": "2026-03-05", "name": "第2集"},
        {"season": 2, "episode": 1, "air_date": "2026-04-01", "name": None},
    ]
    # 请求数 = 1(tv 详情) + 2(正片季)；season 0 不回源
    assert len(calls) == 3
    assert not any("/season/0" in u for u in calls)


def test_get_tv_all_episodes_cache_hit_skips_refetch(monkeypatch):
    """进程内 TTL 缓存：第二次调用命中缓存，不再回源。"""
    monkeypatch.setattr(settings, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(config_store, "_cache", {})
    routes = {
        "/3/tv/430": _tv_detail_payload([1]),
        "/season/1": _season_payload([
            {"episode_number": 1, "air_date": "2026-03-05", "name": "第1集"},
        ]),
    }
    calls = []
    factory = _make_routed_factory(routes, calls=calls)
    monkeypatch.setattr("app.services.tmdb.httpx.AsyncClient", factory)

    first = run(tmdb_mod.get_tv_all_episodes(430))
    assert len(calls) == 2  # tv 详情 + 季 1
    second = run(tmdb_mod.get_tv_all_episodes(430))
    assert second == first
    assert len(calls) == 2  # 缓存命中，无新增请求


def test_get_tv_all_episodes_no_api_key_returns_empty(monkeypatch):
    """TMDB_API_KEY 缺失 → log warning 并返回 []，绝不抛异常。"""
    monkeypatch.setattr(settings, "TMDB_API_KEY", "")
    monkeypatch.setattr(config_store, "_cache", {})

    def _explode(**kwargs):
        raise AssertionError("api_key 缺失时不应发起任何请求")

    monkeypatch.setattr("app.services.tmdb.httpx.AsyncClient", _explode)
    assert run(tmdb_mod.get_tv_all_episodes(431)) == []


def test_get_tv_all_episodes_non_200_returns_empty(monkeypatch):
    """tv 详情非 200 → log warning 并返回 []。"""
    monkeypatch.setattr(settings, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(config_store, "_cache", {})

    class _ErrResp:
        status_code = 500

        def json(self):
            return {}

    class _ErrClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, params=None):
            return _ErrResp()

    monkeypatch.setattr(
        "app.services.tmdb.httpx.AsyncClient", lambda **kw: _ErrClient()
    )
    assert run(tmdb_mod.get_tv_all_episodes(432)) == []


def test_get_tv_all_episodes_network_error_returns_empty(monkeypatch):
    """回源网络异常 → log warning 并返回 []（单季失败仅跳过该季）。"""
    monkeypatch.setattr(settings, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(config_store, "_cache", {})

    class _BoomClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, params=None):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(
        "app.services.tmdb.httpx.AsyncClient", lambda **kw: _BoomClient()
    )
    assert run(tmdb_mod.get_tv_all_episodes(433)) == []


# ---------------------------------------------------------------------------
# episode_info_cache 集信息缓存（episode-status-cache）
# ---------------------------------------------------------------------------
# 测试模式与 test_library_check.py 一致：同步 def test_ + run() 包装 +
# in-memory SQLite（StaticPool）db fixture + monkeypatch tmdb_mod.async_session。
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  注册全部 ORM 模型
from app.models import EpisodeInfoCache  # noqa: F401


@pytest.fixture()
def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    maker.engine = engine  # 供列结构检查（inspect）访问实际建表引擎

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    run(_create())
    yield maker
    run(engine.dispose())


def _seed_episode_info(db, rows):
    async def _do():
        async with db() as s:
            for r in rows:
                s.add(EpisodeInfoCache(**r))
            await s.commit()
    run(_do())


def test_get_episode_info_empty_cache_returns_empty_list(db, monkeypatch):
    monkeypatch.setattr(tmdb_mod, "async_session", db)
    result = run(tmdb_mod.get_episode_info(12345))
    assert result == []


def test_get_episode_info_returns_sorted_episodes(db, monkeypatch):
    monkeypatch.setattr(tmdb_mod, "async_session", db)
    _seed_episode_info(db, [
        {"tmdb_id": 9, "season": 1, "episode": 2, "name": "Ep2", "air_date": "2026-01-02"},
        {"tmdb_id": 9, "season": 1, "episode": 1, "name": "Ep1", "air_date": "2026-01-01"},
    ])
    result = run(tmdb_mod.get_episode_info(9))
    assert [(r["season"], r["episode"]) for r in result] == [(1, 1), (1, 2)]
    assert result[0]["name"] == "Ep1"


def test_refresh_episode_info_upserts_and_counts(db, monkeypatch):
    monkeypatch.setattr(tmdb_mod, "async_session", db)
    async def fake_all(tmdb_id):
        return [
            {"season": 1, "episode": 1, "air_date": "2026-01-01", "name": "A"},
            {"season": 1, "episode": 2, "air_date": "2026-01-08", "name": "B"},
        ]
    monkeypatch.setattr(tmdb_mod, "get_tv_all_episodes", fake_all)
    n = run(tmdb_mod.refresh_episode_info(10))
    assert n == 2
    async def _count():
        async with db() as s:
            return len((await s.execute(select(EpisodeInfoCache).where(EpisodeInfoCache.tmdb_id == 10))).scalars().all())
    assert run(_count()) == 2


def test_refresh_episode_info_empty_preserves_old(db, monkeypatch):
    monkeypatch.setattr(tmdb_mod, "async_session", db)
    _seed_episode_info(db, [{"tmdb_id": 11, "season": 1, "episode": 1, "name": "Old", "air_date": "2026-01-01"}])
    async def fake_empty(tmdb_id):
        return []
    monkeypatch.setattr(tmdb_mod, "get_tv_all_episodes", fake_empty)
    n = run(tmdb_mod.refresh_episode_info(11))
    assert n == 0
    async def _count():
        async with db() as s:
            return len((await s.execute(select(EpisodeInfoCache).where(EpisodeInfoCache.tmdb_id == 11))).scalars().all())
    assert run(_count()) == 1


# ---------------------------------------------------------------------------
# aliases 列（tmdb-alias-search-match：别名持久化数据基础）
# ---------------------------------------------------------------------------

def test_tmdb_cache_aliases_column(db):
    """TmdbCache 含可空 aliases 列（迁移后）。

    inspect 作用于 db.engine（async_sessionmaker 本身不可被 inspect，
    由 db fixture 挂载的 engine 属性提供实际建表引擎）。
    """
    from sqlalchemy import inspect

    async def _cols():
        async with db.engine.connect() as conn:
            return await conn.run_sync(
                lambda sc: {c["name"] for c in inspect(sc).get_columns("tmdb_cache")}
            )

    cols = run(_cols())
    assert "aliases" in cols


# ---------------------------------------------------------------------------
# aliases 归一化与落库（tmdb-alias-search-match：get_by_tmdb_id 详情回源解析）
# ---------------------------------------------------------------------------

def test_normalize_aliases_lowercase_strips_and_dedups():
    """归一化：original_title 前置、小写、去空格、去括号版本后缀、去重。"""
    raw = ["Soul Land", "  Dou Luo Da Lu  ", "Soul Land", "斗罗大陆 (2020)", "", None]
    out = tmdb_mod._normalize_aliases(raw, "  Soul Land (2018) ")
    assert out == ["soulland", "douluodalu", "斗罗大陆"]  # 重复项跳过，括号年份去除


def test_normalize_aliases_caps_at_20():
    """上限 20 条防膨胀：超量别名时截断。"""
    raw = [f"alias-{i}" for i in range(30)]
    out = tmdb_mod._normalize_aliases(raw, "origin")
    assert len(out) == 20


def test_get_by_tmdb_id_parses_aliases_and_caches(monkeypatch):
    """tv 详情回源时 original_name + also_known_as 归一化落库（JSON 数组字符串）并随返回带出。"""
    monkeypatch.setattr(settings, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(config_store, "_cache", {})
    maker, session = _fake_sessionmaker(lambda: None)  # 缓存未命中 → 回源新增
    monkeypatch.setattr(tmdb_mod, "async_session", maker)

    factory, _ = _make_http_factory({
        "id": 123, "name": "斗罗大陆", "original_name": "Soul Land",
        "also_known_as": ["Soul Land", "Douluo Dalu", "斗罗大陆", "Soul Land (2020)"],
        "first_air_date": "2018-01-20", "status": "Returning Series",
        "number_of_episodes": 263,
    })
    monkeypatch.setattr("app.services.tmdb.httpx.AsyncClient", factory)

    result = run(tmdb_mod.get_by_tmdb_id(123, "tv"))
    assert "soulland" in result["aliases"]
    assert "douluodalu" in result["aliases"]
    assert "斗罗大陆" in result["aliases"]
    assert result["aliases"].count("soulland") == 1  # original_title + raw 同值去重
    assert not any("(2020)" in a for a in result["aliases"])  # 括号版本后缀已去除

    new_row = session.add.call_args.args[0]
    assert json.loads(new_row.aliases) == result["aliases"]  # JSON 数组字符串落库


def test_get_by_tmdb_id_movie_aliases_from_original_title(monkeypatch):
    """movie 详情回源用 original_title 作为别名来源（tv 才用 original_name）。"""
    monkeypatch.setattr(settings, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(config_store, "_cache", {})
    maker, session = _fake_sessionmaker(lambda: None)
    monkeypatch.setattr(tmdb_mod, "async_session", maker)

    factory, _ = _make_http_factory({
        "id": 456, "title": "流浪地球", "original_title": "The Wandering Earth",
        "also_known_as": ["Wandering Earth"], "release_date": "2019-02-05",
    })
    monkeypatch.setattr("app.services.tmdb.httpx.AsyncClient", factory)

    result = run(tmdb_mod.get_by_tmdb_id("456", "movie"))
    assert result["aliases"][0] == "thewanderingearth"  # original_title 前置且归一化
    assert "wanderingearth" in result["aliases"]


def test_get_by_tmdb_id_no_alias_sources_returns_empty(monkeypatch):
    """响应无 original_*/also_known_as → aliases 空列表（不报错、空列表落库）。"""
    monkeypatch.setattr(settings, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(config_store, "_cache", {})
    maker, session = _fake_sessionmaker(lambda: None)
    monkeypatch.setattr(tmdb_mod, "async_session", maker)

    factory, _ = _make_http_factory({
        "id": 7, "name": "剧集G", "first_air_date": "2021-01-01",
    })
    monkeypatch.setattr("app.services.tmdb.httpx.AsyncClient", factory)

    result = run(tmdb_mod.get_by_tmdb_id("7", "tv"))
    assert result["aliases"] == []
    new_row = session.add.call_args.args[0]
    assert new_row.aliases == "[]"  # 详情回源是权威来源，空也对（覆盖旧值）


def test_search_multi_does_not_overwrite_aliases(monkeypatch):
    """search_multi upsert 传 aliases=None → 不覆盖已落库的别名（回退保护）。"""
    monkeypatch.setattr(settings, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(config_store, "_cache", {})
    existing = _cache_row(tmdb_id="42", media_type="tv", aliases='["soulland"]')
    maker, session = _fake_sessionmaker(lambda: existing)
    monkeypatch.setattr(tmdb_mod, "async_session", maker)

    payload = {"results": [{"id": 42, "name": "剧集B", "media_type": "tv",
                            "first_air_date": "2021-01-01", "poster_path": "/p2.jpg"}]}
    factory, _ = _make_http_factory(payload)
    monkeypatch.setattr("app.services.tmdb.httpx.AsyncClient", factory)

    run(tmdb_mod.search_multi("测试"))
    assert existing.aliases == '["soulland"]'  # 别名保持已落库值
    assert session.commit.called
