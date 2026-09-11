"""size_estimated 落库 / 拷贝 / aria2 真实大小回填链路测试（Task 2）。

基建完全参照 backend/tests/test_library_check.py / test_transfer.py：
- 独立 in-memory SQLite（StaticPool 共享连接），create_all 建表；
- fake 依赖（monkeypatch 模块级 aria2/notifier/async_session），不连真实外部服务；
- 迁移冒烟用 subprocess 跑真 alembic（隔离临时 LUMENCLOUD_DATA_DIR，不污染测试库）。

用例：
1. scan._enqueue(size_estimated=True) → TaskQueue 行 size_estimated=True（写入链路）
2. transfer._fetch_from_task_queue 取件 → DownloadQueue.size_estimated 与 TQ 一致（拷贝链路）
3. transfer._complete_download(real_size=123) → DQ.file_size==123 且 size_estimated=False（轮询回填）
4. transfer._complete_download(real_size=None) → 不改 file_size、不清估算标记（幂等/缺省）
5. 迁移 0016 upgrade/downgrade 冒烟：upgrade head 后两表含 size_estimated 列，
   downgrade 0015_episode_info_cache 后列消失（对称回滚）。
   注：本 change 已在 0016_queue_size_estimated 之上插入新迁移头
   （0016_tmdb_cache_aliases），alembic 的 -1 按 down_revision 只回退 1 版、
   不再等于回到 0015，故显式指定目标 revision。
"""
import asyncio
import os
import sqlite3
import subprocess
import sys
import tempfile
import types
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  注册全部 ORM 模型
import app.tasks.scan as scan_mod
import app.tasks.transfer as transfer_mod
from app.models import DownloadQueue, Media, TaskQueue


def run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# 基建：in-memory SQLite + fake 依赖（同 test_transfer.py 模式）
# ---------------------------------------------------------------------------

class FakeNotifier:
    def __init__(self):
        self.events = []

    async def notify(self, event):
        self.events.append(event)


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
    """fake notifier + _spawn 跟踪（_complete_download → _after_complete_promote 需要）。"""
    fakes = {
        "notifier": FakeNotifier(),
        "spawn": [],
    }
    monkeypatch.setattr(transfer_mod, "notifier", fakes["notifier"])
    monkeypatch.setattr(transfer_mod, "_spawn", lambda factory: fakes["spawn"].append(factory))
    return fakes


def patch_db(monkeypatch, db):
    """把被测模块使用的 async_session 换成测试库。"""
    monkeypatch.setattr(scan_mod, "async_session", db)
    monkeypatch.setattr(transfer_mod, "async_session", db)


# ---------------------------------------------------------------------------
# 种子数据 / 读取
# ---------------------------------------------------------------------------

async def seed_media(db, title="估算标记测试剧") -> int:
    async with db() as s:
        media = Media(title=title, media_type="tv", tmdb_id=None, status="tracking")
        s.add(media)
        await s.flush()
        await s.commit()
        return media.id


async def get_tq_by_media(db, media_id):
    async with db() as s:
        return (
            await s.execute(
                select(TaskQueue).where(TaskQueue.media_id == media_id)
            )
        ).scalars().first()


async def get_dq_by_media(db, media_id):
    async with db() as s:
        return (
            await s.execute(
                select(DownloadQueue).where(DownloadQueue.media_id == media_id)
            )
        ).scalars().first()


async def seed_downloading(db, mid, *, file_size=1024, size_estimated=True, gid="gid-1"):
    """media + download_queue(downloading)。返回 dq_id。"""
    async with db() as s:
        dq = DownloadQueue(
            media_id=mid, episode="S01E01", file_name="ep.mkv", file_size=file_size,
            share_code="sc123", stoken="st", receive_code="rc", fids="[]",
            fid_tokens="[]", folder_id="fd", status="downloading",
            aria2_gid=gid, quark_path="/quark/ep.mkv", size_estimated=size_estimated,
            node_attempt=0, node_started_at=_now(), updated_at=_now(),
        )
        s.add(dq)
        await s.flush()
        await s.commit()
        return dq.id


async def read_row(db, model, obj_id):
    async with db() as s:
        return await s.get(model, obj_id)


# ---------------------------------------------------------------------------
# 1. 写入链路：scan._enqueue(size_estimated=True) → TaskQueue 落库
# ---------------------------------------------------------------------------

def test_enqueue_persists_size_estimated(db, monkeypatch):
    patch_db(monkeypatch, db)
    mid = run(seed_media(db))

    res = run(scan_mod._enqueue(
        mid, "S01E01", "ep.mkv", 1024, "sc123",
        {"fids": ["f1"], "fid_tokens": ["ft1"], "pwd_id": "pwd", "stoken": "st"},
        size_estimated=True,
    ))
    assert res == "enqueued"

    tq = run(get_tq_by_media(db, mid))
    assert tq is not None
    assert tq.size_estimated is True


def test_enqueue_default_size_estimated_false(db, monkeypatch):
    """缺省 size_estimated=False：非估算入队（真实 size）不标估算标记。"""
    patch_db(monkeypatch, db)
    mid = run(seed_media(db))

    run(scan_mod._enqueue(
        mid, "S01E02", "ep2.mkv", 2048, "sc456", {"fids": ["f2"]},
    ))

    tq = run(get_tq_by_media(db, mid))
    assert tq.size_estimated is False


# ---------------------------------------------------------------------------
# 2. 拷贝链路：取件 _fetch_from_task_queue → DownloadQueue 与 TQ 一致
# ---------------------------------------------------------------------------

async def seed_tq(db, mid, *, episode="S01E01", size_estimated=True):
    async with db() as s:
        tq = TaskQueue(
            media_id=mid, episode=episode, file_name=f"{episode}.mkv", file_size=1024,
            share_code="sc123", pwd_id="pwd", stoken="st", receive_code="rc",
            fids="[]", fid_tokens="[]", folder_id="fd", status="ready",
            size_estimated=size_estimated,
            probe_attempt=1, created_at=_now(), updated_at=_now(),
        )
        s.add(tq)
        await s.flush()
        await s.commit()
        return tq.id


def test_fetch_copies_size_estimated_from_tq(db, monkeypatch):
    patch_db(monkeypatch, db)
    mid = run(seed_media(db))
    run(seed_tq(db, mid, size_estimated=True))

    fetched = run(transfer_mod._fetch_from_task_queue())

    assert fetched == 1
    dq = run(get_dq_by_media(db, mid))
    assert dq is not None
    assert dq.size_estimated is True


def test_fetch_copies_size_estimated_false_when_tq_false(db, monkeypatch):
    """TQ 未标估算（False）→ 取件产物同样 False（不误标）。"""
    patch_db(monkeypatch, db)
    mid = run(seed_media(db))
    run(seed_tq(db, mid, size_estimated=False))

    run(transfer_mod._fetch_from_task_queue())

    dq = run(get_dq_by_media(db, mid))
    assert dq.size_estimated is False


# ---------------------------------------------------------------------------
# 3/4. 回填链路：_complete_download(real_size) → 真实 file_size + 清估算标记
# ---------------------------------------------------------------------------

def test_complete_download_backfills_real_size_and_clears_flag(db, env, monkeypatch):
    patch_db(monkeypatch, db)
    mid = run(seed_media(db))
    dq_id = run(seed_downloading(db, mid, file_size=1024, size_estimated=True))

    run(transfer_mod._complete_download(
        dq_id, mid, "S01E01", "ep.mkv", "/quark/ep.mkv", 0, 0, real_size=123,
    ))

    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "scrape"
    assert dq.file_size == 123          # aria2 totalLength 回填
    assert dq.size_estimated is False   # 估算标记清除


def test_complete_download_without_real_size_keeps_file_size(db, env, monkeypatch):
    """real_size=None（totalLength 缺失/取数失败）→ 不覆盖 file_size、不清估算标记。"""
    patch_db(monkeypatch, db)
    mid = run(seed_media(db))
    dq_id = run(seed_downloading(db, mid, file_size=1024, size_estimated=True))

    run(transfer_mod._complete_download(
        dq_id, mid, "S01E01", "ep.mkv", "/quark/ep.mkv", 0, 0, real_size=None,
    ))

    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "scrape"
    assert dq.file_size == 1024         # 未被覆盖
    assert dq.size_estimated is True    # 估算标记保留（等待真实值）
    assert env["notifier"].events and env["spawn"] == [transfer_mod.scrape_runner]


# ---------------------------------------------------------------------------
# 5. 迁移冒烟：alembic upgrade head / downgrade -1 对称增删列
# ---------------------------------------------------------------------------

def _sqlite_columns(db_path: str, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r[1] for r in rows}
    finally:
        conn.close()


def test_migration_0016_upgrade_downgrade_symmetry():
    """迁移 0016 冒烟：upgrade head 后 task_queue/download_queue 含 size_estimated 列；
    显式 downgrade 0015_episode_info_cache（回到 0015）后列对称删除。
    注：本 change 在 0016_queue_size_estimated 之上插入了 0016_tmdb_cache_aliases，
    alembic 的 -1 只会沿 down_revision 回退 1 版（落在 0016_queue_size_estimated，
    列仍在），故此处显式指定目标 revision 而非 -1。
    subprocess 隔离临时数据目录。"""
    backend = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="lumencloud_mig_") as td:
        db_path = os.path.join(td, "lumencloud.db")
        env = dict(os.environ, LUMENCLOUD_DATA_DIR=td, DATABASE_URL="")
        base = [sys.executable, "-m", "alembic", "-c", "alembic/alembic.ini"]
        assert "size_estimated" not in _sqlite_columns(db_path, "task_queue")

        # upgrade head：全链迁移（含 0016）后两表均含列
        subprocess.run(base + ["upgrade", "head"], cwd=backend, env=env,
                       check=True, capture_output=True, text=True)
        assert "size_estimated" in _sqlite_columns(db_path, "task_queue")
        assert "size_estimated" in _sqlite_columns(db_path, "download_queue")

        # downgrade 0015_episode_info_cache：回退到 0015 → size_estimated 列消失（对称删除），表仍在
        subprocess.run(base + ["downgrade", "0015_episode_info_cache"], cwd=backend, env=env,
                       check=True, capture_output=True, text=True)
        assert "size_estimated" not in _sqlite_columns(db_path, "task_queue")
        assert "size_estimated" not in _sqlite_columns(db_path, "download_queue")
        assert "file_size" in _sqlite_columns(db_path, "task_queue")  # 表仍存在
