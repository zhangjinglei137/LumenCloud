"""list_library_folders（/Library/MediaFolders）测试。"""
import asyncio
from typing import Any, Callable, Optional

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

import app.services.emby as emby_mod
from app.database import Base
from app.services.emby import EmbyUnavailable, list_library, list_library_folders


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
    """替换 emby_mod._get：handler(path, params) -> payload dict。

    兼容同步 handler（返回 dict）与 async handler（如抛 EmbyUnavailable 的桩）。
    """
    async def _fake_get(path: str, params: dict[str, Any], **kwargs):
        result = handler(path, params)
        if asyncio.iscoroutine(result):
            return await result
        return result
    monkeypatch.setattr(emby_mod, "_get", _fake_get)


def _folder(fid: str, name: str, ct: Optional[str]) -> dict[str, Any]:
    return {"Id": fid, "Name": name, "CollectionType": ct}


def test_mediafolders_parsed_with_is_anime(monkeypatch, _db_maker):
    _use_test_db(monkeypatch, _db_maker)

    def _handler(path, params):
        assert path == "/Library/MediaFolders"
        return {"Items": [
            _folder("m1", "电影", "movies"),
            _folder("t1", "剧集", "tvshows"),
            _folder("t2", "动漫番组", "tvshows"),
            _folder("x1", "混合", "mixed"),
            _folder("mu", "音乐", "music"),
            _folder("hv", "家庭视频", "homevideos"),
        ]}

    _install_get(monkeypatch, _handler)
    result = run(list_library_folders())

    assert result == [
        {"id": "m1", "name": "电影", "collection_type": "movies", "is_anime": False},
        {"id": "t1", "name": "剧集", "collection_type": "tvshows", "is_anime": False},
        {"id": "t2", "name": "动漫番组", "collection_type": "tvshows", "is_anime": True},
        {"id": "x1", "name": "混合", "collection_type": "mixed", "is_anime": False},
    ]
    # 非影视库（music/homevideos/book 等）不进入列表，mixed 保留
    assert {lib["id"] for lib in result} == {"m1", "t1", "t2", "x1"}


def test_mediafolders_whitelist_filters_tvshows(monkeypatch, _db_maker):
    _use_test_db(monkeypatch, _db_maker)
    monkeypatch.setattr(
        emby_mod.config_store, "get",
        lambda key, default=None: "t1" if key == "emby_series_library_ids" else default,
    )

    def _handler(path, params):
        return {"Items": [
            _folder("m1", "电影", "movies"),
            _folder("t1", "剧集", "tvshows"),
            _folder("t2", "另一个剧集", "tvshows"),
        ]}

    _install_get(monkeypatch, _handler)
    result = run(list_library_folders())

    assert [lib["id"] for lib in result] == ["m1", "t1"]


def test_mediafolders_not_configured_raises(monkeypatch, _db_maker):
    _use_test_db(monkeypatch, _db_maker)

    async def _boom(path, params):
        raise EmbyUnavailable("Emby 未配置")

    _install_get(monkeypatch, _boom)
    with pytest.raises(EmbyUnavailable):
        run(list_library_folders())


# ---------------------------------------------------------------------------
# list_library（library_id 必选单库查询 / SeriesStatus 映射）
# ---------------------------------------------------------------------------


def _library_item(name, kind="Series", tmdb=1001):
    return {
        "Id": f"id-{name}",
        "Name": name,
        "Type": kind,
        "ProductionYear": 2024,
        "ProviderIds": {"Tmdb": str(tmdb)},
        "ImageTags": {"Primary": "x"},
        "SeriesStatus": None,
    }


def _set_cache(monkeypatch):
    """注入 config_store._cache（emby_base_url/emby_api_key）双保险，防意外路径。

    全量套件中前序测试可能触发 lifespan load_from_db 把 temp-DB 的
    emby_base_url="" 灌入模块级 _cache 并全局残留，_base_url() 会误判未配置；
    与 test_emby_server_id / test_emby_series_status 的约定一致。
    """
    monkeypatch.setattr(emby_mod.config_store, "_cache", {
        "emby_base_url": "http://emby.test",
        "emby_api_key": "test-key",
    })


def test_list_library_passes_library_id_as_parent(monkeypatch, _db_maker):
    _use_test_db(monkeypatch, _db_maker)
    _set_cache(monkeypatch)

    captured: dict[str, Any] = {}

    def _handler(path, params):
        if path == "/System/Info/Public":
            return {"Id": "srv1"}
        if path == "/Items":
            captured["path"] = path
            captured["params"] = params
            return {"Items": [_library_item("A", kind="Movie", tmdb=11)]}
        raise AssertionError(f"unexpected path: {path}")

    _install_get(monkeypatch, _handler)

    result = run(list_library("m1", "movie", None))

    assert captured["path"] == "/Items"
    assert captured["params"]["ParentId"] == "m1"
    assert "Movie" in captured["params"]["IncludeItemTypes"]
    assert result[0]["emby_id"] == "id-A"


def test_list_library_maps_status_to_series_status(monkeypatch, _db_maker):
    _use_test_db(monkeypatch, _db_maker)
    _set_cache(monkeypatch)

    captured: dict[str, Any] = {}

    def _handler(path, params):
        if path == "/System/Info/Public":
            return {"Id": "srv1"}
        if path == "/Items":
            captured["params"] = params
            return {"Items": [_library_item("S1")]}
        raise AssertionError(f"unexpected path: {path}")

    _install_get(monkeypatch, _handler)

    run(list_library("t1", "series", "continuing"))

    assert captured["params"]["ParentId"] == "t1"
    assert captured["params"]["SeriesStatus"] == "continuing"
    assert "Series" in captured["params"]["IncludeItemTypes"]
