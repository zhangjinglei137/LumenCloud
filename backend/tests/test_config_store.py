"""config_store 进程内配置缓存单测（Phase 8 配置入库）。

不连真实数据库：mock config_store.async_session，验证 load_from_db / get /
refresh 语义（DB 优先 + env fallback、加载失败不阻断且回退 default、
敏感键集合覆盖），以及 settings PATCH 后「保存即生效」的 refresh 行为。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import config_store


@pytest.fixture(autouse=True)
def _reset_cache():
    """每个测试前后重置 config_store 模块级缓存（单 worker 全局状态，防跨测试污染）。"""
    saved = (config_store._cache, config_store._loaded)
    config_store._cache = {}
    config_store._loaded = False
    yield
    config_store._cache, config_store._loaded = saved


def _row(key: str, value: str):
    row = MagicMock()
    row.key = key
    row.value = value
    return row


def _fake_sessionmaker(rows_factory):
    """构造 async_session mock；rows_factory() 动态返回当前行列表（可模拟 DB 变化）。

    SQLAlchemy AsyncSession.execute 是 async 方法（awaitable），故用 AsyncMock。
    """
    session = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.side_effect = lambda: list(rows_factory())
    session.execute = AsyncMock(return_value=result)
    maker = MagicMock()
    maker.return_value.__aenter__ = AsyncMock(return_value=session)
    maker.return_value.__exit__ = AsyncMock(return_value=False)
    return maker


# ---- get：未加载时安全返回 default（env fallback 语义）----

def test_get_before_load_returns_default():
    assert config_store.get("alist_token", "env-token") == "env-token"
    assert config_store.get("alist_token") is None
    assert not config_store.is_loaded()


# ---- load_from_db：全量加载 + DB 优先 + 缺失键回退 default ----

def test_load_from_db_populates_cache(monkeypatch):
    monkeypatch.setattr(
        config_store, "async_session",
        _fake_sessionmaker(lambda: [_row("alist_token", "db-token"), _row("quark_quota_gb", "210")]),
    )
    asyncio.run(config_store.load_from_db())
    assert config_store.is_loaded()
    assert config_store.get("alist_token") == "db-token"
    assert config_store.get("quark_quota_gb") == "210"
    # 未在 DB 中的键 → 回退 default（services 层传 settings.X 即 env fallback）
    assert config_store.get("pushplus_token", "env-pp") == "env-pp"


# ---- refresh：settings PATCH commit 后重新加载（保存即生效）----

def test_refresh_reloads_latest(monkeypatch):
    rows = [_row("alist_token", "v1")]
    monkeypatch.setattr(config_store, "async_session", _fake_sessionmaker(lambda: rows))
    asyncio.run(config_store.load_from_db())
    assert config_store.get("alist_token") == "v1"
    # 模拟 settings PATCH 后 DB 已更新 → refresh 拉到最新值
    rows[0] = _row("alist_token", "v2")
    asyncio.run(config_store.refresh())
    assert config_store.get("alist_token") == "v2"


# ---- load 失败：仅告警、不抛、_loaded 保持 False、get 仍回退 default ----
# 注意：不依赖 caplog——APScheduler 在 lifespan 启动时会对 root logger 挂
# StreamHandler（logging.basicConfig），禁用 pytest 日志插件导致 caplog.records
# 为空（跨测试环境污染，与本模块无关）。改用 monkeypatch logger.warning 断言。

def test_load_failure_warns_and_keeps_fallback(monkeypatch):
    maker = MagicMock()
    maker.return_value.__aenter__ = AsyncMock(side_effect=RuntimeError("db down"))
    monkeypatch.setattr(config_store, "async_session", maker)
    warned: list = []
    monkeypatch.setattr(
        config_store.logger, "warning",
        lambda *args, **kwargs: warned.append(args),
    )
    asyncio.run(config_store.load_from_db())
    assert not config_store.is_loaded()
    assert warned, "加载失败应记录 warning（回退 env，不阻断）"
    assert any("加载失败" in str(a) for a in warned)
    # 未加载时 get 仍安全返回 default（回退 settings/env 值，不阻断启动）
    assert config_store.get("alist_token", "env-token") == "env-token"


# ---- 敏感键集合覆盖（settings GET 不回显值的键）----

def test_sensitive_keys_covered():
    for key in (
        "alist_base_url", "alist_token",              # alist 统一处理（内部地址 + token）
        "cloudsaver_username", "cloudsaver_password",
        "aria2_rpc_url", "aria2_token",               # aria2 统一处理（RPC 端点 + token）
        "nastools_username", "nastools_password",
        "emby_api_key",
        "tmdb_api_key",
        "pushplus_token",
        "quark_default_folder",
        "jwt_secret",
        "init_admin_password",
    ):
        assert config_store.is_sensitive(key), key
    # URL 类键（base_url / proxy）不算敏感，可回显
    for key in ("cloudsaver_base_url", "emby_base_url", "nastools_base_url", "tmdb_proxy"):
        assert not config_store.is_sensitive(key), key
    # 非敏感系统键
    assert not config_store.is_sensitive("quark_quota_gb")
    assert not config_store.is_sensitive("scheduler_enabled")
