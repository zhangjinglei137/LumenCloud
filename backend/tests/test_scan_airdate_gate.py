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
import json
import types
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  注册全部 ORM 模型
from app.database import Base
from app.models import Media, TaskQueue, TaskRun
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


# ---------------------------------------------------------------------------
# _scan_one 端到端：已播出门控接入 + TMDB 失败 fail-safe（Task 5）
# ---------------------------------------------------------------------------

@pytest.fixture()
def db():
    """独立 in-memory SQLite（StaticPool 共享连接）→ 返回 sessionmaker。"""
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


async def _seed_media(db, *, tmdb_id=42):
    async with db() as s:
        media = Media(title="剑来", media_type="tv", tmdb_id=tmdb_id, status="tracking")
        s.add(media)
        await s.commit()
        return media.id


def _patch_scan_env(monkeypatch, db, *, missing, air_dates):
    """scan 外部依赖全 mock：Emby 基线缺失集（dict）、TMDB 季 air_date、cloudSaver。

    - missing: `_emby_missing_codes` 返回值（dict 列表 / [None] 哨兵）
    - air_dates: `tmdb.get_tv_season_air_dates` 返回值（dict=成功 / None=失败哨兵）
    搜索链由各用例按需覆写 `_search_and_rank`，便于断言是否被触发。
    """
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    monkeypatch.setattr(scan_mod, "_emby_missing_codes", AsyncMock(return_value=missing))
    monkeypatch.setattr(scan_mod.tmdb, "get_tv_season_air_dates",
                        AsyncMock(return_value=air_dates))
    monkeypatch.setattr(
        scan_mod.cloudsaver, "share_info",
        AsyncMock(return_value={"pwd_id": "pd", "stoken": "st", "receive_code": "",
                               "fileSize": 9999}),
    )
    monkeypatch.setattr(
        scan_mod.cloudsaver, "share_list",
        AsyncMock(return_value={"list": []}),
    )
    monkeypatch.setattr(scan_mod, "_read_size_limits", AsyncMock(return_value=(0.0, 100.0, 0.0, 100.0)))
    monkeypatch.setattr(scan_mod, "_trigger_transfer", AsyncMock(return_value=None))
    return scan_mod


async def _read_scan_state(db, mid):
    async with db() as s:
        tr = (await s.execute(
            select(TaskRun).where(TaskRun.media_id == mid)
        )).scalars().first()
        tq = (await s.execute(
            select(TaskQueue).where(TaskQueue.media_id == mid)
        )).scalars().all()
        return tr, tq


def test_scan_one_skips_unaired_no_date(db, monkeypatch):
    """无播出日期集（premiere_date=None + TMDB 该季成功但空）→ 门控剔除 →
    不触发搜索、不入队，task_run skipped。"""
    scan_mod = _patch_scan_env(
        monkeypatch, db,
        missing=[{"code": "S03E01", "premiere_date": None}],
        air_dates={},  # TMDB 成功但该季无集 → 无日期 → 剔除
    )
    rank_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(scan_mod, "_search_and_rank", rank_mock)

    mid = run(_seed_media(db))
    rid = run(scan_mod._scan_one(mid))
    tr, tq = run(_read_scan_state(db, mid))

    assert tr.id == rid and tr.status == "skipped"
    rank_mock.assert_not_awaited()  # 门控短路：不搜索
    assert tq == []                  # 不入队


def test_scan_one_skips_on_tmdb_failure(db, monkeypatch):
    """TMDB 该季取数失败（None）→ 门控后无保留 → fail-safe skipped，文案标明季号；
    不触发搜索、不入队。"""
    scan_mod = _patch_scan_env(
        monkeypatch, db,
        missing=[{"code": "S03E01", "premiere_date": "2020-01-01T00:00:00Z"}],
        air_dates=None,  # 失败哨兵（即便 Emby 有已过日期，失败季仍不保留）
    )
    rank_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(scan_mod, "_search_and_rank", rank_mock)

    mid = run(_seed_media(db))
    rid = run(scan_mod._scan_one(mid))
    tr, tq = run(_read_scan_state(db, mid))

    assert tr.id == rid and tr.status == "skipped"
    assert "TMDB 播出日期取数失败" in tr.message
    assert "季 [3]" in tr.message
    rank_mock.assert_not_awaited()
    assert tq == []


def test_scan_one_enqueues_aired(db, monkeypatch):
    """已播出集（TMDB air_date 已过）→ 照常搜索入队（task_queue ready）。"""
    scan_mod = _patch_scan_env(
        monkeypatch, db,
        missing=[{"code": "S01E01", "premiere_date": None}],
        air_dates={1: "2020-01-01"},  # 已播出 → 保留
    )
    monkeypatch.setattr(scan_mod, "_search_and_rank",
                        AsyncMock(return_value=[{"title": "剑来 S01", "share_code": "abc123"}]))
    monkeypatch.setattr(
        scan_mod.cloudsaver, "share_list",
        AsyncMock(return_value={"list": [{
            "fileName": "S01E01.mkv", "fileId": "f1", "fileIdToken": "ft1",
            "isFolder": False, "size": 1024,
        }]}),
    )

    mid = run(_seed_media(db))
    rid = run(scan_mod._scan_one(mid))
    tr, tq = run(_read_scan_state(db, mid))

    assert tr.id == rid and tr.status == "success"
    assert [t.episode for t in tq] == ["S01E01"]
    assert tq[0].status == "ready"


def test_scan_one_same_show_three_groups_only_aired_enqueued(db, monkeypatch):
    """同剧同轮三组集组合集成：仅「已播出且媒体库缺失」的集入队，未来/无日期集不产出任务。

    场景：同一部剧 S01E01（TMDB 已过日期）/ S02E01（TMDB 未来日期）/ S03E01（TMDB
    成功但无该集且 Emby 无日期）三集皆不在 Emby 媒体库（基线 missing），且搜索候选
    同时提供三集文件（资源均存在）。执行一次 `_scan_one` 后断言：仅 S01E01 进入
    `task_queue`（status=ready），S02E01 / S03E01 因门控剔除而 matched_key 不命中 →
    不产生任何任务。若门控失效（未来/无日期集被保留），三集文件均会命中并各入队一次
    → 本用例失败，故可判别回归。
    """
    scan_mod = _patch_scan_env(
        monkeypatch, db,
        missing=[
            {"code": "S01E01", "premiere_date": None},  # 已过（TMDB S1）
            {"code": "S02E01", "premiere_date": None},  # 未来（TMDB S2）
            {"code": "S03E01", "premiere_date": None},  # 无日期（TMDB S3 成功但空）
        ],
        air_dates={},  # 占位；下面按季覆写为真实取值
    )

    async def air_dates_by_season(tmdb_id, season_number):
        return {
            1: {1: "2020-01-01"},  # 已过 → 保留
            2: {1: "2999-01-01"},  # 未来 → 剔除
            3: {},                 # 成功但该季无集 → 无日期 → 剔除
        }[season_number]

    monkeypatch.setattr(scan_mod.tmdb, "get_tv_season_air_dates",
                        AsyncMock(side_effect=air_dates_by_season))
    monkeypatch.setattr(scan_mod, "_search_and_rank",
                        AsyncMock(return_value=[{"title": "剑来", "share_code": "abc123"}]))
    monkeypatch.setattr(
        scan_mod.cloudsaver, "share_list",
        AsyncMock(return_value={"list": [
            {"fileName": "剑来.S01E01.mkv", "fileId": "f1", "fileIdToken": "ft1",
             "isFolder": False, "size": 1024},
            {"fileName": "剑来.S02E01.mkv", "fileId": "f2", "fileIdToken": "ft2",
             "isFolder": False, "size": 1024},
            {"fileName": "剑来.S03E01.mkv", "fileId": "f3", "fileIdToken": "ft3",
             "isFolder": False, "size": 1024},
        ]}),
    )

    mid = run(_seed_media(db))
    rid = run(scan_mod._scan_one(mid))
    tr, tq = run(_read_scan_state(db, mid))

    assert tr.id == rid and tr.status == "success"
    # 仅已播出且库中缺失的集入队；未来/无日期集不产生任何任务
    assert [t.episode for t in tq] == ["S01E01"]
    assert tq[0].status == "ready"
    assert tq[0].file_name == "剑来.S01E01.mkv"
    assert len(tq) == 1  # S02E01 / S03E01 未入队


# ---------------------------------------------------------------------------
# _scan_one 全量兜底路径门控（Task 6）：按 TMDB 全集播出日期门控
# ---------------------------------------------------------------------------

def _patch_full_mode_env(monkeypatch, db, *, all_eps, files=None, total_episodes=2):
    """tv 未收录全量（movie_missing）端到端 mock。

    复用 _patch_scan_env（missing=[None] 全量哨兵、Emby/TMDB 季/size/transfer），
    另 mock TMDB 全集播出日期（get_tv_all_episodes）、总集数（_media_total_episodes）、
    单候选搜索与文件列表。返回 scan_mod（其 _search_and_rank 为可断言的 AsyncMock）。
    """
    scan_mod = _patch_scan_env(
        monkeypatch, db,
        missing=[None],
        air_dates={},  # None 哨兵由 _filter_aired 原样保留，不触 TMDB 回源
    )
    monkeypatch.setattr(scan_mod.tmdb, "get_tv_all_episodes",
                        AsyncMock(return_value=all_eps))
    monkeypatch.setattr(scan_mod, "_search_and_rank", AsyncMock(return_value=[
        {"title": "剑来 全量", "share_code": "2c16748e7818"},
    ]))
    monkeypatch.setattr(
        scan_mod, "_cloudsaver_share_info",
        AsyncMock(return_value={"pwd_id": "pd", "stoken": "st", "receive_code": "",
                                "fileSize": 9999}),
    )
    monkeypatch.setattr(
        scan_mod.cloudsaver, "share_list",
        AsyncMock(return_value={"list": [
            {"fileName": n, "fileId": f"f_{n}", "fileIdToken": f"ft_{n}",
             "isFolder": False, "size": 1024 * 1024 * 1024}
            for n in (files or [])
        ]}),
    )
    monkeypatch.setattr(scan_mod, "_media_total_episodes",
                        AsyncMock(return_value=total_episodes))
    return scan_mod


def test_scan_one_tv_full_mode_skips_all_unaired(db, monkeypatch):
    """tv 全量兜底：TMDB 全集全未播出（未开播）→ 搜索前短路，不搜索、不入队。"""
    scan_mod = _patch_full_mode_env(
        monkeypatch, db,
        all_eps=[
            {"season": 1, "episode": 1, "air_date": "2999-01-01", "name": None},
            {"season": 1, "episode": 2, "air_date": "2999-01-02", "name": None},
        ],
    )
    mid = run(_seed_media(db))
    rid = run(scan_mod._scan_one(mid))
    tr, tq = run(_read_scan_state(db, mid))

    assert tr.id == rid and tr.status == "skipped"
    assert "TMDB 无已播出集（未开播），本轮跳过" in tr.message
    scan_mod._search_and_rank.assert_not_awaited()  # 门控短路：不搜索
    assert tq == []                                  # 不入队


def test_scan_one_tv_full_mode_skips_tmdb_all_eps_failure(db, monkeypatch):
    """tv 全量兜底：TMDB 全集取数失败（None）→ 搜索前短路，不搜索、不入队。"""
    scan_mod = _patch_full_mode_env(monkeypatch, db, all_eps=None)
    mid = run(_seed_media(db))
    rid = run(scan_mod._scan_one(mid))
    tr, tq = run(_read_scan_state(db, mid))

    assert tr.id == rid and tr.status == "skipped"
    assert "TMDB 播出日期取数失败" in tr.message
    scan_mod._search_and_rank.assert_not_awaited()
    assert tq == []


def test_scan_one_tv_full_mode_skips_missing_tmdb_id(db, monkeypatch):
    """tv 全量兜底：缺 tmdb_id，无法判定播出日期 → 搜索前短路，不触 TMDB 全集。"""
    scan_mod = _patch_full_mode_env(monkeypatch, db, all_eps=[])
    mid = run(_seed_media(db, tmdb_id=None))
    rid = run(scan_mod._scan_one(mid))
    tr, tq = run(_read_scan_state(db, mid))

    assert tr.id == rid and tr.status == "skipped"
    assert "缺 tmdb_id" in tr.message
    scan_mod.tmdb.get_tv_all_episodes.assert_not_awaited()
    scan_mod._search_and_rank.assert_not_awaited()
    assert tq == []


def test_scan_one_tv_full_mode_gates_unaired_files(db, monkeypatch):
    """tv 全量兜底：TMDB 含已播出集 → 已播出文件入队；未播出集文件被拒。"""
    scan_mod = _patch_full_mode_env(
        monkeypatch, db,
        all_eps=[
            {"season": 1, "episode": 1, "air_date": "2020-01-01", "name": None},  # 已播
            {"season": 1, "episode": 2, "air_date": "2999-01-01", "name": None},  # 未播
        ],
        files=["剑来.S01E01.mkv", "剑来.S01E02.mkv"],
        total_episodes=2,
    )
    mid = run(_seed_media(db))
    rid = run(scan_mod._scan_one(mid))
    tr, tq = run(_read_scan_state(db, mid))

    assert tr.id == rid and tr.status == "success"
    assert [t.episode for t in tq] == ["S01E01"]      # 仅已播出集入队
    assert tq[0].file_name == "剑来.S01E01.mkv"
    detail = json.loads(tr.scan_detail)
    assert detail["unrelated_filtered"] == 1          # 未播出集文件被拒并计数
