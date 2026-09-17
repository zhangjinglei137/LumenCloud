"""C4（fix-audit-issues）准入 quota_wait 分支 CAS 冲突识别回归。

对应 delta spec pipeline-admission「等待准入冲突处理」：容量不足走置 quota_wait 的
条件更新（CAS，WHERE status='pending'）未命中（并发方已把行推进出 pending）时，
系统 SHALL 将本次显式记为并发冲突（返回 'conflict'）并跳过，不得按「容量不足」
错误语义返回 'quota_wait'（审查 B7：误报配额等待、quota_count 计数失真、告警误报）。

测试风格沿用 test_admission_tx_boundary.py：同步测试函数 + run(coro) 驱动，全部
依赖 monkeypatch（async_session / capacity.provider / _preflight_quark_mount /
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
    """独立 in-memory SQLite（StaticPool 单连接共享）→ 返回 sessionmaker。"""
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


async def _read_dq(db, dq_id):
    async with db() as s:
        return await s.get(DownloadQueue, dq_id)


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
# C4 核心：quota_wait 分支 CAS 未命中 → 显式 conflict，不误报 quota_wait
# ---------------------------------------------------------------------------

def test_quota_wait_cas_miss_marks_conflict(db, monkeypatch):
    """容量不足分支的置 quota_wait CAS 未命中 → 返回 'conflict'（不误报 'quota_wait'）。

    并发方在锁外 check 之后、事务 B 之前把行推进出 pending（如抢占/取消，已提交）；
    事务 B 内容量复判不足 → 置 quota_wait 的条件更新（WHERE status='pending'）
    rowcount=0。修复前该分支不设 conflict，函数误报 'quota_wait'（quota_count=0、
    配额告警计数失真，审查 B7）；修复后与抢占分支同款处理——conflict=True →
    返回 'conflict'，行保持并发方推进后的状态、不被误置 quota_wait、不计数。
    """
    mid, dq_id = run(_seed_pending(db))

    async def fake_check(candidate_bytes):
        # 模拟并发方在锁外 check 之后推进该行出 pending（独立短事务，已提交）
        async with db() as s:
            await s.execute(
                update(DownloadQueue)
                .where(DownloadQueue.id == dq_id)
                .values(status="transferring", updated_at=_now())
            )
            await s.commit()
        return False  # 容量不足 → 本应走 quota_wait 置位分支

    monkeypatch.setattr(transfer_mod, "async_session", db)
    _patch_provider(monkeypatch, fake_check)
    monkeypatch.setattr(transfer_mod, "_preflight_quark_mount", AsyncMock(return_value=None))
    chain = AsyncMock(return_value="admitted")
    monkeypatch.setattr(transfer_mod, "_transfer_chain", chain)

    result = run(transfer_mod._try_admit_one(0.0))
    assert result == "conflict"  # 修复前误报 "quota_wait"
    # 行保持 transferring（并发方已推进；未被误置 quota_wait / 未被重复抢占）
    dq = run(_read_dq(db, dq_id))
    assert dq.status == "transferring"
    assert dq.quota_reject_count == 0  # CAS 未命中不计数（不误记配额拒绝）
    chain.assert_not_awaited()


# ---------------------------------------------------------------------------
# 回归保护：quota_wait 正常路径（CAS 命中）语义保持不变
# ---------------------------------------------------------------------------

def test_quota_wait_cas_hit_returns_quota_wait(db, monkeypatch):
    """容量不足且 CAS 命中（行仍 pending）→ 置 quota_wait，返回 'quota_wait'（本批停止）。

    修复只处理 CAS 未命中的误报分支；正常 quota_wait 语义（置位 + quota_reject_count++
    + 返回 'quota_wait' 停止本批）必须保持不变（主循环停止语义不回归）。
    """
    mid, dq_id = run(_seed_pending(db))

    async def fake_check(candidate_bytes):
        return False  # 容量不足（行保持 pending，CAS 命中）

    monkeypatch.setattr(transfer_mod, "async_session", db)
    _patch_provider(monkeypatch, fake_check)
    monkeypatch.setattr(transfer_mod, "_preflight_quark_mount", AsyncMock(return_value=None))
    chain = AsyncMock(return_value="admitted")
    monkeypatch.setattr(transfer_mod, "_transfer_chain", chain)

    result = run(transfer_mod._try_admit_one(0.0))
    assert result == "quota_wait"
    dq = run(_read_dq(db, dq_id))
    assert dq.status == "quota_wait"
    assert dq.quota_reject_count == 1
    chain.assert_not_awaited()
