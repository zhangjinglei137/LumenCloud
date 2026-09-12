"""取件-创建原子化（fix-transfer-flow-reliability T6）与取件契约单测。

T6（design §T6）：_fetch_from_task_queue 由「先 CAS ready→done + 保存点 INSERT DQ」
改为「单语句条件 INSERT（INSERT...SELECT...WHERE NOT EXISTS）+ 命中行同事务置 done」。
旧实现撞 UNIQUE 时保存点回滚但 done 已在外层事务提交 → 源行被误标终态；
新实现影响行数 0（撞 UNIQUE 或源行已被并发取件）→ 不置 done、保持 ready。

保留契约：只取 status='ready'；NOT EXISTS 同键排除（保持 ready 不占 LIMIT 名额）；
按 (created_at, id) FIFO；返回生成行数；num=10；download_name 不填。

fixture/模式参照 test_transfer.py 既有体系：独立 in-memory SQLite（StaticPool 共享
连接），不连任何真实外部服务/数据库。
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Select, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  注册全部 ORM 模型
import app.tasks.transfer as transfer_mod
from app.models import DownloadQueue, Media, TaskQueue


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


@pytest.fixture()
def transfer_env(monkeypatch, db):
    """把 transfer 模块的 async_session 指向测试库（_fetch_from_task_queue 直接调用）。"""
    monkeypatch.setattr(transfer_mod, "async_session", db)
    return db


# ---------------------------------------------------------------------------
# 种子数据 / 读取
# ---------------------------------------------------------------------------

async def seed_media(db, *, title="测试剧", media_type="tv"):
    async with db() as s:
        media = Media(title=title, media_type=media_type, tmdb_id=None, status="tracking")
        s.add(media)
        await s.flush()
        await s.commit()
        return media.id


async def seed_tq(db, mid, *, episode="S01E01", status="ready", share_code="TqXxYyZz1234",
                  created_at=None, file_name="ep.mkv", file_size=1024):
    async with db() as s:
        tq = TaskQueue(
            media_id=mid, episode=episode, file_name=file_name, file_size=file_size,
            share_code=share_code, pwd_id="pwd", stoken="st", receive_code="rc",
            fids="[]", fid_tokens="[]", folder_id="fd", status=status,
            probe_attempt=1, silent_until=None, error=None,
            created_at=created_at or _now(), updated_at=_now(),
        )
        s.add(tq)
        await s.flush()
        await s.commit()
        return tq.id


async def seed_dq(db, mid, *, episode="S01E01", status="pending", task_queue_id=None):
    async with db() as s:
        dq = DownloadQueue(
            media_id=mid, episode=episode, task_queue_id=task_queue_id,
            file_name="ep.mkv", file_size=1024, share_code="DqAaBbCcDdEe",
            pwd_id="pwd", stoken="st", receive_code="rc", fids="[]",
            fid_tokens="[]", folder_id="fd", status=status,
            enqueued_at=_now(), updated_at=_now(),
        )
        s.add(dq)
        await s.flush()
        await s.commit()
        return dq.id


async def read_tq(db, tq_id):
    async with db() as s:
        return await s.get(TaskQueue, tq_id)


async def count_dq(db, *, media_id=None):
    async with db() as s:
        stmt = select(DownloadQueue)
        if media_id is not None:
            stmt = stmt.where(DownloadQueue.media_id == media_id)
        return len((await s.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# T6：取件-创建原子化
# ---------------------------------------------------------------------------

def test_fetch_conflict_during_loop_keeps_source_row_ready(db, monkeypatch, transfer_env):
    """T6 核心：取件循环期间另一路径抢先建同键 DQ → 源行保持 ready，不误标 done。

    构造：预筛候选 SELECT 通过后（循环开始前）注入同键 DQ 行，模拟另一条 DQ 写入
    路径在「预筛与创建之间」抢先落库。旧实现此时已 CAS ready→done，保存点 INSERT
    撞 UNIQUE 回滚但 done 已在外层事务提交 → 源行被误标 done（RED）；新实现单语句
    条件 INSERT 的 NOT EXISTS 看到注入行 → 影响 0 行 → 不置 done（GREEN）。

    生产代码中被此测试捕获的缺陷：CAS 置 done 与 DQ 创建未原子化（撞 UNIQUE 时
    源行终态误标）。
    """
    mid = run(seed_media(db))
    tq_id = run(seed_tq(db, mid, episode="S01E01", status="ready"))

    real_execute = AsyncSession.execute
    injected = False

    async def _execute(self, stmt, *args, **kwargs):
        nonlocal injected
        result = await real_execute(self, stmt, *args, **kwargs)
        # hook：_fetch_from_task_queue 内首个 SELECT（候选预筛）返回后，在同一事务内
        # 注入同键 DQ 行——模拟并发 DQ 写入路径在预筛通过后抢先建 DQ。
        if not injected and isinstance(stmt, Select):
            injected = True
            await real_execute(self, insert(DownloadQueue).values(
                media_id=mid, episode="S01E01", task_queue_id=tq_id,
                file_name="ep.mkv", file_size=1024, share_code="Conflict111111",
                pwd_id="pwd", stoken="st", receive_code="rc", fids="[]",
                fid_tokens="[]", folder_id="fd", status="pending",
                enqueued_at=_now(), updated_at=_now(),
            ))
        return result

    monkeypatch.setattr(AsyncSession, "execute", _execute)

    n = run(transfer_mod._fetch_from_task_queue())

    tq = run(read_tq(db, tq_id))
    # 影响 0 行 → 不置 done：源行必须保持 ready（等待下轮或并发路径处理）
    assert n == 0
    assert tq.status == "ready"
    # DQ 行数不变（仅注入行，_fetch 未重复生成）
    assert run(count_dq(db, media_id=mid)) == 1


def test_fetch_conflict_keeps_source_row_ready(db, monkeypatch, transfer_env):
    """同键 DQ 已存在（任意来源，如另一 DQ 写入路径先行落库）→ 该 ready 任务被
    SQL 层排除：源行保持 ready（不误标 done）、不生成新行、返回计数不包含该行。"""
    mid = run(seed_media(db))
    tq_id = run(seed_tq(db, mid, episode="S01E01", status="ready"))
    dq_id = run(seed_dq(db, mid, episode="S01E01", status="pending"))

    n = run(transfer_mod._fetch_from_task_queue())

    tq = run(read_tq(db, tq_id))
    assert n == 0
    assert tq.status == "ready"
    assert run(count_dq(db, media_id=mid)) == 1


def test_fetch_generates_pending_and_done_fifo(db, monkeypatch, transfer_env):
    """正常路径：ready 行按 (created_at, id) FIFO 取件 → DownloadQueue(pending) +
    源行同事务置 done；返回生成行数。确保原子化改造不破坏取件主链路契约。"""
    mid = run(seed_media(db))
    base = _now()
    t1 = run(seed_tq(db, mid, episode="S01E01", created_at=base - timedelta(minutes=2),
                     share_code="FifoAaa111111"))
    t2 = run(seed_tq(db, mid, episode="S01E02", created_at=base - timedelta(minutes=1)))

    n = run(transfer_mod._fetch_from_task_queue())

    assert n == 2

    async def _rows():
        async with db() as s:
            dqs = (await s.execute(
                select(DownloadQueue).where(DownloadQueue.media_id == mid)
                .order_by(DownloadQueue.id)
            )).scalars().all()
            tqs = (await s.execute(
                select(TaskQueue).where(TaskQueue.media_id == mid)
                .order_by(TaskQueue.created_at, TaskQueue.id)
            )).scalars().all()
            return dqs, tqs
    dqs, tqs = run(_rows())

    # FIFO 顺序生成 pending，task_queue_id 溯源；download_name 取件阶段不填
    assert [d.episode for d in dqs] == ["S01E01", "S01E02"]
    assert all(d.status == "pending" for d in dqs)
    assert dqs[0].task_queue_id == t1 and dqs[1].task_queue_id == t2
    assert all(d.download_name is None for d in dqs)
    # 取件源行同事务置 done（防重复取件）
    assert all(t.status == "done" for t in tqs)

    # 幂等：二次取件无新行、无新状态变更
    assert run(transfer_mod._fetch_from_task_queue()) == 0
    assert run(count_dq(db, media_id=mid)) == 2
