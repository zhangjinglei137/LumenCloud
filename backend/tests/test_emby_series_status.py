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


def _library_item(name, kind="Series", series_status=_MISSING, tmdb=1001):
    """构造 Emby 库 Item；series_status=_MISSING 表示 SeriesStatus 字段缺失。"""
    item = {
        "Id": f"id-{name}",
        "Name": name,
        "Type": kind,
        "ProviderIds": {"Tmdb": str(tmdb)},
        "ProductionYear": 2024,
    }
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