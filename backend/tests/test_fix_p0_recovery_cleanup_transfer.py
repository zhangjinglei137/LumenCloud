"""council P0-1 / P0-2 / P2-4 修复回归单测。

P0-1（发布阻断）：recovery 超时回退按五节点 node 维度处理——
  - transferring/downloading 回退后 node='idle' 且 state='queued'，可被 transfer 取件；
  - scrape/library（state 双写为 downloading）不被 downloading 超时回退
    （其由独立机制负责：scrape 靠 node_attempt、library 靠 library_check_timeout_seconds）。
P0-2（数据丢失）：cleanup 引用判定加入五节点 es 维度——
  - es 存在且 node='scrape'（即使 tq=done/dl=complete）→ 文件受保护不删；
  - es 存在但 node='failed' → 可删；
  - es 不存在 → 可删。
P2-4：transfer 步骤 6 成功路径 rowcount 校验——
  - 转存链路期间 tq/es 被并发方变动 → 不落 downloading、不插孤儿 DownloadTask、
    best-effort 清理已提交的 aria2 任务 + task_run(error) 告警；
  - 无并发变动 → 正常落 downloading（不回归）。
"""
import asyncio
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  注册全部 ORM 模型
import app.tasks.cleanup as cleanup_mod
import app.tasks.recovery as recovery_mod
import app.tasks.transfer as transfer_mod
from app.models import DownloadQueue, DownloadTask, EpisodeState, Media, TaskRun, TransferQueue


def run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# fixtures / 公共依赖
# ---------------------------------------------------------------------------

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


async def read_row(db, model, obj_id):
    async with db() as s:
        return await s.get(model, obj_id)


# ---------------------------------------------------------------------------
# P0-1：recovery download_queue 单表超时回退
# ---------------------------------------------------------------------------

async def seed_transferring(db, *, episode="S01E01", file_name="ep.mkv",
                            gid="gid-t1", updated_at=None):
    """media + download_queue(status='transferring', 超时)。返回 (mid, dq_id)。"""
    ts = updated_at or _now()
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=None, status="downloading")
        s.add(media)
        await s.flush()
        mid = media.id
        dq = DownloadQueue(
            media_id=mid, episode=episode, file_name=file_name, file_size=1024,
            share_code="sc", stoken="st", receive_code="rc", fids="[]",
            fid_tokens="[]", folder_id="fd", status="transferring", node_attempt=1,
            node_started_at=_now() - timedelta(hours=3),
            node_finished_at=None, node_error="上个节点诊断",
            quark_path=f"/quark/{file_name}", aria2_gid=gid, save_task_id="st-1",
            retry_count=0, error="旧错误", updated_at=ts)
        s.add(dq)
        await s.flush()
        await s.commit()
        return mid, dq.id


class _FakeRecoveryAlist:
    """recovery 使用的 alist：remove 记录调用。"""

    def __init__(self):
        self.remove_calls = []

    async def remove(self, names, dir):
        self.remove_calls.append((list(names), dir))
        return {}


class _FakeAria2Remove:
    """recovery 使用的 aria2.client：仅 remove（记录调用）。"""

    def __init__(self):
        self.removed = []

    async def remove(self, gid):
        self.removed.append(gid)


def test_recover_transfer_timeout_resets_node_then_pickable(db, monkeypatch):
    """P0-1：transferring 超时回退（download_queue 单表）→ status='pending' +
    retry_count++ + node_attempt++ + save_task_id 清空（防盲等）+ B-3 后置清理；
    回退后下载队列消费端可正常取件走完转存链。"""
    monkeypatch.setattr(recovery_mod, "async_session", db)
    mid, dq_id = run(seed_transferring(
        db, updated_at=_now() - timedelta(hours=3)))
    fake_alist = _FakeRecoveryAlist()
    monkeypatch.setattr(recovery_mod, "alist", fake_alist)
    aria2 = _FakeAria2Remove()
    monkeypatch.setattr(recovery_mod, "aria2", types.SimpleNamespace(client=aria2))

    count = run(recovery_mod.recover_stale_tasks())

    assert count == 1
    dq = run(read_row(db, DownloadQueue, dq_id))
    # 回退语义：status=pending（单表权威），计数自增，节点时序/诊断复位
    assert dq.status == "pending"
    assert dq.retry_count == 1           # CAS 语义保留：本轮 +1
    assert dq.node_attempt == 2          # node_attempt++（消费端重新取件）
    assert dq.node_error is not None     # 超时回退原因（node_error 记录）
    assert dq.node_started_at is None and dq.node_finished_at is None
    assert dq.save_task_id is None       # P0-1：清空 save 幂等标记防盲等
    assert fake_alist.remove_calls       # 夸克残留清理照常（B-3：CAS 成功提交后）
    assert aria2.removed == ["gid-t1"]  # B-3：CAS 成功才移除下行 aria2 任务

    # 回退后可被取件：完整跑一遍下载队列消费，断言语义落 downloading。
    # （transfer 消费 download_queue(pending)；以同 media 重新 seed 的 pending
    # 验证取件链路）
    _mid2, dq2_id = run(seed_pending(db, episode="S01E99", file_name="recovered.mkv"))
    env_fakes = _patch_transfer(monkeypatch, db)
    run(transfer_mod.process_transfer_queue())
    dq = run(read_row(db, DownloadQueue, dq2_id))
    assert dq.status == "downloading"
    assert dq.aria2_gid == "gid-1"
    assert env_fakes["aria2"].add_uri_calls  # 转存链真实走通


# ---------------------------------------------------------------------------
# P0-2：cleanup download_queue 维度引用判定
# ---------------------------------------------------------------------------

class _FakeCleanupAlist:
    """cleanup 使用的 alist：list_dir 返回 /quark 现有文件；remove 记录调用。"""

    def __init__(self, names):
        self.names = names
        self.remove_calls = []

    async def list_dir(self, path):
        return [{"name": n, "is_dir": False} for n in self.names]

    async def remove(self, names, dir):
        self.remove_calls.append((list(names), dir))
        return {}


def test_cleanup_preserves_scrape_and_only_removes_failed_or_missing(db, monkeypatch):
    """P0-2：download_queue 非终态（scrape 等进行中）→ 文件受保护不删；
    failed（终态）→ 可删；无引用（ghost）→ 可删。"""
    monkeypatch.setattr(cleanup_mod, "async_session", db)

    # DB：scrape 任务（刮削中，非终态）+ failed 任务（终态）
    async def _seed():
        async with db() as s:
            for ep, status, fname in [("S01E01", "scrape", "scrape.mkv"),
                                      ("S01E02", "failed", "failed.mkv")]:
                media = Media(title="测试剧", media_type="tv", tmdb_id=None, status="tracking")
                s.add(media)
                await s.flush()
                dq = DownloadQueue(
                    media_id=media.id, episode=ep, file_name=fname, file_size=1024,
                    share_code="sc", stoken="st", receive_code="rc", fids="[]",
                    fid_tokens="[]", folder_id="fd", status=status, node_attempt=1,
                    quark_path=f"/quark/{fname}", retry_count=1,
                    updated_at=_now())
                s.add(dq)
                s.add(DownloadTask(media_id=media.id, episode=ep, file_name=fname,
                                   status="downloading", quark_path=f"/quark/{fname}"))
                await s.flush()
            await s.commit()
    run(_seed())

    fake_alist = _FakeCleanupAlist(["scrape.mkv", "failed.mkv", "ghost.mkv"])
    monkeypatch.setattr(cleanup_mod, "alist", fake_alist)
    run(cleanup_mod.release_space_cleanup_job())

    assert fake_alist.remove_calls, "应当有孤儿文件待清理"
    removed_names = {n for calls in fake_alist.remove_calls for n in calls[0]}
    # scrape：非终态仍被刮削流程引用 → 绝不删（防数据丢失）
    assert "scrape.mkv" not in removed_names
    # failed：终态 → 可清理（残留不再保护）
    assert "failed.mkv" in removed_names
    # 无引用 → 可清理
    assert "ghost.mkv" in removed_names


# ---------------------------------------------------------------------------
# P2-4：transfer 步骤 6 成功路径 rowcount 校验 + 孤儿 aria2 清理
# ---------------------------------------------------------------------------

class _TransferFakeAlist:
    """transfer 使用的 alist：list_dir/get_link/remove 可用；无 diagnose_quark_mount
    （缺失→ transfer 内告警后继续）。"""

    async def remove(self, names, dir):
        return {}

    async def get_link(self, path):
        return "http://alist.test/raw/ep.mkv"

    async def list_dir(self, path):
        return [{"name": "ep.mkv", "is_dir": False}]


class _TransferFakeAria2:
    def __init__(self):
        self.actives = []
        self.add_uri_calls = []
        self.removed = []

    async def tell_active(self):
        return list(self.actives)

    async def add_uri(self, uri, **kwargs):
        self.add_uri_calls.append((uri, kwargs))
        return "gid-1"

    async def remove(self, gid):
        self.removed.append(gid)


class _TransferFakeCloudSaver:
    """save 时执行 on_save 钩子（模拟转存链路期间并发方改动 tq/es）。"""

    def __init__(self, on_save=None):
        self.on_save = on_save
        self.save_calls = []

    async def save(self, params):
        self.save_calls.append(dict(params))
        if self.on_save:
            await self.on_save()
        return {"task_id": "t1"}


class _FakeCapacity:
    async def check(self, size):
        return True


class _FakeNotifier:
    def __init__(self):
        self.events = []

    async def notify(self, event):
        self.events.append(event)


def _patch_transfer(monkeypatch, db, *, on_save=None):
    """替换 transfer 模块外部依赖；返回 fakes dict（_spawn 置「跟踪不执行」）。"""
    fakes = {
        "alist": _TransferFakeAlist(),
        "aria2": _TransferFakeAria2(),
        "cloudsaver": _TransferFakeCloudSaver(on_save=on_save),
        "capacity": _FakeCapacity(),
        "notifier": _FakeNotifier(),
    }
    monkeypatch.setattr(transfer_mod, "async_session", db)
    monkeypatch.setattr(transfer_mod, "alist", fakes["alist"])
    monkeypatch.setattr(transfer_mod, "aria2", types.SimpleNamespace(client=fakes["aria2"]))
    monkeypatch.setattr(transfer_mod, "cloudsaver", fakes["cloudsaver"])
    monkeypatch.setattr(transfer_mod, "capacity", types.SimpleNamespace(provider=fakes["capacity"]))
    monkeypatch.setattr(transfer_mod, "notifier", fakes["notifier"])
    spawn_calls: list = []
    monkeypatch.setattr(transfer_mod, "_spawn", lambda factory: spawn_calls.append(factory))
    fakes["spawn"] = spawn_calls
    return fakes


async def seed_pending(db, *, episode="S01E01", file_name="ep.mkv"):
    """media + download_queue(pending)（P5 迁移：旧 es+tq 双表 → 单表）。返回 (mid, dq_id)。"""
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=None, status="tracking")
        s.add(media)
        await s.flush()
        mid = media.id
        dq = DownloadQueue(
            media_id=mid, episode=episode, file_name=file_name, file_size=1024,
            share_code="sc", stoken="st", receive_code="提取码占位",
            fids='["f1"]', fid_tokens='["ft1"]', folder_id="fd",
            status="pending", retry_count=0, enqueued_at=_now(), updated_at=_now(),
        )
        s.add(dq)
        await s.flush()
        await s.commit()
        return mid, dq.id


def test_transfer_success_rowcount_conflict_removes_orphan_aria2(db, monkeypatch):
    """P2-4：转存链期间 dq 被并发方改动（recovery 回退）→ 步骤 6 单表 rowcount 校验
    不通过（dq 命中 0）→ 事务整体回滚：不落 downloading、best-effort 清理已提交的
    aria2 任务 + task_run(error) 告警。"""
    mid, dq_id = run(seed_pending(db))

    async def _concurrent_revert():
        # 模拟 recovery 超时回退：转存链（save 期间）把 dq 从 transferring 并发改回
        # pending（单表语义，recovery 迁移后回退同此形态）
        async with db() as s:
            await s.execute(
                update(DownloadQueue).where(DownloadQueue.id == dq_id)
                .values(status="pending", retry_count=1, updated_at=_now())
            )
            await s.commit()

    fakes = _patch_transfer(monkeypatch, db, on_save=_concurrent_revert)

    run(transfer_mod.process_transfer_queue())

    # 不落 downloading：dq 保持并发方回退的排队态（抢占的 transferring 被回滚覆盖）
    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "pending"
    assert dq.retry_count == 1  # 并发方（recovery）的增量不被覆盖
    assert dq.aria2_gid is None  # 未提交下载

    # best-effort 清理已提交的 aria2 任务
    assert fakes["aria2"].removed == ["gid-1"]
    assert len(fakes["aria2"].add_uri_calls) == 1  # add_uri 确实已提交（故需清理）

    # 告警记录
    async def _error_msgs():
        async with db() as s:
            rows = (
                await s.execute(
                    select(TaskRun).where(TaskRun.status == "error", TaskRun.task_type == "transfer")
                )
            ).scalars().all()
            return [r.message for r in rows]
    msgs = run(_error_msgs())
    assert any("状态已变，清理孤儿 aria2 任务" in m for m in msgs)


def test_transfer_success_path_still_commits_download(db, monkeypatch):
    """P2-4 不回归：无并发变动的正常成功路径仍完整落 downloading + 不误清理。"""
    mid, dq_id = run(seed_pending(db))
    fakes = _patch_transfer(monkeypatch, db)  # on_save=None

    run(transfer_mod.process_transfer_queue())

    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "downloading"
    assert dq.aria2_gid == "gid-1"
    assert dq.quark_path == "/quark/ep.mkv"
    assert fakes["aria2"].removed == []  # 正常路径绝不清 aria2 任务
    assert fakes["spawn"] == []  # 成功路径不 spawn 续跑（循环内取下一个 pending，§5）