"""大小上下限过滤单测（change media-min-size-and-emby-redownload 车道 B / 2.1 + 2.2）。

覆盖：
- _size_limits 四值（ep_min/ep_max/movie_min/movie_max）优先级：media 覆盖 > 全局 > settings 默认
- _size_bounds_gb 按 media_type 选组
- _size_out_of_bounds：None/0 不限制、边界值放行、低于下限/高于上限
- 端到端 scan 入队路径：20MB 被下限拦截、超上限拦截、等于边界放行、未知大小跳过、
  单影视覆盖生效、min=0 不限制、不中断同轮其它文件、纯数字兜底分支同样受上下限约束
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.database import Base
from app.models import Media, SystemConfig, TaskQueue, TaskRun

GB = 1024 ** 3


def run(coro):
    return asyncio.run(coro)


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


# ---------------------------------------------------------------------------
# 1) _size_limits：四值优先级
# ---------------------------------------------------------------------------

def test_size_limits_default_priority(db):
    from app.config import settings
    from app.tasks import scan as scan_mod

    async def go():
        async with db() as s:
            media = Media(title="t", media_type="tv", tmdb_id=None, status="tracking")
            s.add(media)
            await s.commit()
            return await scan_mod._size_limits(s, media)

    ep_min, ep_max, movie_min, movie_max = run(go())
    assert (ep_min, ep_max) == (
        settings.DEFAULT_MIN_EPISODE_SIZE_GB, settings.DEFAULT_MAX_EPISODE_SIZE_GB,
    )
    assert (movie_min, movie_max) == (
        settings.DEFAULT_MIN_MOVIE_SIZE_GB, settings.DEFAULT_MAX_MOVIE_SIZE_GB,
    )


def test_size_limits_global_config_overrides_default(db):
    from app.tasks import scan as scan_mod

    async def go():
        async with db() as s:
            s.add_all([
                SystemConfig(key="min_episode_size_gb", value="2.5"),
                SystemConfig(key="min_movie_size_gb", value="7.5"),
            ])
            media = Media(title="t", media_type="tv", tmdb_id=None, status="tracking")
            s.add(media)
            await s.commit()
            return await scan_mod._size_limits(s, media)

    ep_min, _ep_max, movie_min, _movie_max = run(go())
    assert ep_min == 2.5
    assert movie_min == 7.5


def test_size_limits_media_override_beats_global(db):
    from app.tasks import scan as scan_mod

    async def go():
        async with db() as s:
            s.add(SystemConfig(key="min_episode_size_gb", value="2.5"))
            media = Media(title="t", media_type="tv", tmdb_id=None, status="tracking",
                          min_episode_size_gb=3.5)
            s.add(media)
            await s.commit()
            return await scan_mod._size_limits(s, media)

    ep_min, _ep_max, _movie_min, _movie_max = run(go())
    assert ep_min == 3.5


def test_size_limits_media_zero_override_means_no_limit(db):
    """单影视显式 0 覆盖全局（0 非 None → 生效为 0 = 不限制）。"""
    from app.tasks import scan as scan_mod

    async def go():
        async with db() as s:
            s.add(SystemConfig(key="min_episode_size_gb", value="2.5"))
            media = Media(title="t", media_type="tv", tmdb_id=None, status="tracking",
                          min_episode_size_gb=0)
            s.add(media)
            await s.commit()
            return await scan_mod._size_limits(s, media)

    ep_min, _ep_max, _movie_min, _movie_max = run(go())
    assert ep_min == 0


# ---------------------------------------------------------------------------
# 2) _size_bounds_gb / _size_out_of_bounds
# ---------------------------------------------------------------------------

def test_size_bounds_selects_group_by_media_type():
    from app.tasks.scan import _size_bounds_gb

    tv = SimpleNamespace(media_type="tv")
    movie = SimpleNamespace(media_type="movie")
    assert _size_bounds_gb(tv, 0.1, 1.5, 0.5, 5.0) == (0.1, 1.5)
    assert _size_bounds_gb(movie, 0.1, 1.5, 0.5, 5.0) == (0.5, 5.0)


def test_size_out_of_bounds_none_or_zero_no_limit_and_edges():
    from app.tasks.scan import _size_out_of_bounds

    assert _size_out_of_bounds(1, None, None) is None
    assert _size_out_of_bounds(1, 0, 0) is None
    # 等于下限 / 上限 → 放行
    assert _size_out_of_bounds(int(0.5 * GB), 0.5, 0.5) is None
    assert _size_out_of_bounds(int(0.5 * GB), 0.5, None) is None
    # 低于下限 / 高于上限
    assert _size_out_of_bounds(int(0.5 * GB) - 1, 0.5, None) == "low"
    assert _size_out_of_bounds(int(0.5 * GB) + 1, None, 0.5) == "high"


# ---------------------------------------------------------------------------
# 3) 端到端 scan 入队路径（mock cloudsaver / _walk_share / emby）
# ---------------------------------------------------------------------------

def _share_file(name: str, size: int) -> dict:
    return {
        "fileName": name, "fileId": f"f_{name}", "fileIdToken": f"ft_{name}",
        "isFolder": False, "size": size,
    }


def _patch_scan(monkeypatch, db, *, missing_codes, files, share_size=0):
    """基础 mock：emby 基线返回 missing_codes；真实读大小限制（不 mock _read_size_limits）。"""
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    monkeypatch.setattr(scan_mod, "_emby_missing_codes", AsyncMock(return_value=missing_codes))
    monkeypatch.setattr(scan_mod, "_search_and_rank", AsyncMock(return_value=[
        {"title": "测试剧", "share_code": "sc1"},
    ]))
    monkeypatch.setattr(scan_mod, "_cloudsaver_share_info", AsyncMock(return_value={
        "pwd_id": "pd", "stoken": "st", "receive_code": "", "fileSize": share_size,
    }))
    monkeypatch.setattr(scan_mod.cloudsaver, "share_list",
                        AsyncMock(return_value={"list": files}))
    monkeypatch.setattr(scan_mod, "_trigger_transfer", AsyncMock(return_value=None))
    return scan_mod


async def _seed_media(db, **overrides):
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=None, status="tracking",
                      **overrides)
        s.add(media)
        await s.commit()
        return media.id


async def _read_tq(db, mid):
    async with db() as s:
        return (
            await s.execute(
                select(TaskQueue).where(TaskQueue.media_id == mid)
                .order_by(TaskQueue.episode.asc())
            )
        ).scalars().all()


async def _last_run(db, mid):
    async with db() as s:
        return (
            await s.execute(
                select(TaskRun).where(TaskRun.media_id == mid)
                .order_by(TaskRun.id.desc())
            )
        ).scalars().first()


def _missing(*codes):
    return [{"code": c, "premiere_date": "2020-01-01"} for c in codes]


def test_scan_below_min_filtered(db, monkeypatch):
    """20MB 单集 < 下限 0.1GB → 跳过、计入 size_filtered、不写 task_queue。"""
    scan_mod = _patch_scan(
        monkeypatch, db, missing_codes=_missing("S01E01"),
        files=[_share_file("S01E01.mkv", int(0.02 * GB))],
    )
    mid = run(_seed_media(db, min_episode_size_gb=0.1))
    run(scan_mod._scan_one(mid))

    assert run(_read_tq(db, mid)) == []
    detail = json.loads(run(_last_run(db, mid)).scan_detail)
    assert detail["size_filtered"] == 1


def test_scan_above_max_filtered(db, monkeypatch):
    """超上限 1GB > 0.5GB → 跳过。"""
    scan_mod = _patch_scan(
        monkeypatch, db, missing_codes=_missing("S01E01"),
        files=[_share_file("S01E01.mkv", GB)],
    )
    mid = run(_seed_media(db, max_episode_size_gb=0.5))
    run(scan_mod._scan_one(mid))

    assert run(_read_tq(db, mid)) == []
    assert json.loads(run(_last_run(db, mid)).scan_detail)["size_filtered"] == 1


def test_scan_boundary_value_passes(db, monkeypatch):
    """大小恰好等于下限与上限（均 0.5GB）→ 放行入队。"""
    exact = int(0.5 * GB)
    scan_mod = _patch_scan(
        monkeypatch, db, missing_codes=_missing("S01E01"),
        files=[_share_file("S01E01.mkv", exact)],
    )
    mid = run(_seed_media(db, min_episode_size_gb=0.5, max_episode_size_gb=0.5))
    run(scan_mod._scan_one(mid))

    tq = run(_read_tq(db, mid))
    assert [(r.episode, r.file_name) for r in tq] == [("S01E01", "S01E01.mkv")]


def test_scan_unknown_size_skipped(db, monkeypatch):
    """大小未知（size=0 且分享总大小缺失）→ fail-closed 跳过。"""
    scan_mod = _patch_scan(
        monkeypatch, db, missing_codes=_missing("S01E01"),
        files=[_share_file("S01E01.mkv", 0)], share_size=0,
    )
    mid = run(_seed_media(db, min_episode_size_gb=0.1))
    run(scan_mod._scan_one(mid))

    assert run(_read_tq(db, mid)) == []
    assert json.loads(run(_last_run(db, mid)).scan_detail)["size_filtered"] == 1


def test_scan_media_override_disables_min(db, monkeypatch):
    """默认下限 0.1GB 会拦截 20MB；单影视覆盖 min=0 → 不限制 → 入队。"""
    scan_mod = _patch_scan(
        monkeypatch, db, missing_codes=_missing("S01E01"),
        files=[_share_file("S01E01.mkv", int(0.02 * GB))],
    )
    mid = run(_seed_media(db, min_episode_size_gb=0))
    run(scan_mod._scan_one(mid))

    assert [(r.episode, r.file_name) for r in run(_read_tq(db, mid))] == [
        ("S01E01", "S01E01.mkv"),
    ]


def test_scan_min_zero_no_limit(db, monkeypatch):
    """min=0（显式）→ 该侧不限制，极小文件也放行。"""
    scan_mod = _patch_scan(
        monkeypatch, db, missing_codes=_missing("S01E01"),
        files=[_share_file("S01E01.mkv", 20 * 1024 * 1024)],
    )
    mid = run(_seed_media(db, min_episode_size_gb=0))
    run(scan_mod._scan_one(mid))

    assert len(run(_read_tq(db, mid))) == 1


def test_scan_size_filter_does_not_interrupt_other_files(db, monkeypatch):
    """同轮一文件被下限拦截，另一文件仍正常入队（过滤不中断本轮巡检）。"""
    scan_mod = _patch_scan(
        monkeypatch, db, missing_codes=_missing("S01E01", "S01E02"),
        files=[_share_file("S01E01.mkv", int(0.02 * GB)),
               _share_file("S01E02.mkv", GB)],
    )
    mid = run(_seed_media(db, min_episode_size_gb=0.1))
    run(scan_mod._scan_one(mid))

    assert [(r.episode, r.file_name) for r in run(_read_tq(db, mid))] == [
        ("S01E02", "S01E02.mkv"),
    ]
    assert json.loads(run(_last_run(db, mid)).scan_detail)["size_filtered"] == 1


def test_scan_numeric_fallback_respects_min(db, monkeypatch):
    """纯数字兜底分支同样受下限约束：20MB 的 190.mkv 被拦截。"""
    scan_mod = _patch_scan(
        monkeypatch, db, missing_codes=_missing("S01E190"),
        files=[_share_file("190.mkv", int(0.02 * GB))],
    )
    mid = run(_seed_media(db, min_episode_size_gb=0.1))
    run(scan_mod._scan_one(mid))

    assert run(_read_tq(db, mid)) == []
    assert json.loads(run(_last_run(db, mid)).scan_detail)["size_filtered"] == 1
