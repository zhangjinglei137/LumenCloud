"""scan._due_filter 双方言 SQL 渲染与到期语义 + _enqueue 提交语义测试（Task D2）。

背景（审查 A16/A17）：
- A16：PG 分支 `minutes * text("interval '1 minute'")` 编译为
  `coalesce(...) * interval '1 minute'`——PG 仅支持 `interval * numeric`，
  `integer * interval` 在实库执行报错（编译期不报错，静默生成非法 SQL）。
- A17：_enqueue 在 `async with tx.begin()` 块内显式 commit/rollback，与上下文
  管理器双重管理提交，行为依赖 SQLAlchemy 版本；冲突捕获未走 savepoint。

覆盖：
- PG 方言下 _due_filter 编译生成 make_interval 合法 SQL（不出现 integer * interval）
- SQLite 方言下 _due_filter 编译不抛异常（双方言渲染；SQLite 分支保持原样）
- 到期判定语义：last_scan_at 为 NULL / 超期 → 筛出；刚巡检过 → 跳过（真 SQLite 执行）
- _enqueue 写冲突（UNIQUE）→ 返回 conflict 单条跳过、不抛异常；随后再入队正常
  （整轮不中断、事务未中毒）
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.database import Base
from app.models import Media, TaskQueue


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


def _patch_scan_engine(monkeypatch, dialect_name: str):
    """让 scan 模块的 engine.dialect.name 指向目标方言（决定 _due_filter 分支）。"""
    import app.tasks.scan as scan_mod

    monkeypatch.setattr(
        scan_mod, "engine", SimpleNamespace(dialect=SimpleNamespace(name=dialect_name))
    )
    return scan_mod


def _patch_scan_session(monkeypatch, db):
    """让 scan 模块的 async_session 指向测试 DB（_enqueue 路径）。"""
    import app.tasks.scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    return scan_mod


# ---------- _due_filter 双方言渲染 ----------


def test_due_filter_pg_compiles_to_make_interval(monkeypatch):
    """A16 修复：PG 分支必须生成 make_interval 合法 SQL，不再出现 integer * interval。

    旧实现 `minutes * text("interval '1 minute'")` 编译为
    `coalesce(...) * interval '1 minute'`——PG 仅支持 interval * numeric，
    integer * interval 在实库执行报错（审查 A16，编译期静默非法）。
    """
    _patch_scan_engine(monkeypatch, "postgresql")
    from app.tasks.scan import _due_filter

    expr = _due_filter()
    sql = str(expr.compile(dialect=postgresql.dialect()))
    assert "make_interval" in sql.lower()
    assert "* interval" not in sql.lower()


def test_due_filter_sqlite_compiles_with_datetime(monkeypatch):
    """双方言渲染：SQLite 分支编译不抛异常（保持原 datediff 类写法不动）。"""
    _patch_scan_engine(monkeypatch, "sqlite")
    from app.tasks.scan import _due_filter

    expr = _due_filter()
    sql = str(expr.compile(dialect=sqlite.dialect()))
    assert "datetime" in sql.lower()


# ---------- 到期判定语义（真 SQLite 执行） ----------


async def _seed_media(db, **kw):
    async with db() as s:
        media = Media(title=kw.pop("title", "测试剧"), media_type="tv", **kw)
        s.add(media)
        await s.commit()
        return media.id


def test_due_filter_semantics(monkeypatch, db):
    """到期语义：last_scan_at 为 NULL 或已超配置间隔 → 筛出；刚巡检过 → 跳过。"""
    _patch_scan_engine(monkeypatch, "sqlite")
    from app.tasks.scan import _due_filter

    now = _now()
    never_id = run(_seed_media(db, tmdb_id=1001, status="tracking", last_scan_at=None))
    overdue_id = run(_seed_media(
        db, tmdb_id=1002, status="tracking",
        scan_interval_minutes=60, last_scan_at=now - timedelta(days=10),
    ))
    fresh_id = run(_seed_media(
        db, tmdb_id=1003, status="tracking",
        scan_interval_minutes=60, last_scan_at=now - timedelta(minutes=5),
    ))
    paused_id = run(_seed_media(db, tmdb_id=1004, status="paused", last_scan_at=None))

    async def _query():
        async with db() as s:
            rows = (await s.execute(
                select(Media.id)
                .where(Media.status.in_(("tracking", "downloading")))
                .where(_due_filter())
            )).scalars().all()
            return set(rows)

    due_ids = run(_query())
    assert never_id in due_ids      # 从未巡检 → 到期
    assert overdue_id in due_ids    # 超间隔 → 到期
    assert fresh_id not in due_ids  # 刚巡检 → 未到期（不筛出）
    assert paused_id not in due_ids  # paused 不在巡检集合（status 过滤先行，与 scan_all_media 一致）


# ---------- _enqueue 冲突单条跳过、不中断整轮 ----------


def _payload():
    return {
        "pwd_id": None,
        "stoken": None,
        "receive_code": None,
        "fids": None,
        "fid_tokens": None,
        "folder_id": None,
    }


def test_enqueue_conflict_skips_single_and_recovers(monkeypatch, db):
    """A17 修复：写冲突（UNIQUE(media_id, episode)）→ 返回 conflict 单条跳过、不抛异常；
    随后再次入队正常 → 整轮不中断、事务未中毒。

    并发冲突外部条件（另一事务先提交同键记录）在单连接 SQLite 无法真实构造，以
    「首次 flush 抛 IntegrityError」模拟——这是真实并发冲突在 SQLAlchemy 中的唯一
    触发路径（flush/commit 时撞唯一约束）。
    """
    scan_mod = _patch_scan_session(monkeypatch, db)
    mid = run(_seed_media(db, tmdb_id=2001, status="tracking"))

    original_flush = AsyncSession.flush
    state = {"n": 0}

    async def _flaky_flush(self):
        state["n"] += 1
        if state["n"] == 1:
            raise IntegrityError(
                "INSERT INTO task_queue",
                {},
                Exception("UNIQUE constraint failed: media_id, episode"),
            )
        return await original_flush(self)

    monkeypatch.setattr(AsyncSession, "flush", _flaky_flush)

    from app.tasks.scan import _enqueue

    r1 = run(_enqueue(mid, "S01E05", "渗透 - 第05集.mp4", 1_000_000, "share123", _payload()))
    assert r1 == "conflict"  # 单条冲突跳过（不抛异常，调用方可 continue）

    r2 = run(_enqueue(mid, "S01E06", "渗透 - 第06集.mp4", 1_000_000, "share123", _payload()))
    assert r2 == "enqueued"  # 整轮不中断：事务恢复，后续入队正常

    async def _count_task_queue():
        async with db() as s:
            return (await s.execute(select(func.count()).select_from(TaskQueue))).scalar_one()

    assert run(_count_task_queue()) == 1  # 仅成功入队 1 条，冲突未留下半状态
