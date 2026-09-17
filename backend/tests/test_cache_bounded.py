"""进程内有界缓存统一（tmdb/emby，Design D7）单测。

覆盖（对应 Task D1）：
- utils.BoundedLRUCache：超限淘汰最旧、命中 move_to_end、set 更新刷新位置、clear/len、未命中返回 None
- tmdb._SEASON_AIR_CACHE / _ALL_EPS_CACHE 与 emby._INGESTED_CACHE 换用有界缓存实例
  （原先无界 dict，审查 A1/A7）
- Emby 配置变更（emby_base_url 切换）后 _get_server_id / _get_user_id 按配置指纹重新获取
  （原先不失效，审查 A5）
- _build_library_params SeriesStatus 首字母大写（Continuing/Ended，Emby SeriesStatus 契约值；
  前端仍用小写枚举，仅请求参数对齐，审查 A6）
- refresh_episode_info 批量 upsert 结果与逐条一致（新增 + 更新，防回归，审查 A2）
- list_all_library emby_web_url 精简两段式赋值后结果一致（防回归，审查 A10）

模式对齐既有测试：stub emby._get / config_store._cache；_attach_in_media_flag 用隔离
in-memory SQLite（StaticPool + Base.metadata.create_all）+ monkeypatch emby_mod.async_session。
"""
import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  注册全部 ORM 模型
from app.database import Base
from app.services import config_store as cs
from app.services import emby as emby_mod


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# utils.BoundedLRUCache 单元（lazy import：RED 阶段仅相关用例失败）
# ---------------------------------------------------------------------------

def _make(max_items):
    from app.utils import BoundedLRUCache
    return BoundedLRUCache(max_items)


def test_lru_evicts_oldest_beyond_limit():
    """超上限插入 → 淘汰最旧（无界 dict 行为，RED 阶段类不存在即失败）。"""
    c = _make(2)
    c.set("a", 1)
    c.set("b", 2)
    c.set("c", 3)
    assert c.get("a") is None  # 最旧被淘汰
    assert c.get("b") == 2
    assert c.get("c") == 3
    assert len(c) == 2


def test_lru_get_moves_to_end():
    """命中 move_to_end：访问过的 key 不被下一轮淘汰。"""
    c = _make(2)
    c.set("a", 1)
    c.set("b", 2)
    assert c.get("a") == 1  # 命中 a → a 变为最新
    c.set("c", 3)
    assert c.get("a") == 1   # a 仍在
    assert c.get("b") is None  # b（最旧）被淘汰


def test_lru_set_existing_refreshes_position():
    """对已存在 key 再次 set → 更新值并刷新为最新。"""
    c = _make(2)
    c.set("a", 1)
    c.set("b", 2)
    c.set("a", 10)  # 更新 a → a 移到最新
    c.set("c", 3)
    assert c.get("a") == 10
    assert c.get("b") is None


def test_lru_get_missing_and_clear():
    """未命中返回 None；clear 清空（既有测试依赖 _SEASON_AIR_CACHE.clear() 等）。"""
    c = _make(3)
    c.set("a", 1)
    assert c.get("missing") is None
    c.clear()
    assert len(c) == 0
    assert c.get("a") is None


# ---------------------------------------------------------------------------
# 模块级缓存有界化（审查 A1/A7）：无界 dict → BoundedLRUCache
# ---------------------------------------------------------------------------

def test_tmdb_process_caches_are_bounded():
    """tmdb._SEASON_AIR_CACHE / _ALL_EPS_CACHE 换用有界缓存（RED：当前为 dict）。"""
    from app.services import tmdb as tmdb_mod
    from app.utils import BoundedLRUCache
    assert isinstance(tmdb_mod._SEASON_AIR_CACHE, BoundedLRUCache)
    assert isinstance(tmdb_mod._ALL_EPS_CACHE, BoundedLRUCache)


def test_emby_ingested_cache_is_bounded():
    """emby._INGESTED_CACHE 换用有界缓存（RED：当前为 dict）。"""
    from app.utils import BoundedLRUCache
    assert isinstance(emby_mod._INGESTED_CACHE, BoundedLRUCache)


# ---------------------------------------------------------------------------
# Emby server_id / user_id 配置指纹失效（审查 A5）
# ---------------------------------------------------------------------------

def _reset_server_id(monkeypatch):
    monkeypatch.setattr(emby_mod, "_SERVER_ID", None)
    monkeypatch.setattr(emby_mod, "_SERVER_ID_LOADED", False)
    monkeypatch.setattr(emby_mod, "_SERVER_ID_FP", None)


def _reset_user_id(monkeypatch):
    monkeypatch.setattr(emby_mod, "_USER_ID", None)
    monkeypatch.setattr(emby_mod, "_USER_ID_LOADED", False)
    monkeypatch.setattr(emby_mod, "_USER_ID_FP", None)


def test_server_id_refetched_after_config_change(monkeypatch):
    """切换 emby_base_url（配置指纹变化）→ _get_server_id 重新请求并返回新值。

    RED：当前实现仅 LOADED 标志缓存，配置变更后仍命中旧值 → 断言失败。
    """
    _reset_server_id(monkeypatch)
    cache = {"emby_base_url": "http://emby-a.test", "emby_api_key": "k"}
    monkeypatch.setattr(cs, "_cache", cache)
    calls: list[str] = []

    async def _fake(path, params, timeout=None):
        calls.append(cache["emby_base_url"])
        return {"Id": f"id-{cache['emby_base_url']}"}

    monkeypatch.setattr(emby_mod, "_get", _fake)

    # 同配置下惰性缓存：只请求一次
    assert run(emby_mod._get_server_id()) == "id-http://emby-a.test"
    assert run(emby_mod._get_server_id()) == "id-http://emby-a.test"
    assert len(calls) == 1

    # 配置变更（base_url 切换）→ 指纹变化 → 自动重取
    cache["emby_base_url"] = "http://emby-b.test"
    assert run(emby_mod._get_server_id()) == "id-http://emby-b.test"
    assert len(calls) == 2


def test_user_id_refetched_after_config_change(monkeypatch):
    """切换 emby_base_url → _get_user_id 重新请求并返回新值。"""
    _reset_user_id(monkeypatch)
    cache = {"emby_base_url": "http://emby-a.test", "emby_api_key": "k"}
    monkeypatch.setattr(cs, "_cache", cache)
    calls: list[str] = []

    async def _fake(path, params, timeout=None):
        calls.append(cache["emby_base_url"])
        return [{"Id": f"user-{cache['emby_base_url']}"}]

    monkeypatch.setattr(emby_mod, "_get", _fake)

    assert run(emby_mod._get_user_id()) == "user-http://emby-a.test"
    assert run(emby_mod._get_user_id()) == "user-http://emby-a.test"
    assert len(calls) == 1

    cache["emby_base_url"] = "http://emby-b.test"
    assert run(emby_mod._get_user_id()) == "user-http://emby-b.test"
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# SeriesStatus 契约对齐：首字母大写传入 Emby（审查 A6）
# ---------------------------------------------------------------------------

def test_build_library_params_status_capitalized():
    """continuing→Continuing、ended→Ended；None/空保持不传 SeriesStatus。

    RED：当前实现小写透传 → 断言失败。
    """
    params = emby_mod._build_library_params("series", "continuing")
    assert params["SeriesStatus"] == "Continuing"
    params = emby_mod._build_library_params("series", "ended")
    assert params["SeriesStatus"] == "Ended"
    # None / 空 → 不携带 SeriesStatus 参数（既有语义）
    assert "SeriesStatus" not in emby_mod._build_library_params("series", None)
    assert "SeriesStatus" not in emby_mod._build_library_params("series", "")
    # 已大写输入幂等（正常路径内部枚举为小写，仅防调用方直接传大写）
    assert emby_mod._build_library_params("series", "Ended")["SeriesStatus"] == "Ended"


def test_list_library_status_param_capitalized(monkeypatch):
    """list_library(status=...) 实际请求参数为 Continuing（端到端参数形态）。

    注：list_library 内部还会经 _get_server_id 请求 /System/Info/Public，故从
    await_args_list 中筛选 /Items 调用断言（而非 await_args 最后一次调用）；
    并重置 server_id 惰性缓存，保证用例独立可运行（不依赖其他用例预置缓存）。
    """
    from unittest.mock import AsyncMock

    _reset_server_id(monkeypatch)
    mock = AsyncMock(return_value={"Items": []})
    monkeypatch.setattr(emby_mod, "_get", mock)
    monkeypatch.setattr(cs, "_cache", {
        "emby_base_url": "http://emby.test",
        "emby_api_key": "test-key",
    })

    run(emby_mod.list_library("m1", status="continuing"))
    items_calls = [
        c.args[:2] for c in mock.await_args_list if c.args and c.args[0] == "/Items"
    ]
    assert items_calls, "应至少发起一次 /Items 请求"
    assert items_calls[0][1]["SeriesStatus"] == "Continuing"


# ---------------------------------------------------------------------------
# refresh_episode_info 批量 upsert 防回归（审查 A2）：结果与逐条一致
# ---------------------------------------------------------------------------

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


def _read_rows(maker, tmdb_id):
    """读回 episode_info_cache 指定影视的全部行（dict 列表，供断言比较）。"""
    from app.models import EpisodeInfoCache

    async def _do():
        async with maker() as s:
            rows = (
                (await s.execute(
                    select(EpisodeInfoCache)
                    .where(EpisodeInfoCache.tmdb_id == tmdb_id)
                    .order_by(EpisodeInfoCache.season.asc(), EpisodeInfoCache.episode.asc())
                ))
                .scalars()
                .all()
            )
            return [
                {"season": r.season, "episode": r.episode, "name": r.name, "air_date": r.air_date}
                for r in rows
            ]

    return run(_do())


def _seed_episode_info(maker, rows):
    from app.models import EpisodeInfoCache

    async def _do():
        async with maker() as s:
            for r in rows:
                s.add(EpisodeInfoCache(**r))
            await s.commit()

    run(_do())


def test_refresh_episode_info_batch_upsert_inserts_and_updates(_db_maker, monkeypatch):
    """批量 upsert 与逐条语义一致：已有行更新（不新增）、新行插入、count 为处理条数。"""
    from app.services import tmdb as tmdb_mod

    monkeypatch.setattr(tmdb_mod, "async_session", _db_maker)

    # 既有行：S1E1 旧值（本次应被更新而非新增重复行）
    _seed_episode_info(_db_maker, [
        {"tmdb_id": 10, "season": 1, "episode": 1, "name": "Old", "air_date": "2020-01-01"},
    ])

    async def fake_all(tmdb_id):
        return [
            {"season": 1, "episode": 1, "air_date": "2026-01-01", "name": "A"},
            {"season": 1, "episode": 2, "air_date": "2026-01-08", "name": "B"},
        ]

    monkeypatch.setattr(tmdb_mod, "get_tv_all_episodes", fake_all)

    n = run(tmdb_mod.refresh_episode_info(10))
    assert n == 2  # count = 处理条数（与逐条实现一致）

    rows = _read_rows(_db_maker, 10)
    assert len(rows) == 2  # S1E1 更新而非新增；S1E2 新增
    assert rows[0] == {"season": 1, "episode": 1, "air_date": "2026-01-01", "name": "A"}
    assert rows[1] == {"season": 1, "episode": 2, "air_date": "2026-01-08", "name": "B"}


def test_refresh_episode_info_batch_empty_preserves_old(_db_maker, monkeypatch):
    """批量路径下空回源保留旧缓存（与逐条实现一致）。"""
    from app.services import tmdb as tmdb_mod

    monkeypatch.setattr(tmdb_mod, "async_session", _db_maker)
    _seed_episode_info(_db_maker, [
        {"tmdb_id": 11, "season": 1, "episode": 1, "name": "Old", "air_date": "2026-01-01"},
    ])

    async def fake_empty(tmdb_id):
        return []

    monkeypatch.setattr(tmdb_mod, "get_tv_all_episodes", fake_empty)

    n = run(tmdb_mod.refresh_episode_info(11))
    assert n == 0
    assert _read_rows(_db_maker, 11) == [
        {"season": 1, "episode": 1, "name": "Old", "air_date": "2026-01-01"}
    ]


# ---------------------------------------------------------------------------
# list_all_library emby_web_url 防回归（审查 A10）：精简两段式赋值后结果一致
# ---------------------------------------------------------------------------

def _library_item(name, kind="Series", tmdb=1001):
    return {
        "Id": f"id-{name}",
        "Name": name,
        "Type": kind,
        "ProviderIds": {"Tmdb": str(tmdb)},
        "ProductionYear": 2024,
        "ImageTags": {"Primary": "x"},
        "SeriesStatus": None,
    }


def _install_get(monkeypatch, handler):
    async def _fake(path, params, timeout=None):
        result = handler(path, params)
        if asyncio.iscoroutine(result):
            return await result
        return result

    monkeypatch.setattr(emby_mod, "_get", _fake)


def test_list_all_library_web_url_unchanged(monkeypatch, _db_maker):
    """精简后 emby_web_url 仍为 base + item?id + serverId 形态（server_id 有值）。"""
    monkeypatch.setattr(emby_mod, "_SERVER_ID", None)
    monkeypatch.setattr(emby_mod, "_SERVER_ID_LOADED", False)
    monkeypatch.setattr(emby_mod, "_SERVER_ID_FP", None)
    monkeypatch.setattr(emby_mod, "_USER_ID", None)
    monkeypatch.setattr(emby_mod, "_USER_ID_LOADED", False)
    monkeypatch.setattr(emby_mod, "_USER_ID_FP", None)
    monkeypatch.setattr(emby_mod, "async_session", _db_maker)
    monkeypatch.setattr(cs, "_cache", {
        "emby_base_url": "http://emby.test",
        "emby_api_key": "test-key",
    })

    def _handler(path, params):
        if path == "/Users":
            return [{"Id": "user1", "Name": "admin"}]
        if path == "/Users/user1/Views":
            return {"Items": [{"Id": "m1", "Name": "电影", "CollectionType": "movies"}]}
        if path == "/System/Info/Public":
            return {"Id": "srv1"}
        if path == "/Items":
            return {"Items": [_library_item("S1", tmdb=101)]}
        raise AssertionError(f"unexpected path: {path}")

    _install_get(monkeypatch, _handler)

    result = run(emby_mod.list_all_library())
    assert len(result) == 1
    assert result[0]["emby_web_url"] == (
        "http://emby.test/web/index.html#!/item?id=id-S1&serverId=srv1"
    )
    assert result[0]["tmdb_id"] == "101"


def test_list_all_library_web_url_none_without_server_id(monkeypatch, _db_maker):
    """server_id 获取失败 → emby_web_url 仍为 None（精简后不因两段式缺失而行为变化）。"""
    monkeypatch.setattr(emby_mod, "_SERVER_ID", None)
    monkeypatch.setattr(emby_mod, "_SERVER_ID_LOADED", False)
    monkeypatch.setattr(emby_mod, "_SERVER_ID_FP", None)
    monkeypatch.setattr(emby_mod, "_USER_ID", None)
    monkeypatch.setattr(emby_mod, "_USER_ID_LOADED", False)
    monkeypatch.setattr(emby_mod, "_USER_ID_FP", None)
    monkeypatch.setattr(emby_mod, "async_session", _db_maker)
    monkeypatch.setattr(cs, "_cache", {
        "emby_base_url": "http://emby.test",
        "emby_api_key": "test-key",
    })

    def _handler(path, params):
        if path == "/Users":
            return [{"Id": "user1", "Name": "admin"}]
        if path == "/Users/user1/Views":
            return {"Items": [{"Id": "m1", "Name": "电影", "CollectionType": "movies"}]}
        if path == "/System/Info/Public":
            raise emby_mod.EmbyUnavailable("emby down")
        if path == "/Items":
            return {"Items": [_library_item("S1", tmdb=101)]}
        raise AssertionError(f"unexpected path: {path}")

    _install_get(monkeypatch, _handler)

    result = run(emby_mod.list_all_library())
    assert len(result) == 1
    assert result[0]["emby_web_url"] is None
    assert result[0]["title"] == "S1"
