"""transfer 下载队列消费单测（P5：旧三表 → DownloadQueue 单表 + 容量预算并发 + 回调推进）。

全部使用 fake 依赖（monkeypatch app.tasks.transfer 模块内的
aria2/cloudsaver/alist/capacity/notifier/scrape_runner/async_session），
不连任何真实外部服务/数据库。数据库用独立 in-memory SQLite（StaticPool 共享连接）。

验证场景（对应影视下载两队列重设计 §4.2/§5/§6.2）：
- 阶段 A：tell_status=complete → dq downloading→scrape + download_complete 通知
  + 刮削执行器触发 + 幂等（二次运行不重复）
- 阶段 A：active → 刷新 updated_at；error → retry_count 递增 → 第 3 次 failed + flow_error
- 阶段 B：容量 False → 保持 pending + quota_reject_count++ 且 retry_count 不变；
  容量异常（CapacityUnavailable）→ 保持 pending 且 quota_reject_count 不变 + flow_error
- 阶段 B：save 连续失败 3 次 → failed + retry_count=3
- 容量预算并发（§5）：准入无并发数上限（唯一约束=网盘容量）；reserved 聚合计入容量 check
- GID 校验：tell_active 返回陌生 comment 任务 → 整批跳过 + 不转存 + flow_error；
  本系统 comment 任务 → 不阻断正常转存提交；tell_active 故障 → fail-closed
- aria2 回调推进（§6.2）：trigger_download_complete 反查 gid → downloading→scrape；
  幂等（二次 False）；未知 gid → False
- P2：aria2 落盘名 = dq.download_name（缺失回退原始名）
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
from app.services.alist import AlistUnavailable as RealAlistUnavailable
from app.services.capacity import CapacityUnavailable


def run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# fake 服务（全部异步方法 + 调用记录）
# ---------------------------------------------------------------------------

class FakeAria2Client:
    """aria2.client：tell_status / tell_active / add_uri / remove。"""

    def __init__(self):
        self.statuses = {}        # gid -> "active"/"complete"/"error"...
        self.actives = []         # tell_active 返回值 [{gid,status,comment}]
        self.add_uri_calls = []   # [(uri, kwargs)]
        self.removed = []         # remove(gid)

    async def tell_status(self, gid):
        return {"status": self.statuses.get(gid, "active")}

    async def tell_active(self):
        return list(self.actives)

    async def add_uri(self, uri, **kwargs):
        self.add_uri_calls.append((uri, kwargs))
        return f"gid-{len(self.add_uri_calls)}"

    async def remove(self, gid):
        self.removed.append(gid)


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
        self.rename_calls = []  # [(path, new_name, overwrite)]
        self.list_dir_calls = []  # [path]
        self.link = "http://alist.test/raw/ep.mkv"

    async def remove(self, names, dir):
        self.remove_calls.append((list(names), dir))
        return {"success": True}

    async def rename(self, path, new_name, overwrite=True):
        self.rename_calls.append((path, new_name, overwrite))
        return {}

    async def get_link(self, path):
        return self.link

    async def list_dir(self, path):
        self.list_dir_calls.append(path)
        return [{"name": "ep.mkv", "is_dir": False, "size": 123}]


class FakeCapacityProvider:
    """capacity.provider：check 记录入参（reserved+file_size 聚合断言用）。"""

    def __init__(self):
        self.result = True
        self.raise_error = None
        self.check_calls = []  # 每次 check 的 candidate_bytes 入参

    async def check(self, candidate_bytes):
        self.check_calls.append(candidate_bytes)
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

    monkeypatch.setattr(transfer_mod, "aria2", types.SimpleNamespace(client=fakes["aria2"]))
    monkeypatch.setattr(transfer_mod, "cloudsaver", fakes["cloudsaver"])
    monkeypatch.setattr(transfer_mod, "alist", fakes["alist"])
    monkeypatch.setattr(transfer_mod, "capacity", types.SimpleNamespace(provider=fakes["capacity"]))
    monkeypatch.setattr(transfer_mod, "notifier", fakes["notifier"])
    # 刮削执行器触发用 _spawn——单测置「跟踪不执行」（验证触发行为，不真正跑刮削）
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
                       retry_count=0, quota_reject_count=0, download_name=None,
                       save_task_id=None, save_attempt_at=None, quark_path=None,
                       aria2_gid=None):
    """写入 media + download_queue(pending)。返回 (mid, dq_id)。"""
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=None, status="tracking")
        s.add(media)
        await s.flush()
        mid = media.id
        dq = DownloadQueue(
            media_id=mid, episode=episode, file_name=file_name, file_size=file_size,
            share_code=share_code, stoken=stoken, receive_code="提取码占位",
            fids=fids, fid_tokens=fid_tokens, folder_id=folder_id,
            download_name=download_name, status="pending",
            retry_count=retry_count, quota_reject_count=quota_reject_count,
            save_task_id=save_task_id, save_attempt_at=save_attempt_at,
            quark_path=quark_path, aria2_gid=aria2_gid,
            enqueued_at=_now(), updated_at=_now(),
        )
        s.add(dq)
        await s.flush()
        await s.commit()
        return mid, dq.id


async def seed_downloading(db, *, episode="S01E01", file_name="ep.mkv", gid="gid1",
                           retry_count=0, node_attempt=0, file_size=1024):
    """写入 media + download_queue(downloading)。返回 (mid, dq_id)。"""
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=None, status="downloading")
        s.add(media)
        await s.flush()
        mid = media.id
        dq = DownloadQueue(
            media_id=mid, episode=episode, file_name=file_name, file_size=file_size,
            share_code="sc123", stoken="stoken-x", fids='["f1"]',
            status="downloading", aria2_gid=gid, quark_path=f"/quark/{file_name}",
            retry_count=retry_count, node_attempt=node_attempt,
            node_started_at=_now(), updated_at=_now(),
        )
        s.add(dq)
        await s.flush()
        await s.commit()
        return mid, dq.id


async def read_row(db, model, obj_id):
    async with db() as s:
        return await s.get(model, obj_id)


async def get_dq_by_media(db, media_id):
    async with db() as s:
        return (
            await s.execute(select(DownloadQueue).where(DownloadQueue.media_id == media_id))
        ).scalars().first()


async def get_all_dq(db):
    async with db() as s:
        return (await s.execute(select(DownloadQueue).order_by(DownloadQueue.id))).scalars().all()


# ---------------------------------------------------------------------------
# 阶段 A：complete → downloading→scrape + 触发刮削
# ---------------------------------------------------------------------------

def test_poll_complete_enters_scrape_and_triggers_scrape(db, env, monkeypatch):
    """下载完成（§4.2/G6）：dq downloading→scrape（不置 done、不删夸克）；
    触发刮削执行器 + download_complete 通知。回调/轮询并发推进幂等（二次不重复）。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_downloading(db))
    env["aria2"].statuses["gid1"] = "complete"

    run(transfer_mod.process_transfer_queue())

    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "scrape"
    assert dq.node_attempt == 0
    assert dq.node_started_at is not None
    assert dq.node_finished_at is not None
    assert dq.node_error is None
    # G6：下载完成不删夸克（入库确认后才删，由后续 lane 执行），零删除调用
    assert env["alist"].remove_calls == []
    # download_complete 通知（全体）
    done_events = [e for e in env["notifier"].events if e.event_type == "download_complete"]
    assert len(done_events) == 1
    assert done_events[0].title == "下载完成: ep.mkv"
    assert done_events[0].extra["media_id"] == mid
    assert done_events[0].extra["episode"] == "S01E01"
    # 刮削执行器事件触发（L3，不阻塞转存链）；成功推进不 spawn process_transfer_queue
    # （容量预算并发下循环内续跑，见 test_admit_processes_multiple_pending）
    assert env["spawn"] == [transfer_mod.scrape_runner]
    # 幂等：二次运行不重复处理（dq 已 scrape，轮询不再命中 downloading）
    run(transfer_mod.process_transfer_queue())
    assert env["spawn"] == [transfer_mod.scrape_runner]  # 不再触发
    assert len([e for e in env["notifier"].events if e.event_type == "download_complete"]) == 1


def test_poll_active_refreshes_updated_at(db, env, monkeypatch):
    """active → 仍在下载：updated_at 被刷新（防 recover 超时误回退），状态不变。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_downloading(db))
    env["aria2"].statuses["gid1"] = "active"

    run(transfer_mod.process_transfer_queue())

    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "downloading"
    assert dq.updated_at is not None


def test_poll_error_retries_then_failed(db, env, monkeypatch):
    """aria2 error → 确定性失败路径（§4.2）：retry_count 递增 + 回退 pending + 清理残留。

    process_transfer_queue 两阶段依序执行：阶段 A 回退（retry 0→1）后阶段 B 立即
    取到同一任务重试（retry 1→2）→ 每轮 retry_count +2；第二轮不再命中 downloading，
    阶段 B 重试达上限（retry 2→3）转 failed。
    """
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_downloading(db, gid="gid-err"))
    env["aria2"].statuses["gid-err"] = "error"
    env["cloudsaver"].fail_save = CapacityUnavailable("分享已失效")

    # 第一轮：阶段 A retry 0→1 回退 pending + 清理残留；
    #        阶段 B 立即重试 → save 失败 → retry 1→2，仍回退 pending
    run(transfer_mod.process_transfer_queue())
    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.retry_count == 2
    assert dq.node_attempt == 2
    assert dq.status == "pending"
    assert env["alist"].remove_calls  # 回退前清理夸克残留（A 与 B 至少一次）

    # 第二轮：阶段 A 不再命中（status 已 pending）；阶段 B retry 2→3 → failed + flow_error
    run(transfer_mod.process_transfer_queue())
    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.retry_count == 3
    assert dq.status == "failed"
    assert any(e.event_type == "flow_error" for e in env["notifier"].events)


# ---------------------------------------------------------------------------
# 阶段 B：容量门槛（fail-closed，§5）
# ---------------------------------------------------------------------------

def test_quota_reject_keeps_pending_and_increments_reject(db, env, monkeypatch):
    """容量不足（P1 裁决）→ 置 quota_wait 幽灵态 + wait_since + quota_reject_count++。

    绝不消耗 retry/node_attempt（§4.5）；quota_wait 不再被取件命中（取件只认
    pending）。第二次消费入口唤醒回 pending 后仍容量不足 → 又置回 quota_wait。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_pending(db, retry_count=1, quota_reject_count=3))
    env["capacity"].result = False

    run(transfer_mod.process_transfer_queue())
    run(transfer_mod.process_transfer_queue())

    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "quota_wait"           # 容量不足 → quota_wait（非 pending）
    assert dq.wait_since is not None           # 进入时间已记录
    assert dq.quota_reject_count == 5          # 3 + 2
    assert dq.retry_count == 1                 # 容量拒绝绝不消耗 retry_count
    assert dq.node_attempt == 0                # 也不消耗 node_attempt
    assert env["cloudsaver"].save_calls == []  # 未转存


def test_quota_wait_wakeup_and_admit_on_capacity_free(db, env, monkeypatch):
    """容量释放后（P1 裁决）：消费入口统一唤醒 quota_wait → pending → 容量充足则推进。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_pending(db))
    env["capacity"].result = False

    # 第一轮：容量不足 → quota_wait
    run(transfer_mod.process_transfer_queue())
    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "quota_wait"
    assert dq.wait_since is not None
    assert dq.quota_reject_count == 1
    assert env["cloudsaver"].save_calls == []

    # 第二轮：容量释放（result=True）→ 入口唤醒 quota_wait→pending → 正常转存推进
    env["capacity"].result = True
    run(transfer_mod.process_transfer_queue())
    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "downloading"
    assert dq.wait_since is None  # 唤醒时清空
    assert len(env["cloudsaver"].save_calls) == 1  # 唤醒后被正常转存


def test_quota_unavailable_keeps_pending_without_inc(db, env, monkeypatch):
    """容量接口不可用（CapacityUnavailable）→ fail-closed：保持 pending + 不 ++ + flow_error。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_pending(db, quota_reject_count=2))
    env["capacity"].raise_error = CapacityUnavailable("容量接口不可用")

    run(transfer_mod.process_transfer_queue())

    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "pending"      # 容量不可用不置 quota_wait（无状态变更）
    assert dq.quota_reject_count == 2  # 不 ++
    assert dq.retry_count == 0  # 不耗 retry
    assert env["cloudsaver"].save_calls == []
    assert any(e.event_type == "flow_error" for e in env["notifier"].events)


# ---------------------------------------------------------------------------
# P0（议会裁决）：暂停开关落地（§8.2）
# ---------------------------------------------------------------------------

def test_pause_blocks_admission_then_resume_allows(db, env, monkeypatch):
    """暂停（system_config download_queue_paused=true）→ 本轮不取新任务（task_run
    skipped、无 add_uri、任务保持 pending、quota_wait 不唤醒）；恢复后正常取件。"""
    patch_db(monkeypatch, db)
    from app.models import SystemConfig
    mid, dq_id = run(seed_pending(db))

    async def _set_pause(value: bool):
        async with db() as s:
            await s.merge(SystemConfig(key="download_queue_paused", value="true" if value else "false"))
            await s.commit()

    # 暂停：不取件
    run(_set_pause(True))
    run(transfer_mod.process_transfer_queue())
    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "pending"  # 未被取件
    assert env["cloudsaver"].save_calls == []  # 未转存
    assert env["aria2"].add_uri_calls == []
    # task_run(skipped) 记录暂停语义
    async def _skipped_msgs():
        from app.models import TaskRun
        from sqlalchemy import select
        async with db() as s:
            rows = (await s.execute(select(TaskRun).where(
                TaskRun.status == "skipped", TaskRun.task_type == "transfer",
            ))).scalars().all()
            return [r.message for r in rows]
    assert any("队列已暂停" in m for m in run(_skipped_msgs()))

    # 恢复：正常取件
    run(_set_pause(False))
    run(transfer_mod.process_transfer_queue())
    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "downloading"
    assert len(env["cloudsaver"].save_calls) == 1


def test_pause_does_not_wake_quota_wait(db, env, monkeypatch):
    """暂停期间不唤醒 quota_wait（防暂停期间反复写，P0/P1 裁决时序）。"""
    patch_db(monkeypatch, db)
    from app.models import SystemConfig

    mid, dq_id = run(seed_pending(db))
    env["capacity"].result = False
    # 先置 quota_wait（未暂停时容量不足）
    run(transfer_mod.process_transfer_queue())
    assert run(read_row(db, DownloadQueue, dq_id)).status == "quota_wait"

    # 暂停中再跑消费：不唤醒 quota_wait（保持原状）
    async def _set_pause():
        async with db() as s:
            s.add(SystemConfig(key="download_queue_paused", value="true"))
            await s.commit()
    run(_set_pause())
    env["capacity"].result = True  # 即使容量已充足，暂停中也不唤醒
    run(transfer_mod.process_transfer_queue())
    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "quota_wait"
    assert env["cloudsaver"].save_calls == []


# ---------------------------------------------------------------------------
# 阶段 B：转存失败重试路径（save 失败 3 次 → failed）
# ---------------------------------------------------------------------------

def test_save_failure_retries_then_failed(db, env, monkeypatch):
    """三次连续失败（每轮重试后回退 pending，可再次被取到）→ failed + retry_count=3。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_pending(db))
    env["cloudsaver"].fail_save = CapacityUnavailable("分享已失效")

    for _ in range(3):
        run(transfer_mod.process_transfer_queue())

    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.retry_count == 3
    assert dq.status == "failed"
    assert dq.error  # 记录错误原因
    assert any(e.event_type == "flow_error" for e in env["notifier"].events)
    # 每轮失败都尝试清理夸克残留
    assert len(env["alist"].remove_calls) == 3

    # 已 failed → 无 pending；第四次为空跑，计数不再变
    run(transfer_mod.process_transfer_queue())
    assert run(read_row(db, DownloadQueue, dq_id)).retry_count == 3


def test_save_success_commits_download(db, env, monkeypatch):
    """正常转存链路：save → get_link → add_uri → dq downloading + gid/quark_path 落库。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_pending(db))
    env["aria2"].actives = []  # 无活动任务（GID 校验 gid 白名单口径：空列表直接放行）

    run(transfer_mod.process_transfer_queue())

    # receiveCode 必须用 stoken（双语义 G4），不是提取码
    assert len(env["cloudsaver"].save_calls) == 1
    params = env["cloudsaver"].save_calls[0]
    assert params["receiveCode"] == "stoken-x"
    assert params["shareCode"] == "sc123"
    assert params["fids"] == ["f1"]
    assert params["fidTokens"] == ["ft1"]
    assert params["folderId"] == "folder-1"
    # aria2 提交：out=落盘名（download_name 缺失回退原始名），comment=lumencloud:<media>:<episode>
    assert len(env["aria2"].add_uri_calls) == 1
    uri, kwargs = env["aria2"].add_uri_calls[0]
    assert uri == env["alist"].link
    assert kwargs["out"] == "ep.mkv"  # download_name 为 None → 回退原始名
    assert kwargs["comment"] == "lumencloud:1:S01E01"

    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "downloading"
    assert dq.aria2_gid == "gid-1"
    assert dq.quark_path == "/quark/ep.mkv"
    assert dq.local_path == "/downloads/ep.mkv"
    assert dq.node_attempt == 0  # 进入 downloading 节点重新计数
    assert dq.node_error is None


def test_download_started_notification_after_commit(db, env, monkeypatch):
    """P1（议会裁决 gamma）：addUri 成功落 downloading 后发出 download_started 通知。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_pending(
        db, file_name="Show.S01E02.1080p.mkv", download_name="测试剧 - S01E02 - 第 2 集.mkv",
    ))

    run(transfer_mod.process_transfer_queue())

    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "downloading"
    started = [e for e in env["notifier"].events if e.event_type == "download_started"]
    assert len(started) == 1
    assert started[0].title == "下载开始: 测试剧 - S01E02 - 第 2 集.mkv"  # 用落盘名
    assert started[0].extra["media_id"] == mid
    assert started[0].extra["episode"] == "S01E01"
    # 落盘名/本地路径与 download_name 对齐
    assert dq.quark_path == "/quark/Show.S01E02.1080p.mkv"
    assert dq.local_path == "/downloads/测试剧 - S01E02 - 第 2 集.mkv"


def test_transfer_renames_quark_after_save(db, env, monkeypatch):
    """用户需求：转存落盘后、取直链前，在 alist/quark 用 /api/fs/rename 改名
    （download_name 提供且与落盘真实名不同 → 改名；quark_path 落库新名，
    后续清理/删除按新名定位）。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_pending(
        db, file_name="ep.mkv", download_name="测试剧 - S01E02 - 第 2 集.mkv",
    ))

    run(transfer_mod.process_transfer_queue())

    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "downloading"
    # 改名调用：/quark/ep.mkv → 测试剧 - S01E02 - 第 2 集.mkv
    assert env["alist"].rename_calls == [
        ("/quark/ep.mkv", "测试剧 - S01E02 - 第 2 集.mkv", True)
    ]
    assert dq.quark_path == "/quark/测试剧 - S01E02 - 第 2 集.mkv"
    assert dq.local_path == "/downloads/测试剧 - S01E02 - 第 2 集.mkv"


def test_transfer_keeps_original_name_without_download_name(db, env, monkeypatch):
    """download_name 缺失（旧数据/异常）→ 不改名，quark_path 用原始名。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_pending(db, file_name="ep.mkv", download_name=None))

    run(transfer_mod.process_transfer_queue())

    assert env["alist"].rename_calls == []  # 无 download_name → 不触发改名
    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.quark_path == "/quark/ep.mkv"


def test_admit_processes_multiple_pending(db, env, monkeypatch):
    """容量预算并发（§5）：一次调用准入全部 pending（准入唯一约束=容量，无并发数上限），
    成功路径不再 spawn process_transfer_queue 续跑（循环内继续取件）。"""
    patch_db(monkeypatch, db)
    ids = []
    for ep in ("S01E01", "S01E02", "S01E03"):
        mid, dq_id = run(seed_pending(db, episode=ep))
        ids.append(dq_id)

    run(transfer_mod.process_transfer_queue())

    rows = run(get_all_dq(db))
    assert {r.status for r in rows} == {"downloading"}
    assert len(env["cloudsaver"].save_calls) == 3
    # 成功路径不 spawn（续跑由循环内取下一个 pending 完成，非后台触发）
    assert env["spawn"] == []
    # 二次运行：已全部 downloading → 无 pending，空跑不重复转存
    run(transfer_mod.process_transfer_queue())
    assert len(env["cloudsaver"].save_calls) == 3


# ---------------------------------------------------------------------------
# 阶段 B：容量预算并发（§5，准入无并发数上限，唯一约束 = 容量）
# ---------------------------------------------------------------------------

def test_inflight_statuses_exclude_downloading():
    """P1-4（议会裁决）：reserved 口径不含 downloading（已落盘由 used 覆盖，防双重计算）。

    _ACTIVE_STATUSES（media 处理中判定）**保持含 downloading**（另一语义，勿随
    reserved 收紧）。"""
    assert transfer_mod._INFLIGHT_STATUSES == ("transferring", "scrape", "library")
    assert "downloading" not in transfer_mod._INFLIGHT_STATUSES
    assert "downloading" in transfer_mod._ACTIVE_STATUSES          # media 处理中判定


def test_reserved_aggregation_included_in_capacity_check(db, env, monkeypatch):
    """reserved 聚合计入容量 check（§5.1）：check 入参 = 未落盘在途 file_size 和 + 本集。

    P1-4 收紧后：downloading 不计入 reserved（已落盘由 used 覆盖），
    仅 transferring/scrape/library 计入。"""
    patch_db(monkeypatch, db)
    # 1 个 transferring（未落盘在途，1000B，计入 reserved）
    run(seed_pending(db, episode="S01E01", file_size=1000))
    async def _to_transferring():
        async with db() as s:
            await s.execute(
                update(DownloadQueue).where(DownloadQueue.episode == "S01E01")
                .values(status="transferring", updated_at=_now())
            )
            await s.commit()
    run(_to_transferring())
    # 1 个 downloading（已落盘，1000B，**不计入** reserved）
    run(seed_downloading(db, episode="S01E02", gid="gid-b", file_size=1000))
    # 1 个 pending（500B）→ 准入时 check 入参应为 1000(transferring) + 500 = 1500
    run(seed_pending(db, episode="S01E03", file_size=500))

    run(transfer_mod.process_transfer_queue())

    assert env["capacity"].check_calls, "容量 check 应至少被调用一次"
    assert env["capacity"].check_calls[0] == 1500  # downloading 不计入 reserved
    # pending 正常准入（容量足）
    rows = run(get_all_dq(db))
    assert sum(1 for r in rows if r.status == "downloading") == 2


# ---------------------------------------------------------------------------
# 阶段 B：GID 来源校验兜底（§12.2 简化版）
# ---------------------------------------------------------------------------

def test_gid_source_check_blocks_foreign_task(db, env, monkeypatch):
    """存在陌生 aria2 活动任务（gid 不在本系统 download_queue 已签发集合）→ 整批跳过
    + 不转存 + quota/retry 计数不变。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_pending(db))
    env["aria2"].actives = [{"gid": "n8n-gid", "status": "active", "comment": "n8n:legacy"}]

    run(transfer_mod.process_transfer_queue())

    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "pending"            # 不处理
    assert dq.quota_reject_count == 0        # 不 ++ quota_reject
    assert dq.retry_count == 0               # 不耗 retry
    assert env["cloudsaver"].save_calls == []  # 不转存
    assert any(e.event_type == "flow_error" for e in env["notifier"].events)


def test_gid_source_check_accepts_known_gid(db, env, monkeypatch):
    """aria2 活动任务 gid 在本系统 download_queue 已签发集合内（downloading 行）→
    不阻断转存（2026-09 修订：gid 白名单口径，不依赖 aria2 comment）。"""
    patch_db(monkeypatch, db)
    run(seed_downloading(db, gid="own-1", file_name="已知剧集.mkv"))
    mid, dq_id = run(seed_pending(db))
    env["aria2"].actives = [{"gid": "own-1", "status": "active", "comment": "lumencloud:9:S02E03"}]

    run(transfer_mod.process_transfer_queue())

    assert len(env["cloudsaver"].save_calls) == 1  # 正常转存
    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "downloading"


def test_gid_check_failure_blocks_round(db, env, monkeypatch):
    """tell_active 故障 → fail-closed：整批跳过 + flow_error，不转存。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_pending(db))

    async def boom():
        raise RuntimeError("aria2 RPC 不可用")

    env["aria2"].tell_active = boom
    run(transfer_mod.process_transfer_queue())

    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "pending"
    assert dq.quota_reject_count == 0
    assert dq.retry_count == 0
    assert env["cloudsaver"].save_calls == []
    assert any(e.event_type == "flow_error" for e in env["notifier"].events)


def test_no_pending_is_skipped(db, env, monkeypatch):
    """空队列 → task_run(skipped)，无副作用。"""
    patch_db(monkeypatch, db)
    run(transfer_mod.process_transfer_queue())
    assert env["cloudsaver"].save_calls == []
    assert env["notifier"].events == []


# ---------------------------------------------------------------------------
# 落盘等待超时上限（Q3：线上反馈大文件超时放宽兜底）
# ---------------------------------------------------------------------------

def test_link_wait_timeout_constant_and_default():
    """_LINK_WAIT_TIMEOUT=300s，且函数默认超时与常量一致（防魔法数漂移）。"""
    import inspect

    assert transfer_mod._LINK_WAIT_TIMEOUT == 300.0
    assert inspect.signature(
        transfer_mod._get_link_wait_visible
    ).parameters["timeout"].default == transfer_mod._LINK_WAIT_TIMEOUT


# ---------------------------------------------------------------------------
# P0-1（线上反馈「转存多次失败」）：save 受理但未落盘 → 失败重试路径清空 save_task_id
# ---------------------------------------------------------------------------

def test_link_timeout_clears_save_task_id_for_retry(db, env, monkeypatch):
    """save 返回 task_id 但文件始终不落盘（get_link 持续失败超时）→ 失败重试路径清空。

    P0-1 关键断言：失败回退后 DownloadQueue.save_task_id 被清空为 NULL——
    下一轮重试会重新 save（打破「已受理即跳过 save」的盲等死循环到重试上限）。

    同时验证超时诊断：抛错前会列 /quark 目录记录实际内容；异常消息含 folderId
    与 alist 管理 API /api/admin/storage/list 核对提示。
    """
    patch_db(monkeypatch, db)
    monkeypatch.setattr(transfer_mod, "_LINK_WAIT_TIMEOUT", 0.0)  # 单测不等 300s
    fake_store = types.SimpleNamespace(
        get=lambda key, default=None: "9b852b37f9fb4d11938046a6ab5356a7"
    )
    monkeypatch.setattr(transfer_mod, "config_store", fake_store)
    mid, dq_id = run(seed_pending(db))

    # cloudSaver 正常受理（返回 task_id=t1），但文件从未落盘 → get_link 一直失败
    async def always_fail(path):
        raise RuntimeError("object not found")

    env["alist"].get_link = always_fail

    # 第一轮：save 受理并把 task_id 落库 → get_link 超时 → 失败回退 + 清空 save_task_id
    run(transfer_mod.process_transfer_queue())
    dq = run(read_row(db, DownloadQueue, dq_id))
    assert len(env["cloudsaver"].save_calls) == 1
    assert dq.status == "pending"
    assert dq.save_task_id is None                    # 关键：失败重试路径清空幂等标记
    assert dq.retry_count == 1
    assert dq.node_attempt == 1
    assert dq.error is not None
    assert "folderId=" in dq.error                    # 超时消息带 folderId（诊断）
    assert "/api/admin/storage/list" in dq.error      # 含配置核对提示
    assert env["alist"].list_dir_calls == ["/quark"]  # 抛错前列目录（诊断）

    # 第二轮：save_task_id 已清空 → 重新 save（防死循环的核心行为，而非跳过 save 盲等）
    run(transfer_mod.process_transfer_queue())
    dq = run(read_row(db, DownloadQueue, dq_id))
    assert len(env["cloudsaver"].save_calls) == 2
    assert dq.save_task_id is None
    assert dq.retry_count == 2


# ---------------------------------------------------------------------------
# P0-1（council）：save_task_id 全链路清理 + save_attempt_at 超时兜底（P0-1）
# ---------------------------------------------------------------------------

def test_stale_save_attempt_forces_resave(db, env, monkeypatch):
    """改动 2g：save_task_id 存在但 save_attempt_at 超 10 分钟 → 强制重新 save。

    P0-1 兜底断言：即使任一清空路径漏了，超时也会强制重 save（不盲等），
    且 save_task_id / save_attempt_at 更新为新的受理结果。
    """
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_pending(
        db, save_task_id="stale-t1", save_attempt_at=_now() - timedelta(minutes=11),
    ))

    run(transfer_mod.process_transfer_queue())

    # 强制重新 save（不盲等），受理标记更新为新一轮结果
    assert len(env["cloudsaver"].save_calls) == 1
    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.save_task_id == "t1"
    assert dq.save_attempt_at is not None
    assert (dq.save_attempt_at - _now()).total_seconds() > -60  # 刚更新（容差）
    assert dq.status == "downloading"


def test_fresh_save_attempt_keeps_idempotent_skip(db, env, monkeypatch):
    """改动 2g 对照：save_task_id 存在且受理未超时 → 保持幂等跳过 save（不重复转存）。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_pending(db, save_task_id="t1", save_attempt_at=_now()))

    run(transfer_mod.process_transfer_queue())

    assert len(env["cloudsaver"].save_calls) == 0  # 幂等：跳过 save 直接等落盘
    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "downloading"
    assert dq.save_task_id == "t1"


# ---------------------------------------------------------------------------
# 下载完成幂等（回调/轮询并发推进，§6.2）
# ---------------------------------------------------------------------------

def test_complete_idempotent_when_already_promoted(db, env, monkeypatch):
    """下载完成已被并发方推进（dq 已 scrape）→ 轮询不再命中，零副作用（幂等）。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_downloading(db))

    # 模拟 aria2 回调已先推进：dq 已是 scrape
    async def _promote():
        async with db() as s:
            await s.execute(
                update(DownloadQueue).where(DownloadQueue.id == dq_id)
                .values(status="scrape", updated_at=_now())
            )
            await s.commit()
    run(_promote())

    env["aria2"].statuses["gid1"] = "complete"
    run(transfer_mod.process_transfer_queue())

    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "scrape"
    # 轮询查 downloading 不命中 → 不重复通知/触发刮削
    assert env["notifier"].events == []
    assert env["spawn"] == []


def test_complete_enters_scrape_without_removing_quark(db, env, monkeypatch):
    """G6/L3 新语义：下载完成仅推进到 scrape（node_attempt 归零），不删夸克。

    删除夸克移至「入库确认」（library 节点完成）后的后续 lane 执行。
    """
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_downloading(db))
    env["aria2"].statuses["gid1"] = "complete"

    run(transfer_mod._poll_downloading_tasks())

    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "scrape"
    assert dq.node_attempt == 0
    # 下载完成阶段不删夸克（G6：入库确认后才释放）
    assert env["alist"].remove_calls == []
    assert (["ep.mkv"], "/quark/") not in env["alist"].remove_calls


# ---------------------------------------------------------------------------
# P6：aria2 回调推进 trigger_download_complete（§6.2）
# ---------------------------------------------------------------------------

def test_trigger_download_complete_promotes_to_scrape(db, env, monkeypatch):
    """回调推进：按 aria2_gid 反查 downloading → downloading→scrape + 通知 + 触发刮削。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_downloading(db, gid="gid-1"))

    result = run(transfer_mod.trigger_download_complete("gid-1"))

    assert result is True
    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "scrape"
    assert dq.node_attempt == 0
    assert dq.node_finished_at is not None
    # 通知 + 刮削执行器触发（同轮询推进语义）
    done_events = [e for e in env["notifier"].events if e.event_type == "download_complete"]
    assert len(done_events) == 1
    assert done_events[0].extra["media_id"] == mid
    assert done_events[0].extra["episode"] == "S01E01"
    assert env["spawn"] == [transfer_mod.scrape_runner]


def test_trigger_download_complete_idempotent_second_false(db, env, monkeypatch):
    """幂等：二次回调（dq 已 scrape）→ 返回 False，不重复推进/通知/触发刮削。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_downloading(db, gid="gid-1"))

    assert run(transfer_mod.trigger_download_complete("gid-1")) is True
    # 二次回调：条件更新 downloading→scrape 命中 0 行 → False
    assert run(transfer_mod.trigger_download_complete("gid-1")) is False

    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "scrape"
    assert len([e for e in env["notifier"].events if e.event_type == "download_complete"]) == 1
    assert env["spawn"] == [transfer_mod.scrape_runner]


def test_trigger_download_complete_unknown_gid_false(db, env, monkeypatch):
    """未知 gid（查不到 downloading 任务）→ 返回 False（幂等，不推进）。"""
    patch_db(monkeypatch, db)
    run(seed_pending(db))  # 只有 pending，无 downloading

    assert run(transfer_mod.trigger_download_complete("unknown-gid")) is False
    assert env["notifier"].events == []
    assert env["spawn"] == []


def test_trigger_download_complete_empty_gid_false(db, env, monkeypatch):
    """空 gid → 直接返回 False（参数防护）。"""
    patch_db(monkeypatch, db)
    run(seed_downloading(db, gid="gid-1"))
    assert run(transfer_mod.trigger_download_complete("")) is False


# ---------------------------------------------------------------------------
# P2：aria2 落盘名格式化（影视下载两队列重设计 §7，对齐 n8n formatFileName）
# ---------------------------------------------------------------------------

def test_format_download_name_tv_sxxexx():
    """剧集 + SxxExx → 「剧名 - SxxExx - 第 N 集.ext」（n8n 格式）。"""
    assert transfer_mod._format_download_name(
        "Show.S01E02.1080p.mkv", "测试剧", "tv"
    ) == "测试剧 - S01E02 - 第 2 集.mkv"


def test_format_download_name_tv_three_digit_episode():
    """三位集数（E100）保留三位。"""
    assert transfer_mod._format_download_name(
        "Show.S01E100.mkv", "测试剧", "tv"
    ) == "测试剧 - S01E100 - 第 100 集.mkv"


def test_format_download_name_movie_title_ext():
    """电影 → 「剧名.ext」（统一格式化，去掉夸克杂乱分享名）。"""
    assert transfer_mod._format_download_name(
        "某某.2024.1080p.BluRay.mkv", "某某", "movie"
    ) == "某某.mkv"


def test_format_download_name_tv_no_sxxexx_keeps_original():
    """剧集但匹配不到 SxxExx → 保持原名（n8n fallback，不误改）。"""
    assert transfer_mod._format_download_name("ep.mkv", "测试剧", "tv") == "ep.mkv"


def test_format_download_name_tv_no_sxxexx_uses_episode_key():
    """纯数字分享文件名（190.mkv）+ episode_key → 「剧名 - S01E190 - 第 190 集.mkv」
    （下线反馈：不改名下载的裸文件名无法在媒体库识别集号）。"""
    assert transfer_mod._format_download_name(
        "190.mkv", "凡人修仙传", "tv", episode_key="S01E190"
    ) == "凡人修仙传 - S01E190 - 第 190 集.mkv"


def test_format_download_name_episode_key_fallback_other_cases():
    """episode_key 兜底：三位集号保留；非 SxxExx 键不误格式化。"""
    assert transfer_mod._format_download_name(
        "190.mkv", "测试剧", "tv", episode_key="S01E100"
    ) == "测试剧 - S01E100 - 第 100 集.mkv"
    # 键非 SxxExx（如全量模式文件名键）→ 保持原名，不产生残缺名
    assert transfer_mod._format_download_name(
        "movie-xyz.mkv", "测试剧", "tv", episode_key="movie:测试剧"
    ) == "movie-xyz.mkv"


def test_format_download_name_no_title_keeps_original():
    """标题缺失 → 保持原名（避免产生残缺名）。"""
    assert transfer_mod._format_download_name(
        "Show.S01E02.mkv", "", "tv"
    ) == "Show.S01E02.mkv"


def test_transfer_out_uses_download_name_or_original(db, env, monkeypatch):
    """P2 集成：addUri out 用 dq.download_name（scan promote 生成）；缺失回退原始名。

    quark_path 仍用原始名（quark 侧不动，防重键不受影响）。
    """
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_pending(
        db, file_name="Show.S01E02.1080p.mkv",
        download_name="测试剧 - S01E02 - 第 2 集.mkv",
    ))

    run(transfer_mod.process_transfer_queue())

    assert len(env["aria2"].add_uri_calls) == 1
    _, kwargs = env["aria2"].add_uri_calls[0]
    assert kwargs["out"] == "测试剧 - S01E02 - 第 2 集.mkv"  # 用 download_name
    assert kwargs["comment"] == "lumencloud:1:S01E01"
    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.quark_path == "/quark/Show.S01E02.1080p.mkv"  # 夸克侧原始名，防重键不受影响
    assert dq.local_path == "/downloads/测试剧 - S01E02 - 第 2 集.mkv"
