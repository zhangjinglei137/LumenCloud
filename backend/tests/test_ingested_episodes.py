"""emby.get_ingested_episode_codes 单测：已入库集聚合 + 进程内 TTL 缓存。

覆盖：
- 命中缓存：mock 服务端一次调用，二次调用零网络（find_emby_id/list_episodes 不再被调）
- 配置变化失效：config_store 值变化 → 缓存 key 变化 → 重新查询
- find_emby_id 未命中（影视不在 Emby）→ 空集且缓存（二次调用零网络）
- EmbyUnavailable 上抛（不缓存）

执行机制：仓库未安装 pytest-asyncio/anyio（pytest 9 不支持原生 async def 测试），
按既有服务层测试约定（test_emby_aired.py 等）用 run(coro)=asyncio.run(coro) 同步包装。
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.services import emby as emby_mod


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clear_ingested_cache():
    emby_mod._INGESTED_CACHE.clear()
    yield
    emby_mod._INGESTED_CACHE.clear()


def test_hit_returns_cached_set_without_network(monkeypatch):
    monkeypatch.setattr(emby_mod, "find_emby_id", AsyncMock(return_value="e1"))
    monkeypatch.setattr(emby_mod, "list_episodes", AsyncMock(return_value=[
        {"code": "S01E01"}, {"code": "S01E03"},
    ]))

    first = run(emby_mod.get_ingested_episode_codes(1001, "剧名"))
    assert first == {"S01E01", "S01E03"}
    assert emby_mod.find_emby_id.await_count == 1

    second = run(emby_mod.get_ingested_episode_codes(1001, "剧名"))
    assert second == {"S01E01", "S01E03"}
    assert emby_mod.find_emby_id.await_count == 1  # 二次调用零网络


def test_config_change_invalidates_cache(monkeypatch):
    from app.services import config_store

    monkeypatch.setattr(emby_mod, "find_emby_id", AsyncMock(return_value="e1"))
    monkeypatch.setattr(emby_mod, "list_episodes", AsyncMock(return_value=[{"code": "S01E01"}]))
    # 先取一次（配置为空时 find_emby_id 直接抛 EmbyUnavailable，但被 mock 短路）
    run(emby_mod.get_ingested_episode_codes(1002, "剧名"))

    # 模拟配置变化（base_url 变化 → 指纹变化）
    monkeypatch.setattr(config_store, "get", lambda key, default=None: {
        "emby_base_url": "http://new-emby:8096",
        "emby_api_key": "new-key",
    }.get(key, default))

    run(emby_mod.get_ingested_episode_codes(1002, "剧名"))
    assert emby_mod.find_emby_id.await_count == 2  # 配置变化后重新查询


def test_not_in_emby_caches_empty(monkeypatch):
    monkeypatch.setattr(emby_mod, "find_emby_id", AsyncMock(return_value=None))
    monkeypatch.setattr(emby_mod, "list_episodes", AsyncMock(return_value=[{"code": "S01E01"}]))

    assert run(emby_mod.get_ingested_episode_codes(1003, "剧名")) == set()
    assert run(emby_mod.get_ingested_episode_codes(1003, "剧名")) == set()
    assert emby_mod.find_emby_id.await_count == 1  # 空集同样缓存


def test_emby_unavailable_not_cached(monkeypatch):
    from app.services.emby import EmbyUnavailable

    async def boom(*_a, **_k):
        raise EmbyUnavailable("EMBY_BASE_URL 未配置")

    # AsyncMock(side_effect=boom)：保留抛异常行为 + 记录调用次数（await_count）
    monkeypatch.setattr(emby_mod, "find_emby_id", AsyncMock(side_effect=boom))
    with pytest.raises(EmbyUnavailable):
        run(emby_mod.get_ingested_episode_codes(1004, "剧名"))
    # 失败不缓存 → 再次调用仍会重试（find_emby_id 被再次调用）
    with pytest.raises(EmbyUnavailable):
        run(emby_mod.get_ingested_episode_codes(1004, "剧名"))
    assert emby_mod.find_emby_id.await_count == 2