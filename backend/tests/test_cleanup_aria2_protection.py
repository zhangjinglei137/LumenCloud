"""quark-cleanup-safety：release_space_cleanup_job 对 aria2 下载源的保护与 fail-safe。

场景：active 下载源保护 / waiting 下载源保护 / 无下载任务正常清理 / aria2 查询失败不删。
mock：cleanup_mod.alist（list_dir/remove）、cleanup_mod._aria2_client（list_source_basenames）。
"""
import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  注册全部 ORM 模型
from app.tasks import cleanup as cleanup_mod
from app.models import Media


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


class _FakeAlist:
    def __init__(self, names):
        self.names = names
        self.remove_calls = []

    async def list_dir(self, path):
        return [{"name": n, "is_dir": False, "size": 1} for n in self.names]

    async def remove(self, names, dir):
        self.remove_calls.append((list(names), dir))
        return {}


def _patch_env(monkeypatch, db, alist, aria2_sources=None, aria2_error=None):
    monkeypatch.setattr(cleanup_mod, "async_session", db)
    monkeypatch.setattr(cleanup_mod, "alist", alist)
    fake_aria2 = type("FakeAria2", (), {})()
    if aria2_error is not None:
        async def _boom():
            raise aria2_error
        fake_aria2.list_source_basenames = _boom
    else:
        async def _sources():
            return set(aria2_sources or [])
        fake_aria2.list_source_basenames = _sources
    monkeypatch.setattr(cleanup_mod, "_aria2_client", lambda: fake_aria2)


def test_cleanup_protects_active_download_source(db, monkeypatch):
    """aria2 正在下载（active）的源文件不进入删除列表，其余孤儿正常删除。"""
    async def _seed():
        async with db() as s:
            s.add(Media(title="测试剧", media_type="tv", tmdb_id=None, status="tracking"))
            await s.commit()
    run(_seed())
    alist = _FakeAlist(["下载中.mkv", "孤儿.mkv"])
    _patch_env(monkeypatch, db, alist, aria2_sources={"下载中.mkv"})
    run(cleanup_mod.release_space_cleanup_job())
    removed = {n for calls in alist.remove_calls for n in calls[0]}
    assert "下载中.mkv" not in removed
    assert "孤儿.mkv" in removed


def test_cleanup_protects_waiting_download_source(db, monkeypatch):
    """aria2 等待（waiting）队列中的源文件同样受保护。"""
    async def _seed():
        async with db() as s:
            s.add(Media(title="测试剧", media_type="tv", tmdb_id=None, status="tracking"))
            await s.commit()
    run(_seed())
    alist = _FakeAlist(["排队中.mkv", "孤儿.mkv"])
    _patch_env(monkeypatch, db, alist, aria2_sources={"排队中.mkv"})
    run(cleanup_mod.release_space_cleanup_job())
    removed = {n for calls in alist.remove_calls for n in calls[0]}
    assert "排队中.mkv" not in removed
    assert "孤儿.mkv" in removed


def test_cleanup_without_downloads_removes_orphans(db, monkeypatch):
    """aria2 无下载任务（保护集为空）→ 按既有孤儿判定正常清理。"""
    async def _seed():
        async with db() as s:
            s.add(Media(title="测试剧", media_type="tv", tmdb_id=None, status="tracking"))
            await s.commit()
    run(_seed())
    alist = _FakeAlist(["孤儿1.mkv", "孤儿2.mkv"])
    _patch_env(monkeypatch, db, alist, aria2_sources=set())
    run(cleanup_mod.release_space_cleanup_job())
    assert alist.remove_calls and len(alist.remove_calls[0][0]) == 2


def test_cleanup_failsafe_when_aria2_unavailable(db, monkeypatch):
    """aria2 查询失败 → 本轮不删除任何孤儿（fail-safe），记 task_run error。"""
    async def _seed():
        async with db() as s:
            s.add(Media(title="测试剧", media_type="tv", tmdb_id=None, status="tracking"))
            await s.commit()
    run(_seed())
    alist = _FakeAlist(["孤儿.mkv"])
    _patch_env(monkeypatch, db, alist, aria2_error=RuntimeError("aria2 RPC 不可用"))
    run(cleanup_mod.release_space_cleanup_job())
    assert alist.remove_calls == []  # 绝不删除