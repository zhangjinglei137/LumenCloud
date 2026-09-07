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
from app.models import DownloadTask, EpisodeState, Media, TaskRun, TransferQueue


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
# P0-1：recovery 五节点维度超时回退
# ---------------------------------------------------------------------------

async def seed_transferring(db, *, episode="S01E01", file_name="ep.mkv",
                            gid="gid-t1", node="transfer", updated_at=None):
    """media + es(node='transfer', state='transferring', 超时) + tq(transferring)。
    返回 (mid, es_id, tq_id)。"""
    ts = updated_at or _now()
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=None, status="downloading")
        s.add(media)
        await s.flush()
        mid = media.id
        es = EpisodeState(media_id=mid, episode=episode, state="transferring",
                          node=node, node_attempt=1,
                          node_started_at=_now() - timedelta(hours=3),
                          node_finished_at=None, node_error="上个节点诊断",
                          file_name=file_name, file_size=1024, share_code="sc",
                          quark_path=f"/quark/{file_name}", aria2_gid=gid,
                          retry_count=0, error="旧错误", updated_at=ts)
        s.add(es)
        await s.flush()
        tq = TransferQueue(media_id=mid, episode=episode, file_name=file_name,
                           file_size=1024, share_code="sc", stoken="st",
                           receive_code="提取码占位", fids='["f1"]', fid_tokens='["ft1"]',
                           folder_id="fd", status="transferring", updated_at=ts)
        s.add(tq)
        await s.flush()
        await s.commit()
        return mid, es.id, tq.id


async def seed_stalled_node(db, *, episode, node, file_name, state="downloading"):
    """media + es(指定 node, state=downloading 超时) + tq(done)。返回 (mid, es_id, tq_id)。"""
    ts = _now() - timedelta(hours=3)  # 超时（timeout=2h）
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=None, status="downloading")
        s.add(media)
        await s.flush()
        mid = media.id
        es = EpisodeState(media_id=mid, episode=episode, state=state,
                          node=node, node_attempt=2,
                          node_started_at=_now() - timedelta(hours=5),
                          node_finished_at=ts, node_error="进行中",
                          file_name=file_name, file_size=1024, share_code="sc",
                          quark_path=f"/quark/{file_name}", retry_count=1, updated_at=ts)
        s.add(es)
        await s.flush()
        tq = TransferQueue(media_id=mid, episode=episode, file_name=file_name,
                           file_size=1024, share_code="sc", stoken="st",
                           status="done", updated_at=ts)
        s.add(tq)
        await s.flush()
        await s.commit()
        return mid, es.id, tq.id


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
    """P0-1：transferring 超时回退 → 五节点字段复位（node='idle' 等）+ state='queued'，
    随后 process_transfer_queue 可正常取件走完转存链。"""
    monkeypatch.setattr(recovery_mod, "async_session", db)
    mid, es_id, tq_id = run(seed_transferring(
        db, updated_at=_now() - timedelta(hours=3)))
    fake_alist = _FakeRecoveryAlist()
    monkeypatch.setattr(recovery_mod, "alist", fake_alist)
    aria2 = _FakeAria2Remove()
    monkeypatch.setattr(recovery_mod, "aria2", types.SimpleNamespace(client=aria2))

    count = run(recovery_mod.recover_stale_tasks())

    assert count == 1
    es = run(read_row(db, EpisodeState, es_id))
    tq = run(read_row(db, TransferQueue, tq_id))
    # 回退语义：state=queued + 五节点字段全部复位（取件条件 node='idle' & state='queued'）
    assert es.state == "queued"
    assert es.node == "idle"
    assert es.node_attempt == 0
    assert es.node_error is None
    assert es.node_started_at is None
    assert es.node_finished_at is None
    assert es.retry_count == 1  # CAS 语义保留：本轮 +1
    assert tq.status == "pending"
    assert fake_alist.remove_calls  # 夸克残留清理照常
    assert aria2.removed == ["gid-t1"]  # B-3：CAS 成功才移除下行 aria2 任务

    # 回退后可被取件：完整跑一遍阶段 B，断言语义落 downloading
    env_fakes = _patch_transfer(monkeypatch, db)
    run(transfer_mod.process_transfer_queue())
    es = run(read_row(db, EpisodeState, es_id))
    tq = run(read_row(db, TransferQueue, tq_id))
    assert es.node == "downloading"
    assert es.state == "downloading"
    assert tq.status == "downloading"
    async def _count_dl():
        async with db() as s:
            return len((await s.execute(select(DownloadTask))).scalars().all())
    assert run(_count_dl()) == 1  # 中介 download_task 已建立
    assert env_fakes["aria2"].add_uri_calls  # 转存链真实走通


def test_recover_skips_scrape_and_library_nodes(db, monkeypatch):
    """P0-1：五节点下 scrape/library（state 双写为 downloading）不被 downloading
    超时回退——它们由独立机制（node_attempt 重试 / library_check_timeout_seconds）
    负责，recovery 按 node 维度排除，杜绝回退成 node≠idle+queued 的卡死态。"""
    monkeypatch.setattr(recovery_mod, "async_session", db)
    run(seed_stalled_node(db, episode="S01E01", node="scrape", file_name="a.mkv"))
    run(seed_stalled_node(db, episode="S01E02", node="library", file_name="b.mkv"))
    monkeypatch.setattr(recovery_mod, "alist", _FakeRecoveryAlist())
    monkeypatch.setattr(recovery_mod, "aria2",
                        types.SimpleNamespace(client=_FakeAria2Remove()))

    count = run(recovery_mod.recover_stale_tasks())

    assert count == 0  # scrape/library 均不参与回退
    async def _rows():
        async with db() as s:
            return (await s.execute(select(EpisodeState))).scalars().all()
    for es in run(_rows()):
        assert es.state == "downloading"      # state 双写保持 downloading（不置 queued）
        assert es.node in ("scrape", "library")
        assert es.retry_count == 1            # 未被回退自增
        assert es.node_attempt == 2           # 节点级进度未被触碰


# ---------------------------------------------------------------------------
# P0-2：cleanup 五节点维度引用判定（双保险）
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
    """P0-2：es 存在且 node='scrape'（tq=done）→ 文件受保护不删；
    es 存在但 node='failed' → 可删；es 不存在 → 可删。"""
    monkeypatch.setattr(cleanup_mod, "async_session", db)

    # DB：scrape 集（五节点刮削中，tq 已 done）+ failed 集 + ghost 无 es
    async def _seed():
        async with db() as s:
            for ep, node, fname in [("S01E01", "scrape", "scrape.mkv"),
                                    ("S01E02", "failed", "failed.mkv")]:
                media = Media(title="测试剧", media_type="tv", tmdb_id=None, status="tracking")
                s.add(media)
                await s.flush()
                es = EpisodeState(media_id=media.id, episode=ep, state="downloading",
                                  node=node, node_attempt=1, file_name=fname,
                                  file_size=1024, share_code="sc",
                                  quark_path=f"/quark/{fname}", retry_count=1,
                                  updated_at=_now())
                s.add(es)
                await s.flush()
                tq = TransferQueue(media_id=media.id, episode=ep, file_name=fname,
                                   file_size=1024, share_code="sc", stoken="st",
                                   status="done", updated_at=_now())
                s.add(tq)
                await s.flush()
            await s.commit()
    run(_seed())

    fake_alist = _FakeCleanupAlist(["scrape.mkv", "failed.mkv", "ghost.mkv"])
    monkeypatch.setattr(cleanup_mod, "alist", fake_alist)
    run(cleanup_mod.release_space_cleanup_job())

    assert fake_alist.remove_calls, "应当有孤儿文件待清理"
    removed_names = {n for calls in fake_alist.remove_calls for n in calls[0]}
    # scrape 集：tq=done 但仍被五节点刮削流程引用 → 绝不删（防数据丢失）
    assert "scrape.mkv" not in removed_names
    # failed 集（es 存在但 node='failed'）→ 可清理
    assert "failed.mkv" in removed_names
    # es 不存在 → 可清理
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
    """media + es(node='idle', state='queued') + tq(pending)。返回 (mid, es_id, tq_id)。"""
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=None, status="tracking")
        s.add(media)
        await s.flush()
        mid = media.id
        es = EpisodeState(media_id=mid, episode=episode, state="queued",
                          node="idle", node_attempt=0,
                          file_name=file_name, file_size=1024, share_code="sc",
                          retry_count=0, updated_at=_now())
        s.add(es)
        await s.flush()
        tq = TransferQueue(media_id=mid, episode=episode, file_name=file_name,
                           file_size=1024, share_code="sc", stoken="st",
                           receive_code="提取码占位", fids='["f1"]', fid_tokens='["ft1"]',
                           folder_id="fd", status="pending", updated_at=_now())
        s.add(tq)
        await s.flush()
        await s.commit()
        return mid, es.id, tq.id


def test_transfer_success_rowcount_conflict_removes_orphan_aria2(db, monkeypatch):
    """P2-4：转存链路期间 es 被并发方改动（recovery 回退）→ 步骤 6 双表 rowcount 校验
    不通过（es 命中 0）→ 事务整体回滚：不落 downloading、不插孤儿 DownloadTask、
    best-effort 清理已提交的 aria2 任务 + task_run(error) 告警。"""
    mid, es_id, tq_id = run(seed_pending(db))

    async def _concurrent_revert():
        # 模拟 recovery 超时回退：转存链路（save 期间）把 es 从 transfer/transferring
        # 并发改回 queued/idle
        async with db() as s:
            await s.execute(
                update(EpisodeState).where(EpisodeState.id == es_id)
                .values(state="queued", node="idle", retry_count=1, updated_at=_now())
            )
            await s.commit()

    fakes = _patch_transfer(monkeypatch, db, on_save=_concurrent_revert)

    run(transfer_mod.process_transfer_queue())

    # 不落 downloading：es 保持并发方回退的排队态，tq 回滚后停留 transferring
    es = run(read_row(db, EpisodeState, es_id))
    tq = run(read_row(db, TransferQueue, tq_id))
    assert es.state == "queued"
    assert es.node == "idle"
    assert es.retry_count == 1  # 并发方（recovery）的增量不被覆盖
    assert tq.status != "downloading"
    assert tq.status != "pending"  # 事务回滚：抢占的 transferring 保持原样

    # 不产生孤儿 DownloadTask
    async def _count_dl():
        async with db() as s:
            return len((await s.execute(select(DownloadTask))).scalars().all())
    assert run(_count_dl()) == 0

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
    """P2-4 不回归：无并发变动的正常成功路径仍完整落 downloading + dl + 不误清理。"""
    mid, es_id, tq_id = run(seed_pending(db))
    fakes = _patch_transfer(monkeypatch, db)  # on_save=None

    run(transfer_mod.process_transfer_queue())

    es = run(read_row(db, EpisodeState, es_id))
    tq = run(read_row(db, TransferQueue, tq_id))
    assert es.node == "downloading"
    assert es.state == "downloading"
    assert tq.status == "downloading"
    assert es.aria2_gid == "gid-1"
    assert es.quark_path == "/quark/ep.mkv"
    async def _rows():
        async with db() as s:
            return (await s.execute(select(DownloadTask))).scalars().all()
    dl_rows = run(_rows())
    assert len(dl_rows) == 1
    assert dl_rows[0].status == "downloading"
    assert fakes["aria2"].removed == []  # 正常路径绝不清aria2 任务
    assert fakes["spawn"]  # A-1 续跑触发保持