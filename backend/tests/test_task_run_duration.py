"""Q8①（P2）：运行日志「耗时恒 0」修复——record_task_run 真实耗时 + duration_seconds 列。

- record_task_run 支持 duration_seconds（job 入口 time.monotonic() 传入）与
  started_at（可选，entry 级起表时间）；旧调用（不传新参数）行为完全不变
- logs 路由返回项含 duration_seconds 字段（前端 LogsView 优先展示真实耗时，
  历史记录为 null 时回退 started/finished 差值）

隔离的 in-memory SQLite（Base.metadata.create_all，不触全局 app.database
engine / TestClient），参照 test_fix_online 模式。
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  注册全部 ORM 模型
from app.database import Base
from app.models import TaskRun
from app.routers.logs import list_logs
from app.tasks import record_task_run


def run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def _db_maker():
    """隔离的 in-memory SQLite（StaticPool 共享连接），create_all 最新模型结构。"""
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
# Q8①：record_task_run 新参数
# ---------------------------------------------------------------------------

def test_record_task_run_persists_duration_and_started_at(_db_maker):
    """传 duration_seconds=3.14 → 落库 3.14；started_at 生效（finished_at > started_at）。"""
    async def _case():
        async with _db_maker() as session:
            started = _now()
            rid = await record_task_run(
                session, "scan_all_media", "success", "巡检完成",
                duration_seconds=3.14, started_at=started,
            )
            row = await session.get(TaskRun, rid)
            assert row.duration_seconds == 3.14
            assert row.started_at == started
            assert row.finished_at > row.started_at  # 真实起止时间戳落地

    run(_case())


def test_record_task_run_backward_compatible_defaults(_db_maker):
    """不传新参数 → duration_seconds 为 None、started_at == finished_at（旧行为完全不变）。"""
    async def _case():
        async with _db_maker() as session:
            rid = await record_task_run(session, "notify", "skipped", "空跑")
            row = await session.get(TaskRun, rid)
            assert row.duration_seconds is None
            assert row.started_at == row.finished_at

    run(_case())


# ---------------------------------------------------------------------------
# Q8①：logs 路由返回 duration_seconds
# ---------------------------------------------------------------------------

def test_logs_dto_includes_duration_seconds(_db_maker):
    """直接调 list_logs（绕过 Depends 注入，admin=MagicMock）→ 返回项含 duration_seconds=3.14。"""
    async def _seed():
        async with _db_maker() as session:
            session.add(TaskRun(
                task_type="transfer", status="success", message="转存完成",
                started_at=_now(), duration_seconds=3.14,
            ))
            await session.commit()

    run(_seed())

    async def _case():
        async with _db_maker() as session:
            rows = await list_logs(
                admin=MagicMock(),
                session=session,
                task_type=None,
                status=None,
                media_id=None,
                tmdb_id=None,
                title=None,  # P1-2 新增参数：绕过 FastAPI 注入时须显式传默认值
                limit=50,
                offset=0,
            )
            assert len(rows) == 1
            assert "duration_seconds" in rows[0]
            assert rows[0]["duration_seconds"] == 3.14

    run(_case())