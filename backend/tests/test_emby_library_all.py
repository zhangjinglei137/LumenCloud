"""list_all_library（全部影视类库聚合 / GET /api/emby/library/all 服务层）测试。

mock 契约：/Users → 首用户；/Users/user1/Views → 影视类库；/System/Info/Public → serverId；
/Items → 按 params["ParentId"] 分发各库条目（对齐 test_emby_library_folders 既有模式）。
"""
import asyncio
from typing import Any, Callable, Optional

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

import app.services.emby as emby_mod
from app.database import Base
from app.services.emby import EmbyUnavailable, list_all_library


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def _db_maker():
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
    monkeypatch.setattr(emby_mod, "async_session", maker)


def _install_get(monkeypatch: pytest.MonkeyPatch, handler: Callable):
    async def _fake_get(path: str, params: dict[str, Any], **kwargs):
        result = handler(path, params)
        if asyncio.iscoroutine(result):
            return await result
        return result
    monkeypatch.setattr(emby_mod, "_get", _fake_get)


def _set_cache(monkeypatch):
    """注入 config_store._cache（emby_base_url/emby_api_key）防全量套件残留误判（既有约定）。"""
    monkeypatch.setattr(emby_mod.config_store, "_cache", {
        "emby_base_url": "http://emby.test",
        "emby_api_key": "test-key",
    })


def _reset_user_id(monkeypatch):
    """重置模块级惰性缓存（UserId），防跨用例串缓存（对齐 test_emby_library_folders 既有约定）。"""
    monkeypatch.setattr(emby_mod, "_USER_ID", None)
    monkeypatch.setattr(emby_mod, "_USER_ID_LOADED", False)


def _reset_server_id(monkeypatch):
    """重置模块级惰性缓存（serverId），防跨用例串缓存（对齐 test_emby_server_id 既有约定）。"""
    monkeypatch.setattr(emby_mod, "_SERVER_ID", None)
    monkeypatch.setattr(emby_mod, "_SERVER_ID_LOADED", False)


def _item(name, kind="Series", tmdb=1001):
    return {
        "Id": f"id-{name}",
        "Name": name,
        "Type": kind,
        "ProductionYear": 2024,
        "ProviderIds": {"Tmdb": str(tmdb)},
        "ImageTags": {"Primary": "x"},
        "SeriesStatus": None,
    }


def _handler(items_by_lib: dict[str, list[dict]], folders: Optional[list[dict]] = None):
    """按库分发 /Items 的桩；folders 缺省为 movies/tvshows/mixed 三类各一个。"""
    if folders is None:
        folders = [
            {"Id": "m1", "Name": "电影", "CollectionType": "movies"},
            {"Id": "t1", "Name": "剧集", "CollectionType": "tvshows"},
            {"Id": "x1", "Name": "混合", "CollectionType": "mixed"},
        ]

    def _h(path, params):
        if path == "/Users":
            return [{"Id": "user1", "Name": "admin"}]
        if path == "/Users/user1/Views":
            return {"Items": folders}
        if path == "/System/Info/Public":
            return {"Id": "srv1"}
        if path == "/Items":
            return {"Items": items_by_lib.get(params.get("ParentId"), [])}
        raise AssertionError(f"unexpected path: {path}")
    return _h


def test_all_aggregates_movies_tvshows_mixed(monkeypatch, _db_maker):
    _reset_user_id(monkeypatch)
    _reset_server_id(monkeypatch)
    _use_test_db(monkeypatch, _db_maker)
    _set_cache(monkeypatch)
    _install_get(monkeypatch, _handler({
        "m1": [_item("M", "Movie", 11)],
        "t1": [_item("T", "Series", 22)],
        "x1": [_item("X", "Movie", 33)],
    }))
    result = run(list_all_library())
    ids = sorted(i["emby_id"] for i in result)
    assert ids == ["id-M", "id-T", "id-X"]  # movies/tvshows/mixed 三类库条目全部聚合


def test_all_dedupes_by_emby_id(monkeypatch, _db_maker):
    _reset_user_id(monkeypatch)
    _reset_server_id(monkeypatch)
    _use_test_db(monkeypatch, _db_maker)
    _set_cache(monkeypatch)
    _install_get(monkeypatch, _handler({
        "m1": [_item("dup", "Movie", 11)],
        "t1": [_item("dup", "Series", 11)],  # 同 emby_id 跨库 → 去重保留先到者
    }))
    result = run(list_all_library())
    assert len(result) == 1
    assert result[0]["emby_id"] == "id-dup"


def test_all_no_libraries_returns_empty(monkeypatch, _db_maker):
    _reset_user_id(monkeypatch)
    _reset_server_id(monkeypatch)
    _use_test_db(monkeypatch, _db_maker)
    _set_cache(monkeypatch)
    _install_get(monkeypatch, _handler({}, folders=[]))
    assert run(list_all_library()) == []


def test_all_partial_failure_keeps_success(monkeypatch, _db_maker):
    _reset_user_id(monkeypatch)
    _reset_server_id(monkeypatch)
    _use_test_db(monkeypatch, _db_maker)
    _set_cache(monkeypatch)

    def _h(path, params):
        if path == "/Users":
            return [{"Id": "user1", "Name": "admin"}]
        if path == "/Users/user1/Views":
            return {"Items": [{"Id": "m1", "Name": "电影", "CollectionType": "movies"},
                              {"Id": "t1", "Name": "剧集", "CollectionType": "tvshows"}]}
        if path == "/System/Info/Public":
            return {"Id": "srv1"}
        if path == "/Items":
            if params.get("ParentId") == "t1":
                raise EmbyUnavailable("Emby 请求失败: boom")
            return {"Items": [_item("M", "Movie", 11)]}
        raise AssertionError(f"unexpected path: {path}")

    _install_get(monkeypatch, _h)
    result = run(list_all_library())  # 一个库失败 → 不抛，返回成功部分
    assert [i["emby_id"] for i in result] == ["id-M"]


def test_all_all_failed_raises(monkeypatch, _db_maker):
    _reset_user_id(monkeypatch)
    _reset_server_id(monkeypatch)
    _use_test_db(monkeypatch, _db_maker)
    _set_cache(monkeypatch)

    async def _boom(path, params):
        raise EmbyUnavailable("Emby 请求失败: connection refused")

    _install_get(monkeypatch, _boom)
    with pytest.raises(EmbyUnavailable):
        run(list_all_library())


def test_all_not_configured_raises(monkeypatch, _db_maker):
    _reset_user_id(monkeypatch)
    _reset_server_id(monkeypatch)
    _use_test_db(monkeypatch, _db_maker)
    _set_cache(monkeypatch)

    async def _boom(path, params):
        raise EmbyUnavailable("EMBY_API_KEY 未配置")

    _install_get(monkeypatch, _boom)
    with pytest.raises(EmbyUnavailable):
        run(list_all_library())


def test_all_partial_failure_success_empty_not_raises(monkeypatch, _db_maker, caplog):
    """部分失败边界：1 库成功但返回 0 条 + 2 库失败 → 返回 [] 且不抛（warn）。

    旧判据 `if errors and not items` 会误抛（成功库无条目时 items 为空）；
    契约「部分失败返回成功部分」要求仅当全部库失败（len(errors) == len(folders)）才上抛。
    """
    _reset_user_id(monkeypatch)
    _reset_server_id(monkeypatch)
    _use_test_db(monkeypatch, _db_maker)
    _set_cache(monkeypatch)

    def _h(path, params):
        if path == "/Users":
            return [{"Id": "user1", "Name": "admin"}]
        if path == "/Users/user1/Views":
            return {"Items": [
                {"Id": "m1", "Name": "电影", "CollectionType": "movies"},
                {"Id": "t1", "Name": "剧集", "CollectionType": "tvshows"},
                {"Id": "x1", "Name": "混合", "CollectionType": "mixed"},
            ]}
        if path == "/System/Info/Public":
            return {"Id": "srv1"}
        if path == "/Items":
            if params.get("ParentId") == "m1":
                return {"Items": []}  # 成功但 0 条
            raise EmbyUnavailable("Emby 请求失败: boom")  # t1/x1 失败
        raise AssertionError(f"unexpected path: {path}")

    _install_get(monkeypatch, _h)
    result = run(list_all_library())  # 部分失败 + 成功库 0 条 → 不抛，返回空
    assert result == []
    assert "部分库失败" in caplog.text  # warn 日志
