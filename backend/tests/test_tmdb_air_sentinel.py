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
