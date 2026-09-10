"""入库确认卡死修复：遗漏集 / 缺 tmdb_id 路径纳入超时窗口。

背景：library_check 轮询 status='library' 的任务，Emby 命中后若
`_episode_in_missing` 持续为 True（遗漏集路径）则 continue 且不经过超时判定，
永不 finalize；media.tmdb_id is None 分支同样不消耗超时。本测试验证两条路径
纳入超时窗口后：超时 → failed（记录原因），未超时 → 保持等待不误杀；
Emby 故障分支仍不消耗超时（保持既有语义）。

全部使用 fake 依赖（monkeypatch app.tasks.library_check 模块内的
emby/alist/notifier/async_session），数据库用独立 in-memory SQLite
（StaticPool 共享连接）——与 test_library_check.py 同模式。
"""
import asyncio
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  注册全部 ORM 模型
import app.tasks.library_check as library_check_mod
import app.tasks.transfer as transfer_mod
from app.models import DownloadQueue, Media
from app.services.emby import EmbyUnavailable


def run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class FakeAlist:
    def __init__(self):
        self.remove_calls = []  # [(names, dir)]

    async def remove(self, names, dir):
        self.remove_calls.append((list(names), dir))
        return {"success": True}

    async def list_dir(self, path, per_page=1000):
        return []


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
    """全套 fake 服务 + 替换 library_check 模块内的依赖引用。"""
    spawn_calls = []
    fakes = {
        "alist": FakeAlist(),
        "notifier": FakeNotifier(),
        "emby": types.SimpleNamespace(
            find_emby_id=AsyncMock(return_value=None),
            # 剧集入库确认的遗漏集查询默认返回空（当前集不在遗漏 → 可 finalize）
            get_missing_episodes=AsyncMock(return_value=[]),
        ),
        "spawn_calls": spawn_calls,
    }
    monkeypatch.setattr(library_check_mod, "alist", fakes["alist"])
    monkeypatch.setattr(library_check_mod, "notifier", fakes["notifier"])
    monkeypatch.setattr(library_check_mod, "emby", fakes["emby"])
    # _spawn 续跑记录不真实创建后台任务（防 asyncio.run 退出时挂起未完成任务）
    monkeypatch.setattr(library_check_mod, "_spawn", lambda f: spawn_calls.append(f))
    return fakes


def patch_db(monkeypatch, db):
    monkeypatch.setattr(library_check_mod, "async_session", db)
    monkeypatch.setattr(transfer_mod, "async_session", db)


# ---------------------------------------------------------------------------
# 种子数据 / 读取
# ---------------------------------------------------------------------------

async def seed_library(db, *, episode="S01E01", started_at=None, tmdb_id=42,
                       media_type="tv", quark_path="/quark/ep.mkv",
                       media_status="downloading"):
    """media + download_queue(status='library'，node_started_at 可指定超时场景)。返回 (mid, dq_id)。"""
    async with db() as s:
        media = Media(title="测试剧", media_type=media_type, tmdb_id=tmdb_id, status=media_status)
        s.add(media)
        await s.flush()
        dq = DownloadQueue(
            media_id=media.id, episode=episode, status="library",
            file_name="ep.mkv", file_size=1024, share_code="sc123",
            quark_path=quark_path, node_attempt=0,
            node_started_at=started_at or _now(), updated_at=_now(),
        )
        s.add(dq)
        await s.flush()
        await s.commit()
        return media.id, dq.id


async def get_dq(db, dq_id):
    async with db() as s:
        return await s.get(DownloadQueue, dq_id)


async def get_media(db, media_id):
    async with db() as s:
        return await s.get(Media, media_id)


# ---------------------------------------------------------------------------
# 卡死修复用例：遗漏集 / 缺 tmdb_id 路径纳入超时窗口
# ---------------------------------------------------------------------------

def test_missing_episode_always_true_expired_marks_failed(db, env, monkeypatch):
    """修复核心：Emby 命中但当前集持续在遗漏集，且已超时 → 置 failed（含超时原因），
    不再无限等待（修复前 continue 不经过超时判定，永不 finalize）。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_library(db, episode="S01E10", started_at=_now() - timedelta(seconds=700)))
    env["emby"].find_emby_id = AsyncMock(return_value="emby-1")
    env["emby"].get_missing_episodes = AsyncMock(return_value=[
        {"code": "S01E10", "season": 1, "episode": 10, "name": "E10"},
    ])

    run(library_check_mod.library_check())

    dq = run(get_dq(db, dq_id))
    assert dq.status == "failed"  # 不再无限等待
    assert "入库超时" in (dq.node_error or "")
    assert "遗漏集" in (dq.node_error or "")  # 记录「Emby 收录超时」原因
    # P1-6：超时 failed 后 best-effort 清理夸克中转文件
    assert env["alist"].remove_calls == [(["ep.mkv"], "/quark/")]
    assert run(get_media(db, mid)).status == "tracking"


def test_missing_episode_not_expired_keeps_waiting(db, env, monkeypatch):
    """遗漏集命中但未超时 → 保持 library 等待（不误杀、不删夸克、不通知）。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_library(db, episode="S01E10"))  # started_at=now，未超时
    env["emby"].find_emby_id = AsyncMock(return_value="emby-1")
    env["emby"].get_missing_episodes = AsyncMock(return_value=[
        {"code": "S01E10", "season": 1, "episode": 10, "name": "E10"},
    ])

    run(library_check_mod.library_check())

    dq = run(get_dq(db, dq_id))
    assert dq.status == "library"  # 未被误判 failed
    assert dq.node_error is None
    assert env["alist"].remove_calls == []  # 未 finalize 不删夸克
    assert env["notifier"].events == []
    assert env["spawn_calls"] == []
    assert run(get_media(db, mid)).status == "downloading"  # media 不误回退


def test_tmdb_id_none_expired_marks_failed(db, env, monkeypatch):
    """media.tmdb_id is None（配置缺失）且已超时 → 置 failed 并记录「缺 tmdb_id」原因。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_library(db, tmdb_id=None, started_at=_now() - timedelta(seconds=700)))
    env["emby"].find_emby_id = AsyncMock(return_value="emby-1")  # 不应被调用

    run(library_check_mod.library_check())

    dq = run(get_dq(db, dq_id))
    assert dq.status == "failed"
    assert "入库超时" in (dq.node_error or "")
    assert "tmdb_id" in (dq.node_error or "")  # 记录「缺 tmdb_id」原因
    env["emby"].find_emby_id.assert_not_awaited()  # 缺 tmdb_id 不查 Emby
    assert env["alist"].remove_calls == [(["ep.mkv"], "/quark/")]
    assert run(get_media(db, mid)).status == "tracking"


def test_emby_fault_skips_round_without_consuming_timeout(db, env, monkeypatch):
    """find_emby_id 抛异常（Emby 故障）→ 本轮跳过不消耗超时（即便已超时也不误杀）。
    保持既有语义：等 Emby 恢复后下轮正常判定。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_library(db, started_at=_now() - timedelta(seconds=700)))
    env["emby"].find_emby_id = AsyncMock(side_effect=EmbyUnavailable("Emby 请求超时"))

    run(library_check_mod.library_check())

    dq = run(get_dq(db, dq_id))
    assert dq.status == "library"  # 不误判 failed
    assert dq.node_error is None
    assert env["alist"].remove_calls == []
    assert run(get_media(db, mid)).status == "downloading"


def test_finalize_done_normal_path_no_regression(db, env, monkeypatch):
    """_finalize_done 正常路径不回归：Emby 收录（当前集不在遗漏集）→ done。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_library(db))
    env["emby"].find_emby_id = AsyncMock(return_value="emby-1")
    env["emby"].get_missing_episodes = AsyncMock(return_value=[])

    run(library_check_mod.library_check())

    dq = run(get_dq(db, dq_id))
    assert dq.status == "done"
    assert dq.node_error is None
    assert env["alist"].remove_calls == [(["ep.mkv"], "/quark/")]
    assert run(get_media(db, mid)).status == "tracking"


def test_mark_timeout_started_at_missing_is_conservative(db, env, monkeypatch):
    """node_started_at 缺失（旧数据）→ 保守不判定：即便 Emby 未命中也不置 failed，继续等待。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_library(db, started_at=_now() - timedelta(seconds=700)))
    env["emby"].find_emby_id = AsyncMock(return_value=None)

    # 手动把 node_started_at 置空，模拟旧数据（seed_library 的 `or _now()` 会兜底）
    async def clear_started_at():
        async with db() as s:
            dq = await s.get(DownloadQueue, dq_id)
            dq.node_started_at = None
            await s.commit()
    run(clear_started_at())

    run(library_check_mod.library_check())

    dq = run(get_dq(db, dq_id))
    assert dq.status == "library"  # 保守不判定，继续等待
    assert dq.node_error is None
    assert env["alist"].remove_calls == []
    assert run(get_media(db, mid)).status == "downloading"
