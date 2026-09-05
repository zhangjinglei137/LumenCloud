"""council 审查修复单测（P0-2 / P0-3a / P0-3b / P1-4 / P1-6）。

全部 mock 外部依赖（monkeypatch 模块内 async_session / notifier / alist / aria2 /
cloudsaver / nastools / trigger_transfer），数据库用独立 in-memory SQLite（StaticPool）。

覆盖：
- P0-2：process_transfer_queue 全局串行锁——两路并发 save 串行（max_active=1）
- P0-3a：recovery 回退 downloading 时同步终结 download_task + 尝试 aria2.remove
- P0-3b：_complete_download 双表失联（rowcount=0）→ 无完成通知 / 无 nastools 触发
- P1-4：_resolve_done_states 三处 delete 加状态条件（非 done 记录不被误删）
- P1-6：scan._trigger_transfer 改为 fire-and-forget（不阻塞、后台 task 持引用）
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
import app.tasks.transfer as transfer_mod
from app.models import DownloadTask, EpisodeState, Media, TransferQueue


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


async def seed_pending(db, *, episode="S01E01", file_name="ep.mkv"):
    """media + es(queued) + tq(pending)。返回 (mid, es_id, tq_id)。"""
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=42, status="tracking")
        s.add(media)
        await s.flush()
        mid = media.id
        es = EpisodeState(media_id=mid, episode=episode, state="queued",
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


async def seed_downloading(db, *, episode="S01E01", file_name="ep.mkv", gid="gid1",
                           updated_at=None):
    """media + es(downloading) + tq(downloading) + dl(downloading)。返回 (mid, es_id, tq_id, dl_id)。"""
    ts = updated_at or _now()
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=42, status="downloading")
        s.add(media)
        await s.flush()
        mid = media.id
        es = EpisodeState(media_id=mid, episode=episode, state="downloading",
                          file_name=file_name, file_size=1024, share_code="sc",
                          quark_path=f"/quark/{file_name}", aria2_gid=gid,
                          retry_count=0, updated_at=ts)
        s.add(es)
        await s.flush()
        tq = TransferQueue(media_id=mid, episode=episode, file_name=file_name,
                           file_size=1024, share_code="sc", status="downloading", updated_at=ts)
        s.add(tq)
        await s.flush()
        dl = DownloadTask(media_id=mid, transfer_id=tq.id, episode=episode,
                          file_name=file_name, aria2_gid=gid, status="downloading",
                          quark_path=f"/quark/{file_name}")
        s.add(dl)
        await s.flush()
        await s.commit()
        return mid, es.id, tq.id, dl.id


async def seed_mixed_done(db, *, episode="S01E01", file_name="ep.mkv", dl_status="complete"):
    """media + es(done) + tq(done) + dl(指定状态)。返回 (mid, es_id, tq_id, dl_id)。"""
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=42, status="tracking")
        s.add(media)
        await s.flush()
        mid = media.id
        es = EpisodeState(media_id=mid, episode=episode, state="done",
                          file_name=file_name, file_size=1024, share_code="sc", updated_at=_now())
        s.add(es)
        await s.flush()
        tq = TransferQueue(media_id=mid, episode=episode, file_name=file_name,
                           file_size=1024, share_code="sc", status="done", updated_at=_now())
        s.add(tq)
        await s.flush()
        dl = DownloadTask(media_id=mid, transfer_id=tq.id, episode=episode,
                          file_name=file_name, aria2_gid="g1", status=dl_status,
                          quark_path=f"/quark/{file_name}")
        s.add(dl)
        await s.flush()
        await s.commit()
        return mid, es.id, tq.id, dl.id


class FakeNotifier:
    def __init__(self):
        self.events = []

    async def notify(self, event):
        self.events.append(event)


class FakeAlist:
    def __init__(self):
        self.remove_calls = []

    async def remove(self, names, dir):
        self.remove_calls.append((list(names), dir))
        return {}

    async def get_link(self, path):
        return "http://alist.test/x"


class ConcurrentSaveCloudSaver:
    """记录 save 并发度（max_active > 1 即证明容量检查-转存未串行）。"""

    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.save_calls = []

    async def save(self, params):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.save_calls.append(dict(params))
        self.active -= 1
        return {"task_id": "t"}


class FakeAria2Client:
    def __init__(self):
        self.actives = []

    async def tell_active(self):
        return list(self.actives)

    async def tell_status(self, gid):
        return {"status": "active"}

    async def add_uri(self, uri, **kwargs):
        return "gid-new"

    async def remove(self, gid):
        pass


class FakeCapacityProvider:
    async def check(self, size):
        return True


def patch_transfer_env(monkeypatch, db, *, cloudsaver=None, notifier=None, nas=None):
    """替换 transfer 模块全部外部依赖；_spawn 置「跟踪不执行」；返回 (spawn_calls, notifier, nas)。"""
    fake_notifier = notifier or FakeNotifier()
    fake_nas = nas or types.SimpleNamespace(nastools_sync=AsyncMock(return_value=None))
    monkeypatch.setattr(transfer_mod, "async_session", db)
    monkeypatch.setattr(transfer_mod, "aria2", types.SimpleNamespace(client=FakeAria2Client()))
    monkeypatch.setattr(transfer_mod, "cloudsaver", cloudsaver or ConcurrentSaveCloudSaver())
    monkeypatch.setattr(transfer_mod, "alist", FakeAlist())
    monkeypatch.setattr(transfer_mod, "capacity",
                        types.SimpleNamespace(provider=FakeCapacityProvider()))
    monkeypatch.setattr(transfer_mod, "notifier", fake_notifier)
    monkeypatch.setattr(transfer_mod, "nastools_sync", fake_nas)
    spawn_calls: list = []
    monkeypatch.setattr(transfer_mod, "_spawn", lambda factory: spawn_calls.append(factory))
    return spawn_calls, fake_notifier, fake_nas


# ---------------------------------------------------------------------------
# P0-2：process_transfer_queue 全局串行锁
# ---------------------------------------------------------------------------

def test_process_transfer_queue_serialized_by_lock(db, monkeypatch):
    """两路并发消费不同 pending：经 _process_lock 串行 → save 无并发重叠。"""
    mid1, _, _ = run(seed_pending(db, episode="S01E01"))
    mid2, _, _ = run(seed_pending(db, episode="S01E02"))
    cloud = ConcurrentSaveCloudSaver()
    spawn, notifier, nas = patch_transfer_env(monkeypatch, db, cloudsaver=cloud)

    async def scenario():
        await asyncio.gather(
            transfer_mod.process_transfer_queue(),
            transfer_mod.process_transfer_queue(),
        )

    run(scenario())

    # P0-2：容量检查-转存两步在锁内串行，绝无双过检并发转存
    assert cloud.max_active == 1
    assert len(cloud.save_calls) == 2  # 两个任务都成功提交转存

    async def _count_downloading():
        async with db() as s:
            return (await s.execute(
                select(TransferQueue).where(TransferQueue.status == "downloading")
            )).scalars().all()

    assert len(run(_count_downloading())) == 2  # 双表 downloading


# ---------------------------------------------------------------------------
# P0-3a：recovery 回退 downloading 同步终结 download_task + aria2.remove
# ---------------------------------------------------------------------------

def test_recover_downloading_marks_dl_failed(db, monkeypatch):
    from app.tasks import recovery as recovery_mod

    monkeypatch.setattr(recovery_mod, "async_session", db)
    mid, es_id, tq_id, dl_id = run(seed_downloading(
        db, gid="gid-x", updated_at=_now() - timedelta(hours=3)))  # 超时（timeout=2h）
    fake_alist = FakeAlist()
    monkeypatch.setattr(recovery_mod, "alist", fake_alist)
    remove_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(recovery_mod, "aria2",
                        types.SimpleNamespace(client=types.SimpleNamespace(remove=remove_mock)))

    count = run(recovery_mod.recover_stale_tasks())

    assert count == 1
    dl = run(read_row(db, DownloadTask, dl_id))
    es = run(read_row(db, EpisodeState, es_id))
    tq = run(read_row(db, TransferQueue, tq_id))
    assert dl.status == "failed"  # P0-3a：中介 download_task 同步终结，防阶段 A 重复轮询
    assert es.state == "queued"
    assert tq.status == "pending"
    assert remove_mock.await_count == 1  # 尝试 aria2.remove(gid-x)
    assert fake_alist.remove_calls  # 夸克残留清理


def test_recover_aria2_remove_failure_does_not_block(db, monkeypatch):
    """aria2.remove 抛异常 → 仅 warning，不阻断回退。"""
    from app.tasks import recovery as recovery_mod

    monkeypatch.setattr(recovery_mod, "async_session", db)
    mid, es_id, tq_id, dl_id = run(seed_downloading(
        db, gid="gid-x", updated_at=_now() - timedelta(hours=3)))
    monkeypatch.setattr(recovery_mod, "alist", FakeAlist())

    async def boom(gid):
        raise RuntimeError("aria2 RPC 不可用")

    monkeypatch.setattr(recovery_mod, "aria2",
                        types.SimpleNamespace(client=types.SimpleNamespace(remove=boom)))

    count = run(recovery_mod.recover_stale_tasks())
    assert count == 1
    assert run(read_row(db, DownloadTask, dl_id)).status == "failed"  # 回退仍完成


# ---------------------------------------------------------------------------
# P0-3b：_complete_download 双表失联 → 无完成通知 / 无 nastools 触发
# ---------------------------------------------------------------------------

def test_complete_with_lost_double_table_no_notify(db, monkeypatch):
    spawn, notifier, nas = patch_transfer_env(monkeypatch, db)
    mid, es_id, tq_id, dl_id = run(seed_downloading(db))

    # 模拟 recovery 已回退：tq→pending、es→queued（双表失联）
    async def _break():
        async with db() as s:
            await s.execute(
                update(TransferQueue).where(TransferQueue.id == tq_id).values(status="pending")
            )
            await s.execute(
                update(EpisodeState).where(EpisodeState.id == es_id).values(state="queued")
            )
            await s.commit()
    run(_break())

    run(transfer_mod._complete_download(dl_id, mid, tq_id, "S01E01", "ep.mkv", "/quark/ep.mkv"))

    dl = run(read_row(db, DownloadTask, dl_id))
    assert dl.status == "complete"  # 仅置中介终态
    # P0-3b：无完成通知、无 nastools 触发、无 _spawn
    assert not any(e.event_type == "download_complete" for e in notifier.events)
    assert nas.nastools_sync.await_count == 0
    assert len(spawn) == 0

    # 正常路径对比：完整 downloading 三件套 → 通知 + nastools 触发
    mid2, es2_id, tq2_id, dl2_id = run(seed_downloading(db, episode="S01E02"))
    run(transfer_mod._complete_download(dl2_id, mid2, tq2_id, "S01E02", "ep2.mkv", "/quark/ep2.mkv"))
    assert any(e.event_type == "download_complete" for e in notifier.events)
    assert len(spawn) == 1  # nastools_sync 事件触发


# ---------------------------------------------------------------------------
# P1-4：_resolve_done_states 三处 delete 加状态条件
# ---------------------------------------------------------------------------

def test_done_resolution_delete_guards_non_done_records(db, monkeypatch):
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    # es/tq done + dl downloading（不一致：下载任务仍在进行，P1-4 防误删）
    mid, es_id, tq_id, dl_id = run(seed_mixed_done(db, episode="S01E01", dl_status="downloading"))
    # 另一条 downloading 的 es 记录（非 done，不应被处理）
    async def _add_other_es():
        async with db() as s:
            s.add(EpisodeState(media_id=mid, episode="S01E03", state="downloading",
                               file_name="ep3.mkv", file_size=1, share_code="sc", updated_at=_now()))
            await s.commit()
    run(_add_other_es())

    run(scan_mod._resolve_done_states(types.SimpleNamespace(id=mid), {"S01E02"}, False))

    # done 的 es/tq 被删除（Emby 已确认）
    assert run(read_row(db, EpisodeState, es_id)) is None
    assert run(read_row(db, TransferQueue, tq_id)) is None
    # dl 因 status='downloading' 不受 delete(WHERE status='complete') 影响 → 保留
    dl = run(read_row(db, DownloadTask, dl_id))
    assert dl is not None and dl.status == "downloading"
    # 非 done 的 es 记录完全不受影响
    async def _get_other():
        async with db() as s:
            return (await s.execute(
                select(EpisodeState).where(EpisodeState.episode == "S01E03")
            )).scalars().first()
    other = run(_get_other())
    assert other is not None and other.state == "downloading"


# ---------------------------------------------------------------------------
# P1-6：scan._trigger_transfer fire-and-forget
# ---------------------------------------------------------------------------

def test_scan_trigger_transfer_fire_and_forget(monkeypatch):
    from app.tasks import scan as scan_mod

    trigger = AsyncMock(return_value=None)
    monkeypatch.setattr(transfer_mod, "trigger_transfer", trigger)  # 延迟导入后 getattr 命中

    created: list = []
    orig_create_task = asyncio.create_task

    def _track_create_task(coro):
        t = orig_create_task(coro)
        created.append(t)
        return t

    monkeypatch.setattr(asyncio, "create_task", _track_create_task)

    async def scenario():
        await scan_mod._trigger_transfer()
        await asyncio.sleep(0)  # 让后台任务执行 trigger()

    run(scenario())

    # P1-6：fire-and-forget——后台 task 创建并持引用（_background），trigger 被调用，不阻塞
    assert len(created) == 1
    assert trigger.await_count == 1
    assert len(scan_mod._background) == 0  # done 回调已移除引用