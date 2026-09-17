"""C4（fix-audit-issues）cancel_task 取消已完成任务不误标失败。

对应 delta spec pipeline-transfer「取消已下载完成任务不误标失败」：用户取消
downloading 任务时先向 aria2.tell_status 确认实际状态——
- aria2 已 complete（DB 轮询未及时推进的 downloading→scrape 窗口）→ 不标 failed，
  走 _complete_download 完成路径（downloading→scrape；不 remove aria2、不删夸克），
  审查 B5：完成态不再被取消丢失；
- aria2 未 complete → 维持现逻辑（CAS 置 failed + remove + 删夸克 + tq done）；
- aria2 查询失败 → fail-closed 保守：不确定完成态则不修改任务状态（409 中止取消），
  宁可保守不误删完成态。

测试风格沿用 test_queue.py：直接调用 app.routers.queue 的路由函数（绕过 HTTP 层，
注入 db session 与 fake admin），外部服务（aria2/alist）一律 monkeypatch；
_complete_download 用独立事务写测试库（transfer_mod.async_session → db），
_after_complete_promote 以 AsyncMock 替代（不真实触发刮削执行器）。
"""
import asyncio
import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  注册全部 ORM 模型
import app.routers.queue as queue_mod
import app.tasks.transfer as transfer_mod
from app.models import DownloadQueue, Media, TaskQueue


def run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _admin():
    return types.SimpleNamespace(role="admin")


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
def env(monkeypatch, db):
    """fake 外部服务 + 让 transfer._complete_download 落到测试库。"""
    fakes = {
        "aria2": types.SimpleNamespace(
            client=types.SimpleNamespace(
                remove=AsyncMock(return_value={}),
                tell_status=AsyncMock(return_value={}),
            )
        ),
        "alist": types.SimpleNamespace(remove=AsyncMock(return_value={})),
    }
    monkeypatch.setattr(queue_mod, "aria2", fakes["aria2"])
    monkeypatch.setattr(queue_mod, "alist", fakes["alist"])
    # _complete_download 自开独立事务（async_session）；_after_complete_promote
    # 为刮削触发（fire-and-forget），测试用 AsyncMock 替代不真实触发
    monkeypatch.setattr(transfer_mod, "async_session", db)
    monkeypatch.setattr(transfer_mod, "_after_complete_promote", AsyncMock(return_value=None))
    return fakes


# ---------------------------------------------------------------------------
# 种子数据 / 读取
# ---------------------------------------------------------------------------

async def seed_media(db, *, status="tracking"):
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=None, status=status)
        s.add(media)
        await s.flush()
        await s.commit()
        return media.id


async def seed_dq(db, mid, *, episode="S01E01", status="downloading",
                  aria2_gid="gid-1", quark_path="/quark/ep.mkv"):
    async with db() as s:
        dq = DownloadQueue(
            media_id=mid, episode=episode, file_name="ep.mkv", file_size=1024,
            share_code="ScAaBbCcDdEe", stoken="st", receive_code="rc", fids="[]",
            fid_tokens="[]", folder_id="fd", status=status, retry_count=0,
            node_attempt=0, quark_path=quark_path, aria2_gid=aria2_gid,
            node_started_at=_now(), enqueued_at=_now(), updated_at=_now(),
        )
        s.add(dq)
        await s.flush()
        await s.commit()
        return dq.id


async def seed_tq(db, mid, *, episode="S01E01", status="ready"):
    async with db() as s:
        tq = TaskQueue(
            media_id=mid, episode=episode, file_name="ep.mkv", file_size=1024,
            share_code="TqXxYyZz1234", pwd_id="pwd", stoken="st", receive_code="rc",
            fids="[]", fid_tokens="[]", folder_id="fd", status=status,
            probe_attempt=1, created_at=_now(), updated_at=_now(),
        )
        s.add(tq)
        await s.flush()
        await s.commit()
        return tq.id


async def read_dq(db, dq_id):
    async with db() as s:
        return await s.get(DownloadQueue, dq_id)


async def read_tq(db, tq_id):
    async with db() as s:
        return await s.get(TaskQueue, tq_id)


async def read_media(db, mid):
    async with db() as s:
        return await s.get(Media, mid)


# ---------------------------------------------------------------------------
# C4 场景 1：aria2 已 complete → 走完成路径，不标 failed
# ---------------------------------------------------------------------------

def test_cancel_completed_aria2_promotes_not_failed(db, env):
    """aria2 已 complete（DB 回写滞后）→ 取消不标 failed，走 _complete_download。

    断言：返回 {"ok": True}；dq → scrape（非 failed，无 error 标记）；file_size
    回填 totalLength、size_estimated 清标记；aria2.remove / alist.remove 均不调用
    （不 remove、G6 不删夸克）；TaskQueue 同步 done；media 无在途 → 回落 tracking。
    """
    mid = run(seed_media(db, status="downloading"))
    tq_id = run(seed_tq(db, mid))
    dq_id = run(seed_dq(db, mid, status="downloading"))
    env["aria2"].client.tell_status = AsyncMock(
        return_value={"status": "complete", "totalLength": 2048}
    )

    async def _cancel():
        async with db() as s:
            return await queue_mod.cancel_task(task_id=dq_id, admin=_admin(), session=s)
    assert run(_cancel()) == {"ok": True}

    dq = run(read_dq(db, dq_id))
    assert dq.status == "scrape"  # 完成路径：downloading → scrape
    assert dq.error is None and dq.node_error is None  # 未标 failed
    assert dq.file_size == 2048 and dq.size_estimated is False
    env["aria2"].client.remove.assert_not_awaited()  # 不 remove aria2（完成路径）
    env["alist"].remove.assert_not_awaited()  # G6：完成路径不删夸克
    assert run(read_tq(db, tq_id)).status == "done"
    # scrape 属 _ACTIVE_STATUSES（在途），后续 lane 继续推进 → media 保持 downloading
    # （仅取消/失败等终态才会由 _sync_media_status 回落 tracking）
    assert run(read_media(db, mid)).status == "downloading"


# ---------------------------------------------------------------------------
# C4 场景 2：aria2 未 complete → 维持现逻辑正常取消
# ---------------------------------------------------------------------------

def test_cancel_incomplete_aria2_marks_failed(db, env):
    """aria2 未 complete（active）→ 维持现逻辑：标 failed + remove + 删夸克 + tq done。"""
    mid = run(seed_media(db, status="downloading"))
    tq_id = run(seed_tq(db, mid))
    dq_id = run(seed_dq(db, mid, status="downloading"))
    env["aria2"].client.tell_status = AsyncMock(return_value={"status": "active"})

    async def _cancel():
        async with db() as s:
            return await queue_mod.cancel_task(task_id=dq_id, admin=_admin(), session=s)
    assert run(_cancel()) == {"ok": True}

    dq = run(read_dq(db, dq_id))
    assert dq.status == "failed" and dq.error == "人工取消"
    env["aria2"].client.remove.assert_awaited_once_with("gid-1")
    env["alist"].remove.assert_awaited_once_with(["ep.mkv"], "/quark/")
    assert run(read_tq(db, tq_id)).status == "done"


# ---------------------------------------------------------------------------
# C4 场景 3：aria2 查询失败 → fail-closed 保守中止取消
# ---------------------------------------------------------------------------

def test_cancel_tell_status_failure_fail_closed(db, env):
    """aria2 查询失败 → fail-closed 中止取消（409），任务状态不被修改。

    不确定完成态时不得把可能已完成的下载误标 failed（不误删完成态）；返回 409
    让用户刷新重试，任务保持 downloading、不 remove、不删夸克。
    """
    mid = run(seed_media(db, status="downloading"))
    dq_id = run(seed_dq(db, mid, status="downloading"))
    env["aria2"].client.tell_status = AsyncMock(side_effect=RuntimeError("aria2 挂"))

    async def _cancel():
        async with db() as s:
            return await queue_mod.cancel_task(task_id=dq_id, admin=_admin(), session=s)
    with pytest.raises(Exception) as ei:
        run(_cancel())
    assert ei.value.status_code == 409

    dq = run(read_dq(db, dq_id))
    assert dq.status == "downloading"  # 未被标 failed（不误删完成态）
    env["aria2"].client.remove.assert_not_awaited()
    env["alist"].remove.assert_not_awaited()
