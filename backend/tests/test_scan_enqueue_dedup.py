"""_enqueue 跨键防重回归测试（2026-09 重复下载事故修复）。

背景（生产事故）：影视「渗透」同一集在 download_queue 出现两条 downloading 记录。
根因：Emby 收录状态变化导致同一物理文件在两轮巡检产生不同防重键——
- 13:37 轮 Emby 未收录 → 全量模式 → 文件名无 SxxExx → 键=文件名「渗透 - 第05集.mp4」
- 14:19 轮 Emby 已收录 → 标准模式 → match_missing 归一化为「S01E05」
两个键字符串不同，绕过 UNIQUE(media_id, episode)，同一集被下载两次。

修复：_enqueue 幂等检查在「精确键」之外追加「同 media 同 file_name」兼容检查——
同一物理文件无论以哪种键入队，第二次一律视为已存在（existing），杜绝重复下载。

覆盖：
- 同 media 同键重复入队 → existing（既有行为）
- 同 media 不同键（文件名键 ↔ SxxExx 键）但 file_name 相同 → existing（本次修复）
- 同 media 不同 file_name → 正常入队（不误伤）
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.database import Base
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


def _patch_enqueue_env(monkeypatch, db):
    """让 scan 模块的 async_session 指向测试 DB。"""
    import app.tasks.scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    return scan_mod


async def _seed_media(db, *, title="渗透", tmdb_id=999):
    async with db() as s:
        media = Media(title=title, media_type="tv", tmdb_id=tmdb_id)
        s.add(media)
        await s.commit()
        return media.id


async def _seed_download_queue(db, media_id, *, episode, file_name, status="pending"):
    """预置 download_queue 行（同 task_queue 的 episode 形态：SxxExx 或 文件名）。"""
    async with db() as s:
        s.add(DownloadQueue(
            media_id=media_id,
            episode=episode,
            file_name=file_name,
            file_size=1_000_000,
            share_code="share123",
            status=status,
        ))
        await s.commit()


def _payload():
    return {
        "pwd_id": None,
        "stoken": None,
        "receive_code": None,
        "fids": None,
        "fid_tokens": None,
        "folder_id": None,
    }


async def _enqueue_once(scan_mod, media_id, episode_key, file_name):
    """直接调用 _enqueue，返回其返回值。"""
    from app.tasks.scan import _enqueue

    return await _enqueue(media_id, episode_key, file_name, 1_000_000, "share123", _payload())


def test_enqueue_same_key_duplicate_returns_existing(db, monkeypatch):
    """既有行为：同 media 同 episode 键重复入队 → existing。"""
    scan_mod = _patch_enqueue_env(monkeypatch, db)
    mid = run(_seed_media(db))

    r1 = run(_enqueue_once(scan_mod, mid, "S01E05", "渗透 - 第05集.mp4"))
    assert r1 == "enqueued"
    r2 = run(_enqueue_once(scan_mod, mid, "S01E05", "渗透 - 第05集.mp4"))
    assert r2 == "existing"


def test_enqueue_cross_key_same_file_returns_existing(db, monkeypatch):
    """本次修复：全量模式文件名键入队后，标准模式 SxxExx 键同文件再入队 → existing。

    对应生产事故：13:37 全量模式以「渗透 - 第05集.mp4」入队，
    14:19 标准模式以「S01E05」再次入队同一文件——必须被拦截。
    """
    scan_mod = _patch_enqueue_env(monkeypatch, db)
    mid = run(_seed_media(db))

    r1 = run(_enqueue_once(scan_mod, mid, "渗透 - 第05集.mp4", "渗透 - 第05集.mp4"))
    assert r1 == "enqueued"
    r2 = run(_enqueue_once(scan_mod, mid, "S01E05", "渗透 - 第05集.mp4"))
    assert r2 == "existing"


def test_enqueue_cross_key_reverse_same_file_returns_existing(db, monkeypatch):
    """反向场景：标准模式 SxxExx 键先入队，全量模式文件名键同文件再入队 → existing。"""
    scan_mod = _patch_enqueue_env(monkeypatch, db)
    mid = run(_seed_media(db))

    r1 = run(_enqueue_once(scan_mod, mid, "S01E05", "渗透 - 第05集.mp4"))
    assert r1 == "enqueued"
    r2 = run(_enqueue_once(scan_mod, mid, "渗透 - 第05集.mp4", "渗透 - 第05集.mp4"))
    assert r2 == "existing"


def test_enqueue_different_file_same_episode_key_returns_enqueued(db, monkeypatch):
    """不同物理文件（同键但不同 file_name，如同名同集不同来源版本）→ 正常入队。"""
    scan_mod = _patch_enqueue_env(monkeypatch, db)
    mid = run(_seed_media(db))

    r1 = run(_enqueue_once(scan_mod, mid, "S01E05", "渗透 - 第05集.mp4"))
    assert r1 == "enqueued"
    r2 = run(_enqueue_once(scan_mod, mid, "S01E06", "渗透 - 第06集.mp4"))
    assert r2 == "enqueued"


def test_enqueue_different_media_same_file_returns_enqueued(db, monkeypatch):
    """不同 media 相同 file_name → 正常入队（file_name 兼容检查限定同 media）。"""
    scan_mod = _patch_enqueue_env(monkeypatch, db)
    mid1 = run(_seed_media(db, title="渗透", tmdb_id=999))
    mid2 = run(_seed_media(db, title="另一部剧", tmdb_id=998))

    r1 = run(_enqueue_once(scan_mod, mid1, "S01E05", "渗透 - 第05集.mp4"))
    assert r1 == "enqueued"
    r2 = run(_enqueue_once(scan_mod, mid2, "S01E05", "渗透 - 第05集.mp4"))
    assert r2 == "enqueued"


def test_enqueue_skips_when_download_queue_has_same_episode(db, monkeypatch):
    """T8.8：download_queue 已有同 media 同集（跨文件名）→ existing 不入队。

    全量模式防重：同一集以不同文件名在两轮巡检被识别（Emby 收录状态变化 / 不同
    资源文件名），download_queue 已存在该集任意状态 → 视为已存在，task_queue 不新增。
    """
    scan_mod = _patch_enqueue_env(monkeypatch, db)
    mid = run(_seed_media(db))

    run(_seed_download_queue(db, mid, episode="S01E01", file_name="甲.mkv"))
    r = run(_enqueue_once(scan_mod, mid, "S01E01", "乙.mkv"))
    assert r == "existing"

    async def _count_task_queue():
        async with db() as s:
            return (await s.execute(select(func.count()).select_from(TaskQueue))).scalar_one()

    assert run(_count_task_queue()) == 0


def test_enqueue_normalizes_episode_key_for_dedup(db, monkeypatch):
    """T8.8 归一化：download_queue 'S1E1' 与请求键 'S01E01'（不同文件名）→ 防重命中。

    覆盖集号多种表示（SxxExx 季+集任意位数 / 第N集 / 独立数字），归一化后比较。
    """
    scan_mod = _patch_enqueue_env(monkeypatch, db)
    mid = run(_seed_media(db))

    run(_seed_download_queue(db, mid, episode="S1E1", file_name="甲.mkv"))
    r = run(_enqueue_once(scan_mod, mid, "S01E01", "乙.mkv"))
    assert r == "existing"


def test_enqueue_not_blocked_by_download_queue_other_episode(db, monkeypatch):
    """T8.8 不误伤：download_queue 已有 S01E01，入队的是同 media 不同集 → 正常入队。"""
    scan_mod = _patch_enqueue_env(monkeypatch, db)
    mid = run(_seed_media(db))

    run(_seed_download_queue(db, mid, episode="S01E01", file_name="甲.mkv"))
    r = run(_enqueue_once(scan_mod, mid, "S01E06", "乙.mkv"))
    assert r == "enqueued"
