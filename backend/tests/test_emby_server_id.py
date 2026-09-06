"""Emby serverId（D-1/P1）：emby_web_url 补 serverId 参数单测（服务层，stub _get）。

- emby_web_url 详情路由 `#!/item?id=X&serverId=Y` 依赖 serverId 定位后端服务器
  实例（缺参前端打开空白页）；serverId 取 /System/Info/Public 的 Id（Public 端点
  无需 api_key），模块级惰性缓存（成功/失败均只尝试一次）。
- stub app.services.emby._get（按 path 分发）绕过 _check_config/_base_url 与网络调用；
- 注入 config_store._cache 双保险；_attach_in_media_flag 用隔离 in-memory SQLite
  （StaticPool + Base.metadata.create_all），monkeypatch emby_mod.async_session；
- 每个用例前重置模块级缓存 _SERVER_ID / _SERVER_ID_LOADED（防用例间串缓存）。
"""
import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  注册全部 ORM 模型
from app.database import Base
from app.services import config_store as cs
from app.services import emby as emby_mod
from app.services.emby import EmbyUnavailable


def run(coro):
    return asyncio.run(coro)


def _library_item(name, kind="Series", tmdb=1001):
    """构造 Emby 库 Item（带 tmdb_id，避免被目录性质过滤丢弃）。"""
    return {
        "Id": f"id-{name}",
        "Name": name,
        "Type": kind,
        "ProviderIds": {"Tmdb": str(tmdb)},
        "ProductionYear": 2024,
    }


def _set_cache(monkeypatch):
    monkeypatch.setattr(cs, "_cache", {
        "emby_base_url": "http://emby.test",
        "emby_api_key": "test-key",
    })


def _reset_server_id(monkeypatch):
    """重置模块级惰性缓存（每个用例独立，防跨用例串缓存）。"""
    monkeypatch.setattr(emby_mod, "_SERVER_ID", None)
    monkeypatch.setattr(emby_mod, "_SERVER_ID_LOADED", False)


def _install_get(monkeypatch, handler):
    """把 emby._get 替换为按 (path, params) 分发的 async 桩。"""
    async def _fake(path, params, timeout=None):
        return await handler(path, params)

    monkeypatch.setattr(emby_mod, "_get", _fake)


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


def test_emby_web_url_includes_server_id(monkeypatch, _db_maker):
    """Public 返回 Id → emby_web_url 拼上 &serverId=<Id>。"""
    _reset_server_id(monkeypatch)
    _set_cache(monkeypatch)
    _use_test_db(monkeypatch, _db_maker)

    async def _handler(path, params):
        if path == "/System/Info/Public":
            return {"Id": "d52a1f41ab"}
        if path == "/Items":
            return {"Items": [_library_item("S1", tmdb=101)]}
        raise AssertionError(f"unexpected path: {path}")

    _install_get(monkeypatch, _handler)

    result = run(emby_mod.list_library())

    assert len(result) == 1
    assert result[0]["emby_web_url"] == (
        "http://emby.test/web/index.html#!/item?id=id-S1&serverId=d52a1f41ab"
    )
    # 其余字段正常
    assert result[0]["title"] == "S1"


def test_server_id_failure_degrades_web_url_none(monkeypatch, _db_maker):
    """Public 获取失败 → emby_web_url=None，其余字段正常（前端隐藏入口）。"""
    _reset_server_id(monkeypatch)
    _set_cache(monkeypatch)
    _use_test_db(monkeypatch, _db_maker)

    async def _handler(path, params):
        if path == "/System/Info/Public":
            raise EmbyUnavailable("emby down")
        if path == "/Items":
            return {"Items": [_library_item("S1", tmdb=101)]}
        raise AssertionError(f"unexpected path: {path}")

    _install_get(monkeypatch, _handler)

    result = run(emby_mod.list_library())

    assert len(result) == 1
    assert result[0]["emby_web_url"] is None
    assert result[0]["title"] == "S1"


def test_get_server_id_cached_single_request(monkeypatch):
    """连续两次 _get_server_id() 只请求一次（第二次走模块级缓存）。"""
    _reset_server_id(monkeypatch)
    calls: list[str] = []

    async def _fake(path, params, timeout=None):
        calls.append(path)
        return {"Id": "srv-cached"}

    monkeypatch.setattr(emby_mod, "_get", _fake)

    assert run(emby_mod._get_server_id()) == "srv-cached"
    assert run(emby_mod._get_server_id()) == "srv-cached"
    assert calls == ["/System/Info/Public"]


def test_server_id_missing_returns_none(monkeypatch):
    """Public 响应无 Id 字段 → None（列表时 emby_web_url 降级 None）。"""
    _reset_server_id(monkeypatch)
    calls: list[str] = []

    async def _fake(path, params, timeout=None):
        calls.append(path)
        return {"ServerName": "no-id-response"}

    monkeypatch.setattr(emby_mod, "_get", _fake)

    assert run(emby_mod._get_server_id()) is None
    # 失败（无 Id）同样只尝试一次并缓存
    assert run(emby_mod._get_server_id()) is None
    assert calls == ["/System/Info/Public"]