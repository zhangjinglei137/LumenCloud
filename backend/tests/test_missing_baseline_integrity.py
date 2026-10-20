"""missing-baseline-integrity 单测：P1 取数/候选 + P2 自检/替换/落库告警。

P1:
- 1.1 `_tmdb_aired_axis`：只读 episode_info_cache，air_date <= today 归一 SxxExx；
  缺 tmdb_id / 非 tv / 缓存为空 → None；季号集合供 season_mismatch。
- 1.2 `_merge_baseline_candidates`：逐项标 baseline_source（both/emby_missing/tmdb_axis），
  Emby 项 premiere_date 保留。
- 1.3 `_resolve_baseline_candidates`：统一入口，TMDB 轴不可用或已入库取数失败时降级为
  Emby 单源（degraded=true + degraded_reason），不改变 [None] 全量模式语义。

P2:
- 2.1 `_baseline_suspect`：硬矛盾 / 比例超阈值双条件；降级或已播轴为空不判。
- 2.2 suspect 轮候选仅保留 TMDB 轴（丢弃 Emby 候选）。
- 2.3 `scan_detail` 顶层落 baseline_suspect/baseline_detail，告警 24h 冷却。

风格对齐 test_scan_outcome_attribution.py：mock 外部依赖，独立 in-memory SQLite。
"""
import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  注册全部 ORM 模型
from app.database import Base
from app.models import (
    DownloadQueue,
    EpisodeInfoCache,
    Media,
    Notification,
    TaskQueue,
    TaskRun,
)
from app.routers import logs as logs_router
from app.services.notifier import EVENT_FLOW_ERROR, InAppNotifier, NotifierChain
from app.tasks import scan as scan_mod
from app.tasks import transfer as transfer_mod


def run(coro):
    return asyncio.run(coro)


def _media(**kw):
    base = dict(id=1, tmdb_id=100, media_type="tv", title="斗破苍穹")
    base.update(kw)
    return SimpleNamespace(**base)


def _ep(season, episode, air_date="2020-01-01"):
    return {"season": season, "episode": episode, "name": f"E{episode}", "air_date": air_date}


def _patch_tmdb(monkeypatch, episodes):
    monkeypatch.setattr(
        scan_mod.tmdb, "get_episode_info", AsyncMock(return_value=episodes),
    )


def _patch_ingested(monkeypatch, codes):
    monkeypatch.setattr(
        scan_mod.emby, "get_ingested_episode_codes", AsyncMock(return_value=set(codes)),
    )


# ---------------------------------------------------------------------------
# 1.1 TMDB 已播轴
# ---------------------------------------------------------------------------

def test_tmdb_axis_filters_future_and_normalizes(monkeypatch):
    _patch_tmdb(monkeypatch, [
        _ep(1, 1),
        _ep(1, 2, "2999-01-01"),  # 未来集 → 不在轴上
        _ep(2, 3, None),          # 无 air_date → 不在轴上
        _ep(2, 100),              # 三位集号保留
    ])
    codes, seasons = run(scan_mod._tmdb_aired_axis(_media()))
    assert codes == {"S01E01", "S02E100"}
    assert seasons == [1, 2]


def test_tmdb_axis_none_without_tmdb_id(monkeypatch):
    _patch_tmdb(monkeypatch, [_ep(1, 1)])
    assert run(scan_mod._tmdb_aired_axis(_media(tmdb_id=None))) is None


def test_tmdb_axis_none_for_movie(monkeypatch):
    _patch_tmdb(monkeypatch, [_ep(1, 1)])
    assert run(scan_mod._tmdb_aired_axis(_media(media_type="movie"))) is None


def test_tmdb_axis_none_on_empty_cache(monkeypatch):
    _patch_tmdb(monkeypatch, [])
    assert run(scan_mod._tmdb_aired_axis(_media())) is None


def test_tmdb_axis_empty_when_nothing_aired(monkeypatch):
    """缓存有集但全未播 → 轴可用但为空（不是 cache_empty）。"""
    _patch_tmdb(monkeypatch, [_ep(1, 1, "2999-01-01")])
    assert run(scan_mod._tmdb_aired_axis(_media())) == (set(), [])


# ---------------------------------------------------------------------------
# 1.2 并集候选与来源标识
# ---------------------------------------------------------------------------

def test_merge_candidates_marks_source_and_keeps_premiere_date():
    emby_missing = [
        {"code": "S01E01", "premiere_date": "2020-01-01T00:00:00Z"},
        {"code": "S01E02", "premiere_date": None},
    ]
    tmdb_axis_missing = [
        {"code": "S01E01", "premiere_date": None},
        {"code": "S01E03", "premiere_date": None},
    ]
    merged = scan_mod._merge_baseline_candidates(emby_missing, tmdb_axis_missing)
    by_code = {m["code"]: m for m in merged}

    assert set(by_code) == {"S01E01", "S01E02", "S01E03"}
    assert by_code["S01E01"]["baseline_source"] == "both"
    # Emby 项的 premiere_date 必须保留（_filter_aired 兜底依赖）
    assert by_code["S01E01"]["premiere_date"] == "2020-01-01T00:00:00Z"
    assert by_code["S01E02"]["baseline_source"] == "emby_missing"
    assert by_code["S01E03"]["baseline_source"] == "tmdb_axis"


def test_merge_candidates_preserves_none_sentinel():
    merged = scan_mod._merge_baseline_candidates([None], [])
    assert merged == [None]


# ---------------------------------------------------------------------------
# 1.3 统一入口：正常并集
# ---------------------------------------------------------------------------

def test_resolve_union_candidates_and_detail(monkeypatch):
    _patch_tmdb(monkeypatch, [_ep(1, 1), _ep(1, 2), _ep(1, 3), _ep(1, 4)])
    _patch_ingested(monkeypatch, {"S01E04"})
    emby_missing = [
        {"code": "S01E01", "premiere_date": "2020-01-01T00:00:00Z"},
        {"code": "S01E02", "premiere_date": "2020-01-02T00:00:00Z"},
    ]

    candidates, detail = run(scan_mod._resolve_baseline_candidates(_media(), emby_missing))

    assert {c["code"] for c in candidates} == {"S01E01", "S01E02", "S01E03"}
    assert {c["baseline_source"] for c in candidates} == {"both", "tmdb_axis"}
    assert detail["emby_missing_count"] == 2
    assert detail["tmdb_aired_count"] == 4
    assert detail["ingested_count"] == 1
    assert detail["ratio"] == 0.5
    assert detail["threshold"] == 0.5
    assert detail["source_used"] == "both"
    assert detail["degraded"] is False
    assert detail["degraded_reason"] is None
    assert detail["season_mismatch"] == []
    assert "baseline_suspect" not in detail  # 唯一事实来源是 scan_detail 顶层


def test_resolve_records_season_mismatch(monkeypatch):
    _patch_tmdb(monkeypatch, [_ep(1, 1)])
    _patch_ingested(monkeypatch, set())
    emby_missing = [{"code": "S02E01", "premiere_date": "2020-01-01T00:00:00Z"}]

    _, detail = run(scan_mod._resolve_baseline_candidates(_media(), emby_missing))

    assert detail["season_mismatch"] == [1, 2]


def test_resolve_full_mode_sentinel_unchanged(monkeypatch):
    """6.3 tv 未收录全量模式 [None]：不并入 TMDB 轴，也不产出 baseline_detail。"""
    _patch_tmdb(monkeypatch, [_ep(1, 1)])
    _patch_ingested(monkeypatch, set())

    candidates, detail = run(scan_mod._resolve_baseline_candidates(_media(), [None]))

    assert candidates == [None]
    assert detail is None  # 未做交叉校验 → 不伪造基线结论


# ---------------------------------------------------------------------------
# 1.3 统一入口：降级回 Emby 单源
# ---------------------------------------------------------------------------

def test_resolve_degraded_without_tmdb_id(monkeypatch):
    emby_missing = [{"code": "S01E01", "premiere_date": "2020-01-01T00:00:00Z"}]
    _patch_ingested(monkeypatch, set())

    candidates, detail = run(
        scan_mod._resolve_baseline_candidates(_media(tmdb_id=None), emby_missing)
    )

    assert [c["code"] for c in candidates] == ["S01E01"]
    assert candidates[0]["baseline_source"] == "emby_missing"
    assert candidates[0]["premiere_date"] == "2020-01-01T00:00:00Z"
    assert detail["degraded"] is True
    assert detail["degraded_reason"] == "no_tmdb_id"
    assert detail["source_used"] == "degraded_emby"
    assert "baseline_suspect" not in detail


def test_resolve_degraded_on_empty_cache(monkeypatch):
    _patch_tmdb(monkeypatch, [])
    _patch_ingested(monkeypatch, set())
    emby_missing = [{"code": "S01E01", "premiere_date": None}]

    candidates, detail = run(scan_mod._resolve_baseline_candidates(_media(), emby_missing))

    assert [c["code"] for c in candidates] == ["S01E01"]
    assert detail["degraded"] is True
    assert detail["degraded_reason"] == "cache_empty"
    assert detail["source_used"] == "degraded_emby"


def test_resolve_empty_axis_not_degraded(monkeypatch):
    """缓存有集但全未播：轴可用但为空 → 不降级、不除零，候选退化为 Emby 单源。"""
    _patch_tmdb(monkeypatch, [_ep(1, 1, "2999-01-01")])
    _patch_ingested(monkeypatch, set())
    emby_missing = [{"code": "S01E01", "premiere_date": "2999-01-01T00:00:00Z"}]

    candidates, detail = run(scan_mod._resolve_baseline_candidates(_media(), emby_missing))

    assert [c["code"] for c in candidates] == ["S01E01"]
    assert detail["degraded"] is False
    assert detail["degraded_reason"] is None
    assert detail["source_used"] == "emby_missing"
    assert detail["tmdb_aired_count"] == 0
    assert detail["ratio"] == 0.0


def test_resolve_degraded_on_emby_unavailable(monkeypatch):
    _patch_tmdb(monkeypatch, [_ep(1, 1)])
    monkeypatch.setattr(
        scan_mod.emby, "get_ingested_episode_codes",
        AsyncMock(side_effect=scan_mod.emby.EmbyUnavailable("boom")),
    )
    emby_missing = [{"code": "S01E02", "premiere_date": None}]

    candidates, detail = run(scan_mod._resolve_baseline_candidates(_media(), emby_missing))

    assert [c["code"] for c in candidates] == ["S01E02"]
    assert detail["degraded"] is True
    assert detail["degraded_reason"] == "ingested_unavailable"
    assert detail["source_used"] == "degraded_emby"
    assert detail["tmdb_aired_count"] == 1
    assert detail["ingested_count"] == 0


# ---------------------------------------------------------------------------
# 2.1 基线自检判定（D2 双条件）
# ---------------------------------------------------------------------------

def _baseline_detail(**kw):
    base = {
        "emby_missing_count": 0,
        "tmdb_aired_count": 0,
        "ingested_count": 0,
        "ratio": 0.0,
        "threshold": 0.5,
        "source_used": "emby_missing",
        "degraded": False,
        "degraded_reason": None,
        "season_mismatch": [],
    }
    base.update(kw)
    return base


def test_baseline_suspect_hard_contradiction():
    detail = _baseline_detail(
        emby_missing_count=300, tmdb_aired_count=275, ratio=300 / 275,
    )
    assert scan_mod._baseline_suspect(detail) is True


def test_baseline_suspect_doupo_two_counts_over_aired():
    """6.1 斗破：239 + 271 = 510 > 275 → 硬矛盾（不依赖阈值）。"""
    detail = _baseline_detail(
        emby_missing_count=239, tmdb_aired_count=275, ingested_count=271,
        ratio=239 / 275,
    )
    assert scan_mod._baseline_suspect(detail) is True


def test_baseline_suspect_new_show_not_suspect():
    """6.1 新剧整剧未入库：缺 100 / 已播 100 / 已入库 0 → 不判异常。"""
    detail = _baseline_detail(
        emby_missing_count=100, tmdb_aired_count=100, ingested_count=0, ratio=1.0,
    )
    assert scan_mod._baseline_suspect(detail) is False


def test_baseline_suspect_ratio_over_threshold():
    """6.1 比例条件：已入库 40、缺 60 / 已播 100（0.6 > 0.5）→ 判异常。"""
    detail = _baseline_detail(
        emby_missing_count=60, tmdb_aired_count=100, ingested_count=40, ratio=0.6,
    )
    assert scan_mod._baseline_suspect(detail) is True


def test_baseline_suspect_ratio_at_threshold_not_suspect():
    detail = _baseline_detail(
        emby_missing_count=1, tmdb_aired_count=2, ingested_count=1, ratio=0.5,
    )
    assert scan_mod._baseline_suspect(detail) is False


def test_baseline_suspect_zero_aired_not_suspect():
    detail = _baseline_detail(emby_missing_count=10, tmdb_aired_count=0, ratio=0.0)
    assert scan_mod._baseline_suspect(detail) is False


def test_baseline_suspect_degraded_not_suspect():
    detail = _baseline_detail(
        emby_missing_count=300, tmdb_aired_count=275, ratio=300 / 275, degraded=True,
    )
    assert scan_mod._baseline_suspect(detail) is False


# ---------------------------------------------------------------------------
# 2.2 suspect 轮替换候选（仅 TMDB 轴）
# ---------------------------------------------------------------------------

def test_resolve_suspect_replaces_candidates_with_tmdb_axis(monkeypatch):
    """斗破苍穹实况：Emby 缺 240 / TMDB 已播 275 / 已入库 271 → 候选收敛为 4 集。"""
    _patch_tmdb(monkeypatch, [_ep(1, i) for i in range(1, 276)])
    _patch_ingested(monkeypatch, {scan_mod._fmt_episode(1, i) for i in range(1, 272)})
    emby_missing = [
        {"code": scan_mod._fmt_episode(1, i), "premiere_date": "2020-01-01T00:00:00Z"}
        for i in range(1, 241)
    ]

    candidates, detail = run(scan_mod._resolve_baseline_candidates(_media(), emby_missing))

    assert [c["code"] for c in candidates] == [
        scan_mod._fmt_episode(1, i) for i in range(272, 276)
    ]
    assert {c["baseline_source"] for c in candidates} == {"tmdb_axis"}
    assert detail["source_used"] == "tmdb_axis"
    assert "baseline_suspect" not in detail


def test_resolve_normal_round_keeps_union(monkeypatch):
    """比例未超阈值 → 不替换，并集候选保留两源来源标识。"""
    _patch_tmdb(monkeypatch, [_ep(1, i) for i in range(1, 11)])
    _patch_ingested(monkeypatch, set())
    emby_missing = [{"code": "S01E01", "premiere_date": None}]

    candidates, detail = run(scan_mod._resolve_baseline_candidates(_media(), emby_missing))

    assert len(candidates) == 10
    assert {c["baseline_source"] for c in candidates} == {"both", "tmdb_axis"}
    assert detail["source_used"] == "both"
    assert scan_mod._baseline_suspect(detail) is False


def test_resolve_season_mismatch_suppresses_replacement(monkeypatch):
    """6.2 两源季结构不对称 → 仍标 suspect，但不丢弃 Emby 候选（保留并集）。"""
    _patch_tmdb(monkeypatch, [_ep(1, i) for i in range(1, 11)])
    _patch_ingested(monkeypatch, {scan_mod._fmt_episode(1, i) for i in range(1, 10)})
    emby_missing = [
        {"code": "S02E01", "premiere_date": "2020-01-01T00:00:00Z"},
        {"code": "S02E02", "premiere_date": "2020-01-01T00:00:00Z"},
    ]

    candidates, detail = run(scan_mod._resolve_baseline_candidates(_media(), emby_missing))

    assert {c["code"] for c in candidates} == {"S02E01", "S02E02", "S01E10"}
    assert detail["season_mismatch"] == [1, 2]
    assert scan_mod._baseline_suspect(detail) is True
    assert detail["source_used"] == "both"  # 未替换


# ---------------------------------------------------------------------------
# 2.3 落库收口 + 告警（24h 冷却）
# ---------------------------------------------------------------------------

class _RecordingNotifier:
    def __init__(self) -> None:
        self.events: list = []

    async def notify(self, event) -> None:
        self.events.append(event)


@pytest.fixture(autouse=True)
def _reset_scan_alert_cooldown():
    scan_mod._scan_alert_cooldown.clear()
    yield
    scan_mod._scan_alert_cooldown.clear()


@pytest.fixture()
def db():
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


async def _seed_media(db, *, title="斗破苍穹", tmdb_id=42):
    async with db() as s:
        media = Media(title=title, media_type="tv", tmdb_id=tmdb_id, status="tracking")
        s.add(media)
        await s.commit()
        return media.id


def _patch_scan_env(monkeypatch, db, *, missing, air_date="2020-01-01"):
    monkeypatch.setattr(scan_mod, "async_session", db)
    monkeypatch.setattr(scan_mod, "_emby_missing_codes", AsyncMock(return_value=missing))

    async def _fake_air_dates(tmdb_id, season_number):
        out = {}
        for c in missing or []:
            code = c.get("code") if isinstance(c, dict) else None
            if code and scan_mod._season_of_key(code) == season_number:
                ep = scan_mod._parse_episode_number(code)
                if ep is not None:
                    out[ep] = air_date
        return out

    monkeypatch.setattr(scan_mod.tmdb, "get_tv_season_air_dates", _fake_air_dates)
    monkeypatch.setattr(scan_mod, "_search_and_rank", AsyncMock(return_value=[]))
    monkeypatch.setattr(scan_mod, "_read_size_limits",
                        AsyncMock(return_value=(0.0, 100.0, 0.0, 100.0)))
    monkeypatch.setattr(scan_mod, "_media_total_episodes", AsyncMock(return_value=8))
    monkeypatch.setattr(scan_mod, "_trigger_transfer", AsyncMock(return_value=None))


def _read_detail(db, media_id):
    async def _case():
        async with db() as s:
            return (
                await s.execute(select(TaskRun).where(TaskRun.media_id == media_id))
            ).scalars().first()

    return json.loads(run(_case()).scan_detail)


def test_alert_baseline_suspect_body_and_cooldown(monkeypatch):
    recorder = _RecordingNotifier()
    monkeypatch.setattr(scan_mod, "notifier", recorder)
    detail = _baseline_detail(
        emby_missing_count=240, tmdb_aired_count=275, ingested_count=271,
        ratio=240 / 275, source_used="tmdb_axis",
    )

    run(scan_mod._alert_baseline_suspect(7, "斗破苍穹", detail))
    run(scan_mod._alert_baseline_suspect(7, "斗破苍穹", detail))  # 24h 内第二轮

    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event.event_type == EVENT_FLOW_ERROR
    for token in ("240", "275", "271", "0.87", "0.5", "tmdb_axis"):
        assert token in event.body


def test_no_missing_shortcut_skips_baseline_resolution(db, monkeypatch):
    """emby_missing 为空 → 不解析、不写 baseline_*，保持 no_missing 快速路径。"""
    _patch_scan_env(monkeypatch, db, missing=[])
    resolve = AsyncMock()
    monkeypatch.setattr(scan_mod, "_resolve_baseline_candidates", resolve)
    mid = run(_seed_media(db))

    run(scan_mod._scan_one(mid))

    detail = _read_detail(db, mid)
    assert detail["skip_reason"] == "no_missing"
    assert "baseline_detail" not in detail
    assert "baseline_suspect" not in detail
    resolve.assert_not_awaited()


def test_scan_detail_writes_baseline_suspect_and_alert(db, monkeypatch):
    """suspect 轮：scan_detail 顶层落布尔与明细，且立即告警一次。"""
    _patch_scan_env(monkeypatch, db, missing=[{"code": "S01E01", "premiere_date": None}])
    baseline = _baseline_detail(
        emby_missing_count=240, tmdb_aired_count=275, ingested_count=271,
        ratio=240 / 275, source_used="tmdb_axis",
    )
    monkeypatch.setattr(
        scan_mod, "_resolve_baseline_candidates",
        AsyncMock(return_value=(
            [{"code": "S01E01", "premiere_date": None, "baseline_source": "tmdb_axis"}],
            baseline,
        )),
    )
    recorder = _RecordingNotifier()
    monkeypatch.setattr(scan_mod, "notifier", recorder)
    mid = run(_seed_media(db))

    run(scan_mod._scan_one(mid))

    detail = _read_detail(db, mid)
    assert detail["baseline_suspect"] is True
    assert detail["baseline_detail"]["source_used"] == "tmdb_axis"
    assert "baseline_suspect" not in detail["baseline_detail"]
    assert len(recorder.events) == 1


def test_gated_out_branch_keeps_baseline_detail(db, monkeypatch):
    """短路分支（gated_out）也带基线结论：_finish 统一收口。"""
    _patch_scan_env(monkeypatch, db, missing=[{"code": "S01E01", "premiere_date": None}],
                    air_date="2999-01-01")
    baseline = _baseline_detail(
        emby_missing_count=1, tmdb_aired_count=10, ingested_count=0, ratio=0.1,
    )
    monkeypatch.setattr(
        scan_mod, "_resolve_baseline_candidates",
        AsyncMock(return_value=(
            [{"code": "S01E01", "premiere_date": None, "baseline_source": "both"}],
            baseline,
        )),
    )
    mid = run(_seed_media(db))

    run(scan_mod._scan_one(mid))

    detail = _read_detail(db, mid)
    assert detail["skip_reason"] == "gated_out"
    assert detail["baseline_suspect"] is False
    assert detail["baseline_detail"]["emby_missing_count"] == 1


# ---------------------------------------------------------------------------
# P3：真实链路追溯 / 去重 / 只读缓存
# ---------------------------------------------------------------------------

def _video(name, size=1 * 1024 ** 3):
    return {"fileName": name, "fileId": f"f_{name}", "fileIdToken": "ft",
            "isFolder": False, "size": size}


def _patch_real_chain_env(monkeypatch, db, *, emby_missing, tmdb_episodes,
                          ingested, files):
    """真实 scan 链路，仅 mock 外部服务（Emby / TMDB / cloudSaver / 容量与触发）。"""
    monkeypatch.setattr(scan_mod, "async_session", db)
    monkeypatch.setattr(scan_mod.emby, "find_emby_id", AsyncMock(return_value="emby-1"))
    monkeypatch.setattr(scan_mod.emby, "get_missing_episodes",
                        AsyncMock(return_value=emby_missing))
    monkeypatch.setattr(scan_mod.emby, "get_ingested_episode_codes",
                        AsyncMock(return_value=set(ingested)))
    monkeypatch.setattr(scan_mod.tmdb, "get_episode_info",
                        AsyncMock(return_value=tmdb_episodes))

    air = {
        e["episode"]: e["air_date"] for e in tmdb_episodes if e["season"] == 1
    }

    async def _fake_air_dates(tmdb_id, season_number):
        return air if season_number == 1 else {}

    monkeypatch.setattr(scan_mod.tmdb, "get_tv_season_air_dates", _fake_air_dates)
    # 真实 _search_and_rank：只 mock TMDB 详情（年份/别名）与 cloudSaver 搜索
    monkeypatch.setattr(
        scan_mod.tmdb, "get_by_tmdb_id",
        AsyncMock(return_value={"year": 2020, "aliases": []}),
    )
    monkeypatch.setattr(
        scan_mod.cloudsaver, "search",
        AsyncMock(return_value=[{
            "title": "斗破苍穹 S01",
            "cloud_links": [{"cloud_type": "quark",
                             "link": "https://pan.quark.cn/s/share1"}],
        }]),
    )
    monkeypatch.setattr(
        scan_mod.cloudsaver, "share_info",
        AsyncMock(return_value={"pwd_id": "pd", "stoken": "st", "receive_code": "",
                                "fileSize": 9999}),
    )
    monkeypatch.setattr(scan_mod.cloudsaver, "share_list",
                        AsyncMock(return_value={"list": files}))
    monkeypatch.setattr(scan_mod, "_trigger_transfer", AsyncMock(return_value=None))


def _read_runs(db, media_id):
    async def _case():
        async with db() as s:
            return (
                await s.execute(
                    select(TaskRun).where(TaskRun.media_id == media_id)
                    .order_by(TaskRun.id.asc())
                )
            ).scalars().all()

    return run(_case())


def _count_rows(db, model, media_id):
    async def _case():
        async with db() as s:
            return len(
                (await s.execute(
                    select(model.id).where(model.media_id == media_id)
                )).scalars().all()
            )

    return run(_case())


def _codes(db, model, media_id):
    async def _case():
        async with db() as s:
            return sorted(
                (await s.execute(
                    select(model.episode).where(model.media_id == media_id)
                )).scalars().all()
            )

    return run(_case())


def test_new_episode_searches_and_enqueues_on_real_chain(db, monkeypatch):
    """3.1 斗破苍穹实况：Emby 240 / TMDB 已播 275 / 已入库 271 → 4 集新缺口真实入队。"""
    emby_missing = [
        {"code": scan_mod._fmt_episode(1, i), "premiere_date": "2020-01-01T00:00:00Z"}
        for i in range(1, 241)
    ]
    tmdb_episodes = [_ep(1, i) for i in range(1, 276)]
    ingested = {scan_mod._fmt_episode(1, i) for i in range(1, 272)}
    files = [_video(f"斗破苍穹 S01E{i}.mkv") for i in range(272, 276)]
    _patch_real_chain_env(monkeypatch, db, emby_missing=emby_missing,
                          tmdb_episodes=tmdb_episodes, ingested=ingested, files=files)
    recorder = _RecordingNotifier()
    monkeypatch.setattr(scan_mod, "notifier", recorder)
    mid = run(_seed_media(db))

    run(scan_mod._scan_one(mid))

    tr = _read_runs(db, mid)[0]
    detail = json.loads(tr.scan_detail)
    expected = [scan_mod._fmt_episode(1, i) for i in range(272, 276)]

    assert detail["baseline_suspect"] is True
    assert detail["baseline_detail"]["emby_missing_count"] == 240
    assert detail["baseline_detail"]["tmdb_aired_count"] == 275
    assert detail["baseline_detail"]["ingested_count"] == 271
    assert detail["baseline_detail"]["source_used"] == "tmdb_axis"
    assert detail["missing_total"] == 4
    assert [i["episode"] for i in detail["missing_items"]] == expected
    assert {i["baseline_source"] for i in detail["missing_items"]} == {"tmdb_axis"}
    assert all(i["result"] == "enqueued" for i in detail["missing_items"])
    assert detail["enqueued"] == 4
    assert _codes(db, TaskQueue, mid) == expected
    assert len(recorder.events) == 1  # 基线异常告警（D3 首轮立即告警）


def test_repeated_candidates_produce_single_download_queue(db, monkeypatch):
    """3.2 同一集连续两轮候选 → TaskQueue/DownloadQueue 各仅一条（UNIQUE 去重）。"""
    files = [_video("斗破苍穹 S01E01.mkv")]
    _patch_real_chain_env(
        monkeypatch, db,
        emby_missing=[{"code": "S01E01", "premiere_date": "2020-01-01T00:00:00Z"}],
        tmdb_episodes=[_ep(1, 1)],
        ingested=set(),
        files=files,
    )
    monkeypatch.setattr(transfer_mod, "async_session", db)
    mid = run(_seed_media(db))

    run(scan_mod._scan_one(mid))
    promoted = run(transfer_mod._fetch_from_task_queue())
    run(scan_mod._scan_one(mid))

    assert promoted == 1
    assert _count_rows(db, TaskQueue, mid) == 1
    assert _count_rows(db, DownloadQueue, mid) == 1


async def _read_log(db, run_id):
    async with db() as s:
        return await logs_router.get_log(run_id, admin=None, session=s)


async def _list_logs(db):
    async with db() as s:
        return await logs_router.list_logs(
            admin=None, session=s, task_type=None, status=None, media_id=None,
            tmdb_id=None, title=None, limit=50, offset=0,
        )


def test_traceability_readback_through_logs_router(db, monkeypatch):
    """4.1 写入后经 routers/logs.py 读回：baseline_detail 全键 + 顶层布尔自洽。"""
    _patch_scan_env(monkeypatch, db, missing=[{"code": "S01E01", "premiere_date": None}])
    baseline = _baseline_detail(
        emby_missing_count=1, tmdb_aired_count=10, ingested_count=0,
        ratio=0.1, source_used="both",
    )
    monkeypatch.setattr(
        scan_mod, "_resolve_baseline_candidates",
        AsyncMock(return_value=(
            [{"code": "S01E01", "premiere_date": None, "baseline_source": "both"}],
            baseline,
        )),
    )
    mid = run(_seed_media(db))

    run_id = run(scan_mod._scan_one(mid))
    payload = run(_read_log(db, run_id))
    detail = payload["scan_detail"]

    assert set(detail["baseline_detail"]) == {
        "emby_missing_count", "tmdb_aired_count", "ingested_count", "ratio",
        "threshold", "source_used", "degraded", "degraded_reason", "season_mismatch",
    }
    assert detail["baseline_suspect"] == scan_mod._baseline_suspect(detail["baseline_detail"])
    assert detail["missing_items"][0]["baseline_source"] == "both"


def test_legacy_scan_detail_readable(db):
    """4.2 旧 scan_detail（无 baseline_* 键）经 logs 路由读回不抛异常。"""
    legacy = {
        "missing_total": 2,
        "skip_reason": "no_candidates",
        "missing_items": [{"episode": "S01E01", "result": "not_found"}],
    }

    async def _seed():
        async with db() as s:
            run_row = TaskRun(
                task_type="scan_media", media_id=mid, status="skipped",
                message="旧记录", scan_detail=json.dumps(legacy),
            )
            s.add(run_row)
            await s.commit()
            return run_row.id

    mid = run(_seed_media(db))
    run_id = run(_seed())

    payload = run(_read_log(db, run_id))
    assert payload["scan_detail"]["missing_total"] == 2
    assert "baseline_detail" not in payload["scan_detail"]
    assert payload["scan_detail"].get("baseline_suspect") is None

    listing = run(_list_logs(db))
    assert listing["items"][0]["scan_detail"]["skip_reason"] == "no_candidates"


def test_scan_keeps_episode_info_cache_readonly(db, monkeypatch):
    """4.3 巡检只读 episode_info_cache：有 SELECT、无 INSERT/UPDATE/DELETE。"""
    async def _seed_cache():
        async with db() as s:
            for i in range(1, 4):
                s.add(EpisodeInfoCache(
                    tmdb_id=42, season=1, episode=i, name=f"E{i}",
                    air_date="2020-01-01",
                ))
            await s.commit()

    run(_seed_cache())
    _patch_scan_env(monkeypatch, db, missing=[{"code": "S01E01", "premiere_date": None}])
    monkeypatch.setattr(scan_mod.tmdb, "async_session", db)
    mid = run(_seed_media(db))

    statements: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = db.kw["bind"]
    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        run(scan_mod._scan_one(mid))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)

    assert any("episode_info_cache" in s for s in statements), "应读取 episode_info_cache"
    writes = [
        s for s in statements
        if re.search(r"\b(INSERT|UPDATE|DELETE)\b[^;]*episode_info_cache", s, re.I)
    ]
    assert writes == []


def test_no_new_alembic_revision():
    """4.3 本次 change 无数据库迁移：alembic/versions 数量与改动前一致（20）。"""
    versions = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    files = sorted(p.name for p in versions.glob("*.py"))
    assert len(files) == 20
    assert not any("baseline" in name.lower() for name in files)


# ---------------------------------------------------------------------------
# 5.1 斗破苍穹场景端到端（真实链路 + 真实站内通知表）
# ---------------------------------------------------------------------------

def _flow_errors(db):
    async def _case():
        async with db() as s:
            return (
                await s.execute(
                    select(Notification)
                    .where(Notification.event_type == EVENT_FLOW_ERROR)
                    .order_by(Notification.id.asc())
                )
            ).scalars().all()

    return run(_case())


def test_integration_suspect_scenario_alert_dedupe_and_detail(db, monkeypatch):
    """5.1 Emby 239 / TMDB 已播 275 / 已入库 271：候选收敛 4 集、告警恰好 1 条且 24h 去重。"""
    emby_missing = [
        {"code": scan_mod._fmt_episode(1, i), "premiere_date": "2020-01-01T00:00:00Z"}
        for i in range(1, 240)  # 239 条历史缺口
    ]
    tmdb_episodes = [_ep(1, i) for i in range(1, 276)]
    ingested = {scan_mod._fmt_episode(1, i) for i in range(1, 272)}
    files = [_video(f"斗破苍穹 S01E{i}.mkv") for i in range(272, 276)]
    _patch_real_chain_env(monkeypatch, db, emby_missing=emby_missing,
                          tmdb_episodes=tmdb_episodes, ingested=ingested, files=files)
    # 真实站内通道（不配 PushPlus，避免外部网络）→ notifications 表可断言
    monkeypatch.setattr(
        scan_mod, "notifier",
        NotifierChain([InAppNotifier(session_factory=db)]),
    )
    mid = run(_seed_media(db))
    expected = [scan_mod._fmt_episode(1, i) for i in range(272, 276)]

    # 第一轮：真实巡检链路（外部服务 mock）
    run(scan_mod._scan_one(mid))

    detail = json.loads(_read_runs(db, mid)[0].scan_detail)
    assert detail["baseline_suspect"] is True
    assert detail["baseline_detail"]["source_used"] == "tmdb_axis"
    assert detail["baseline_detail"]["emby_missing_count"] == 239
    assert detail["missing_total"] == 4
    assert [i["episode"] for i in detail["missing_items"]] == expected
    assert {i["baseline_source"] for i in detail["missing_items"]} == {"tmdb_axis"}
    # Emby 的 239 条历史缺口未进入候选
    emby_only = {scan_mod._fmt_episode(1, i) for i in range(1, 240)}
    assert emby_only.isdisjoint(i["episode"] for i in detail["missing_items"])

    events = _flow_errors(db)
    assert len(events) == 1
    assert "基线异常" in events[0].title
    assert "239" in events[0].body and "275" in events[0].body
    assert "tmdb_axis" in events[0].body

    # 第二轮：24h 冷却内不重复告警，scan_detail 形状保持
    run(scan_mod._scan_one(mid))
    runs = _read_runs(db, mid)
    assert len(runs) == 2
    second = json.loads(runs[1].scan_detail)
    assert second["baseline_suspect"] is True
    assert second["missing_total"] == 4
    assert len(_flow_errors(db)) == 1


# ---------------------------------------------------------------------------
# 6.x 审查修复
# ---------------------------------------------------------------------------

def test_full_mode_scan_writes_no_baseline_conclusion(db, monkeypatch):
    """6.3 全量兜底模式（[None] 哨兵）不写 baseline_*，也不产生基线告警。"""
    _patch_scan_env(monkeypatch, db, missing=[None])
    monkeypatch.setattr(
        scan_mod.tmdb, "get_tv_all_episodes",
        AsyncMock(return_value=[{"season": 1, "episode": 1, "air_date": "2020-01-01"}]),
    )
    monkeypatch.setattr(
        scan_mod, "notifier",
        NotifierChain([InAppNotifier(session_factory=db)]),
    )
    mid = run(_seed_media(db))

    run(scan_mod._scan_one(mid))

    detail = _read_detail(db, mid)
    assert "baseline_detail" not in detail
    assert "baseline_suspect" not in detail
    assert _flow_errors(db) == []


def _seed_done(db, media_id, episode):
    async def _case():
        async with db() as s:
            s.add(DownloadQueue(
                media_id=media_id, episode=episode, file_name=f"{episode}.mkv",
                file_size=1024, share_code="share1", status="done",
            ))
            await s.commit()

    run(_case())


def _dq_rows(db, media_id):
    async def _case():
        async with db() as s:
            return (
                await s.execute(
                    select(DownloadQueue).where(DownloadQueue.media_id == media_id)
                )
            ).scalars().all()

    return run(_case())


def test_done_row_uses_premerge_emby_missing(db, monkeypatch):
    """6.4 done 行「已收录」判定用合并前 Emby 缺失集：仅 TMDB 轴缺的集直接解除防重。"""
    _patch_real_chain_env(
        monkeypatch, db,
        emby_missing=[{"code": "S01E01", "premiere_date": "2020-01-01T00:00:00Z"}],
        tmdb_episodes=[_ep(1, 1), _ep(1, 2)],
        ingested=set(),
        files=[],
    )
    mid = run(_seed_media(db))
    _seed_done(db, mid, "S01E01")  # Emby 仍报缺失 → 宽限递进
    _seed_done(db, mid, "S01E02")  # 仅 TMDB 轴认为缺 → 视为已收录，解除防重

    run(scan_mod._scan_one(mid))

    rows = {r.episode: r for r in _dq_rows(db, mid)}
    assert "S01E02" not in rows
    assert rows["S01E01"].status == "failed"
    assert rows["S01E01"].retry_count == 1


def test_scan_mismatch_keeps_emby_candidates_and_alerts(db, monkeypatch):
    """6.2 端到端：季结构不一致的 suspect 轮仍告警，但 Emby 独有季候选保留在 scan_detail。"""
    _patch_real_chain_env(
        monkeypatch, db,
        emby_missing=[
            {"code": "S02E01", "premiere_date": "2020-01-01T00:00:00Z"},
            {"code": "S02E02", "premiere_date": "2020-01-01T00:00:00Z"},
        ],
        tmdb_episodes=[_ep(1, i) for i in range(1, 11)],
        ingested={scan_mod._fmt_episode(1, i) for i in range(1, 10)},
        files=[],
    )
    monkeypatch.setattr(
        scan_mod, "notifier",
        NotifierChain([InAppNotifier(session_factory=db)]),
    )
    mid = run(_seed_media(db))

    run(scan_mod._scan_one(mid))

    detail = _read_detail(db, mid)
    assert detail["baseline_suspect"] is True
    assert detail["baseline_detail"]["season_mismatch"] == [1, 2]
    assert detail["baseline_detail"]["source_used"] == "both"
    episodes = {i["episode"] for i in detail["missing_items"]}
    assert {"S02E01", "S02E02"} <= episodes  # Emby 独有季候选未被丢弃
    assert "S01E10" in episodes
    assert len(_flow_errors(db)) == 1
