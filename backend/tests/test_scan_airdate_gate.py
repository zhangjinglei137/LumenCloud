"""scan 已播出门控 `_filter_aired` 及 `_emby_missing_codes` 新契约单测。

背景：Emby 遗漏集里含「未来才播」的集（TMDB 有 air_date），旧逻辑直接按缺失
搜索入队 → 转存占空间却拿不到未播出资源。本改造：
- `_emby_missing_codes` 返回值由 code 字符串列表升级为
  `list[{"code": str, "premiere_date": str | None} | None]`（携带 Emby 首播日期，
  供第二个数据源兜底）；
- `_filter_aired` 按播出日期过滤：TMDB 该季 air_date 优先，缺失回退 Emby
  premiere_date，两源皆无 → 未播出剔除；某季 TMDB 取数失败 → 记入 failed。

None 哨兵语义保持不变：movie 已库 `[]` / 整部缺失 `[None]`；tv 未收录默认
`[None]`（软处理）；强防重 `None`。
"""
import asyncio
import types
from unittest.mock import AsyncMock

from app.tasks import scan as scan_mod


def run(coro):
    return asyncio.run(coro)


def _tv(emby_id="e1", tmdb_id=42):
    return types.SimpleNamespace(tmdb_id=tmdb_id, title="剑来", media_type="tv", id=1)


# ---------------------------------------------------------------------------
# _emby_missing_codes 契约扩展：dict 携带 code + premiere_date
# ---------------------------------------------------------------------------

def test_emby_missing_codes_returns_dicts(monkeypatch):
    """tv 已收录 → 缺失集为 dict 列表（code + premiere_date），非旧字符串列表。"""
    monkeypatch.setattr(scan_mod.emby, "find_emby_id", AsyncMock(return_value="e1"))
    monkeypatch.setattr(scan_mod.emby, "get_missing_episodes", AsyncMock(return_value=[
        {"code": "S03E01", "premiere_date": None},
    ]))
    out = run(scan_mod._emby_missing_codes(_tv()))
    assert out == [{"code": "S03E01", "premiere_date": None}]


def test_emby_missing_codes_carries_premiere_date(monkeypatch):
    """Emby premiere_date 被原样带出（第二数据源兜底所需）。"""
    monkeypatch.setattr(scan_mod.emby, "find_emby_id", AsyncMock(return_value="e1"))
    monkeypatch.setattr(scan_mod.emby, "get_missing_episodes", AsyncMock(return_value=[
        {"code": "S01E02", "premiere_date": "2020-05-01T00:00:00Z"},
    ]))
    out = run(scan_mod._emby_missing_codes(_tv()))
    assert out == [{"code": "S01E02", "premiere_date": "2020-05-01T00:00:00Z"}]


def test_emby_missing_codes_filters_empty_code(monkeypatch):
    """code 缺失（None/空）的条目仍被过滤，仅保留有 code 的 dict。"""
    monkeypatch.setattr(scan_mod.emby, "find_emby_id", AsyncMock(return_value="e1"))
    monkeypatch.setattr(scan_mod.emby, "get_missing_episodes", AsyncMock(return_value=[
        {"code": "S01E01", "premiere_date": None},
        {"code": None, "premiere_date": None},
        {"premiere_date": None},
    ]))
    out = run(scan_mod._emby_missing_codes(_tv()))
    assert out == [{"code": "S01E01", "premiere_date": None}]


# ---------------------------------------------------------------------------
# _filter_aired：已播出门控
# ---------------------------------------------------------------------------

def test_filter_aired_drops_no_airdate(monkeypatch):
    """TMDB 该季成功但不含该集 → 回退 Emby（也无）→ 未播出 → 剔除。"""
    monkeypatch.setattr(scan_mod.tmdb, "get_tv_season_air_dates",
                        AsyncMock(return_value={}))
    missing = [{"code": "S03E01", "premiere_date": None}]
    kept, failed = run(scan_mod._filter_aired(_tv(), missing))
    assert kept == [] and failed == set()


def test_filter_aired_keeps_aired(monkeypatch):
    """TMDB 有已过 air_date → 保留。"""
    monkeypatch.setattr(scan_mod.tmdb, "get_tv_season_air_dates",
                        AsyncMock(return_value={1: "2020-01-01"}))
    missing = [{"code": "S01E01", "premiere_date": None}]
    kept, failed = run(scan_mod._filter_aired(_tv(), missing))
    assert kept == [{"code": "S01E01", "premiere_date": None}]
    assert failed == set()


def test_filter_aired_tmdb_future_drops(monkeypatch):
    """TMDB air_date 在未来 → 未播出 → 剔除。"""
    monkeypatch.setattr(scan_mod.tmdb, "get_tv_season_air_dates",
                        AsyncMock(return_value={1: "2999-01-01"}))
    missing = [{"code": "S01E01", "premiere_date": None}]
    kept, failed = run(scan_mod._filter_aired(_tv(), missing))
    assert kept == [] and failed == set()


def test_filter_aired_embys_fallback(monkeypatch):
    """TMDB 无该集，Emby 有已过日期 → 保留。"""
    monkeypatch.setattr(scan_mod.tmdb, "get_tv_season_air_dates",
                        AsyncMock(return_value={}))
    missing = [{"code": "S01E01", "premiere_date": "2020-01-01T00:00:00Z"}]
    kept, failed = run(scan_mod._filter_aired(_tv(), missing))
    assert kept and kept[0]["code"] == "S01E01"


def test_filter_aired_embys_fallback_not_aired_drops(monkeypatch):
    """TMDB 无该集，Emby 日期也在未来 → 两源皆未播出 → 剔除。"""
    monkeypatch.setattr(scan_mod.tmdb, "get_tv_season_air_dates",
                        AsyncMock(return_value={}))
    missing = [{"code": "S01E01", "premiere_date": "2999-01-01T00:00:00Z"}]
    kept, failed = run(scan_mod._filter_aired(_tv(), missing))
    assert kept == [] and failed == set()


def test_filter_aired_none_sentinel_preserved(monkeypatch):
    """None 哨兵项原样保留（全量/电影模式），不套用门控、不触 TMDB。"""
    tmdb_mock = AsyncMock(return_value={})
    monkeypatch.setattr(scan_mod.tmdb, "get_tv_season_air_dates", tmdb_mock)
    kept, failed = run(scan_mod._filter_aired(_tv(), [None]))
    assert kept == [None] and failed == set()
    tmdb_mock.assert_not_awaited()


def test_filter_aired_movie_not_gated(monkeypatch):
    """movie 不套用已播出门控（原样保留），不触 TMDB。"""
    from types import SimpleNamespace

    tmdb_mock = AsyncMock(return_value={})
    monkeypatch.setattr(scan_mod.tmdb, "get_tv_season_air_dates", tmdb_mock)
    movie = SimpleNamespace(tmdb_id=42, title="电影", media_type="movie", id=2)
    missing = [{"code": "S01E01", "premiere_date": None}]
    kept, failed = run(scan_mod._filter_aired(movie, missing))
    assert kept == missing and failed == set()
    tmdb_mock.assert_not_awaited()


def test_filter_aired_tmdb_failure_marks_failed_and_drops(monkeypatch):
    """某季 TMDB 取数返回 None（失败）→ 记入 failed 且该季集不保留。"""
    monkeypatch.setattr(scan_mod.tmdb, "get_tv_season_air_dates",
                        AsyncMock(return_value=None))
    missing = [{"code": "S01E01", "premiere_date": "2020-01-01T00:00:00Z"}]
    kept, failed = run(scan_mod._filter_aired(_tv(), missing))
    assert kept == [] and failed == {1}


def test_filter_aired_tmdb_failure_one_season_keeps_other(monkeypatch):
    """多季：某季失败记 failed 且该季剔除，另一季正常按 air_date 保留。"""

    async def air_dates(tmdb_id, season_number):
        if season_number == 1:
            return None  # 失败哨兵
        return {1: "2020-01-01"}

    monkeypatch.setattr(scan_mod.tmdb, "get_tv_season_air_dates", AsyncMock(side_effect=air_dates))
    missing = [
        {"code": "S01E01", "premiere_date": "2020-01-01T00:00:00Z"},
        {"code": "S02E01", "premiere_date": None},
    ]
    kept, failed = run(scan_mod._filter_aired(_tv(), missing))
    assert [m["code"] for m in kept] == ["S02E01"]
    assert failed == {1}


def test_filter_aired_no_tmdb_id_uses_emby_only(monkeypatch):
    """media.tmdb_id 缺失 → 仅用 Emby premiere_date 单源判定，不触 TMDB。"""
    tmdb_mock = AsyncMock(return_value={})
    monkeypatch.setattr(scan_mod.tmdb, "get_tv_season_air_dates", tmdb_mock)
    media = _tv(tmdb_id=None)
    missing = [
        {"code": "S01E01", "premiere_date": "2020-01-01T00:00:00Z"},  # 已过 → 保留
        {"code": "S01E02", "premiere_date": None},  # 无日期 → 剔除
    ]
    kept, failed = run(scan_mod._filter_aired(media, missing))
    assert [m["code"] for m in kept] == ["S01E01"]
    assert failed == set()
    tmdb_mock.assert_not_awaited()
