"""transfer 转存链单测（阶段 3 交付 B + D 契约验证）。

全部使用 fake 依赖（monkeypatch app.tasks.transfer 模块内的
aria2/cloudsaver/alist/capacity/notifier/nastools_sync/async_session），
不连任何真实外部服务/数据库。数据库用独立 in-memory SQLite（StaticPool 共享连接）。

验证场景：
- 阶段 A：tell_status=complete → 双表 done + download_task complete + download_complete 通知
  + nastools_sync 触发 + 幂等（二次运行不重复）
- 阶段 A：active → 刷新 updated_at；error → retry_count 递增 → 第 3 次双表 failed + flow_error
- 阶段 B：容量 False → pending + quota_reject_count++ 且 retry_count 不变；
  容量异常（CapacityUnavailable）→ pending 且 quota_reject_count 不变 + flow_error
- 阶段 B：save 连续失败 3 次 → failed 双表 + retry_count=3
- GID 校验：tell_active 返回陌生 comment 任务 → 跳过 + 不转存 + flow_error；
  本系统 comment 任务 → 不阻断正常转存提交；tell_active 故障 → fail-closed
"""
import asyncio
import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  注册全部 ORM 模型
import app.tasks.transfer as transfer_mod
from app.models import DownloadTask, EpisodeState, Media, TransferQueue
from app.services.capacity import CapacityUnavailable
from app.services.cloudsaver import CloudSaverUnavailable


def run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# fake 服务（全部异步方法 + 调用记录）
# ---------------------------------------------------------------------------

class FakeAria2Client:
    """aria2.client：tell_status / tell_active / add_uri。"""

    def __init__(self):
        self.statuses = {}        # gid -> "active"/"complete"/"error"...
        self.actives = []         # tell_active 返回值 [{gid,status,comment}]
        self.add_uri_calls = []   # [(uri, kwargs)]

    async def tell_status(self, gid):
        return {"status": self.statuses.get(gid, "active")}

    async def tell_active(self):
        return list(self.actives)

    async def add_uri(self, uri, **kwargs):
        self.add_uri_calls.append((uri, kwargs))
        return f"gid-{len(self.add_uri_calls)}"


class FakeCloudSaver:
    def __init__(self):
        self.save_calls = []
        self.fail_save = None  # 若设置，save 抛此异常

    async def save(self, params):
        if self.fail_save:
            raise self.fail_save
        self.save_calls.append(dict(params))
        return {"task_id": "t1"}


class FakeAlist:
    def __init__(self):
        self.remove_calls = []  # [(names, dir)]
        self.link = "http://alist.test/raw/ep.mkv"

    async def remove(self, names, dir):
        self.remove_calls.append((list(names), dir))
        return {"success": True}

    async def get_link(self, path):
        return self.link


class FakeCapacityProvider:
    def __init__(self):
        self.result = True
        self.raise_error = None
        self.check_calls = 0

    async def check(self, candidate_bytes):
        self.check_calls += 1
        if self.raise_error:
            raise self.raise_error
        return self.result


class FakeNotifier:
    def __init__(self):
        self.events = []

    async def notify(self, event):
        self.events.append(event)


# ---------------------------------------------------------------------------
# fixtures
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


@pytest.fixture()
def env(monkeypatch):
    """全套 fake 服务 + 替换 transfer 模块内的依赖引用。"""
    fakes = {
        "aria2": FakeAria2Client(),
        "cloudsaver": FakeCloudSaver(),
        "alist": FakeAlist(),
        "capacity": FakeCapacityProvider(),
        "notifier": FakeNotifier(),
    }
    nas = types.SimpleNamespace(nastools_sync=AsyncMock(return_value=None))
    fakes["nastools"] = nas

    monkeypatch.setattr(transfer_mod, "aria2", types.SimpleNamespace(client=fakes["aria2"]))
    monkeypatch.setattr(transfer_mod, "cloudsaver", fakes["cloudsaver"])
    monkeypatch.setattr(transfer_mod, "alist", fakes["alist"])
    monkeypatch.setattr(transfer_mod, "capacity", types.SimpleNamespace(provider=fakes["capacity"]))
    monkeypatch.setattr(transfer_mod, "notifier", fakes["notifier"])
    monkeypatch.setattr(transfer_mod, "nastools_sync", nas)
    # P2-6：非终态回退会 _spawn 后台续跑——单测用 asyncio.run 每轮关闭事件循环，
    # 与跨 loop 后台 task 不兼容，此处置为「跟踪不执行」（验证触发行为，不真正续跑；
    # P2-6 续跑语义由 test_oracle_fixes 专门验证）
    spawn_calls: list = []
    monkeypatch.setattr(transfer_mod, "_spawn", lambda factory: spawn_calls.append(factory))
    fakes["spawn"] = spawn_calls
    return fakes


def patch_db(monkeypatch, db):
    """把 transfer 模块使用的 async_session 换成测试库。"""
    monkeypatch.setattr(transfer_mod, "async_session", db)


# ---------------------------------------------------------------------------
# 种子数据 / 读取
# ---------------------------------------------------------------------------

async def seed_pending(db, *, episode="S01E01", file_name="ep.mkv", file_size=1024,
                       share_code="sc123", stoken="stoken-x", fids='["f1"]',
                       fid_tokens='["ft1"]', folder_id="folder-1",
                       retry_count=0, quota_reject_count=0):
    """写入 media + episode_state(queued) + transfer_queue(pending)。返回 (media_id, es_id, tq_id)。"""
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=42, status="tracking")
        s.add(media)
        await s.flush()
        mid = media.id
        es = EpisodeState(media_id=mid, episode=episode, state="queued",
                          file_name=file_name, file_size=file_size,
                          share_code=share_code, retry_count=retry_count, updated_at=_now())
        s.add(es)
        await s.flush()
        tq = TransferQueue(media_id=mid, episode=episode, file_name=file_name,
                           file_size=file_size, share_code=share_code, stoken=stoken,
                           receive_code="提取码占位", fids=fids, fid_tokens=fid_tokens,
                           folder_id=folder_id, status="pending",
                           quota_reject_count=quota_reject_count, updated_at=_now())
        s.add(tq)
        await s.flush()
        await s.commit()
        return mid, es.id, tq.id


async def seed_downloading(db, *, episode="S01E01", file_name="ep.mkv", gid="gid1"):
    """写入 media + es(downloading) + tq(downloading) + download_task(downloading)。返回 (mid, es_id, tq_id, dl_id)。"""
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=42, status="downloading")
        s.add(media)
        await s.flush()
        mid = media.id
        es = EpisodeState(media_id=mid, episode=episode, state="downloading",
                          file_name=file_name, file_size=1024, share_code="sc123",
                          quark_path=f"/quark/{file_name}", aria2_gid=gid,
                          retry_count=0, updated_at=_now())
        s.add(es)
        await s.flush()
        tq = TransferQueue(media_id=mid, episode=episode, file_name=file_name,
                           file_size=1024, share_code="sc123", fids='["f1"]',
                           status="downloading", updated_at=_now())
        s.add(tq)
        await s.flush()
        dl = DownloadTask(media_id=mid, transfer_id=tq.id, episode=episode,
                          file_name=file_name, aria2_gid=gid, status="downloading",
                          quark_path=f"/quark/{file_name}")
        s.add(dl)
        await s.flush()
        await s.commit()
        return mid, es.id, tq.id, dl.id


async def read_row(db, model, obj_id):
    async with db() as s:
        return await s.get(model, obj_id)


async def get_es_by_media(db, media_id):
    async with db() as s:
        return (
            await s.execute(select(EpisodeState).where(EpisodeState.media_id == media_id))
        ).scalars().first()


async def get_first_dl(db):
    async with db() as s:
        return (await s.execute(select(DownloadTask))).scalars().first()


# ---------------------------------------------------------------------------
# 阶段 A：complete → 释放链 + 双表 done
# ---------------------------------------------------------------------------

def test_poll_complete_marks_done_and_triggers_sync(db, env, monkeypatch):
    patch_db(monkeypatch, db)
    mid, es_id, tq_id, dl_id = run(seed_downloading(db))
    env["aria2"].statuses["gid1"] = "complete"

    run(transfer_mod.process_transfer_queue())

    es = run(get_es_by_media(db, mid))
    tq = run(read_row(db, TransferQueue, tq_id))
    dl = run(read_row(db, DownloadTask, dl_id))
    assert dl.status == "complete"
    assert dl.downloaded_at is not None
    assert tq.status == "done"
    assert es.state == "done"

    # 夸克残留已删除（alist.remove([ep.mkv], /quark/)）
    assert (["ep.mkv"], "/quark/") in env["alist"].remove_calls
    # download_complete 通知（全体）
    done_events = [e for e in env["notifier"].events if e.event_type == "download_complete"]
    assert len(done_events) == 1
    assert done_events[0].title == "下载完成: ep.mkv"
    assert done_events[0].extra["media_id"] == mid
    assert done_events[0].extra["episode"] == "S01E01"
    # nastools_sync 事件触发（_spawn 后台任务；单测只验证触发行为）
    assert len(env["spawn"]) == 1
    assert env["spawn"][0] is transfer_mod.nastools_sync.nastools_sync

    # 幂等：二次运行不重复处理（download_task 已 complete，不再命中轮询）
    run(transfer_mod.process_transfer_queue())
    assert len(env["spawn"]) == 1  # 不再触发
    assert len([e for e in env["notifier"].events if e.event_type == "download_complete"]) == 1


def test_poll_active_refreshes_updated_at(db, env, monkeypatch):
    """active → 仍在下载：双表 updated_at 被刷新（防 recover 2h 误回退），状态不变。"""
    patch_db(monkeypatch, db)
    mid, es_id, tq_id, dl_id = run(seed_downloading(db))
    env["aria2"].statuses["gid1"] = "active"

    run(transfer_mod.process_transfer_queue())

    es = run(get_es_by_media(db, mid))
    tq = run(read_row(db, TransferQueue, tq_id))
    dl = run(read_row(db, DownloadTask, dl_id))
    assert tq.status == "downloading"
    assert dl.status == "downloading"
    assert es.state == "downloading"
    assert tq.updated_at is not None and es.updated_at is not None


def test_poll_error_retries_then_failed(db, env, monkeypatch):
    """aria2 error → 确定性失败路径（§4.5）：retry_count 递增 + 回退 + 清理残留 + 中介终止。

    注意：process_transfer_queue 两阶段依序执行，阶段 A 回退（tq→pending）后
    阶段 B 会立即取到同一任务重试转存——故同时令 save 失败，保证每轮
    retry_count 稳定 +2（阶段 A 一次、阶段 B 重试一次），第二轮达上限转 failed。
    """
    patch_db(monkeypatch, db)
    mid, es_id, tq_id, dl_id = run(seed_downloading(db, gid="gid-err"))
    env["aria2"].statuses["gid-err"] = "error"
    env["cloudsaver"].fail_save = CloudSaverUnavailable("分享已失效")

    # 第一轮：阶段 A retry 0→1 回退 queued/pending + 清理残留；
    #        阶段 B 立即重试 → save 失败 → retry 1→2，仍回退 queued/pending
    run(transfer_mod.process_transfer_queue())
    es = run(get_es_by_media(db, mid))
    tq = run(read_row(db, TransferQueue, tq_id))
    dl = run(read_row(db, DownloadTask, dl_id))
    assert es.retry_count == 2
    assert es.state == "queued"
    assert tq.status == "pending"
    assert dl.status == "failed"  # 中介 download_task 终止，防后续重复轮询计数
    assert env["alist"].remove_calls  # 回退前清理夸克残留（A 与 B 至少一次）

    # 第二轮：阶段 A 不再命中（dl 已 failed）；阶段 B retry 2→3 → 双表 failed + flow_error
    run(transfer_mod.process_transfer_queue())
    es = run(get_es_by_media(db, mid))
    tq = run(read_row(db, TransferQueue, tq_id))
    assert es.retry_count == 3
    assert es.state == "failed"
    assert tq.status == "failed"
    assert any(e.event_type == "flow_error" for e in env["notifier"].events)


# ---------------------------------------------------------------------------
# 阶段 B：容量门槛（fail-closed）
# ---------------------------------------------------------------------------

def test_quota_reject_keeps_pending_and_increments_reject(db, env, monkeypatch):
    patch_db(monkeypatch, db)
    mid, es_id, tq_id = run(seed_pending(db, retry_count=1, quota_reject_count=3))
    env["capacity"].result = False

    run(transfer_mod.process_transfer_queue())
    run(transfer_mod.process_transfer_queue())

    es = run(read_row(db, EpisodeState, es_id))
    tq = run(read_row(db, TransferQueue, tq_id))
    assert tq.status == "pending"
    assert tq.quota_reject_count == 5  # 3 + 2
    assert es.state == "queued"
    assert es.retry_count == 1  # 容量拒绝绝不消耗 retry_count
    assert env["cloudsaver"].save_calls == []  # 未转存


def test_quota_unavailable_keeps_pending_without_inc(db, env, monkeypatch):
    patch_db(monkeypatch, db)
    mid, es_id, tq_id = run(seed_pending(db, quota_reject_count=2))
    env["capacity"].raise_error = CapacityUnavailable("容量接口不可用")

    run(transfer_mod.process_transfer_queue())

    es = run(read_row(db, EpisodeState, es_id))
    tq = run(read_row(db, TransferQueue, tq_id))
    assert tq.status == "pending"
    assert tq.quota_reject_count == 2  # 不 ++
    assert es.retry_count == 0  # 不耗 retry
    assert env["cloudsaver"].save_calls == []
    assert any(e.event_type == "flow_error" for e in env["notifier"].events)


# ---------------------------------------------------------------------------
# 阶段 B：转存失败重试路径（save 失败 3 次 → failed）
# ---------------------------------------------------------------------------

def test_save_failure_retries_then_failed(db, env, monkeypatch):
    patch_db(monkeypatch, db)
    mid, es_id, tq_id = run(seed_pending(db))
    env["cloudsaver"].fail_save = CloudSaverUnavailable("分享已失效")

    # 三次连续失败（每轮重试后回退 queued/pending，可再次被取到）
    for _ in range(3):
        run(transfer_mod.process_transfer_queue())

    es = run(read_row(db, EpisodeState, es_id))
    tq = run(read_row(db, TransferQueue, tq_id))
    assert es.retry_count == 3
    assert es.state == "failed"
    assert tq.status == "failed"
    assert tq.error  # 记录错误原因
    assert any(e.event_type == "flow_error" for e in env["notifier"].events)
    # 每轮失败都尝试清理夸克残留（save 失败也可能有部分转存残留）
    assert len(env["alist"].remove_calls) == 3

    # 已 failed → 无 pending；第四次为空跑，计数不再变
    run(transfer_mod.process_transfer_queue())
    assert run(read_row(db, EpisodeState, es_id)).retry_count == 3


def test_save_success_commits_download(db, env, monkeypatch):
    """正常转存链路：save → get_link → add_uri → 双表 downloading + download_task。"""
    patch_db(monkeypatch, db)
    mid, es_id, tq_id = run(seed_pending(db))
    env["aria2"].actives = [{"gid": "own", "status": "active", "comment": "lumencloud:1:S01E01"}]

    run(transfer_mod.process_transfer_queue())

    # receiveCode 必须用 stoken（双语义 G4），不是提取码
    assert len(env["cloudsaver"].save_calls) == 1
    params = env["cloudsaver"].save_calls[0]
    assert params["receiveCode"] == "stoken-x"
    assert params["shareCode"] == "sc123"
    assert params["fids"] == ["f1"]
    assert params["fidTokens"] == ["ft1"]
    assert params["folderId"] == "folder-1"
    # aria2 提交：out=文件名，comment=lumencloud:<media_id>:<episode>
    assert len(env["aria2"].add_uri_calls) == 1
    uri, kwargs = env["aria2"].add_uri_calls[0]
    assert uri == env["alist"].link
    assert kwargs["out"] == "ep.mkv"
    assert kwargs["comment"] == "lumencloud:1:S01E01"

    es = run(read_row(db, EpisodeState, es_id))
    tq = run(read_row(db, TransferQueue, tq_id))
    assert tq.status == "downloading"
    assert es.state == "downloading"
    assert es.aria2_gid == "gid-1"
    assert es.quark_path == "/quark/ep.mkv"

    dl = run(get_first_dl(db))
    assert dl is not None
    assert dl.status == "downloading"
    assert dl.aria2_gid == "gid-1"
    assert dl.quark_path == "/quark/ep.mkv"


# ---------------------------------------------------------------------------
# 阶段 B：GID 来源校验兜底（§12.2 简化版）
# ---------------------------------------------------------------------------

def test_gid_source_check_blocks_foreign_task(db, env, monkeypatch):
    """存在陌生 aria2 活动任务 → 本轮跳过 + 不转存 + quota/retry 计数不变。"""
    patch_db(monkeypatch, db)
    mid, es_id, tq_id = run(seed_pending(db))
    env["aria2"].actives = [{"gid": "n8n-gid", "status": "active", "comment": "n8n:legacy"}]

    run(transfer_mod.process_transfer_queue())

    es = run(read_row(db, EpisodeState, es_id))
    tq = run(read_row(db, TransferQueue, tq_id))
    assert tq.status == "pending"            # 不处理
    assert tq.quota_reject_count == 0        # 不 ++ quota_reject
    assert es.retry_count == 0               # 不耗 retry
    assert es.state == "queued"
    assert env["cloudsaver"].save_calls == []  # 不转存
    assert any(e.event_type == "flow_error" for e in env["notifier"].events)


def test_gid_source_check_accepts_own_comment(db, env, monkeypatch):
    """本系统 comment（lumencloud: 前缀）的活动任务不阻断转存。"""
    patch_db(monkeypatch, db)
    mid, es_id, tq_id = run(seed_pending(db))
    env["aria2"].actives = [{"gid": "own-1", "status": "active", "comment": "lumencloud:9:S02E03"}]

    run(transfer_mod.process_transfer_queue())

    assert len(env["cloudsaver"].save_calls) == 1  # 正常转存
    tq = run(read_row(db, TransferQueue, tq_id))
    assert tq.status == "downloading"


def test_gid_check_failure_blocks_round(db, env, monkeypatch):
    """tell_active 故障 → fail-closed：本轮跳过 + flow_error，不转存。"""
    patch_db(monkeypatch, db)
    mid, es_id, tq_id = run(seed_pending(db))

    async def boom():
        raise RuntimeError("aria2 RPC 不可用")

    env["aria2"].tell_active = boom
    run(transfer_mod.process_transfer_queue())

    es = run(read_row(db, EpisodeState, es_id))
    tq = run(read_row(db, TransferQueue, tq_id))
    assert tq.status == "pending"
    assert tq.quota_reject_count == 0
    assert es.retry_count == 0
    assert env["cloudsaver"].save_calls == []
    assert any(e.event_type == "flow_error" for e in env["notifier"].events)


def test_no_pending_is_skipped(db, env, monkeypatch):
    """空队列 → 阶段 B task_run(skipped)，无副作用。"""
    patch_db(monkeypatch, db)
    run(transfer_mod.process_transfer_queue())
    assert env["cloudsaver"].save_calls == []
    assert env["notifier"].events == []


# ---------------------------------------------------------------------------
# 落盘等待超时上限（Q3：线上反馈大文件超时放宽兜底）
# ---------------------------------------------------------------------------

def test_link_wait_timeout_constant_and_default():
    """_LINK_WAIT_TIMEOUT=300s，且函数默认超时与常量一致（防魔法数漂移）。

    阶段 3 实证 1.5-2.6G 文件落盘 60-180s，180s 上限偏紧导致个别超时；
    放宽至 300s 作兜底（超时仍走外层重试路径 retry_count++，非无限等）。
    """
    import inspect

    assert transfer_mod._LINK_WAIT_TIMEOUT == 300.0
    assert inspect.signature(
        transfer_mod._get_link_wait_visible
    ).parameters["timeout"].default == transfer_mod._LINK_WAIT_TIMEOUT