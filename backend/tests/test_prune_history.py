"""D-1（P2）：task_run / quark_capacity_log 定期清理单测（prune_history_job）。

- 默认保留 30 天（_RETENTION_DAYS）；system_config task_run_retention_days 可覆盖。
- 清理以 started_at（task_run）/ checked_at（quark_capacity_log）为时间轴，
  一次批量 delete；失败 → task_run(error) 不抛异常。
- 隔离 in-memory SQLite（StaticPool 共享连接，Base.metadata.create_all），
  经 monkeypatch cleanup.async_session 注入，不触全局 app.database engine / TestClient。
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  注册全部 ORM 模型
from app.database import Base
from app.models import QuarkCapacityLog, SystemConfig, TaskRun
from app.tasks import cleanup as cleanup_module
from app.tasks.cleanup import prune_history_job


def run(coro):
    return asyncio.run(coro)


def _past(days: int) -> datetime:
    """naive UTC now 减去 days 天（与任务模块 _now / cutoff 同构）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)


@pytest.fixture()
def _db_maker():
    """隔离 in-memory SQLite（StaticPool 共享连接），create_all 最新模型结构。"""
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


def _use_test_db(monkeypatch, maker):
    """prune_history_job 使用测试库的连接工厂。"""
    monkeypatch.setattr(cleanup_module, "async_session", maker)


async def _dump(s, key):
    """查询 helper：seed 的 task_run 按 message、容量快照按 total_gb 区分年龄。
    只统计 scan_media（否则 prune_history 自身的成功记录也会入列，无法隔离断言）。"""
    if key == "task_run":
        rows = (
            await s.execute(select(TaskRun).where(TaskRun.task_type == "scan_media"))
        ).scalars().all()
        return [t.message for t in rows]
    return [l.total_gb for l in (await s.execute(select(QuarkCapacityLog))).scalars().all()]


# ---------------------------------------------------------------------------
# 1) 默认保留 30 天
# ---------------------------------------------------------------------------

def test_prune_history_default_retention_30_days(_db_maker, monkeypatch):
    _use_test_db(monkeypatch, _db_maker)

    async def _seed():
        async with _db_maker() as s:
            s.add_all([
                TaskRun(task_type="scan_media", status="success", message="old40",
                        started_at=_past(40), finished_at=_past(40)),
                TaskRun(task_type="scan_media", status="success", message="new5",
                        started_at=_past(5), finished_at=_past(5)),
                QuarkCapacityLog(total_gb=40.0, used_gb=0.0, source="alist",
                                 checked_at=_past(40)),
                QuarkCapacityLog(total_gb=5.0, used_gb=0.0, source="alist",
                                 checked_at=_past(5)),
            ])
            await s.commit()

    run(_seed())
    run(prune_history_job())

    async def _verify():
        async with _db_maker() as s:
            # 仅 40 天前的被删；5 天前的 task_run 与容量快照均保留
            assert await _dump(s, "task_run") == ["new5"]
            assert await _dump(s, "quark") == [5.0]
            # 写了 success 记录（started_at=now > cutoff，不会被本次批删）
            prunes = [
                r for r in (await s.execute(select(TaskRun))).scalars().all()
                if r.task_type == "prune_history"
            ]
            assert len(prunes) == 1
            assert prunes[0].status == "success"
            assert "task_run 1" in prunes[0].message and "容量快照 1" in prunes[0].message

    run(_verify())


# ---------------------------------------------------------------------------
# 2) system_config task_run_retention_days 覆盖
# ---------------------------------------------------------------------------

def test_prune_history_retention_days_override(_db_maker, monkeypatch):
    _use_test_db(monkeypatch, _db_maker)

    async def _seed():
        async with _db_maker() as s:
            s.add_all([
                TaskRun(task_type="scan_media", status="success", message="old8",
                        started_at=_past(8), finished_at=_past(8)),
                TaskRun(task_type="scan_media", status="success", message="new5",
                        started_at=_past(5), finished_at=_past(5)),
                QuarkCapacityLog(total_gb=8.0, used_gb=0.0, source="alist",
                                 checked_at=_past(8)),
                QuarkCapacityLog(total_gb=5.0, used_gb=0.0, source="alist",
                                 checked_at=_past(5)),
                SystemConfig(key="task_run_retention_days", value="7"),
            ])
            await s.commit()

    run(_seed())
    run(prune_history_job())

    async def _verify():
        async with _db_maker() as s:
            # cutoff 缩短为 7 天：8 天前删、5 天前保留（默认 30 天下都应保留）
            assert await _dump(s, "task_run") == ["new5"]
            assert await _dump(s, "quark") == [5.0]

    run(_verify())


# ---------------------------------------------------------------------------
# 3) 容错：批量 delete 抛异常 → 不抛，写 error 记录
# ---------------------------------------------------------------------------

def test_prune_history_delete_failure_records_error(_db_maker, monkeypatch):
    _use_test_db(monkeypatch, _db_maker)

    def _boom(*args, **kwargs):
        raise RuntimeError("db boom")

    monkeypatch.setattr(cleanup_module, "delete", _boom)

    run(prune_history_job())  # 不向外抛

    async def _verify():
        async with _db_maker() as s:
            recs = (await s.execute(select(TaskRun))).scalars().all()
            assert len(recs) == 1
            assert recs[0].task_type == "prune_history"
            assert recs[0].status == "error"

    run(_verify())