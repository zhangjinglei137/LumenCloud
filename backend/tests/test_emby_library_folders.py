"""list_library_folders（/Users/{UserId}/Views）测试。

数据源说明：MediaFolders 项的 Id 不是 /Items 接受的 ParentId（ViewId）；
合法 ParentId 来源是 /Users/{UserId}/Views（其 Id == VirtualFolders ItemId），
故先经 /Users 取首用户 Id，再查 Views 构建媒体库列表。
"""
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


def _reset_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """重置模块级惰性缓存（UserId），防跨用例串缓存（对齐 _reset_server_id 约定）。"""
    monkeypatch.setattr(emby_mod, "_USER_ID", None)
    monkeypatch.setattr(emby_mod, "_USER_ID_LOADED", False)


def _views_handler(items_payload: dict[str, Any] | list[dict[str, Any]]) -> Callable:
    """按新数据源分发的桩：/Users → 首用户；/Users/user1/Views → 库列表 payload。

    断言 Views 路径携带取到的 UserId（合法 ParentId 来源契约）。
    """
    def _handler(path, params):
        if path == "/Users":
            return [{"Id": "user1", "Name": "admin"}]
        assert path == "/Users/user1/Views", f"unexpected path: {path}"
        return items_payload
    return _handler


def test_views_parsed_with_is_anime(monkeypatch, _db_maker):
    _use_test_db(monkeypatch, _db_maker)
    _reset_user_id(monkeypatch)

    _install_get(monkeypatch, _views_handler({"Items": [
        _folder("m1", "电影", "movies"),
        _folder("t1", "剧集", "tvshows"),
        _folder("t2", "动漫番组", "tvshows"),
        _folder("x1", "混合", "mixed"),
        _folder("mu", "音乐", "music"),
        _folder("hv", "家庭视频", "homevideos"),
    ]}))
    result = run(list_library_folders())

    assert result == [
        {"id": "m1", "name": "电影", "collection_type": "movies", "is_anime": False},
        {"id": "t1", "name": "剧集", "collection_type": "tvshows", "is_anime": False},
        {"id": "t2", "name": "动漫番组", "collection_type": "tvshows", "is_anime": True},
        {"id": "x1", "name": "混合", "collection_type": "mixed", "is_anime": False},
    ]
    # 非影视库（music/homevideos/book 等）不进入列表，mixed 保留
    assert {lib["id"] for lib in result} == {"m1", "t1", "t2", "x1"}


def test_views_whitelist_filters_tvshows(monkeypatch, _db_maker):
    _use_test_db(monkeypatch, _db_maker)
    _reset_user_id(monkeypatch)
    monkeypatch.setattr(
        emby_mod.config_store, "get",
        lambda key, default=None: "t1" if key == "emby_series_library_ids" else default,
    )

    _install_get(monkeypatch, _views_handler({"Items": [
        _folder("m1", "电影", "movies"),
        _folder("t1", "剧集", "tvshows"),
        _folder("t2", "另一个剧集", "tvshows"),
    ]}))
    result = run(list_library_folders())

    assert [lib["id"] for lib in result] == ["m1", "t1"]


def test_views_not_configured_raises(monkeypatch, _db_maker):
    """配置缺失（Views 请求抛「未配置」）→ 仍抛 EmbyUnavailable（前端「未配置空态」）。

    UserId 获取阶段已确认可用（/Users 成功），未配置错误在 Views 请求面暴露
    （list_library_folders 的防御性 re-raise 分支）。
    """
    _use_test_db(monkeypatch, _db_maker)
    _reset_user_id(monkeypatch)

    async def _boom(path, params):
        if path == "/Users":
            return [{"Id": "user1", "Name": "admin"}]
        raise EmbyUnavailable("Emby 未配置")

    _install_get(monkeypatch, _boom)
    with pytest.raises(EmbyUnavailable):
        run(list_library_folders())


def test_views_user_id_not_configured_raises(monkeypatch, _db_maker):
    """纯未配置场景：/Users 请求即抛「未配置」→ list_library_folders 原样 re-raise。

    Finding-1 修复主路径：配置缺失时恢复 503 emby_not_configured（前端「未配置
    空态」），而非降级为空列表。
    """
    _use_test_db(monkeypatch, _db_maker)
    _reset_user_id(monkeypatch)

    async def _boom(path, params):
        raise EmbyUnavailable("EMBY_API_KEY 未配置")

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


# ---------------------------------------------------------------------------
# 错误归一与缺省映射
# ---------------------------------------------------------------------------


def test_views_request_failure_returns_empty(monkeypatch, _db_maker):
    """Views 请求网络故障 → 降级为空列表（不阻断分类展示）。

    UserId 已获取成功，故障发生在 Views 请求面（非「未配置」）。
    """
    _use_test_db(monkeypatch, _db_maker)
    _reset_user_id(monkeypatch)

    async def _boom(path, params):
        if path == "/Users":
            return [{"Id": "user1", "Name": "admin"}]
        raise EmbyUnavailable("Emby 请求失败: connection refused")

    _install_get(monkeypatch, _boom)
    assert run(list_library_folders()) == []


def test_views_user_id_fetch_failure_returns_empty(monkeypatch, _db_maker):
    """/Users 获取失败（无法取得 UserId，非「未配置」错误）→ 降级空列表。"""
    _use_test_db(monkeypatch, _db_maker)
    _reset_user_id(monkeypatch)

    async def _boom(path, params):
        raise EmbyUnavailable("Emby 请求失败: connection refused")

    _install_get(monkeypatch, _boom)
    assert run(list_library_folders()) == []


def test_get_user_id_not_configured_reraises(monkeypatch):
    """/Users 抛「未配置」→ _get_user_id 原样 re-raise（非返回 None）。"""
    _reset_user_id(monkeypatch)

    async def _boom(path, params):
        raise EmbyUnavailable("EMBY_BASE_URL 未配置")

    _install_get(monkeypatch, _boom)
    with pytest.raises(EmbyUnavailable):
        run(emby_mod._get_user_id())


def test_get_user_id_retries_after_not_configured(monkeypatch):
    """「未配置」re-raise 后不固化缓存：配置修复后再次调用可重试成功。

    若 re-raise 时保留 _USER_ID_LOADED=True，用户补配置后本进程内将永久
    返回 None（无法自愈），故未配置失败必须重置缓存标记。
    """
    _reset_user_id(monkeypatch)
    state = {"configured": False}

    async def _handler(path, params):
        if not state["configured"]:
            raise EmbyUnavailable("EMBY_API_KEY 未配置")
        return [{"Id": "user1", "Name": "admin"}]

    _install_get(monkeypatch, _handler)

    with pytest.raises(EmbyUnavailable):
        run(emby_mod._get_user_id())
    # 配置修复后（同一进程内）应能重试成功，而非命中固化缓存返回 None
    state["configured"] = True
    assert run(emby_mod._get_user_id()) == "user1"


def test_views_bare_array_payload_parsed(monkeypatch, _db_maker):
    """Views 返回裸数组（防御性兼容，非 dict 包装）同样解析。"""
    _use_test_db(monkeypatch, _db_maker)
    _reset_user_id(monkeypatch)

    _install_get(monkeypatch, _views_handler([
        _folder("m1", "电影", "movies"),
        _folder("t2", "动漫番组", "tvshows"),
    ]))
    result = run(list_library_folders())

    assert result == [
        {"id": "m1", "name": "电影", "collection_type": "movies", "is_anime": False},
        {"id": "t2", "name": "动漫番组", "collection_type": "tvshows", "is_anime": True},
    ]


def test_list_library_unconfigured_raises(monkeypatch, _db_maker):
    _use_test_db(monkeypatch, _db_maker)

    async def _boom(path, params):
        raise EmbyUnavailable("Emby 未配置")

    _install_get(monkeypatch, _boom)
    with pytest.raises(EmbyUnavailable):
        run(list_library("m1"))


def test_list_library_default_item_types(monkeypatch, _db_maker):
    """缺省 item_type → IncludeItemTypes=Movie,Series。"""
    _use_test_db(monkeypatch, _db_maker)
    # 走真实管线：list_library 内 _base_url() 不经 _get mock，需注入缓存防
    # 全量套件 lifespan 残留的 emby_base_url="" 误判未配置（同文件既有约定）。
    _set_cache(monkeypatch)
    captured: dict[str, Any] = {}

    def _handler(path, params):
        if path == "/System/Info/Public":
            return {"Id": "srv1"}
        if path == "/Items":
            captured["params"] = params
            return {"Items": []}
        raise AssertionError(f"unexpected path: {path}")

    _install_get(monkeypatch, _handler)
    run(list_library("x1"))
    assert captured["params"]["IncludeItemTypes"] == "Movie,Series"
    assert captured["params"]["ParentId"] == "x1"


# ---------------------------------------------------------------------------
# _build_library_params（全部聚合与单库共用参数口径）
# ---------------------------------------------------------------------------


def test_build_params_default_types():
    params = emby_mod._build_library_params(None, None)
    assert params["IncludeItemTypes"] == "Movie,Series"
    assert "ParentId" not in params
    assert "SeriesStatus" not in params


def test_build_params_maps_item_type():
    assert emby_mod._build_library_params("movie", None)["IncludeItemTypes"] == "Movie"
    assert emby_mod._build_library_params("series", None)["IncludeItemTypes"] == "Series"


def test_build_params_status_forces_series():
    params = emby_mod._build_library_params("movie", "continuing", parent_id="m1")
    assert params["IncludeItemTypes"] == "Movie,Series"  # status 非空强制含 Series
    assert params["SeriesStatus"] == "continuing"
    assert params["ParentId"] == "m1"


# ---- _normalize_library_item poster_url 代理格式（Task 5）----


def test_normalize_poster_url_proxy_format():
    item = _library_item("A", kind="Movie", tmdb=11)
    result = emby_mod._normalize_library_item(item, "http://emby.test", server_id="srv1")
    assert result["poster_url"] == "/api/poster?p=emby/id-A/Primary"
    assert "api_key" not in result["poster_url"]  # api_key 不再内嵌
    assert result["emby_web_url"] == (
        "http://emby.test/web/index.html#!/item?id=id-A&serverId=srv1"
    )


def test_normalize_no_poster_returns_null():
    item = {
        "Id": "id-N",
        "Name": "N",
        "Type": "Movie",
        "ProviderIds": {"Tmdb": "11"},
        "ImageTags": {},
    }
    result = emby_mod._normalize_library_item(item, "http://emby.test", server_id="srv1")
    assert result["poster_url"] is None
