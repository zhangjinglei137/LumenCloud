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
from app.models import DownloadQueue, Media


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
    """media + download_queue(pending)。返回 (mid, dq_id)。"""
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=None, status="tracking")
        s.add(media)
        await s.flush()
        mid = media.id
        dq = DownloadQueue(
            media_id=mid, episode=episode, file_name=file_name, file_size=1024,
            share_code="sc", stoken="st", receive_code="rc", fids='["f1"]',
            fid_tokens='["ft1"]', folder_id="fd", status="pending",
            retry_count=0, enqueued_at=_now(), updated_at=_now(),
        )
        s.add(dq)
        await s.flush()
        await s.commit()
        return mid, dq.id


async def seed_downloading(db, *, episode="S01E01", file_name="ep.mkv", gid="gid1",
                           updated_at=None):
    """media + download_queue(downloading)。返回 (mid, dq_id)。"""
    ts = updated_at or _now()
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=None, status="downloading")
        s.add(media)
        await s.flush()
        mid = media.id
        dq = DownloadQueue(
            media_id=mid, episode=episode, file_name=file_name, file_size=1024,
            share_code="sc", stoken="st", receive_code="rc", fids='["f1"]',
            fid_tokens='["ft1"]', folder_id="fd", status="downloading",
            aria2_gid=gid, quark_path=f"/quark/{file_name}",
            retry_count=0, node_attempt=0, updated_at=ts,
        )
        s.add(dq)
        await s.flush()
        await s.commit()
        return mid, dq.id


async def seed_mixed_done(db, *, episode="S01E01", file_name="ep.mkv", dl_status="complete"):
    """media + download_queue(done)。返回 (mid, dq_id)。

    影视下载两队列重设计后 done 防重权威源 = download_queue（dl_status 并入 dq，
    仅保留签名兼容；resolve 按 status='done' 判定，与旧 download_task 状态无关）。
    """
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=None, status="tracking")
        s.add(media)
        await s.flush()
        mid = media.id
        dq = DownloadQueue(
            media_id=mid, episode=episode, file_name=file_name, file_size=1024,
            share_code="sc", stoken="st", receive_code="rc", fids="[]",
            fid_tokens="[]", folder_id="fd", status="done",
            aria2_gid="g1", quark_path=f"/quark/{file_name}",
            retry_count=0, updated_at=_now(),
        )
        s.add(dq)
        await s.flush()
        await s.commit()
        return mid, dq.id


async def seed_dq_recover(db, *, episode="S01E01", file_name="ep.mkv", gid="gid-x",
                          status="downloading", updated_at=None):
    """media + download_queue(指定 status)。返回 (mid, dq_id)。

    影视下载两队列重设计后 recovery 操作对象为 download_queue 单表（§4.2）。
    """
    ts = updated_at or _now()
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=None, status="downloading")
        s.add(media)
        await s.flush()
        mid = media.id
        dq = DownloadQueue(
            media_id=mid, episode=episode, file_name=file_name, file_size=1024,
            share_code="sc", stoken="st", receive_code="rc", fids="[]",
            fid_tokens="[]", folder_id="fd", status=status, node_attempt=0,
            quark_path=f"/quark/{file_name}", aria2_gid=gid,
            retry_count=0, updated_at=ts)
        s.add(dq)
        await s.flush()
        await s.commit()
        return mid, dq.id


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
    # L3：下载完成事件改触发刮削执行器（_spawn 置「跟踪不执行」，此处无需 patch
    # scrape_runner 内部依赖）；旧 nastools_sync patch 已移除（transfer 不再直接调用）
    spawn_calls: list = []
    monkeypatch.setattr(transfer_mod, "_spawn", lambda factory: spawn_calls.append(factory))
    return spawn_calls, fake_notifier, fake_nas


# ---------------------------------------------------------------------------
# P0-2：process_transfer_queue 全局串行锁
# ---------------------------------------------------------------------------

def test_process_transfer_queue_serialized_by_lock(db, monkeypatch):
    """两路并发消费不同 pending：经 _admission_lock 串行 → save 无并发重叠。"""
    mid1, _ = run(seed_pending(db, episode="S01E01"))
    mid2, _ = run(seed_pending(db, episode="S01E02"))
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
                select(DownloadQueue).where(DownloadQueue.status == "downloading")
            )).scalars().all()

    assert len(run(_count_downloading())) == 2  # 单表 downloading


# ---------------------------------------------------------------------------
# P0-3a：recovery 回退 downloading 同步终结 download_task + aria2.remove
# ---------------------------------------------------------------------------

def test_recover_downloading_marks_dl_failed(db, monkeypatch):
    """§4.2：downloading 超时 → 回退 pending（单表）+ aria2.remove + 夸克残留清理。"""
    from app.tasks import recovery as recovery_mod

    monkeypatch.setattr(recovery_mod, "async_session", db)
    mid, dq_id = run(seed_dq_recover(
        db, gid="gid-x", updated_at=_now() - timedelta(hours=3)))  # 超时（timeout=2h）
    fake_alist = FakeAlist()
    monkeypatch.setattr(recovery_mod, "alist", fake_alist)
    remove_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(recovery_mod, "aria2",
                        types.SimpleNamespace(client=types.SimpleNamespace(remove=remove_mock)))

    count = run(recovery_mod.recover_stale_tasks())

    assert count == 1
    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "pending"          # 单表回退（§4.2：回退只置 pending + 计数）
    assert dq.retry_count == 1             # CAS 语义：本轮 +1
    assert remove_mock.await_count == 1    # aria2.remove(gid-x)
    assert fake_alist.remove_calls         # 夸克残留清理（B-3：CAS 成功提交后）


def test_recover_aria2_remove_failure_does_not_block(db, monkeypatch):
    """aria2.remove 抛异常 → 仅 warning，不阻断回退。"""
    from app.tasks import recovery as recovery_mod

    monkeypatch.setattr(recovery_mod, "async_session", db)
    mid, dq_id = run(seed_dq_recover(
        db, gid="gid-x", updated_at=_now() - timedelta(hours=3)))
    monkeypatch.setattr(recovery_mod, "alist", FakeAlist())

    async def boom(gid):
        raise RuntimeError("aria2 RPC 不可用")

    monkeypatch.setattr(recovery_mod, "aria2",
                        types.SimpleNamespace(client=types.SimpleNamespace(remove=boom)))

    count = run(recovery_mod.recover_stale_tasks())
    assert count == 1
    assert run(read_row(db, DownloadQueue, dq_id)).status == "pending"  # 回退仍完成


# ---------------------------------------------------------------------------
# P0-3b：_complete_download 双表失联 → 无完成通知 / 无 nastools 触发
# ---------------------------------------------------------------------------

def test_complete_with_lost_double_table_no_notify(db, monkeypatch):
    """P0-3b：下载完成推进 rowcount=0（recovery 已回退 dq 为 pending）→ 无完成通知。

    影视下载两队列重设计后 _complete_download 单表条件更新 downloading→scrape：
    回退/并发方已推进时命中 0 行 → 幂等返回，不通知、不触发刮削执行器。
    """
    spawn, notifier, nas = patch_transfer_env(monkeypatch, db)
    mid, dq_id = run(seed_downloading(db))

    # 模拟 recovery 已回退：dq downloading→pending（行级条件更新不命中）
    async def _break():
        async with db() as s:
            await s.execute(
                update(DownloadQueue).where(DownloadQueue.id == dq_id).values(status="pending")
            )
            await s.commit()
    run(_break())

    # 新签名：retry_snapshot / node_attempt_snapshot（CAS 快照），此处仅为习惯性传入
    run(transfer_mod._complete_download(dq_id, mid, "S01E01", "ep.mkv", "/quark/ep.mkv", 0, 0))

    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "pending"  # 回退态保持，未被推进
    # P0-3b：无完成通知、无 nastools 触发、无 _spawn
    assert not any(e.event_type == "download_complete" for e in notifier.events)
    assert nas.nastools_sync.await_count == 0
    assert len(spawn) == 0

    # 正常路径对比：完整 downloading 行 → 推进 scrape + 通知 + 触发刮削执行器
    mid2, dq2_id = run(seed_downloading(db, episode="S01E02"))
    run(transfer_mod._complete_download(dq2_id, mid2, "S01E02", "ep2.mkv", "/quark/ep2.mkv", 0, 0))
    dq2 = run(read_row(db, DownloadQueue, dq2_id))
    assert dq2.status == "scrape"
    assert any(e.event_type == "download_complete" for e in notifier.events)
    assert len(spawn) == 1  # _after_complete_promote 仅触发刮削执行器（不删夸克）


# ---------------------------------------------------------------------------
# P1-4：_resolve_done_states 三处 delete 加状态条件
# ---------------------------------------------------------------------------

def test_done_resolution_delete_guards_non_done_records(db, monkeypatch):
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    # dq done + 另一条 dq downloading（并行下载仍进行，P1-4 防误删）
    mid, dq_id = run(seed_mixed_done(db, episode="S01E01", dl_status="downloading"))
    # 另一条 downloading 的 dq 记录（非 done，不应被处理）
    async def _add_other_dq():
        async with db() as s:
            s.add(DownloadQueue(media_id=mid, episode="S01E03", status="downloading",
                                file_name="ep3.mkv", file_size=1, share_code="sc",
                                stoken="st", receive_code="rc", fids="[]",
                                fid_tokens="[]", folder_id="fd",
                                retry_count=0, node_attempt=0, updated_at=_now()))
            await s.commit()
    run(_add_other_dq())

    run(scan_mod._resolve_done_states(types.SimpleNamespace(id=mid), {"S01E02"}, False))

    # done 的 dq 被删除（Emby 已确认 S01E01 入库，防重解除）
    assert run(read_row(db, DownloadQueue, dq_id)) is None
    # 非 done 的 dq 记录完全不受影响（P1-4：delete WHERE status='done' 只删 done 行）
    async def _get_other():
        async with db() as s:
            return (await s.execute(
                select(DownloadQueue).where(DownloadQueue.episode == "S01E03")
            )).scalars().first()
    other = run(_get_other())
    assert other is not None and other.status == "downloading"


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


# ---------------------------------------------------------------------------
# P0-3：recovery 回退 CAS 化——不覆盖 transfer 并发已推进的 retry_count
# ---------------------------------------------------------------------------

def test_recover_cas_conflict_skips_override(db, monkeypatch):
    """CAS 并发协议（§4.2）：transfer 并发已推进 retry_count → recovery CAS 冲突：
    不覆盖（dq 保持 downloading / 新 retry_count 被保留），返回 0。"""
    from app.tasks import recovery as recovery_mod

    monkeypatch.setattr(recovery_mod, "async_session", db)
    mid, dq_id = run(seed_dq_recover(
        db, gid="gid-x", updated_at=_now() - timedelta(hours=3)))  # 超时（timeout=2h）
    remove_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(recovery_mod, "aria2",
                        types.SimpleNamespace(client=types.SimpleNamespace(remove=remove_mock)))

    # 模拟 transfer 的 CAS 写入抢在 recovery 阶段③之前推进 retry_count（0→1）
    async def _bump():
        async with db() as s:
            await s.execute(
                update(DownloadQueue).where(DownloadQueue.id == dq_id)
                .values(retry_count=1, updated_at=_now())
            )
            await s.commit()
    run(_bump())

    count = run(recovery_mod.recover_stale_tasks())

    assert count == 0  # CAS 全部冲突 → 无实际回退
    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "downloading"   # 未被 recovery 覆盖回 pending
    assert dq.retry_count == 1          # 并发增量被保留，recovery 不再 +1（防增量丢失）
    # B-3：CAS 全部冲突 → 无实际回退行 → aria2.remove 绝不被调用
    # （防误杀仍被 transfer 并发推进的下行 aria2 任务）
    assert remove_mock.await_count == 0


def test_recover_partial_cas_conflict(db, monkeypatch):
    """两行一行冲突一行正常：冲突行跳过不覆盖，正常行仍完整回退（count=1）。"""
    from app.tasks import recovery as recovery_mod

    monkeypatch.setattr(recovery_mod, "async_session", db)
    ts = _now() - timedelta(hours=3)
    mid1, dq1_id = run(seed_dq_recover(
        db, episode="S01E01", file_name="a.mkv", gid="g1", updated_at=ts))
    mid2, dq2_id = run(seed_dq_recover(
        db, episode="S01E02", file_name="b.mkv", gid="g2", updated_at=ts))
    remove_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(recovery_mod, "aria2",
                        types.SimpleNamespace(client=types.SimpleNamespace(remove=remove_mock)))

    # 仅对 S01E01 模拟 transfer 并发推进 retry_count
    async def _bump():
        async with db() as s:
            await s.execute(
                update(DownloadQueue).where(DownloadQueue.id == dq1_id)
                .values(retry_count=1, updated_at=_now())
            )
            await s.commit()
    run(_bump())

    count = run(recovery_mod.recover_stale_tasks())

    assert count == 1  # 仅 S01E02 回退
    dq1 = run(read_row(db, DownloadQueue, dq1_id))
    dq2 = run(read_row(db, DownloadQueue, dq2_id))
    assert dq1.status == "downloading" and dq1.retry_count == 1  # 冲突行不被覆盖
    assert dq2.status == "pending" and dq2.retry_count == 1      # 正常行 0→1 回退
    # B-3：仅 CAS 成功的 g2 被 remove 恰好 1 次；冲突行 g1 绝不移除
    assert remove_mock.await_count == 1
    assert remove_mock.await_args.args[0] == "g2"