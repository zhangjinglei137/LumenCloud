"""scan 静默 unmatched 预过滤单测（两队列 §4.1 效率优化，对齐 n8n「解析遗漏集」）。

覆盖（mock 外部依赖 emby/cloudsaver/tmdb，风格对齐 test_scan_run_phases.py）：
- task_queue 有 status='unmatched' + silent_until 未来的 episode → 下一轮巡检跳过
  搜索（_search_and_rank 不被调用、phases.search=skipped、status=skipped、message
  表达「缺失集均在静默期」），task_run 仍照常记录 + scan_detail 统计
- silent_until 已过期 → 不命中静默预过滤，仍正常搜索（_search_and_rank 被调用）
- 无 unmatched 行 → 正常搜索
- 部分静默（只过滤在静默期内的集）→ 只搜索剩余缺失集（_search_and_rank 收到
  过滤后的 missing_keys；scan_detail 只装配待搜索集，静默集计 unmatched_silent_skipped）
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  注册全部 ORM 模型
from app.database import Base
from app.models import Media, TaskQueue, TaskRun


def run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


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


async def _seed_media(db, *, title="测试剧", media_type="tv", status="tracking"):
    async with db() as s:
        media = Media(title=title, media_type=media_type, tmdb_id=42, status=status)
        s.add(media)
        await s.commit()
        return media.id


async def _seed_unmatched(db, mid, episodes, silent_until):
    """向 task_queue 预置 status='unmatched' + silent_until 的行。"""
    now = _now()
    async with db() as s:
        for ep in episodes:
            s.add(TaskQueue(
                media_id=mid,
                episode=ep,
                status="unmatched",
                probe_attempt=1,
                silent_until=silent_until,
                error="搜索确认无网盘资源，静默 N 天后自动重试",
                created_at=now,
                updated_at=now,
            ))
        await s.commit()


def _patch_scan_env(monkeypatch, db, *, missing_codes=None, search=None):
    """scan 外部依赖全 mock（emby 基线/cloudsaver），风格对齐 test_scan_run_phases.py。"""
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    emby_mock = AsyncMock()
    if missing_codes is not None:
        emby_mock.return_value = missing_codes
    monkeypatch.setattr(scan_mod, "_emby_missing_codes", emby_mock)
    monkeypatch.setattr(
        scan_mod.cloudsaver, "search",
        AsyncMock(return_value=search if search is not None else []),
    )
    monkeypatch.setattr(
        scan_mod.cloudsaver, "share_info",
        AsyncMock(return_value={"pwd_id": "pd", "stoken": "st", "receive_code": "",
                               "fileSize": 9999}),
    )
    monkeypatch.setattr(
        scan_mod.cloudsaver, "share_list",
        AsyncMock(return_value={"list": []}),
    )
    monkeypatch.setattr(scan_mod, "_read_size_limits", AsyncMock(return_value=(100.0, 100.0)))
    monkeypatch.setattr(scan_mod, "_trigger_transfer", AsyncMock(return_value=None))
    return scan_mod


async def _read_runs(db, mid):
    async with db() as s:
        rows = (
            await s.execute(
                select(TaskRun).where(TaskRun.media_id == mid)
                .order_by(TaskRun.id.asc())
            )
        ).scalars().all()
        return rows


# ---------------------------------------------------------------------------
# 1) 全部缺失集处于静默期 → 跳过搜索
# ---------------------------------------------------------------------------

def test_scan_one_skips_search_when_all_missing_in_silence(db, monkeypatch):
    """task_queue 两条缺失集均为 status='unmatched' + silent_until 未来 → 下一轮巡检
    不调 _search_and_rank、phases.search=skipped、status=skipped、message 明示静默期；
    task_run 仍照常记录（含 scan_detail 的静默计数）。"""
    from app.tasks import scan as scan_mod

    mid = run(_seed_media(db))
    run(_seed_unmatched(db, mid, ["S01E01", "S01E02"], _now() + timedelta(days=2)))

    scan_mod = _patch_scan_env(monkeypatch, db, missing_codes=["S01E01", "S01E02"])
    search_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(scan_mod, "_search_and_rank", search_mock)

    rid = run(scan_mod._scan_one(mid))
    runs = run(_read_runs(db, mid))
    assert len(runs) == 1 and runs[0].id == rid
    tr = runs[0]
    assert tr.status == "skipped"
    assert "缺失集均在静默期" in tr.message
    assert tr.finished_at is not None

    # 搜索阶段跳过了：共享探测/share-list 全部未触发的本质 = 不调 _search_and_rank
    search_mock.assert_not_awaited()

    import json
    phases = json.loads(tr.phases)
    assert phases["check"]["status"] == "done"
    assert phases["search"]["status"] == "skipped"
    # search 未执行 → match/enqueue/finish 不残留 wait（终态 skipped）
    for key in ("match", "enqueue", "finish"):
        assert phases[key]["status"] == "skipped", key

    detail = json.loads(tr.scan_detail)
    assert detail["failed_phase"] is None
    assert detail["search_status"] == "skipped"
    assert detail["missing_total"] == 0
    assert detail["missing_items"] == []
    assert detail["unmatched_silent_skipped"] == 2


# ---------------------------------------------------------------------------
# 2) silent_until 已过期 → 仍正常搜索
# ---------------------------------------------------------------------------

def test_scan_one_searches_when_silence_expired(db, monkeypatch):
    """unmatched 行 silent_until 已过期（静默窗已过）→ 预过滤不命中，仍正常搜索
    （_search_and_rank 被调用、phases.search=done）；scan_detail 静默计数为 0。"""
    from app.tasks import scan as scan_mod

    mid = run(_seed_media(db))
    run(_seed_unmatched(db, mid, ["S01E01"], _now() - timedelta(days=1)))

    scan_mod = _patch_scan_env(monkeypatch, db, missing_codes=["S01E01"])
    search_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(scan_mod, "_search_and_rank", search_mock)

    rid = run(scan_mod._scan_one(mid))
    tr = run(_read_runs(db, mid))[0]
    assert tr.id == rid and tr.status == "skipped"
    assert "未找到缺失集 S01E01 的资源" in tr.message

    search_mock.assert_awaited_once()

    import json
    phases = json.loads(tr.phases)
    assert phases["search"]["status"] == "done"
    detail = json.loads(tr.scan_detail)
    assert detail["missing_total"] == 1
    assert detail["missing_items"] == [{"episode": "S01E01", "result": "not_found"}]
    assert detail["unmatched_silent_skipped"] == 0


# ---------------------------------------------------------------------------
# 3) 无 unmatched 行 → 正常搜索
# ---------------------------------------------------------------------------

def test_scan_one_searches_when_no_unmatched_rows(db, monkeypatch):
    """task_queue 无 unmatched 行 → 静默预过滤无命中，正常搜索（_search_and_rank 被调用、
    phases.search=done），行为与改造前一致。"""
    from app.tasks import scan as scan_mod

    mid = run(_seed_media(db))

    scan_mod = _patch_scan_env(monkeypatch, db, missing_codes=["S01E01"])
    search_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(scan_mod, "_search_and_rank", search_mock)

    rid = run(scan_mod._scan_one(mid))
    tr = run(_read_runs(db, mid))[0]
    assert tr.id == rid and tr.status == "skipped"

    search_mock.assert_awaited_once()

    import json
    phases = json.loads(tr.phases)
    assert phases["search"]["status"] == "done"
    detail = json.loads(tr.scan_detail)
    assert detail["missing_total"] == 1
    assert detail["unmatched_silent_skipped"] == 0


# ---------------------------------------------------------------------------
# 4) 部分静默 → 只搜索剩余缺失集
# ---------------------------------------------------------------------------

def test_scan_one_partial_silence_searches_only_remaining(db, monkeypatch):
    """S01E01 处静默期、S01E02 未标 → 预过滤只剔除 S01E01，搜索只针对 S01E02
    （_search_and_rank 收到过滤后的 missing_keys）；scan_detail 只装配待搜索集，
    静默集计 unmatched_silent_skipped=1。"""
    from app.tasks import scan as scan_mod

    mid = run(_seed_media(db))
    run(_seed_unmatched(db, mid, ["S01E01"], _now() + timedelta(days=2)))

    scan_mod = _patch_scan_env(monkeypatch, db, missing_codes=["S01E01", "S01E02"])
    search_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(scan_mod, "_search_and_rank", search_mock)

    rid = run(scan_mod._scan_one(mid))
    tr = run(_read_runs(db, mid))[0]
    assert tr.id == rid and tr.status == "skipped"
    # 只搜剩余集（未静默的 S01E02）
    search_mock.assert_awaited_once()
    assert search_mock.await_args.args[1] == {"S01E02"}
    # 剩余集无候选 → 缺集文案只列 S01E02，不掺入静默期 S01E01
    assert "未找到缺失集 S01E02 的资源" in tr.message

    import json
    phases = json.loads(tr.phases)
    assert phases["search"]["status"] == "done"
    detail = json.loads(tr.scan_detail)
    assert detail["missing_total"] == 1
    assert detail["missing_items"] == [{"episode": "S01E02", "result": "not_found"}]
    assert detail["unmatched_silent_skipped"] == 1


# ---------------------------------------------------------------------------
# 5) 手动触发（manual=True）绕过静默期：用户主动操作=明确要立即重试
# ---------------------------------------------------------------------------

def test_scan_one_manual_true_bypasses_silence(db, monkeypatch):
    """缺失集在静默期内（unmatched + silent_until 未来），但 manual=True
    （手动触发：media 扫描按钮 / queue 探测 / 加集 / 审批通过）→ **跳过静默预过滤**，
    缺失集全部照常搜索（_search_and_rank 被调用、phases.search=done、
    scan_detail.unmatched_silent_skipped=0）。"""
    from app.tasks import scan as scan_mod

    mid = run(_seed_media(db))
    run(_seed_unmatched(db, mid, ["S01E01"], _now() + timedelta(days=2)))

    scan_mod = _patch_scan_env(monkeypatch, db, missing_codes=["S01E01"])
    search_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(scan_mod, "_search_and_rank", search_mock)

    rid = run(scan_mod._scan_one(mid, manual=True))
    tr = run(_read_runs(db, mid))[0]
    assert tr.id == rid and tr.status == "skipped"
    # manual=True → 静默期不拦截，照常搜索
    search_mock.assert_awaited_once()

    import json
    phases = json.loads(tr.phases)
    assert phases["search"]["status"] == "done"
    detail = json.loads(tr.scan_detail)
    assert detail["missing_total"] == 1
    assert detail["unmatched_silent_skipped"] == 0  # 未静默过滤任何集