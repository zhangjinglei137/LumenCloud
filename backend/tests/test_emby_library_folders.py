"""list_library_folders（/Library/MediaFolders）测试。"""
import asyncio
from typing import Any, Callable, Optional

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

import app.services.emby as emby_mod
from app.database import Base
from app.services.emby import EmbyUnavailable, list_library_folders


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
        ]}

    _install_get(monkeypatch, _handler)
    result = run(list_library_folders())

    assert result == [
        {"id": "m1", "name": "电影", "collection_type": "movies", "is_anime": False},
        {"id": "t1", "name": "剧集", "collection_type": "tvshows", "is_anime": False},
        {"id": "t2", "name": "动漫番组", "collection_type": "tvshows", "is_anime": True},
        {"id": "x1", "name": "混合", "collection_type": "mixed", "is_anime": False},
    ]


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
