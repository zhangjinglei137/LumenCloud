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
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  注册全部 ORM 模型
import app.routers.queue as queue_mod
import app.tasks.transfer as transfer_mod
from app.models import DownloadTask, EpisodeState, Media, TransferQueue, User
from app.services.alist import AlistUnavailable as RealAlistUnavailable
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
    # P0-1：落盘超时路径会 `raise alist.AlistUnavailable(...)`——fake 需同义异常类
    AlistUnavailable = RealAlistUnavailable

    def __init__(self):
        self.remove_calls = []  # [(names, dir)]
        self.list_dir_calls = []  # [path]
        self.link = "http://alist.test/raw/ep.mkv"

    async def remove(self, names, dir):
        self.remove_calls.append((list(names), dir))
        return {"success": True}

    async def get_link(self, path):
        return self.link

    async def list_dir(self, path):
        self.list_dir_calls.append(path)
        return [{"name": "other.mkv", "is_dir": False, "size": 123}]


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
    # nastools_sync 事件触发 + A-1 转存续跑（_spawn 后台任务；单测只验证触发行为）
    assert len(env["spawn"]) == 2
    assert env["spawn"][0] is transfer_mod.nastools_sync.nastools_sync  # 先触发 nastools_sync
    assert env["spawn"][1] is transfer_mod.process_transfer_queue       # A-1：下载完成释放容量后续跑
    # 幂等：二次运行不重复处理（download_task 已 complete，不再命中轮询）
    run(transfer_mod.process_transfer_queue())
    assert len(env["spawn"]) == 2  # 不再触发
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


def test_success_path_spawns_next_pending_resume(db, env, monkeypatch):
    """A-1（P1）：成功路径步骤 6 提交后 spawn process_transfer_queue 续跑下一 pending。

    与失败非终态回退续跑（P2-6）对称——解决「一次 scan 入队 N 集只处理 1 集」
    的静默积压；_process_lock 保证续跑仅排队等待串行执行（无并发风险）。
    env 的 _spawn 为记录器（不真正执行），此处只验证触发行为。
    """
    patch_db(monkeypatch, db)
    mid, es_id, tq_id = run(seed_pending(db))

    run(transfer_mod.process_transfer_queue())

    # 成功提交后 spawn 续跑下一 pending：记录的工厂必须指向 process_transfer_queue
    assert [f.__name__ for f in env["spawn"]] == ["process_transfer_queue"]
    tq = run(read_row(db, TransferQueue, tq_id))
    es = run(get_es_by_media(db, mid))
    assert tq.status == "downloading"
    assert es.state == "downloading"


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


# ---------------------------------------------------------------------------
# P0-1（线上反馈「转存多次失败」）：save 受理但未落盘 → 失败重试路径清空 save_task_id
# ---------------------------------------------------------------------------

def test_link_timeout_clears_save_task_id_for_retry(db, env, monkeypatch):
    """save 返回 task_id 但文件始终不落盘（get_link 持续失败超时）→ 走 except 重试路径。

    P0-1 关键断言：失败回退后 TransferQueue.save_task_id 被清空为 NULL——
    下一轮重试会重新 save（打破「已受理即跳过 save」的盲等死循环到 retry 上限）。

    同时验证超时诊断：抛错前会列 /quark 目录记录实际内容；异常消息含 folderId
    与 alist 管理 API /api/admin/storage/list 核对提示。
    """
    patch_db(monkeypatch, db)
    monkeypatch.setattr(transfer_mod, "_LINK_WAIT_TIMEOUT", 0.0)  # 单测不等 300s
    fake_store = types.SimpleNamespace(
        get=lambda key, default=None: "9b852b37f9fb4d11938046a6ab5356a7"
    )
    monkeypatch.setattr(transfer_mod, "config_store", fake_store)
    mid, es_id, tq_id = run(seed_pending(db))

    # cloudSaver 正常受理（返回 task_id=t1），但文件从未落盘 → get_link 一直失败
    async def always_fail(path):
        raise RuntimeError("object not found")

    env["alist"].get_link = always_fail

    # 第一轮：save 受理并把 task_id 落库 → get_link 超时 → 失败回退 + 清空 save_task_id
    run(transfer_mod.process_transfer_queue())
    tq = run(read_row(db, TransferQueue, tq_id))
    es = run(read_row(db, EpisodeState, es_id))
    assert len(env["cloudsaver"].save_calls) == 1
    assert tq.status == "pending"
    assert tq.save_task_id is None                    # 关键：失败重试路径清空幂等标记
    assert es.state == "queued"
    assert es.retry_count == 1
    assert tq.error is not None
    assert "folderId=" in tq.error                    # 超时消息带 folderId（诊断）
    assert "/api/admin/storage/list" in tq.error      # 含配置核对提示
    assert env["alist"].list_dir_calls == ["/quark"]  # 抛错前列目录（诊断）

    # 第二轮：save_task_id 已清空 → 重新 save（防死循环的核心行为，而非跳过 save 盲等）
    run(transfer_mod.process_transfer_queue())
    tq = run(read_row(db, TransferQueue, tq_id))
    es = run(read_row(db, EpisodeState, es_id))
    assert len(env["cloudsaver"].save_calls) == 2
    assert tq.save_task_id is None
    assert es.retry_count == 2


# ---------------------------------------------------------------------------
# P0-1（council）：save_task_id 全链路清理 + save_attempt_at 超时兜底（P0-1）
# ---------------------------------------------------------------------------

def _force_failed_with_stale_save(db, tq_id, es_id):
    """把 pending 种子直接置为 failed + save_task_id 残留（模拟历史残留现场）。"""

    async def _set():
        async with db() as s:
            await s.execute(
                update(TransferQueue).where(TransferQueue.id == tq_id).values(
                    status="failed",
                    save_task_id="stale-t1",
                    save_attempt_at=_now() - timedelta(minutes=30),
                    error="转存失败: 分享已失效",
                )
            )
            await s.execute(
                update(EpisodeState).where(EpisodeState.id == es_id).values(
                    state="failed", retry_count=3, error="转存失败: 分享已失效",
                )
            )
            await s.commit()

    run(_set())


def test_manual_retry_clears_save_task_id_then_resaves(db, env, monkeypatch):
    """改动 1a：人工 retry 回退 failed→pending 时同事务清空 save_task_id。

    P0-1 关键断言：failed 任务残留的 save_task_id 在 retry 后为 NULL——下一轮
    重新走完整转存链（重新 save），而非跳过 save 盲等。
    """
    patch_db(monkeypatch, db)
    mid, es_id, tq_id = run(seed_pending(db))
    _force_failed_with_stale_save(db, tq_id, es_id)

    # 阻断 retry 成功后内部的 trigger_transfer（延迟导入会取到 patch 后的引用）
    monkeypatch.setattr(transfer_mod, "trigger_transfer", AsyncMock(return_value=None))

    async def do_retry():
        async with db() as s:
            return await queue_mod.retry_task(task_id=tq_id, admin=User(), session=s)

    result = run(do_retry())
    assert result == {"ok": True}

    tq = run(read_row(db, TransferQueue, tq_id))
    es = run(get_es_by_media(db, mid))
    assert tq.status == "pending"
    assert tq.save_task_id is None      # 关键：人工 retry 清空幂等标记
    assert tq.save_attempt_at is None   # 受理时间一并清空（同生同灭）
    assert tq.quota_reject_count == 0
    assert es.state == "queued"
    assert es.retry_count == 0

    # 下一轮消费：重新走完整转存链 → 重新 save（而非跳过 save 盲等）
    run(transfer_mod.process_transfer_queue())
    assert len(env["cloudsaver"].save_calls) == 1
    tq = run(read_row(db, TransferQueue, tq_id))
    assert tq.status == "downloading"
    assert tq.save_task_id == "t1"


def test_stale_save_attempt_forces_resave(db, env, monkeypatch):
    """改动 2g：save_task_id 存在但 save_attempt_at 超 10 分钟 → 强制重新 save。

    P0-1 兜底断言：即使任一清空路径漏了，超时也会强制重 save（不盲等），
    且 save_task_id / save_attempt_at 更新为新的受理结果。
    """
    patch_db(monkeypatch, db)
    mid, es_id, tq_id = run(seed_pending(db))

    async def set_stale():
        async with db() as s:
            await s.execute(
                update(TransferQueue).where(TransferQueue.id == tq_id).values(
                    save_task_id="stale-t1",
                    save_attempt_at=_now() - timedelta(minutes=11),  # 超 600s
                )
            )
            await s.commit()

    run(set_stale())

    run(transfer_mod.process_transfer_queue())

    # 强制重新 save（不盲等），受理标记更新为新一轮结果
    assert len(env["cloudsaver"].save_calls) == 1
    tq = run(read_row(db, TransferQueue, tq_id))
    assert tq.save_task_id == "t1"
    assert tq.save_attempt_at is not None
    assert (tq.save_attempt_at - _now()).total_seconds() > -60  # 刚更新（容差）
    assert tq.status == "downloading"
    assert run(get_es_by_media(db, mid)).state == "downloading"


def test_fresh_save_attempt_keeps_idempotent_skip(db, env, monkeypatch):
    """改动 2g 对照：save_task_id 存在且受理未超时 → 保持幂等跳过 save（不重复转存）。"""
    patch_db(monkeypatch, db)
    mid, es_id, tq_id = run(seed_pending(db))

    async def set_fresh():
        async with db() as s:
            await s.execute(
                update(TransferQueue).where(TransferQueue.id == tq_id).values(
                    save_task_id="t1",
                    save_attempt_at=_now(),
                )
            )
            await s.commit()

    run(set_fresh())

    run(transfer_mod.process_transfer_queue())

    assert len(env["cloudsaver"].save_calls) == 0  # 幂等：跳过 save 直接等落盘
    tq = run(read_row(db, TransferQueue, tq_id))
    assert tq.status == "downloading"
    assert tq.save_task_id == "t1"


def test_complete_double_table_lost_rolls_back_pending(db, env, monkeypatch):
    """改动 3i：_complete_download 双表失联（tq 已被并发回退 pending）→ 显式回退。

    P0-2 关键断言：tq 回退/保持 pending 且 save_task_id 被清空——下一轮重新走
    完整转存链（重新 save），而非「只置 dl complete 就静默丢弃」。
    """
    patch_db(monkeypatch, db)
    mid, es_id, tq_id, dl_id = run(seed_downloading(db))
    # 模拟双表失联：tq 已被 recovery/人工回退为 pending，且 save_task_id 残留
    async def desync():
        async with db() as s:
            await s.execute(
                update(TransferQueue).where(TransferQueue.id == tq_id).values(
                    status="pending", save_task_id="stale-t1",
                )
            )
            await s.commit()

    run(desync())
    env["aria2"].statuses["gid1"] = "complete"

    # 只跑阶段 A，便于断言回退中间态（阶段 B 会立即把回退任务重新转存）
    run(transfer_mod._poll_downloading_tasks())

    tq = run(read_row(db, TransferQueue, tq_id))
    es = run(get_es_by_media(db, mid))
    dl = run(read_row(db, DownloadTask, dl_id))
    assert tq.status == "pending"       # 保持可重试
    assert tq.save_task_id is None      # 关键：失联回退清空幂等标记
    assert es.state == "queued"         # downloading → queued
    assert dl.status == "complete"      # 中介终态（下载确已完成）
    # 双表失联 → 不发完成通知、不触发 nasTools 同步
    assert not [e for e in env["notifier"].events if e.event_type == "download_complete"]
    assert env["spawn"] == []

    # 下一轮消费：阶段 B 取到回退的 pending → 重新走完整转存链（重新 save）
    run(transfer_mod.process_transfer_queue())
    assert len(env["cloudsaver"].save_calls) == 1


def test_complete_marks_done_before_removing_quark(db, env, monkeypatch):
    """改动 3h：_complete_download 先双表 done（success task_run）再删夸克。

    P0-2 顺序断言：记录顺序为 record:success → remove（先 done 后删夸克），
    防止「先删后 done 校验失败 → 文件已删但状态未推进 → 重试时无文件可取」。
    """
    patch_db(monkeypatch, db)
    mid, es_id, tq_id, dl_id = run(seed_downloading(db))
    env["aria2"].statuses["gid1"] = "complete"

    order: list = []
    orig_remove = env["alist"].remove

    async def spy_remove(names, dir):
        order.append("remove")
        return await orig_remove(names, dir)

    env["alist"].remove = spy_remove
    orig_record = transfer_mod.record_task_run

    async def spy_record(s, task_type, status, message, media_id=None):
        order.append(f"record:{status}")
        return await orig_record(s, task_type, status, message, media_id)

    monkeypatch.setattr(transfer_mod, "record_task_run", spy_record)

    run(transfer_mod._poll_downloading_tasks())

    # 双表 done 的 success task_run 必须先于 alist.remove（先 done 后删夸克）
    assert order == ["record:success", "remove"]
    assert (["ep.mkv"], "/quark/") in env["alist"].remove_calls