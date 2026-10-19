"""Emby 侧删除后自动重下状态机单测（change media-min-size-and-emby-redownload 车道 B / 3.1）。

覆盖：
- 宽限期内（retry_count < 3）只累加宽限计数、不重下
- done→failed 与 failed→failed 都推进宽限计数
- 宽限用尽且自动重下次数未用尽 → 按 retry_task 语义重置 pending 且 auto_redownload_count+1
- 自动重下次数用尽 → failed 终态（_DONE_LIMIT_ERROR），且不重复改写
- Emby 已确认收录 → 删除 done 行与「本规则产生的 failed 行」
- 其它失败类型不被接管（不改写、不删除）
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.database import Base
from app.models import DownloadQueue, Media
from app.utils import now_utc_naive as _now


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


@pytest.fixture(autouse=True)
def _patch_scan_session(monkeypatch, db):
    from app.tasks import scan as scan_mod
    monkeypatch.setattr(scan_mod, "async_session", db)


async def _seed(db, *, episode="S01E01", status="done", retry_count=0,
                auto_redownload_count=0, error=None, node_error=None, node_attempt=0):
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=42, status="tracking")
        s.add(media)
        await s.flush()
        mid = media.id
        dq = DownloadQueue(
            media_id=mid, episode=episode, status=status,
            retry_count=retry_count, auto_redownload_count=auto_redownload_count,
            error=error, node_error=node_error, node_attempt=node_attempt,
            file_name="ep.mkv", file_size=1024, share_code="sc", stoken="st",
            receive_code="rc", fids="[]", fid_tokens="[]", folder_id="fd",
            updated_at=_now(),
        )
        s.add(dq)
        await s.commit()
        return mid, dq.id


async def _read_row(db, dq_id):
    async with db() as s:
        return await s.get(DownloadQueue, dq_id)


def test_grace_period_does_not_redownload(db):
    """done + retry_count=0 仍缺失 → 只转 failed 并推进宽限计数，不重下。"""
    from app.tasks import scan as scan_mod

    mid, dq_id = run(_seed(db))
    run(scan_mod._resolve_done_states(SimpleNamespace(id=mid), {"S01E01"}, False))

    dq = run(_read_row(db, dq_id))
    assert dq.status == "failed"
    assert dq.error == scan_mod._DONE_FAIL_ERROR
    assert dq.retry_count == 1
    assert dq.auto_redownload_count == 0


def test_failed_rule_row_advances_grace_count(db):
    """宽限分支产生的 failed 行（error=_DONE_FAIL_ERROR）仍缺失 → 宽限计数继续推进。"""
    from app.tasks import scan as scan_mod

    mid, dq_id = run(_seed(
        db, status="failed", retry_count=2, error=scan_mod._DONE_FAIL_ERROR,
    ))
    run(scan_mod._resolve_done_states(SimpleNamespace(id=mid), {"S01E01"}, False))

    dq = run(_read_row(db, dq_id))
    assert dq.status == "failed"
    assert dq.retry_count == 3
    assert dq.error == scan_mod._DONE_FAIL_ERROR
    assert dq.auto_redownload_count == 0


def test_grace_exhausted_resets_pending_and_counts(db):
    """retry_count=3 仍缺失且 auto=0 → 重置 pending（retry_task 语义）并 auto+1。"""
    from app.tasks import scan as scan_mod

    mid, dq_id = run(_seed(db, retry_count=3, auto_redownload_count=0))
    run(scan_mod._resolve_done_states(SimpleNamespace(id=mid), {"S01E01"}, False))

    dq = run(_read_row(db, dq_id))
    assert dq.status == "pending"
    assert dq.retry_count == 0
    assert dq.node_attempt == 0
    assert dq.node_error is None
    assert dq.node_started_at is None
    assert dq.node_finished_at is None
    assert dq.save_task_id is None
    assert dq.save_attempt_at is None
    assert dq.error is None
    assert dq.enqueued_at is not None
    assert dq.auto_redownload_count == 1


def test_pending_row_not_reselected_on_next_round(db):
    """重置为 pending 后下一轮 resolve 不再命中（不重复改写/不重复计数）。"""
    from app.tasks import scan as scan_mod

    mid, dq_id = run(_seed(db, retry_count=3, auto_redownload_count=0))
    run(scan_mod._resolve_done_states(SimpleNamespace(id=mid), {"S01E01"}, False))
    dq1 = run(_read_row(db, dq_id))
    first_ts = dq1.updated_at

    run(scan_mod._resolve_done_states(SimpleNamespace(id=mid), {"S01E01"}, False))
    dq2 = run(_read_row(db, dq_id))
    assert dq2.status == "pending"
    assert dq2.auto_redownload_count == 1
    assert dq2.updated_at == first_ts


def test_auto_redownload_exhausted_marks_failed_terminal(db):
    """宽限与自动重下均用尽 → failed 终态（上限文案 + node_attempt 上限值）。"""
    from app.tasks import scan as scan_mod

    mid, dq_id = run(_seed(
        db, retry_count=3, auto_redownload_count=scan_mod._AUTO_REDOWNLOAD_LIMIT,
    ))
    run(scan_mod._resolve_done_states(SimpleNamespace(id=mid), {"S01E01"}, False))

    dq = run(_read_row(db, dq_id))
    assert dq.status == "failed"
    assert dq.error == scan_mod._DONE_LIMIT_ERROR
    assert dq.node_error == scan_mod._DONE_LIMIT_ERROR
    assert dq.node_attempt == scan_mod._DONE_FAIL_RETRY_LIMIT
    assert dq.retry_count == 3
    assert dq.auto_redownload_count == scan_mod._AUTO_REDOWNLOAD_LIMIT


def test_terminal_row_not_rewritten(db):
    """已是终态（failed + 上限文案）→ 再次 resolve 不改写 updated_at。"""
    from app.tasks import scan as scan_mod

    mid, dq_id = run(_seed(
        db, status="failed", retry_count=3,
        auto_redownload_count=scan_mod._AUTO_REDOWNLOAD_LIMIT,
        error=scan_mod._DONE_LIMIT_ERROR, node_error=scan_mod._DONE_LIMIT_ERROR,
        node_attempt=scan_mod._DONE_FAIL_RETRY_LIMIT,
    ))
    run(scan_mod._resolve_done_states(SimpleNamespace(id=mid), {"S01E01"}, False))
    dq1 = run(_read_row(db, dq_id))
    first_ts = dq1.updated_at

    run(scan_mod._resolve_done_states(SimpleNamespace(id=mid), {"S01E01"}, False))
    dq2 = run(_read_row(db, dq_id))
    assert dq2.status == "failed"
    assert dq2.updated_at == first_ts


def test_confirmed_done_row_deleted(db):
    """Emby 已确认收录（集不在 missing）→ 删除 done 行解除防重。"""
    from app.tasks import scan as scan_mod

    mid, dq_id = run(_seed(db))
    run(scan_mod._resolve_done_states(SimpleNamespace(id=mid), {"S01E02"}, False))

    assert run(_read_row(db, dq_id)) is None


def test_confirmed_failed_rule_row_deleted(db):
    """本规则产生的 failed 行在 Emby 已确认收录时同样被删除（修掉失败行泄漏）。"""
    from app.tasks import scan as scan_mod

    mid, dq_id = run(_seed(
        db, status="failed", retry_count=1, error=scan_mod._DONE_FAIL_ERROR,
    ))
    run(scan_mod._resolve_done_states(SimpleNamespace(id=mid), {"S01E02"}, False))

    assert run(_read_row(db, dq_id)) is None


def test_other_failure_type_not_touched(db):
    """其它失败类型（非本规则 error/node_error）不被改写、也不被删除。"""
    from app.tasks import scan as scan_mod

    mid, dq_id = run(_seed(
        db, status="failed", retry_count=0, error="转存失败",
        node_error="cloudsaver boom",
    ))
    run(scan_mod._resolve_done_states(SimpleNamespace(id=mid), {"S01E01"}, False))
    dq = run(_read_row(db, dq_id))
    assert dq.status == "failed"
    assert dq.error == "转存失败"
    assert dq.node_error == "cloudsaver boom"
    assert dq.retry_count == 0

    # Emby 已确认收录也不删除（不接管非本规则失败行）
    run(scan_mod._resolve_done_states(SimpleNamespace(id=mid), set(), False))
    assert run(_read_row(db, dq_id)) is not None


def test_movie_missing_respects_grace_and_confirmed_deletes(db):
    """movie 分支：缺失时走宽限；已入库时删除本规则 failed 行。"""
    from app.tasks import scan as scan_mod

    mid, dq_id = run(_seed(db, episode="电影.mkv", status="failed", retry_count=1,
                           error=scan_mod._DONE_FAIL_ERROR))
    run(scan_mod._resolve_done_states(SimpleNamespace(id=mid), set(), True))
    dq = run(_read_row(db, dq_id))
    assert dq.status == "failed" and dq.retry_count == 2

    run(scan_mod._resolve_done_states(SimpleNamespace(id=mid), set(), False))
    assert run(_read_row(db, dq_id)) is None


def test_redownload_triggers_transfer_consumption(db, monkeypatch):
    """自动重下重置 pending 后应立刻触发转存消费（不依赖每分钟兜底 job / 下一轮巡检）。

    独立审查 IMPORTANT-2：只有自动重下、本轮无新入队时，原实现不触发
    `_trigger_transfer`，pending 行要等兜底 job；本用例断言触发一次。
    场景取「该集仍在缺失集且搜索无候选」→ 本轮 enqueued=0，触发只能来自自动重下。
    """
    from app.tasks import scan as scan_mod

    trigger = AsyncMock(return_value=None)
    monkeypatch.setattr(scan_mod, "_trigger_transfer", trigger)
    monkeypatch.setattr(
        scan_mod, "_emby_missing_codes",
        AsyncMock(return_value=[{"code": "S01E01", "premiere_date": "2020-01-01T00:00:00Z"}]),
    )
    # TMDB 返回空季（非失败哨兵）→ 回退 Emby 首播日期（已播出）→ 该集仍是缺失集
    monkeypatch.setattr(
        scan_mod.tmdb, "get_tv_season_air_dates", AsyncMock(return_value={})
    )
    # 搜索无候选 → 本轮无新入队
    monkeypatch.setattr(scan_mod, "_search_and_rank", AsyncMock(return_value=[]))
    mid, dq_id = run(_seed(db, episode="S01E01", status="done", retry_count=3,
                           auto_redownload_count=0))

    run(scan_mod._scan_one(mid))

    assert trigger.await_count == 1
    dq = run(_read_row(db, dq_id))
    assert dq.status == "pending"
    assert dq.auto_redownload_count == 1
