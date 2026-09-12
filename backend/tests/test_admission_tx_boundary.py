"""P0-1 准入事务边界回归：容量 check 期间无活动 DB 事务；CAS 条件更新仍生效。

Task 1（tasks.md 1.1 / design T1）：准入段拆三段——短事务 A（取 pending + reserved
快照）→ 锁外容量 check（网络 IO + 快照落库脱离外层事务上下文）→ 短事务 B（锁行 +
重读 reserved + 容量复判 + CAS 抢占 / 置 quota_wait）。本文件回归两个边界：

- check 调用时不得存在活动 DB 事务（旧实现 check 在事务 B 内，嵌套 _persist_snapshot
  提交回潮事务内网络 IO）——用 StaticPool 单连接共享检测连接级事务状态；
- 事务 B 的 CAS（WHERE status='pending'）仍生效：并发方在锁外 check 之后抢占该行
  → 事务 B 内 CAS rowcount=0 → 返回 'conflict'，不重复转存。

测试风格沿用 test_transfer.py：同步测试函数 + run(coro) 驱动，全部依赖
monkeypatch（async_session / capacity.provider / _preflight_quark_mount /
_transfer_chain），独立 in-memory SQLite（StaticPool 共享连接）。
"""
import asyncio
import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  注册全部 ORM 模型
import app.tasks.transfer as transfer_mod
from app.models import DownloadQueue, Media


def run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def db():
    """独立 in-memory SQLite（StaticPool 单连接共享）→ 返回 sessionmaker。

    engine 附加在 maker.engine 上：fake capacity.check 用 engine.connect()
    检测**物理连接**的事务状态（StaticPool 下与 transfer 共用同一连接）。
    """
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
    maker.engine = engine
    yield maker
    run(engine.dispose())


async def _seed_pending(db, *, episode="S01E01", file_size=1024):
    """写入 media + download_queue(pending)。返回 (media_id, dq_id)。"""
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=None, status="tracking")
        s.add(media)
        await s.flush()
        mid = media.id
        dq = DownloadQueue(
            media_id=mid, episode=episode, file_name=f"测试剧.{episode}.mkv",
            file_size=file_size, share_code="sc", status="pending",
            enqueued_at=_now(), updated_at=_now(),
        )
        s.add(dq)
        await s.flush()
        await s.commit()
        return mid, dq.id


def _fake_usage():
    """复判用 usage 快照（used_gb + total_gb(=quota)）。"""
    return types.SimpleNamespace(used_gb=0.0, total_gb=100.0, source="fake")


def _patch_provider(monkeypatch, check, usage=None):
    """构造并替换 transfer 模块的 capacity.provider（check + get_usage）。"""
    provider = AsyncMock()
    provider.check = check
    provider.get_usage = AsyncMock(return_value=usage if usage is not None else _fake_usage())
    monkeypatch.setattr(
        transfer_mod, "capacity", types.SimpleNamespace(provider=provider)
    )
    return provider


# ---------------------------------------------------------------------------
# 边界 1：capacity.check 期间无活动 DB 事务（T1 核心）
# ---------------------------------------------------------------------------

def test_check_runs_outside_db_transaction(db, monkeypatch):
    """容量 check 调用时不得存在活动 DB 事务（短事务 A 已提交、事务 B 未开始）。

    StaticPool 单连接：engine.connect() 拿到的物理连接若已被外层事务（旧实现
    事务 B BEGIN 未提交）占用 → conn.in_transaction()=True；锁外调用则连接空闲
    → False。断言失败消息里同时给出 _try_admit_one 的异常（若有），便于定位
    旧实现下 close 回滚事务导致的连带现象。
    """
    in_tx_flags = []
    engine = db.engine

    async def fake_check(candidate_bytes):
        # StaticPool 单连接共享：用底层 DBAPI（aiosqlite）连接的 in_transaction
        # 检测物理连接是否已处于事务中（SQLAlchemy Connection 包装感知不到
        # 其他 session 在共享连接上开启的事务，需穿透 sync_connection）。
        # 旧实现 check 在事务 B（BEGIN 未提交）内 → True；新实现锁外 → False。
        # async with 归还连接（无事务时干净；有事务时 rollback 由 async 上下文
        # 安全处理，避免 raw_connection 的 MissingGreenlet 坑）。
        async with engine.connect() as conn:
            in_tx_flags.append(
                conn.sync_connection.connection.driver_connection.in_transaction
            )
        return True

    monkeypatch.setattr(transfer_mod, "async_session", db)
    _patch_provider(monkeypatch, fake_check)
    monkeypatch.setattr(transfer_mod, "_preflight_quark_mount", AsyncMock(return_value=None))
    monkeypatch.setattr(transfer_mod, "_transfer_chain", AsyncMock(return_value="admitted"))

    run(_seed_pending(db))

    error = None
    result = None
    try:
        result = run(transfer_mod._try_admit_one(0.0))
    except Exception as exc:  # noqa: BLE001
        error = exc

    assert in_tx_flags == [False], (
        f"capacity.check 期间不得有活动 DB 事务（实际 {in_tx_flags}，异常={error}）"
    )
    assert error is None, f"_try_admit_one 不应抛错: {error}"
    assert result == "admitted"


# ---------------------------------------------------------------------------
# 边界 2：事务 B 的 CAS 抢占仍生效（回归保护）
# ---------------------------------------------------------------------------

def test_cas_pending_to_transferring_still_applies(db, monkeypatch):
    """事务 B 的 CAS（WHERE status='pending'）仍生效：并发方已抢占 → conflict。

    并发方在锁外 check 之后、事务 B 之前抢占该行（pending→transferring 已提交）；
    事务 B 内锁行重读后 CAS rowcount=0 → 返回 'conflict'，行保持 transferring
    不被重复转存（_transfer_chain 不应被调用）。
    """
    mid, dq_id = run(_seed_pending(db))

    async def fake_check(candidate_bytes):
        # 模拟并发方在 check 之后抢占了该行（独立短事务，已提交）
        async with db() as s:
            await s.execute(
                update(DownloadQueue)
                .where(DownloadQueue.id == dq_id)
                .values(status="transferring", updated_at=_now())
            )
            await s.commit()
        return True

    monkeypatch.setattr(transfer_mod, "async_session", db)
    _patch_provider(monkeypatch, fake_check)
    monkeypatch.setattr(transfer_mod, "_preflight_quark_mount", AsyncMock(return_value=None))
    chain = AsyncMock(return_value="admitted")
    monkeypatch.setattr(transfer_mod, "_transfer_chain", chain)

    result = run(transfer_mod._try_admit_one(0.0))
    assert result == "conflict"
    # 行保持 transferring（并发方已抢占，不重复转存 / 不置回）
    async def _read_dq():
        async with db() as s:
            return await s.get(DownloadQueue, dq_id)
    dq = run(_read_dq())
    assert dq.status == "transferring"
    chain.assert_not_awaited()


# ---------------------------------------------------------------------------
# 边界 3：get_usage（含 _persist_snapshot 落库）无外层 DB 事务（T1 固化）
# ---------------------------------------------------------------------------

def test_persist_snapshot_commits_independently(db, monkeypatch):
    """get_usage（其内部 _persist_snapshot 自开 session 独立提交）不在外层事务内被调用。

    Task 1（design T1）下 get_usage/check 在短事务 A 已提交、事务 B 未开始的锁外
    上下文执行；_persist_snapshot 使用自有 async_session 独立 commit，无嵌套
    session 提交（旧实现 check 在事务 B 内，嵌套提交回潮事务内网络 IO）。

    同边界 1：StaticPool 单连接共享，穿透到 driver_connection 检测物理连接
    事务状态——get_usage 被调用时连接若处于事务中（旧实现）→ True，锁外 →
    False。断言消息里附 _try_admit_one 的异常（若有）便于定位。
    """
    in_tx_flags = []
    engine = db.engine

    async def fake_get_usage():
        async with engine.connect() as conn:
            in_tx_flags.append(
                conn.sync_connection.connection.driver_connection.in_transaction
            )
        return _fake_usage()

    provider = _patch_provider(monkeypatch, check=AsyncMock(return_value=True))
    provider.get_usage = fake_get_usage
    monkeypatch.setattr(transfer_mod, "async_session", db)
    monkeypatch.setattr(transfer_mod, "_preflight_quark_mount", AsyncMock(return_value=None))
    chain = AsyncMock(return_value="admitted")
    monkeypatch.setattr(transfer_mod, "_transfer_chain", chain)

    run(_seed_pending(db))
    error = None
    result = None
    try:
        result = run(transfer_mod._try_admit_one(0.0))
    except Exception as exc:  # noqa: BLE001
        error = exc

    assert in_tx_flags == [False], (
        f"get_usage（含 _persist_snapshot 落库）不得在活动事务内被调用"
        f"（实际 {in_tx_flags}，异常={error}）"
    )
    assert error is None, f"_try_admit_one 不应抛错: {error}"
    assert result == "admitted"
