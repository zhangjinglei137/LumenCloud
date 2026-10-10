"""`tmdb.get_tv_season_air_dates` 回源失败哨兵（None）单测。

契约：
- 回源失败（_fetch_season_episodes 返回 None）→ 返回 None（失败哨兵，调用方 fail-safe）；
- 成功但空季 → 返回 {}（与失败区分，仍写缓存）；
- 成功有数据 → 返回 {episode_number: air_date} 映射。

不连真实服务：mock app.services.tmdb._fetch_season_episodes。
"""
from unittest.mock import AsyncMock
import pytest
from app.services import tmdb


def test_season_air_dates_returns_none_on_fetch_failure(monkeypatch):
    monkeypatch.setattr(tmdb, "_fetch_season_episodes", AsyncMock(return_value=None))
    tmdb._SEASON_AIR_CACHE.clear()
    import asyncio
    assert asyncio.run(tmdb.get_tv_season_air_dates(1, 1)) is None


def test_season_air_dates_returns_empty_dict_on_empty_season(monkeypatch):
    monkeypatch.setattr(tmdb, "_fetch_season_episodes", AsyncMock(return_value=[]))
    tmdb._SEASON_AIR_CACHE.clear()
    import asyncio
    assert asyncio.run(tmdb.get_tv_season_air_dates(2, 1)) == {}


def test_season_air_dates_returns_map_on_success(monkeypatch):
    monkeypatch.setattr(
        tmdb, "_fetch_season_episodes",
        AsyncMock(return_value=[{"episode_number": 1, "air_date": "2026-01-01"}]),
    )
    tmdb._SEASON_AIR_CACHE.clear()
    import asyncio
    assert asyncio.run(tmdb.get_tv_season_air_dates(3, 1)) == {1: "2026-01-01"}


# ---------------------------------------------------------------------------
# `tmdb.get_tv_all_episodes` 整体失败哨兵（None）
# ---------------------------------------------------------------------------
# 契约：api_key 缺失 / tv 详情回源失败 / 任一被纳入的正片季回源失败
#       → 整体返回 None（不写缓存，避免把失败季误当「无该集」）；
#       成功（含成功但无正片季）→ 返回 list（空为 []）。
# 不连真实服务：mock httpx.AsyncClient（tv 详情）与 _fetch_season_episodes。


def _stub_tv_detail(monkeypatch, payload, status_code=200):
    """monkeypatch httpx.AsyncClient：tv 详情固定返回给定 payload。"""

    class _Resp:
        def __init__(self):
            self.status_code = status_code

        def json(self):
            return payload

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            return _Resp()

    monkeypatch.setattr(tmdb.httpx, "AsyncClient", lambda **kwargs: _Client())


def test_all_episodes_returns_none_on_tv_detail_failure(monkeypatch):
    """tv 详情回源非 200 → 整体失败哨兵 None（旧契约返回 []）。"""
    import asyncio
    from app.config import settings

    monkeypatch.setattr(settings, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(tmdb.config_store, "_cache", {})
    tmdb._ALL_EPS_CACHE.clear()
    _stub_tv_detail(monkeypatch, {}, status_code=500)

    assert asyncio.run(tmdb.get_tv_all_episodes(9001)) is None


def test_all_episodes_returns_none_when_any_season_fetch_fails(monkeypatch):
    """任一被纳入的正片季回源失败 → 整体失败哨兵 None（不返回部分结果、不写缓存）。"""
    import asyncio
    from app.config import settings

    monkeypatch.setattr(settings, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(tmdb.config_store, "_cache", {})
    tmdb._ALL_EPS_CACHE.clear()
    _stub_tv_detail(
        monkeypatch,
        {"seasons": [{"season_number": 1}, {"season_number": 2}]},
    )

    async def _fake_fetch(tmdb_id, season_number):
        if season_number == 1:
            return [{"episode_number": 1, "air_date": "2026-01-01", "name": "A"}]
        return None  # 第 2 季回源失败

    monkeypatch.setattr(tmdb, "_fetch_season_episodes", _fake_fetch)

    assert asyncio.run(tmdb.get_tv_all_episodes(9002)) is None
    # 失败不写缓存：再次调用仍应回源（缓存无该 key）
    assert tmdb._ALL_EPS_CACHE.get("9002:all_episodes") is None


def test_all_episodes_returns_empty_list_when_no_regular_seasons(monkeypatch):
    """成功但无正片季（仅特辑 season 0）→ 成功空 []（非失败哨兵 None）。"""
    import asyncio
    from app.config import settings

    monkeypatch.setattr(settings, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(tmdb.config_store, "_cache", {})
    tmdb._ALL_EPS_CACHE.clear()
    _stub_tv_detail(monkeypatch, {"seasons": [{"season_number": 0}]})

    assert asyncio.run(tmdb.get_tv_all_episodes(9003)) == []
