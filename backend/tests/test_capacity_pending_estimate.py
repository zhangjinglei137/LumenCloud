"""容量积压预估（_pending_estimate_gb）口径测试：改查 DownloadQueue pending 行。

审查 B1：系统迁移至 download_queue 双队列后，旧 transfer_queue 表无数据，
capacity 端点 pending_estimate 恒为 0/None → 前端容量条「未消费积压」失真。
本测试锁定新口径：pending 预估 = download_queue 中 status=pending 行的
file_size 合计（GB）；无 pending 行返回 0（coalesce）；在途状态
（transferring/downloading）不计入 pending 预估（transferring 计入 reserved，
语义区分：pending 未占容量、仅预估展示）。

同 test_capacity.py 范式：独立 in-memory SQLite（StaticPool）直连私有函数，
不依赖外部服务。
"""
import asyncio
from datetime import datetime, timezone

import pytest

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  注册全部 ORM 模型
from app.models import DownloadQueue, Media
from app.routers.capacity import _pending_estimate_gb, _reserved_gb

GB = 1024 ** 3


def run(coro):
    return asyncio.run(coro)


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


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _seed(db, rows):
    """rows: [(episode, file_size, status), ...] → 各插入一条 download_queue 行。"""
    async with db() as s:
        media = Media(title="容量预估测试剧", media_type="tv", tmdb_id=None, status="tracking")
        s.add(media)
        await s.flush()
        for episode, file_size, status in rows:
            s.add(
                DownloadQueue(
                    media_id=media.id, episode=episode, file_name=f"{episode}.mkv",
                    file_size=file_size, share_code="sc123", stoken="st",
                    receive_code="rc", fids="[]", fid_tokens="[]", folder_id="fd",
                    status=status, node_attempt=0,
                    enqueued_at=_now(), updated_at=_now(),
                )
            )
        await s.commit()


def test_pending_estimate_sums_download_queue_pending_rows(db):
    """DownloadQueue 有 pending 行 → pending_estimate = pending 行 file_size 合计（GB）。"""
    async def scenario():
        await _seed(db, [
            ("S01E01", int(1 * GB), "pending"),
            ("S01E02", int(2 * GB), "pending"),
        ])
        async with db() as s:
            estimate = await _pending_estimate_gb(s)
        assert estimate == pytest.approx(3.0, abs=1e-6)

    run(scenario())


def test_pending_estimate_zero_when_no_pending_rows(db):
    """DownloadQueue 无 pending 行 → pending_estimate 为 0（coalesce，非 None）。"""
    async def scenario():
        await _seed(db, [
            ("S01E01", int(1 * GB), "failed"),
        ])
        async with db() as s:
            estimate = await _pending_estimate_gb(s)
        assert estimate == 0.0

    run(scenario())


def test_pending_estimate_excludes_inflight_statuses(db):
    """在途状态（transferring/downloading）不计入 pending 预估；transferring 计入 reserved。"""
    async def scenario():
        await _seed(db, [
            ("S01E01", int(2 * GB), "transferring"),
            ("S01E02", int(4 * GB), "downloading"),
        ])
        async with db() as s:
            estimate = await _pending_estimate_gb(s)
            reserved = await _reserved_gb(s)
        assert estimate == 0.0                       # 在途不占 pending 预估
        assert reserved == pytest.approx(2.0, abs=1e-6)  # transferring 计入预留

    run(scenario())
