"""Oracle 审查修复单测（P1-1/P1-2/P1-3/P2-1/P2-3/P2-4）。

全部 mock 外部依赖（monkeypatch 模块内 async_session / notifier / alist / nastools /
transfer.trigger_transfer），数据库用独立 in-memory SQLite（StaticPool 共享连接），
不连任何真实外部服务。

覆盖：
- P1-1：done 防重解除（Emby 确认入库 → 删除 es/tq/dl；仍在缺失 → 转 failed；movie 两分支）
- P1-2：retry_task commit 成功后触发 transfer
- P2-3：retry 时 episode_state 非 failed → 409 且 tq 回滚保持 failed
- P1-3：cleanup 引用集合只含 downloading（complete 残留可被兜底清理 / downloading 受保护）
- P2-1：nastools_sync 模块级锁串行化（并发无重叠、无双重启）
- P2-4：notification_scan 去重前缀带空格（wr#1 不被 wr#10 误命中、带空格标记去重生效）
"""
import asyncio
import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  注册全部 ORM 模型
import app.tasks.transfer as transfer_mod
from app.models import DownloadTask, EpisodeState, Media, Notification, TaskRun, TransferQueue, WatchRequest
from app.services.notifier import EVENT_APPROVAL_PENDING, EVENT_FLOW_ERROR


def run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# fixtures / 公共依赖
# ---------------------------------------------------------------------------

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


async def read_row(db, model, obj_id):
    async with db() as s:
        return await s.get(model, obj_id)


async def seed_done_state(db, *, episode="S01E01", file_name="ep.mkv", dl_status="complete",
                          media_id=None, retry_count=0):
    """media + es(done) + tq(done) + download_task(指定状态)。返回 (mid, es_id, tq_id, dl_id)。

    media_id 传入时复用既有 media（同一影视多集场景）。
    """
    async with db() as s:
        if media_id is None:
            media = Media(title="测试剧", media_type="tv", tmdb_id=42, status="tracking")
            s.add(media)
            await s.flush()
            mid = media.id
        else:
            mid = media_id
        es = EpisodeState(media_id=mid, episode=episode, state="done",
                          file_name=file_name, file_size=1024, share_code="sc",
                          retry_count=retry_count,
                          quark_path=f"/quark/{file_name}", aria2_gid="g1", updated_at=_now())
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


async def seed_failed(db, *, episode="S01E01", file_name="ep.mkv"):
    """media + es(failed, retry=3) + tq(failed)。返回 (mid, es_id, tq_id)。"""
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=42, status="tracking")
        s.add(media)
        await s.flush()
        mid = media.id
        es = EpisodeState(media_id=mid, episode=episode, state="failed",
                          file_name=file_name, file_size=1024, share_code="sc",
                          retry_count=3, error="e", updated_at=_now())
        s.add(es)
        await s.flush()
        tq = TransferQueue(media_id=mid, episode=episode, file_name=file_name,
                           file_size=1024, share_code="sc", stoken="st",
                           status="failed", quota_reject_count=2, error="e", updated_at=_now())
        s.add(tq)
        await s.flush()
        await s.commit()
        return mid, es.id, tq.id


class FakeNotifier:
    def __init__(self):
        self.events = []

    async def notify(self, event):
        self.events.append(event)


class FakeCleanupAlist:
    def __init__(self):
        self.entries = []
        self.remove_calls = []

    async def list_dir(self, path):
        return list(self.entries)

    async def remove(self, names, dir):
        self.remove_calls.append((list(names), dir))
        return {"success": True}


# ---------------------------------------------------------------------------
# P1-1：done 防重解除（Emby 二次确认）
# ---------------------------------------------------------------------------

def test_done_resolution_deletes_when_emby_confirmed(db, monkeypatch):
    """tv：Emby missing 不含该集 → 已确认入库 → 删除 es/tq/dl（防重解除）。"""
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    mid, es_id, tq_id, dl_id = run(seed_done_state(db, episode="S01E01"))
    # Emby 缺失列表只含 S01E02 → S01E01 已入库 → 删除
    run(scan_mod._resolve_done_states(types.SimpleNamespace(id=mid), {"S01E02"}, False))

    assert run(read_row(db, EpisodeState, es_id)) is None
    assert run(read_row(db, TransferQueue, tq_id)) is None
    assert run(read_row(db, DownloadTask, dl_id)) is None


def test_done_resolution_marks_failed_when_still_missing(db, monkeypatch):
    """tv：Emby missing 仍含该集 → 转 failed 人工确认（双表联动）。"""
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    mid, es_id, tq_id, dl_id = run(seed_done_state(db, episode="S01E01"))
    run(scan_mod._resolve_done_states(types.SimpleNamespace(id=mid), {"S01E01"}, False))

    es = run(read_row(db, EpisodeState, es_id))
    tq = run(read_row(db, TransferQueue, tq_id))
    assert es.state == "failed"
    assert es.error == "下载完成但 Emby 未入库，请人工确认"
    assert tq.status == "failed"
    assert tq.error == "下载完成但 Emby 未入库，请人工确认"


def test_done_resolution_movie_missing_marks_failed(db, monkeypatch):
    """movie 全量模式：Emby 整部缺失（movie_missing=True）→ 转 failed。"""
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    mid, es_id, tq_id, dl_id = run(seed_done_state(db, episode="电影.mkv"))
    run(scan_mod._resolve_done_states(types.SimpleNamespace(id=mid), set(), True))

    es = run(read_row(db, EpisodeState, es_id))
    assert es.state == "failed"
    assert es.error == "下载完成但 Emby 未入库，请人工确认"


def test_done_resolution_movie_confirmed_deletes(db, monkeypatch):
    """movie 全量模式：Emby 已入库（movie_missing=False）→ 全部确认 → 删除。"""
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    mid, es_id, tq_id, dl_id = run(seed_done_state(db, episode="电影.mkv"))
    run(scan_mod._resolve_done_states(types.SimpleNamespace(id=mid), set(), False))

    assert run(read_row(db, EpisodeState, es_id)) is None
    assert run(read_row(db, TransferQueue, tq_id)) is None
    assert run(read_row(db, DownloadTask, dl_id)) is None


def test_done_resolution_keeps_other_episodes(db, monkeypatch):
    """P1-1 删除只影响目标集，同影视其他集记录不受影响。"""
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    mid, es_id, tq_id, dl_id = run(seed_done_state(db, episode="S01E01"))
    _, es2_id, tq2_id, dl2_id = run(seed_done_state(db, episode="S01E02", media_id=mid))
    run(scan_mod._resolve_done_states(types.SimpleNamespace(id=mid), {"S01E02"}, False))

    # S01E01（不在 missing）删除；S01E02（仍在 missing）转 failed
    assert run(read_row(db, EpisodeState, es_id)) is None
    es2 = run(read_row(db, EpisodeState, es2_id))
    assert es2.state == "failed"


# ---------------------------------------------------------------------------
# P1-2 / P2-3：retry_task（commit 后触发转存 / es 不一致 409）
# ---------------------------------------------------------------------------

def test_retry_task_commits_and_triggers_transfer(db, monkeypatch):
    from app.routers import queue as queue_mod

    mid, es_id, tq_id = run(seed_failed(db))
    trigger = AsyncMock(return_value=None)
    monkeypatch.setattr(transfer_mod, "trigger_transfer", trigger)

    async def call():
        async with db() as s:
            return await queue_mod.retry_task(tq_id, types.SimpleNamespace(role="admin"), s)

    result = run(call())
    assert result["ok"] is True
    assert trigger.await_count == 1  # P1-2：commit 成功后触发转存消费

    tq = run(read_row(db, TransferQueue, tq_id))
    es = run(read_row(db, EpisodeState, es_id))
    assert tq.status == "pending"
    assert tq.quota_reject_count == 0
    assert tq.error is None
    assert es.state == "queued"
    assert es.retry_count == 0
    assert es.error is None


def test_retry_es_mismatch_raises_409_and_rolls_back(db, monkeypatch):
    from app.routers import queue as queue_mod

    mid, es_id, tq_id = run(seed_failed(db))
    # 破坏双表一致性：episode_state 非 failed
    async def _break_es():
        async with db() as s:
            es = await s.get(EpisodeState, es_id)
            es.state = "queued"
            await s.commit()
    run(_break_es())

    async def call():
        async with db() as s:
            return await queue_mod.retry_task(tq_id, types.SimpleNamespace(role="admin"), s)

    with pytest.raises(HTTPException) as ei:
        run(call())
    assert ei.value.status_code == 409
    assert "episode_state" in ei.value.detail

    # P2-3：未 commit → tq 已改的 pending 一并回滚，保持 failed 原状
    tq = run(read_row(db, TransferQueue, tq_id))
    assert tq.status == "failed"


# ---------------------------------------------------------------------------
# P1-3：cleanup 引用集合只含 downloading
# ---------------------------------------------------------------------------

def test_cleanup_removes_unreferenced_complete(db, monkeypatch):
    """P1-3：complete 的 download_task 不再保护 quark_path → 残留被兜底清理。"""
    from app.tasks import cleanup as cleanup_mod

    monkeypatch.setattr(cleanup_mod, "async_session", db)
    mid, es_id, tq_id, dl_id = run(seed_done_state(db, dl_status="complete"))

    fake = FakeCleanupAlist()
    fake.entries = [{"name": "ep.mkv", "is_dir": False, "size": 1}]
    monkeypatch.setattr(cleanup_mod, "alist", fake)

    run(cleanup_mod.release_space_cleanup_job())
    assert (["ep.mkv"], "/quark/") in fake.remove_calls


def test_cleanup_keeps_downloading_referenced(db, monkeypatch):
    """downloading 的 download_task 引用文件仍受保护（不误删进行中任务）。"""
    from app.tasks import cleanup as cleanup_mod

    monkeypatch.setattr(cleanup_mod, "async_session", db)
    mid, es_id, tq_id, dl_id = run(seed_done_state(db, dl_status="downloading"))

    fake = FakeCleanupAlist()
    fake.entries = [{"name": "ep.mkv", "is_dir": False, "size": 1}]
    monkeypatch.setattr(cleanup_mod, "alist", fake)

    run(cleanup_mod.release_space_cleanup_job())
    assert fake.remove_calls == []  # 无孤儿


# ---------------------------------------------------------------------------
# P2-1：nastools_sync 模块级互斥锁
# ---------------------------------------------------------------------------

def test_nastools_sync_serialized_by_lock(db, monkeypatch):
    from app.tasks import nastools_sync as nas_mod

    class FakeNasToolsClient:
        def __init__(self):
            self.sequence = []
            self.overlaps = False
            self._active = False

        async def _op(self, name):
            self.sequence.append(f"{name}:start")
            if self._active:
                self.overlaps = True  # 无锁时并发操作会重叠
            self._active = True
            await asyncio.sleep(0.01)
            self._active = False
            self.sequence.append(f"{name}:end")

        async def login(self):
            await self._op("login")

        async def restart(self):
            await self._op("restart")

        async def run_directory_sync(self, sid):
            await self._op("run_directory_sync")

    fake_client = FakeNasToolsClient()
    monkeypatch.setattr(nas_mod, "nastools", types.SimpleNamespace(client=fake_client))
    monkeypatch.setattr(nas_mod, "async_session", db)
    # 把 asyncio.sleep(30)（重启等待）替换为立即返回，避免测试等待 30s
    monkeypatch.setattr(nas_mod, "asyncio", types.SimpleNamespace(sleep=AsyncMock(return_value=None)))

    async def scenario():
        await asyncio.gather(nas_mod.nastools_sync(), nas_mod.nastools_sync())

    run(scenario())

    # P2-1 锁内串行：操作无重叠；第二个并发调用在锁内重读冷却时间戳后跳过 → 只重启一次。
    # sequence 存 "<op>:start"/"<op>:end" 成对记录，按 ":start" 统计实际操作次数
    assert fake_client.overlaps is False
    assert sum(1 for x in fake_client.sequence if x == "login:start") == 2  # 首次同步两次登录
    assert sum(1 for x in fake_client.sequence if x == "restart:start") == 1  # 无双重启
    assert sum(1 for x in fake_client.sequence if x == "run_directory_sync:start") == 1


# ---------------------------------------------------------------------------
# P2-6：阶段 B 非终态失败回退后触发续跑
# ---------------------------------------------------------------------------

class FakeAria2Client:
    def __init__(self):
        self.actives = []
        self.add_uri_calls = []

    async def tell_active(self):
        return list(self.actives)

    async def add_uri(self, uri, **kwargs):
        self.add_uri_calls.append((uri, kwargs))
        return f"gid-{len(self.add_uri_calls)}"


class FakeCloudSaver2:
    """顺序副作用：save_effects 队列——异常则抛、dict 则作为返回值。"""

    def __init__(self):
        self.save_effects = []
        self.save_calls = []

    async def save(self, params):
        self.save_calls.append(dict(params))
        if self.save_effects:
            eff = self.save_effects.pop(0)
            if isinstance(eff, Exception):
                raise eff
            return eff
        return {"task_id": "t"}


class FakeAlist2:
    def __init__(self):
        self.remove_calls = []
        self.link = "http://alist.test/raw/ep.mkv"

    async def remove(self, names, dir):
        self.remove_calls.append((list(names), dir))
        return {"success": True}

    async def get_link(self, path):
        return self.link


class FakeCapacityProvider2:
    async def check(self, candidate_bytes):
        return True


async def seed_pending(db, *, episode="S01E01", file_name="ep.mkv", stoken="stoken-x"):
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
                           file_size=1024, share_code="sc", stoken=stoken,
                           receive_code="提取码占位", fids='["f1"]', fid_tokens='["ft1"]',
                           folder_id="folder-1", status="pending", updated_at=_now())
        s.add(tq)
        await s.flush()
        await s.commit()
        return mid, es.id, tq.id


def test_transfer_failure_backoff_triggers_resume(db, monkeypatch):
    """P2-6：阶段 B 转存失败（非终态回退 pending）后 spawn 续跑，续跑成功推进 downloading。"""
    monkeypatch.setattr(transfer_mod, "async_session", db)
    monkeypatch.setattr(transfer_mod, "aria2", types.SimpleNamespace(client=FakeAria2Client()))
    cloud = FakeCloudSaver2()
    cloud.save_effects = [RuntimeError("暂时失败"), {"task_id": "t2"}]  # 首次失败、续跑成功
    monkeypatch.setattr(transfer_mod, "cloudsaver", cloud)
    monkeypatch.setattr(transfer_mod, "alist", FakeAlist2())
    monkeypatch.setattr(transfer_mod, "capacity", types.SimpleNamespace(provider=FakeCapacityProvider2()))
    monkeypatch.setattr(transfer_mod, "notifier", FakeNotifier())

    mid, es_id, tq_id = run(seed_pending(db))

    # 捕获 _spawn 创建的后台续跑 task（P3-3 持引用机制），并在同一事件循环内显式等待完成
    spawned: list = []
    def _track_create_task(coro):
        t = asyncio.ensure_future(coro)
        spawned.append(t)
        return t
    monkeypatch.setattr(transfer_mod, "asyncio", types.SimpleNamespace(create_task=_track_create_task))

    async def scenario():
        await transfer_mod.process_transfer_queue()
        # 等待全部后台续跑完成（同一 loop，避免跨 run 绑定问题）；
        # A-1 后成功路径（步骤 6）还会再 spawn 下一轮续跑，直到队列清空不再触发
        seen = set()
        while True:
            new = [t for t in spawned if id(t) not in seen]
            if not new:
                break
            seen.update(id(t) for t in new)
            await asyncio.gather(*new)

    run(scenario())

    es = run(read_row(db, EpisodeState, es_id))
    tq = run(read_row(db, TransferQueue, tq_id))
    assert len(spawned) == 2  # #1 非终态回退续跑（P2-6）+ #2 成功路径续跑（A-1）
    assert es.retry_count == 1  # 仅首次失败消耗 retry
    assert es.state == "downloading"  # 续跑成功
    assert tq.status == "downloading"
    assert len(cloud.save_calls) == 2


# ---------------------------------------------------------------------------
# P2-4：notification_scan 去重前缀带空格
# ---------------------------------------------------------------------------

def test_notification_scan_prefix_with_space(db, monkeypatch):
    from app.tasks import notification_scan as notif_mod

    monkeypatch.setattr(notif_mod, "async_session", db)

    async def seed():
        async with db() as s:
            media = Media(title="测试剧", media_type="tv", tmdb_id=1, status="tracking")
            s.add(media)
            await s.flush()
            mid = media.id
            # watch_requests id=1（pending）
            s.add(WatchRequest(requested_by=None, title="想看A", status="pending"))
            # 已存在 wr#10 旧通知（修复前 "wr#1%" 会误命中它）
            s.add(Notification(event_type="approval_pending", title="旧", body="wr#10 想看A"))
            # transfer_queue id=1（failed）
            es = EpisodeState(media_id=mid, episode="S01E01", state="failed", file_name="ep.mkv",
                              file_size=1, share_code="sc", retry_count=3, error="e", updated_at=_now())
            s.add(es)
            await s.flush()
            tq = TransferQueue(media_id=mid, episode="S01E01", file_name="ep.mkv", file_size=1,
                               share_code="sc", status="failed", error="e", updated_at=_now())
            s.add(tq)
            await s.flush()
            # 已存在 tq#10 旧通知（修复前 "tq#1%" 会误命中它）
            s.add(Notification(event_type="flow_error", title="旧", body="tq#10 ep.mkv: e"))
            await s.commit()

    run(seed())
    fake = FakeNotifier()
    monkeypatch.setattr(notif_mod, "notifier", fake)

    run(notif_mod.notification_scan_job())
    wr_events = [e for e in fake.events if e.event_type == EVENT_APPROVAL_PENDING]
    tq_events = [e for e in fake.events if e.event_type == EVENT_FLOW_ERROR]
    # P2-4 修复点：wr#1 / tq#1 不被 wr#10 / tq#10 的 body 误命中
    assert len(wr_events) == 1 and wr_events[0].body == "wr#1 想看A"
    assert len(tq_events) == 1 and tq_events[0].body.startswith("tq#1 ")

    # 模拟 InAppNotifier 已写库（body 带空格标记）→ 再次扫描不再重复推送
    async def record():
        async with db() as s:
            s.add(Notification(event_type="approval_pending", title="新", body="wr#1 想看A"))
            s.add(Notification(event_type="flow_error", title="新", body="tq#1 ep.mkv: e"))
            await s.commit()

    run(record())
    run(notif_mod.notification_scan_job())
    assert len([e for e in fake.events if e.event_type == EVENT_APPROVAL_PENDING]) == 1
    assert len([e for e in fake.events if e.event_type == EVENT_FLOW_ERROR]) == 1


# ---------------------------------------------------------------------------
# P3-1：done→failed 循环上限（retry_count 增量 + 达上限保持 done）
# ---------------------------------------------------------------------------

def test_done_resolution_failed_respects_retry_count_below_limit(db, monkeypatch):
    """P3-1：未达上限（retry_count=2 < 3）→ 转 failed 并发推进位（retry_count +1）+ tq 联动。"""
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    mid, es_id, tq_id, _dl_id = run(seed_done_state(db, episode="S01E01", retry_count=2))
    run(scan_mod._resolve_done_states(types.SimpleNamespace(id=mid), {"S01E01"}, False))

    es = run(read_row(db, EpisodeState, es_id))
    tq = run(read_row(db, TransferQueue, tq_id))
    assert es.state == "failed"
    assert es.retry_count == 3  # done→failed 算一次循环，retry_count SQL 自增
    assert es.error == scan_mod._DONE_FAIL_ERROR
    assert tq.status == "failed"  # es 转 failed 成功 → 联动 tq


def test_done_resolution_hit_retry_limit_keeps_done(db, monkeypatch):
    """P3-1：达循环上限（retry_count=3）→ 保持 done、不删除、仅写上限 error，tq 不联动。"""
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    mid, es_id, tq_id, _dl_id = run(seed_done_state(db, episode="S01E01", retry_count=3))
    run(scan_mod._resolve_done_states(types.SimpleNamespace(id=mid), {"S01E01"}, False))

    es = run(read_row(db, EpisodeState, es_id))
    tq = run(read_row(db, TransferQueue, tq_id))
    assert es is not None and es.state == "done"  # 不再转 failed（防重保留）
    assert es.retry_count == 3  # 不消耗 / 不再递增
    assert es.error == scan_mod._DONE_LIMIT_ERROR  # 上限 error 文案，人工核实 Emby 端
    assert tq.status == "done"  # 上限分支不联动 tq


# ---------------------------------------------------------------------------
# B 定时：scan_all_media 按 last_scan_at 到期过滤（阶段 4，job 每分钟 tick）
# ---------------------------------------------------------------------------

def test_scan_all_media_filters_by_last_scan_at(db, monkeypatch):
    """B 定时：last_scan_at IS NULL 或已到期 → 巡检；未到期 → 跳过（scan_media 不触发）。"""
    from datetime import timedelta

    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    routed = []

    async def fake_scan_media(media_id):
        routed.append(media_id)
        return 1

    monkeypatch.setattr(scan_mod, "scan_media", fake_scan_media)
    now = _now()

    async def seed():
        async with db() as s:
            a = Media(title="A 未巡检", media_type="tv", status="tracking",
                      scan_interval_minutes=1, last_scan_at=None)
            b = Media(title="B 已到期", media_type="tv", status="downloading",
                      scan_interval_minutes=1, last_scan_at=now - timedelta(minutes=2))
            c = Media(title="C 未到期", media_type="tv", status="tracking",
                      scan_interval_minutes=1, last_scan_at=now)
            s.add_all([a, b, c])
            await s.commit()
            return [m.id for m in (a, b, c)]

    ids = run(seed())
    run(scan_mod.scan_all_media())

    # A（last_scan_at=None）与 B（2 分钟前超 1 分钟周期）巡检；C（刚刚）未到期跳过
    assert sorted(routed) == sorted([ids[0], ids[1]])


# ---------------------------------------------------------------------------
# M1（Oracle Gate2）：scan_all_media_job 异常兜底
# ---------------------------------------------------------------------------

def test_scan_all_media_job_records_task_run_on_error(db, monkeypatch):
    """M1：scan_all_media_job 整体异常 → 记录 task_run(scan_all_media, error) 兜底，不外泄。"""
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)

    async def boom():
        raise RuntimeError("DB 抖动")

    monkeypatch.setattr(scan_mod, "scan_all_media", boom)
    run(scan_mod.scan_all_media_job())  # 不应抛出

    async def count():
        async with db() as s:
            return (
                await s.execute(
                    select(TaskRun).where(TaskRun.task_type == "scan_all_media")
                )
            ).scalars().all()

    rows = run(count())
    assert len(rows) == 1
    assert rows[0].status == "error"
    assert "定时巡检异常" in rows[0].message


# ---------------------------------------------------------------------------
# M3（Oracle Gate2）：scan_all_media(force=True) 全量不过滤
# ---------------------------------------------------------------------------

def test_scan_all_media_force_skips_due_filter(db, monkeypatch):
    """M3：force=True 跳过 last_scan_at 到期过滤，全部触及 media 一律巡检。"""
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    routed = []

    async def fake_scan_media(media_id):
        routed.append(media_id)
        return 1

    monkeypatch.setattr(scan_mod, "scan_media", fake_scan_media)
    now = _now()

    async def seed():
        async with db() as s:
            a = Media(title="A 刚巡检未到期", media_type="tv", status="tracking",
                      scan_interval_minutes=60, last_scan_at=now)
            b = Media(title="B 从未巡检", media_type="tv", status="tracking",
                      scan_interval_minutes=60, last_scan_at=None)
            s.add_all([a, b])
            await s.commit()
            return [m.id for m in (a, b)]

    ids = run(seed())
    run(scan_mod.scan_all_media(force=True))

    assert sorted(routed) == sorted(ids)  # force 全量：A（未到期）也巡检


# ---------------------------------------------------------------------------
# m1（Oracle Gate2）：上限分支 error 已写入后不再重复改写
# ---------------------------------------------------------------------------

def test_done_resolution_limit_error_not_rewritten(db, monkeypatch):
    """m1：已达上限记录 error 已写上限文案后，再次 resolve 不再重复写（updated_at 稳定）。"""
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    mid, es_id, tq_id, _dl_id = run(seed_done_state(db, episode="S01E01", retry_count=3))

    run(scan_mod._resolve_done_states(types.SimpleNamespace(id=mid), {"S01E01"}, False))
    es1 = run(read_row(db, EpisodeState, es_id))
    assert es1.error == scan_mod._DONE_LIMIT_ERROR
    first_ts = es1.updated_at  # 首次写入 error 时的时间戳

    run(scan_mod._resolve_done_states(types.SimpleNamespace(id=mid), {"S01E01"}, False))
    es2 = run(read_row(db, EpisodeState, es_id))
    # 第二轮不再改写：error 保持上限文案、updated_at 未被污染
    assert es2.error == scan_mod._DONE_LIMIT_ERROR
    assert es2.updated_at == first_ts