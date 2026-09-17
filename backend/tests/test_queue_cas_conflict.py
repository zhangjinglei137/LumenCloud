"""P3 队列控制面 CAS 门控单测（fix-audit-issues Task C3 / Design D5）。

三条控制面写路径的并发防线（读后写前状态被并发方推进 → rowcount==0 → 409）：
- sort_task up/down：交换 enqueued_at 的 UPDATE 追加 status=='pending' 门控，
  相邻行被并发推进（transferring 等）→ 409，不污染在途任务排序键
  （审查 B3 / OpenSpec queue-inspection-display「排序操作不污染在途任务」）。
- add_queue_task 重置：UPDATE 追加 status IN (pending,error,unmatched,ready)，
  probing 等运行态拒绝重置 → 409，探测结果不落库错位
  （审查 B4 / OpenSpec pipeline-admission「手动加集不打断探测中任务」）。
- retry_task 旧三表分支：transfer_queue 回退 UPDATE 检查 rowcount，==0 → 409
  「状态已变化」且 episode_state 重置一并回滚（审查 B10 / OpenSpec
  pipeline-transfer「重试操作反馈真实结果」）。

并发窗口用「劫持 session.execute：第一条 UPDATE 执行前模拟并发推进」构造
（pending 列表读毕、写之前状态被并发方改变），等价真实并发时序；
SQLite 同事务内先 update 后 update 的 rowcount 语义与 PostgreSQL 一致。
"""
import asyncio
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Update, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  注册全部 ORM 模型
import app.routers.queue as queue_mod
import app.tasks.scan as scan_mod
import app.tasks.transfer as transfer_mod
from app.models import DownloadQueue, EpisodeState, Media, TaskQueue, TransferQueue


def run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _admin():
    return types.SimpleNamespace(role="admin")


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


@pytest.fixture()
def env(monkeypatch):
    """fake 外部服务：aria2/alist、scan 触发、续跑触发（复用 test_queue.py 约定）。"""
    fakes = {
        "aria2": types.SimpleNamespace(
            client=types.SimpleNamespace(
                remove=AsyncMock(return_value={}),
                tell_status=AsyncMock(return_value={}),
            )
        ),
        "alist": types.SimpleNamespace(remove=AsyncMock(return_value={})),
        "scan_trigger": MagicMock(),
        "consume": AsyncMock(return_value=None),
    }
    monkeypatch.setattr(queue_mod, "aria2", fakes["aria2"])
    monkeypatch.setattr(queue_mod, "alist", fakes["alist"])
    monkeypatch.setattr(scan_mod, "trigger_scan_background", fakes["scan_trigger"])
    monkeypatch.setattr(queue_mod, "_trigger_consume", fakes["consume"])
    return fakes


# ---------------------------------------------------------------------------
# 种子数据 / 读取
# ---------------------------------------------------------------------------

async def seed_media(db, *, title="测试剧", media_type="tv", tmdb_id=None):
    async with db() as s:
        media = Media(title=title, media_type=media_type, tmdb_id=tmdb_id, status="tracking")
        s.add(media)
        await s.flush()
        await s.commit()
        return media.id


async def seed_dq(db, mid, *, episode="S01E01", status="pending", enqueued_at=None,
                  retry_count=0, node_attempt=0, share_code="ScAaBbCcDdEe",
                  quark_path="/quark/ep.mkv", aria2_gid="gid-1", error=None):
    async with db() as s:
        dq = DownloadQueue(
            media_id=mid, episode=episode, file_name="ep.mkv", file_size=1024,
            share_code=share_code, stoken="st", receive_code="rc", fids="[]",
            fid_tokens="[]", folder_id="fd", status=status, retry_count=retry_count,
            node_attempt=node_attempt, quark_path=quark_path, aria2_gid=aria2_gid,
            node_started_at=_now(), error=error, enqueued_at=enqueued_at or _now(),
            updated_at=_now(),
        )
        s.add(dq)
        await s.flush()
        await s.commit()
        return dq.id


async def seed_tq(db, mid, *, episode="S01E01", status="ready", share_code="TqXxYyZz1234",
                  probe_attempt=1, silent_until=None, error=None):
    async with db() as s:
        tq = TaskQueue(
            media_id=mid, episode=episode, file_name="ep.mkv", file_size=1024,
            share_code=share_code, status=status, probe_attempt=probe_attempt,
            silent_until=silent_until, error=error, created_at=_now(), updated_at=_now(),
        )
        s.add(tq)
        await s.flush()
        await s.commit()
        return tq.id


async def seed_es(db, mid, *, episode="S01E01", node="failed", state="failed",
                  retry_count=3, error="旧失败"):
    """旧三表 episode_state（node/state 双 failed → 可重试）。"""
    async with db() as s:
        es = EpisodeState(
            media_id=mid, episode=episode, node=node, node_attempt=1,
            state=state, retry_count=retry_count, error=error, updated_at=_now(),
        )
        s.add(es)
        await s.flush()
        await s.commit()
        return es.id


async def seed_tq_old(db, mid, *, episode="S01E01", status="failed", share_code="OldTqXxYyZz12"):
    """旧三表 transfer_queue 兼容回退行。"""
    async with db() as s:
        row = TransferQueue(
            media_id=mid, episode=episode, file_name="ep.mkv", file_size=1024,
            share_code=share_code, status=status, updated_at=_now(),
        )
        s.add(row)
        await s.flush()
        await s.commit()
        return row.id


async def read_dq(db, dq_id):
    async with db() as s:
        return await s.get(DownloadQueue, dq_id)


async def read_tq(db, tq_id):
    async with db() as s:
        return await s.get(TaskQueue, tq_id)


# ---------------------------------------------------------------------------
# sort_task：up/down CAS 门控（在途拒绝 / 并发窗口 409）
# ---------------------------------------------------------------------------

def test_sort_inflight_transferring_rejected_409_and_ts_untouched(db, env):
    """在途（transferring）任务排序被拒 → 409，enqueued_at 未被修改。"""
    mid = run(seed_media(db))
    base = _now()
    dq_id = run(seed_dq(db, mid, status="transferring", enqueued_at=base))
    run(seed_dq(db, mid, episode="S01E02", status="pending",
                enqueued_at=base + timedelta(minutes=1)))

    async def _sort():
        async with db() as s:
            await queue_mod.sort_task(task_id=dq_id,
                                      body=types.SimpleNamespace(direction="up"),
                                      admin=_admin(), session=s)
    with pytest.raises(Exception) as ei:
        run(_sort())
    assert ei.value.status_code == 409
    assert run(read_dq(db, dq_id)).enqueued_at == base


def test_sort_up_down_peer_inflight_cas_conflict_409(db, env):
    """CAS：up/down 交换前相邻行被并发推进 transferring → rowcount==0 → 409，
    且两个 enqueued_at 均未被修改（事务回滚，不污染在途任务排序键）。"""
    mid = run(seed_media(db))
    base = _now()
    a = run(seed_dq(db, mid, episode="S01E01", status="pending", enqueued_at=base))
    b = run(seed_dq(db, mid, episode="S01E02", status="pending",
                    enqueued_at=base + timedelta(minutes=1)))

    async def _sort():
        async with db() as s:
            orig_execute = s.execute
            fired = {"n": 0}

            async def wrapped(stmt, *args, **kwargs):
                if isinstance(stmt, Update):
                    fired["n"] += 1
                    if fired["n"] == 1:
                        # 并发窗口：pending 列表读毕、写之前相邻行被并发方推进
                        await orig_execute(
                            update(DownloadQueue).where(DownloadQueue.id == b)
                            .values(status="transferring", updated_at=_now())
                        )
                return await orig_execute(stmt, *args, **kwargs)

            s.execute = wrapped  # type: ignore[method-assign]
            await queue_mod.sort_task(task_id=a,
                                      body=types.SimpleNamespace(direction="down"),
                                      admin=_admin(), session=s)

    with pytest.raises(Exception) as ei:
        run(_sort())
    assert ei.value.status_code == 409

    a_row = run(read_dq(db, a))
    b_row = run(read_dq(db, b))
    assert a_row.enqueued_at == base
    assert b_row.enqueued_at == base + timedelta(minutes=1)


# ---------------------------------------------------------------------------
# add_queue_task：重置 CAS 门控（probing 拒绝 / 终态重置正常）
# ---------------------------------------------------------------------------

def test_add_task_probing_reset_rejected_409_keeps_probing(db, env):
    """CAS：探测中（probing）任务手动加集重置被拒 → 409，保持 probing 不被打断。"""
    mid = run(seed_media(db))
    tq_id = run(seed_tq(db, mid, status="probing", probe_attempt=2))

    async def _add():
        async with db() as s:
            await queue_mod.add_queue_task(
                body=types.SimpleNamespace(media_id=mid, episode="S01E01"),
                admin=_admin(), session=s)
    with pytest.raises(Exception) as ei:
        run(_add())
    assert ei.value.status_code == 409

    tq = run(read_tq(db, tq_id))
    assert tq.status == "probing" and tq.probe_attempt == 2
    env["scan_trigger"].assert_not_called()  # 409 后不触发探测


def test_add_task_error_reset_normal_path_ok(db, env):
    """正常路径：终态 error 重置 pending（probe_attempt 归零、silent_until 清空）无回归。"""
    mid = run(seed_media(db))
    tq_id = run(seed_tq(db, mid, status="error", probe_attempt=3, error="探测失败",
                        silent_until=_now() + timedelta(days=2)))

    async def _add():
        async with db() as s:
            return await queue_mod.add_queue_task(
                body=types.SimpleNamespace(media_id=mid, episode="S01E01"),
                admin=_admin(), session=s)
    assert run(_add()) == {"ok": True}

    tq = run(read_tq(db, tq_id))
    assert tq.status == "pending" and tq.probe_attempt == 0
    assert tq.error is None and tq.silent_until is None
    env["scan_trigger"].assert_called_once_with(mid, manual=True)


# ---------------------------------------------------------------------------
# retry_task：旧三表回退分支 CAS（rowcount==0 → 409）
# ---------------------------------------------------------------------------

def test_retry_legacy_transfer_queue_cas_conflict_409(db, env):
    """CAS：旧三表回退分支 transfer_queue 状态已变（非 failed/done）→ rowcount==0 →
    409 且用户可见「状态已变化」提示；episode_state 重置一并回滚（反馈真实结果）。"""
    mid = run(seed_media(db))
    es_id = run(seed_es(db, mid))
    tq_old_id = run(seed_tq_old(db, mid))

    async def _retry():
        async with db() as s:
            orig_execute = s.execute
            fired = {"n": 0}

            async def wrapped(stmt, *args, **kwargs):
                if isinstance(stmt, Update):
                    fired["n"] += 1
                    if fired["n"] == 1:
                        # 并发窗口：es 读取后、写入前 transfer_queue 已被并发方推进 pending
                        await orig_execute(
                            update(TransferQueue).where(TransferQueue.id == tq_old_id)
                            .values(status="pending", updated_at=_now())
                        )
                return await orig_execute(stmt, *args, **kwargs)

            s.execute = wrapped  # type: ignore[method-assign]
            return await queue_mod.retry_task(task_id=es_id, admin=_admin(), session=s)

    with pytest.raises(Exception) as ei:
        run(_retry())
    assert ei.value.status_code == 409
    assert "状态已变化" in ei.value.detail

    # 冲突整体回滚：episode_state 未被重置
    async def _es():
        async with db() as s:
            return await s.get(EpisodeState, es_id)
    es = run(_es())
    assert es.node == "failed" and es.node_attempt == 1 and es.retry_count == 3


def test_retry_legacy_transfer_queue_normal_path_ok(db, env):
    """正常路径：旧三表 failed 可重试 → es 重置 queued/idle + transfer_queue 回 pending。"""
    mid = run(seed_media(db))
    es_id = run(seed_es(db, mid))
    tq_old_id = run(seed_tq_old(db, mid))

    async def _retry():
        async with db() as s:
            return await queue_mod.retry_task(task_id=es_id, admin=_admin(), session=s)
    assert run(_retry()) == {"ok": True}

    async def _es():
        async with db() as s:
            return await s.get(EpisodeState, es_id)
    es = run(_es())
    assert es.node == "idle" and es.node_attempt == 0 and es.node_error is None
    assert es.state == "queued" and es.retry_count == 0 and es.error is None

    async def _tq_old():
        async with db() as s:
            return await s.get(TransferQueue, tq_old_id)
    tq = run(_tq_old())
    assert tq.status == "pending" and tq.error is None
    env["consume"].assert_awaited_once()
