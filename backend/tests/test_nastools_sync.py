"""NasTools 同步锁粒度单测（fix-audit-issues Task D3 / 审查 C9）。

验证（对应 design D11「NasTools 同步冷却检查不阻塞刮削」）：
- `_sync_lock` 只保护「冷却检查 + 在途标志 + 时间戳更新」等短临界区；
  `asyncio.sleep(30)`（重启等待）在锁外——冷却 sleep 期间 force（刮削触发）
  不被兜底同步长阻塞（1s 内完成，而非等 30s）
- 同步在途时：普通同步等效冷却跳过、force 立即跳过（防 NasTools 双重启，
  替代原全链路串行）
- 同步成功后更新冷却时间戳 `nastools_last_sync_at`（语义保持）

测试方式：对齐 test_nastools_notify.py——独立 in-memory SQLite + monkeypatch
async_session / nastools.client 方法；`asyncio.sleep` 替换为可挂起的假实现，
精确断言「sleep 期间锁已被释放（force 不被阻塞）」。
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  注册全部 ORM 模型
from app.models import SystemConfig, TaskRun
import app.tasks.nastools_sync as ns_mod


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db():
    """独立 in-memory SQLite（StaticPool 共享连接）→ 返回 sessionmaker（对齐 test_nastools_notify.py）。"""
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


def _reset_sync_state(monkeypatch):
    """防跨用例污染：模块级锁/在途标志是进程级共享状态，每用例开头重置。

    - `_sync_lock` 换成 fresh 锁：asyncio.Lock 若曾在「排队等待」路径被 await，
      会绑定到那次 asyncio.run 的事件循环，后续 `asyncio.run`（新循环）再等待
      时报 `bound to a different event loop`——monkeypatch 逐用例替换可规避，
      同时保证用例独立（生产代码不受影响，pytest 自动还原）。
    - RED 阶段（实现前）模块尚无 `_sync_in_progress` 属性——getattr 容忍，
      让失败表现为「测试行为断言失败」而非「属性缺失 error」。
    """
    monkeypatch.setattr(ns_mod, "_sync_lock", asyncio.Lock())
    in_progress = getattr(ns_mod, "_sync_in_progress", None)
    if in_progress is not None:
        ns_mod._sync_in_progress = False


def _make_sync_env(db, monkeypatch):
    """把 nastools_sync 接到真实 in-memory DB + mock 的 nasTools 客户端方法。"""
    monkeypatch.setattr(ns_mod, "async_session", db)
    monkeypatch.setattr(ns_mod.nastools.client, "login", AsyncMock(return_value="s"))
    monkeypatch.setattr(ns_mod.nastools.client, "restart", AsyncMock(return_value={}))
    monkeypatch.setattr(
        ns_mod.nastools.client, "run_directory_sync", AsyncMock(return_value={}),
    )


def _hanging_sleep(monkeypatch, gate: asyncio.Event, called: asyncio.Event):
    """把 asyncio.sleep 替换为「记录调用 + 挂起直到 gate 放行」的假实现。

    用于精确制造「同步已进入 30s 重启等待」的挂起点（测试快，不真实等 30s）。
    """
    async def fake_sleep(secs):
        called.set()
        await gate.wait()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)


async def _read_task_run_records(db):
    async with db() as s:
        rows = (
            await s.execute(select(TaskRun).order_by(TaskRun.id))
        ).scalars().all()
        return [(r.task_type, r.status) for r in rows]


async def _read_cooldown(db):
    async with db() as s:
        row = await s.get(SystemConfig, ns_mod._COOLDOWN_KEY)
        return row.value if row else None


# ---------------------------------------------------------------------------
# 锁粒度（C9）：冷却 sleep 期间不持锁
# ---------------------------------------------------------------------------

def test_cooling_sleep_not_hold_lock_force_not_blocked(db, monkeypatch):
    """冷却 sleep(30) 期间不持锁：force（刮削触发）不被在途兜底同步的 sleep 阻塞。

    design D3/C9：`_sync_lock` 缩临界区后，登录/重启/sleep(30)/同步在锁外执行；
    在途同步进行时 force 立即跳过（不重复重启，防 NasTools 双重启），而不是
    等待全链路（含 30s 重启等待）结束——刮削触发不被兜底同步长阻塞。
    """
    _reset_sync_state(monkeypatch)
    _make_sync_env(db, monkeypatch)

    gate = asyncio.Event()
    sleep_called = asyncio.Event()
    _hanging_sleep(monkeypatch, gate, sleep_called)

    async def scenario():
        t1 = asyncio.create_task(ns_mod.nastools_sync())  # 日常同步（进入 sleep）
        await sleep_called.wait()  # 已进入 sleep(30)（此时锁应已释放）
        t2 = asyncio.create_task(ns_mod.nastools_sync(force=True))  # 刮削触发
        # 不被 sleep 阻塞：1s 内必须完成（缩锁后立即「在途跳过」）
        await asyncio.wait_for(t2, timeout=1.0)
        gate.set()  # 放行 sleep
        await t1

    run(scenario())

    # force 在途跳过 → 未并发执行同步/重启（restart/run_directory_sync 仍各 1 次，
    # 均来自日常同步）→ 无 NasTools 双重启
    assert ns_mod.nastools.client.restart.await_count == 1
    assert ns_mod.nastools.client.run_directory_sync.await_count == 1
    records = run(_read_task_run_records(db))
    assert ("sync_nastools", "skipped") in records


def test_concurrent_regular_sync_skipped_when_inflight(db, monkeypatch):
    """同步在途时普通同步等效冷却跳过（不重复执行，防 NasTools 双重启）。"""
    _reset_sync_state(monkeypatch)
    _make_sync_env(db, monkeypatch)

    gate = asyncio.Event()
    sleep_called = asyncio.Event()
    _hanging_sleep(monkeypatch, gate, sleep_called)

    async def scenario():
        t1 = asyncio.create_task(ns_mod.nastools_sync())
        await sleep_called.wait()  # 第一路已进入 sleep（在途）
        t2 = asyncio.create_task(ns_mod.nastools_sync())  # 第二路普通同步
        await asyncio.wait_for(t2, timeout=1.0)  # 在途 → 立即跳过
        gate.set()
        await t1

    run(scenario())

    assert ns_mod.nastools.client.restart.await_count == 1  # 只有第一路执行
    records = run(_read_task_run_records(db))
    assert records.count(("sync_nastools", "skipped")) == 1  # 第二路记为 skipped


def test_sync_success_updates_cooldown_timestamp(db, monkeypatch):
    """同步成功后更新冷却时间戳（nastools_last_sync_at），语义保持。"""
    _reset_sync_state(monkeypatch)
    _make_sync_env(db, monkeypatch)
    monkeypatch.setattr(ns_mod.asyncio, "sleep", AsyncMock())  # 不真实等 30s

    run(ns_mod.nastools_sync())

    raw = run(_read_cooldown(db))
    assert raw is not None  # 冷却时间戳已写入
    last = datetime.fromisoformat(raw)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert (now - last).total_seconds() < 60  # 时间戳为最近（同步完成时刻）
    records = run(_read_task_run_records(db))
    assert ("sync_nastools", "success") in records
