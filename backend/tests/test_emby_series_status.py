"""Emby 影视库 series_status 单测（服务层，stub _get，不连真实 Emby）。

Q12（P2）：list_library 的 Fields 请求显式带 SeriesStatus（Emby 默认不返回该
字段），_normalize_library_item 原样透传为 series_status（"continuing"/"ended"，
Movie 或无该字段 → None）；服务层不改变阈值。

- stub app.services.emby._get（AsyncMock）整体绕过 _check_config/_base_url 与网络调用；
- 注入 config_store._cache（emby_base_url/emby_api_key）双保险，防意外路径走真配置；
- _attach_in_media_flag 会走 async_session 查询 Media：用隔离 in-memory SQLite
  （StaticPool + Base.metadata.create_all，媒体表包含 Q12 新增的 series_status 列），
  monkeypatch emby_mod.async_session 注入；seed 无 Media 行 → 全部 in_media=False。
"""
import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  注册全部 ORM 模型
from app.database import Base
from app.services import config_store as cs
from app.services import emby as emby_mod

_MISSING = object()  # 哨兵：SeriesStatus 字段缺失


def run(coro):
    return asyncio.run(coro)


def _library_item(name, kind="Series", series_status=_MISSING, tmdb: int | None = 1001, poster=False):
    """构造 Emby 库 Item；series_status=_MISSING 表示 SeriesStatus 字段缺失。

    tmdb=None → 无 Tmdb ProviderId（无 tmdb_id 条目）；poster=True → 带海报
    （否则 tmdb_id 与海报双空的纯目录条目会被 _normalize_library_item 过滤）。
    """
    item = {
        "Id": f"id-{name}",
        "Name": name,
        "Type": kind,
        "ProviderIds": {"Tmdb": str(tmdb)} if tmdb is not None else {},
        "ProductionYear": 2024,
    }
    if poster:
        item["ImageTags"] = {"Primary": "poster"}
    if series_status is not _MISSING:
        item["SeriesStatus"] = series_status
    return item


@pytest.fixture()
def library_get(monkeypatch):
    """stub emby._get（AsyncMock）+ config_store._cache（双保险，防意外路径）。"""
    mock = AsyncMock()
    monkeypatch.setattr(emby_mod, "_get", mock)
    monkeypatch.setattr(cs, "_cache", {
        "emby_base_url": "http://emby.test",
        "emby_api_key": "test-key",
    })
    return mock


@pytest.fixture()
def _db_maker():
    """隔离 in-memory SQLite（StaticPool 共享连接），create_all 最新模型结构。"""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    run(_create())
    yield maker
    run(engine.dispose())


def _use_test_db(monkeypatch, maker):
    """_attach_in_media_flag 使用测试库的连接工厂（不触全局 app.database engine）。"""
    monkeypatch.setattr(emby_mod, "async_session", maker)


def test_series_status_transparent(library_get, _db_maker, monkeypatch):
    """Series 在更/完结/无该字段 + Movie 无该字段 → series_status 原样透传。"""
    _use_test_db(monkeypatch, _db_maker)
    library_get.return_value = {"Items": [
        _library_item("S1-continuing", series_status="continuing", tmdb=101),
        _library_item("S2-ended", series_status="ended", tmdb=102),
        _library_item("S3-nostatus", series_status=_MISSING, tmdb=103),
        _library_item("M1-movie", kind="Movie", series_status=_MISSING, tmdb=104),
    ]}

    result = run(emby_mod.list_library())

    assert [it["series_status"] for it in result] == ["continuing", "ended", None, None]
    assert [it["type"] for it in result] == ["series", "series", "series", "movie"]
    # 其余核心字段不破
    assert [it["title"] for it in result] == ["S1-continuing", "S2-ended", "S3-nostatus", "M1-movie"]


def test_request_includes_series_status_field(library_get, _db_maker, monkeypatch):
    """请求确实带 Fields 含 SeriesStatus（Emby 默认不返回该字段，须显式请求）。"""
    _use_test_db(monkeypatch, _db_maker)
    library_get.return_value = {"Items": [_library_item("S1", series_status="continuing")]}

    run(emby_mod.list_library())

    path, params = library_get.await_args.args[:2]
    assert path == "/Items"
    assert "SeriesStatus" in params["Fields"]


def test_status_filter_passed_through(library_get, _db_maker, monkeypatch):
    """status 筛选参数仍透传：_get 收到 SeriesStatus=continuing，且类型含 Series。"""
    _use_test_db(monkeypatch, _db_maker)
    library_get.return_value = {"Items": []}

    run(emby_mod.list_library(status="continuing"))

    path, params = library_get.await_args.args[:2]
    assert path == "/Items"
    assert params["SeriesStatus"] == "continuing"
    assert "Series" in params["IncludeItemTypes"]


# ---------------------------------------------------------------------------
# 连载判定 TMDB 优先（_attach_tmdb_series_status）
# ---------------------------------------------------------------------------

def _fake_tmdb_meta(tv_status):
    """构造 get_by_tmdb_id 返回值（判定只读 tv_status 字段）。"""
    return {"tv_status": tv_status}


def test_tmdb_priority_overrides_emby_status(library_get, _db_maker, monkeypatch):
    """TMDB 优先：continuing/缺失的 series 查 TMDB 覆盖；ended 与 movie 不查。"""
    _use_test_db(monkeypatch, _db_maker)
    library_get.return_value = {"Items": [
        _library_item("S1-continuing", series_status="continuing", tmdb=101),
        _library_item("S2-nostatus", series_status=_MISSING, tmdb=102),
        _library_item("S3-ended", series_status="ended", tmdb=103),
        _library_item("M1-movie", kind="Movie", tmdb=104),
    ]}

    called = []

    async def _fake_get(tmdb_id, media_type):
        called.append(str(tmdb_id))
        return _fake_tmdb_meta({"101": "Returning Series", "102": "Ended"}[str(tmdb_id)])

    monkeypatch.setattr(emby_mod, "get_by_tmdb_id", _fake_get)

    result = run(emby_mod.list_library())

    # Returning Series→continuing（覆盖）、Ended→ended（补充）、ended 信任 Emby、movie 不判
    assert [it["series_status"] for it in result] == ["continuing", "ended", "ended", None]
    assert sorted(called) == ["101", "102"]  # ended / movie 不发起 TMDB 调用


def test_tmdb_status_failure_falls_back_to_emby(library_get, _db_maker, monkeypatch):
    """TMDB 查询失败（TMDBUnavailable）→ 静默回退 Emby 值，不抛异常不阻塞。"""
    _use_test_db(monkeypatch, _db_maker)
    library_get.return_value = {"Items": [
        _library_item("S1-continuing", series_status="continuing", tmdb=101),
        _library_item("S2-nostatus", series_status=_MISSING, tmdb=102),
    ]}

    async def _boom(tmdb_id, media_type):
        raise emby_mod.TMDBUnavailable("TMDB_API_KEY 未配置")

    monkeypatch.setattr(emby_mod, "get_by_tmdb_id", _boom)

    result = run(emby_mod.list_library())  # 不应抛异常
    assert [it["series_status"] for it in result] == ["continuing", None]


def test_tmdb_priority_canceled_pilot_and_no_tmdb_id(library_get, _db_maker, monkeypatch):
    """Canceled→ended；Pilot 无映射保留 Emby 值；无 tmdb_id 条目不查 TMDB。"""
    _use_test_db(monkeypatch, _db_maker)
    library_get.return_value = {"Items": [
        _library_item("S1-canceled", series_status="continuing", tmdb=201),
        _library_item("S2-pilot", series_status="continuing", tmdb=202),
        _library_item("S3-no-tmdb", series_status="continuing", tmdb=None, poster=True),
    ]}

    async def _fake_get(tmdb_id, media_type):
        return _fake_tmdb_meta({"201": "Canceled", "202": "Pilot"}[str(tmdb_id)])

    monkeypatch.setattr(emby_mod, "get_by_tmdb_id", _fake_get)

    result = run(emby_mod.list_library())
    assert [it["series_status"] for it in result] == ["ended", "continuing", "continuing"]


# ---------------------------------------------------------------------------
# Task 8：refresh_library（POST /Library/Refresh 全库扫描触发）
# ---------------------------------------------------------------------------

class _FakePostResponse:
    """最小 fake httpx 响应（refresh_library 只读 status_code）。"""

    def __init__(self, status_code: int):
        self.status_code = status_code


class _FakePostClient:
    """最小 fake httpx.AsyncClient：记录 post 调用，返回可配置状态码。"""

    def __init__(self, status_code: int = 204):
        self.status_code = status_code
        self.calls: list[tuple[str, dict]] = []  # (url, params)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url: str, params: dict | None = None, **kwargs):
        self.calls.append((url, params or {}))
        return _FakePostResponse(self.status_code)


def test_refresh_library_posts_library_refresh_with_auth(monkeypatch):
    """refresh_library → POST {base}/Library/Refresh 且带 api_key 认证参数。"""
    client = _FakePostClient(status_code=204)
    monkeypatch.setattr(emby_mod.httpx, "AsyncClient", lambda **kw: client)
    monkeypatch.setattr(cs, "_cache", {
        "emby_base_url": "http://emby.test",
        "emby_api_key": "test-key",
    })

    result = run(emby_mod.refresh_library())

    assert result is None  # 成功返回 None（全库扫描异步执行，无响应体解析）
    assert len(client.calls) == 1
    url, params = client.calls[0]
    assert url == "http://emby.test/Library/Refresh"
    assert params.get("api_key") == "test-key"


def test_refresh_library_non_2xx_raises_emby_unavailable(monkeypatch):
    """refresh_library 非 2xx → 抛 EmbyUnavailable（调用方降级，轮询兜底）。"""
    client = _FakePostClient(status_code=500)
    monkeypatch.setattr(emby_mod.httpx, "AsyncClient", lambda **kw: client)
    monkeypatch.setattr(cs, "_cache", {
        "emby_base_url": "http://emby.test",
        "emby_api_key": "test-key",
    })

    with pytest.raises(emby_mod.EmbyUnavailable):
        run(emby_mod.refresh_library())
