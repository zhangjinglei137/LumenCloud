"""人工 retry 清零自动重下计数单测（change media-min-size-and-emby-redownload 车道 B / 3.2）。"""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
import app.routers.queue as queue_mod
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


def test_retry_task_resets_auto_redownload_count(db):
    """failed → pending 时同步清零 auto_redownload_count（人工重新给足自动重下预算）。"""
    async def seed():
        async with db() as s:
            media = Media(title="测试剧", media_type="tv", tmdb_id=42, status="tracking")
            s.add(media)
            await s.flush()
            dq = DownloadQueue(
                media_id=media.id, episode="S01E01", status="failed",
                retry_count=3, node_attempt=3,
                auto_redownload_count=2,
                node_error="err", error="err", file_name="ep.mkv", file_size=1024,
                share_code="sc", stoken="st", receive_code="rc", fids="[]",
                fid_tokens="[]", folder_id="fd", updated_at=_now(),
            )
            s.add(dq)
            await s.commit()
            return dq.id

    dq_id = run(seed())

    async def do_retry():
        async with db() as s:
            return await queue_mod.retry_task(
                task_id=dq_id, admin=SimpleNamespace(role="admin"), session=s,
            )

    assert run(do_retry()) == {"ok": True}

    async def read():
        async with db() as s:
            return await s.get(DownloadQueue, dq_id)

    dq = run(read())
    assert dq.status == "pending"
    assert dq.auto_redownload_count == 0
    assert dq.retry_count == 0
