"""council P1-1 / P1-3 / P1-5 修复单测（fix-9/fix-10 收尾）。

- P1-1（需求 4）：Emby 防重三入口——create_media / create_approval /
  approve_approval 在本地 tmdb 查重之后、写库之前追加 Emby 查重：
  命中（find_emby_id 非 None）→ 409「该影视已在 Emby 媒体库，无需重复订阅」；
  EmbyUnavailable（含「未配置」，抛自 _check_config）→ fail-open 仅告警放行；
  本地 tmdb 查重回归（本地命中在前且不再调用 find_emby_id；tmdb_id=None 跳过）。
- P1-3：retry_task 对 scrape/library 失败终态的联动——es.node='failed'（新契约）
  且 tq.status='done'（_complete_download 置的）时，retry 重置 es
  idle/queued/retry_count=0 并联动 tq→pending（WHERE status IN ('failed','done')），
  之后可被 _process_one_pending 取走（tq.pending + es idle/queued → 进入转存链）。
- P1-5：_resolve_done_states 达循环上限分支写 node='failed'/state='failed' 终态
  （node_error 非空 / node_attempt 达上限值 / retry_count 不消耗），使 queue.py
  retry_task 可人工解锁（此前保持 done 仅写 error，retry 只认 failed 无法干预卡死）。

DB 用隔离 in-memory SQLite（StaticPool 共享连接，Base.metadata.create_all），
外部依赖全部 monkeypatch（app.services.emby.find_emby_id / notifier /
trigger_scan_background / transfer 模块内 aria2/cloudsaver/alist/capacity/notifier/
async_session/trigger_transfer），不连任何真实外部服务。
"""
import asyncio
import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  注册全部 ORM 模型
import app.routers.queue as queue_mod
import app.tasks.scan as scan_mod
import app.tasks.transfer as transfer_mod
from app.database import Base
from app.models import DownloadQueue, EpisodeState, Media, TransferQueue, WatchRequest
from app.routers.approvals import WatchRequestCreate, approve_approval, create_approval
from app.routers.media import MediaCreate, create_media
from app.services.emby import EmbyUnavailable

# 与三个入口共用的 409 文案（结构与 test_approval_dup.DUP_DETAIL 同源，改动时须同步）
LOCAL_DUP_DETAIL = "该影视已在影视库，无需重复提交"
EMBY_DUP_DETAIL = "该影视已在 Emby 媒体库，无需重复订阅"

# guest/admin 的 id 会落库（requested_by / reviewed_by），须是真实 int——
# MagicMock().id 是嵌套 MagicMock，SQLite 绑定参数会报 ProgrammingError
AKA = types.SimpleNamespace(id=1)


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


class FakeAria2:
    """aria2.client：tell_active（GID 校验）/ add_uri。"""

    def __init__(self):
        self.add_uri_calls = []

    async def tell_active(self):
        return []

    async def add_uri(self, link, **kwargs):
        self.add_uri_calls.append((link, kwargs))
        return "gid-p1"


class FakeCloudSaver:
    def __init__(self):
        self.save_calls = []

    async def save(self, params):
        self.save_calls.append(dict(params))
        return {"task_id": "t-p1"}


class FakeAlist:
    def __init__(self):
        self.link = "http://alist.test/raw/ep.mkv"

    async def diagnose_quark_mount(self):
        # /quark 挂载预检通过（configured == root）
        return {"match": True, "root_folder_id": "root", "configured_folder_id": "root"}

    async def get_link(self, path):
        return self.link


class FakeCapacity:
    def __init__(self):
        self.check_calls = 0

    async def check(self, candidate_bytes):
        self.check_calls += 1
        return True


class FakeNotifier:
    def __init__(self):
        self.events = []

    async def notify(self, event):
        self.events.append(event)


@pytest.fixture()
def transfer_env(monkeypatch):
    """替换 transfer 模块内依赖引用（含 async_session 之外的网络/通知服务）。"""
    fakes = {
        "aria2": FakeAria2(),
        "cloudsaver": FakeCloudSaver(),
        "alist": FakeAlist(),
        "capacity": FakeCapacity(),
        "notifier": FakeNotifier(),
    }
    monkeypatch.setattr(transfer_mod, "aria2", types.SimpleNamespace(client=fakes["aria2"]))
    monkeypatch.setattr(transfer_mod, "cloudsaver", fakes["cloudsaver"])
    monkeypatch.setattr(transfer_mod, "alist", fakes["alist"])
    monkeypatch.setattr(transfer_mod, "capacity", types.SimpleNamespace(provider=fakes["capacity"]))
    monkeypatch.setattr(transfer_mod, "notifier", fakes["notifier"])
    # P2-6 非终态回退的 _spawn：跟踪不执行（asyncio.run 每轮关闭事件循环）
    monkeypatch.setattr(transfer_mod, "_spawn", lambda factory: None)
    return fakes


async def read_row(db, model, obj_id):
    async with db() as s:
        return await s.get(model, obj_id)


async def get_es_by_media(db, media_id):
    async with db() as s:
        return (
            await s.execute(select(EpisodeState).where(EpisodeState.media_id == media_id))
        ).scalars().first()


async def get_dq_by_media(db, media_id):
    """影视下载两队列重设计：取 media 下第一条 download_queue（等价旧 get_es_by_media）。"""
    async with db() as s:
        return (
            await s.execute(select(DownloadQueue).where(DownloadQueue.media_id == media_id))
        ).scalars().first()


def _seed_media(db, *, tmdb_id: int) -> None:
    async def _seed():
        async with db() as s:
            s.add(Media(
                title=f"已有影视{tmdb_id}", tmdb_id=tmdb_id,
                media_type="movie", status="tracking",
            ))
            await s.commit()

    run(_seed())


def _seed_wr(db, tmdb_id: int) -> int:
    async def _seed():
        async with db() as s:
            wr = WatchRequest(
                requested_by=1, title=f"想看{tmdb_id}", tmdb_id=tmdb_id,
                media_type="movie", status="pending",
            )
            s.add(wr)
            await s.commit()
            return wr.id

    return run(_seed())


async def _seed_scrape_failed(db, *, episode="S01E01"):
    """scrape 失败终态：download_queue status='failed'（node_error 保留诊断）。

    DownloadQueue 单表承接旧 es(node='failed') + tq(status='done') 的职责；保留
    save_task_id（下载完成的成功路径保持幂等标记）——P1-3 修复前 retry 无法联动的
    卡死现场在新语义下由「failed 终态」表达（queue.retry_task 对 failed/skipped 解锁）。
    """
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=42, status="tracking")
        s.add(media)
        await s.flush()
        mid = media.id
        dq = DownloadQueue(
            media_id=mid, episode=episode, status="failed",
            node_attempt=3, node_error="刮削失败: xxx", retry_count=3,
            file_name="ep.mkv", file_size=1024, share_code="sc1", stoken="st",
            receive_code="rc", fids='["f1"]', fid_tokens='["ft1"]',
            folder_id="folder-1", quota_reject_count=2,
            save_task_id="stale-t1", save_attempt_at=_now(), error="刮削失败: xxx",
            updated_at=_now(),
        )
        s.add(dq)
        await s.flush()
        await s.commit()
        return mid, dq.id


async def _seed_done_at_limit(db, *, episode="S01E01", retry_count=3):
    """done 防重场景：download_queue status='done'（retry_count 可指定）。"""
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=42, status="tracking")
        s.add(media)
        await s.flush()
        mid = media.id
        dq = DownloadQueue(
            media_id=mid, episode=episode, status="done",
            retry_count=retry_count, file_name="ep.mkv", file_size=1024,
            share_code="sc1", stoken="st", receive_code="rc",
            fids='["f1"]', fid_tokens='["ft1"]', folder_id="folder-1",
            quota_reject_count=2, updated_at=_now(),
        )
        s.add(dq)
        await s.flush()
        await s.commit()
        return mid, dq.id


# ---------------------------------------------------------------------------
# P1-1：Emby 防重三入口
# ---------------------------------------------------------------------------

def test_create_media_emby_present_still_creates(db, monkeypatch):
    """create_media：Emby 中已存在该影视也不再 409（需求移除 Emby 防重），正常创建。"""
    monkeypatch.setattr("app.services.emby.find_emby_id", AsyncMock(return_value="e100"))

    async def _case():
        async with db() as s:
            dto = await create_media(
                payload=MediaCreate(title="重复影视", tmdb_id=42, media_type="movie"),
                admin=MagicMock(),
                session=s,
            )
            assert dto["tmdb_id"] == 42  # Emby 命中不再拦截，落库成功
        async with db() as s:
            row = (await s.execute(select(Media))).scalars().first()
            assert row is not None and row.tmdb_id == 42

    run(_case())


def test_create_media_emby_unavailable_fail_open(db, monkeypatch):
    """create_media：Emby 防重已移除，EmbyUnavailable 不再影响创建（fail-open 语义保持）。"""
    monkeypatch.setattr(
        "app.services.emby.find_emby_id",
        AsyncMock(side_effect=EmbyUnavailable("EMBY_API_KEY 未配置")),
    )

    async def _case():
        async with db() as s:
            dto = await create_media(
                payload=MediaCreate(title="新影视", tmdb_id=43, media_type="movie"),
                admin=MagicMock(),
                session=s,
            )
            assert dto["tmdb_id"] == 43

    run(_case())


def test_create_media_local_dup_short_circuits_emby(db, monkeypatch):
    """P1-1 回归：本地 tmdb 查重仍在——本地命中优先 409（Emby 防重已移除，仍不调 Emby）。"""
    _seed_media(db, tmdb_id=42)
    find = AsyncMock(return_value="e100")
    monkeypatch.setattr("app.services.emby.find_emby_id", find)

    async def _case():
        async with db() as s:
            with pytest.raises(HTTPException) as ei:
                await create_media(
                    payload=MediaCreate(title="重复影视", tmdb_id=42, media_type="movie"),
                    admin=MagicMock(),
                    session=s,
                )
            assert ei.value.status_code == 409
            assert ei.value.detail == LOCAL_DUP_DETAIL  # 本地文案
            assert find.await_count == 0  # 不再调用 Emby 防重

    run(_case())


def test_create_media_without_tmdb_skips_emby(db, monkeypatch):
    """create_media：tmdb_id=None 正常创建（Emby 防重已移除，此路径本就放行）。"""
    find = AsyncMock(return_value="e100")
    monkeypatch.setattr("app.services.emby.find_emby_id", find)

    async def _case():
        async with db() as s:
            dto = await create_media(
                payload=MediaCreate(title="无tmdb影视", tmdb_id=None, media_type="tv"),
                admin=MagicMock(),
                session=s,
            )
            assert dto["tmdb_id"] is None
            assert find.await_count == 0

    run(_case())


def test_create_approval_emby_hit_409(db, monkeypatch):
    """create_approval：find_emby_id 命中 → 409「已在 Emby 媒体库」。"""
    monkeypatch.setattr("app.services.emby.find_emby_id", AsyncMock(return_value="e100"))
    monkeypatch.setattr("app.routers.approvals.notifier.notify", AsyncMock())

    async def _case():
        async with db() as s:
            with pytest.raises(HTTPException) as ei:
                await create_approval(
                    payload=WatchRequestCreate(title="重复想看", tmdb_id=42, media_type="movie"),
                    user=AKA,
                    session=s,
                )
            assert ei.value.status_code == 409
            assert ei.value.detail == EMBY_DUP_DETAIL

    run(_case())


def test_create_approval_emby_unavailable_fail_open(db, monkeypatch):
    """create_approval：EmbyUnavailable → fail-open 放行，正常写入 pending。"""
    monkeypatch.setattr(
        "app.services.emby.find_emby_id",
        AsyncMock(side_effect=EmbyUnavailable("EMBY_BASE_URL 未配置")),
    )
    monkeypatch.setattr("app.routers.approvals.notifier.notify", AsyncMock())

    async def _case():
        async with db() as s:
            res = await create_approval(
                payload=WatchRequestCreate(title="新想看", tmdb_id=43, media_type="tv"),
                user=AKA,
                session=s,
            )
            wr = await s.get(WatchRequest, res["id"])
            assert wr.tmdb_id == 43 and wr.status == "pending"

    run(_case())


def test_approve_approval_emby_hit_409_keeps_pending(db, monkeypatch):
    """approve_approval：find_emby_id 命中 → 409 且 WatchRequest 未被消费（仍 pending）。"""
    monkeypatch.setattr("app.services.emby.find_emby_id", AsyncMock(return_value="e100"))
    monkeypatch.setattr("app.routers.approvals.notifier.notify", AsyncMock())
    trigger_calls: list = []
    monkeypatch.setattr(
        "app.tasks.scan.trigger_scan_background",
        lambda media_id: trigger_calls.append(media_id),
    )
    wr_id = _seed_wr(db, tmdb_id=42)

    async def _case_dup():
        async with db() as s:
            with pytest.raises(HTTPException) as ei:
                await approve_approval(wr_id, admin=AKA, session=s)
            assert ei.value.status_code == 409
            assert ei.value.detail == EMBY_DUP_DETAIL
        # 独立会话复查：wr 保持 pending，管理员可另行 reject
        async with db() as s:
            assert (await s.get(WatchRequest, wr_id)).status == "pending"

    run(_case_dup())
    assert trigger_calls == []  # 409 在事务副作用之前抛出，绝不触发巡检


def test_approve_approval_emby_unavailable_fail_open(db, monkeypatch):
    """approve_approval：EmbyUnavailable → fail-open 放行，正常批准。"""
    monkeypatch.setattr(
        "app.services.emby.find_emby_id",
        AsyncMock(side_effect=EmbyUnavailable("Emby 请求失败: timeout")),
    )
    monkeypatch.setattr("app.routers.approvals.notifier.notify", AsyncMock())
    trigger_calls: list = []
    monkeypatch.setattr(
        "app.tasks.scan.trigger_scan_background",
        lambda media_id: trigger_calls.append(media_id),
    )
    wr_id = _seed_wr(db, tmdb_id=43)

    async def _case():
        async with db() as s:
            res = await approve_approval(wr_id, admin=AKA, session=s)
            assert res["ok"] is True
        async with db() as s:
            wr = await s.get(WatchRequest, wr_id)
            assert wr.status == "approved"
            media = (
                await s.execute(select(Media).where(Media.tmdb_id == 43))
            ).scalars().first()
            assert media is not None and media.status == "tracking"
        assert trigger_calls == [res["media_id"]]

    run(_case())


# ---------------------------------------------------------------------------
# P1-3：retry_task 联动 scrape/library 失败终态（es.node='failed' + tq='done'）
# ---------------------------------------------------------------------------

def test_retry_scrape_failed_done_tq_resets_and_processes(db, transfer_env, monkeypatch):
    """构造 DownloadQueue status='failed'（scrape 失败终态）的任务：

    retry 后 dq → status='pending'/retry_count=0/node_attempt=0（并清空
    save_task_id/save_attempt_at/error），且可被 process_transfer_queue 取走
    （进入转存链，cloudsaver.save 被调用）。
    """
    monkeypatch.setattr(transfer_mod, "async_session", db)
    # retry_task commit 后内部的 trigger_transfer（延迟导入取 patch 后引用）
    monkeypatch.setattr(transfer_mod, "trigger_transfer", AsyncMock(return_value=None))
    mid, dq_id = run(_seed_scrape_failed(db))

    async def do_retry():
        async with db() as s:
            return await queue_mod.retry_task(
                task_id=dq_id, admin=types.SimpleNamespace(role="admin"), session=s,
            )

    assert run(do_retry()) == {"ok": True}

    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "pending"
    assert dq.save_task_id is None       # 重置清空幂等标记防盲等
    assert dq.save_attempt_at is None
    assert dq.node_attempt == 0
    assert dq.node_error is None
    assert dq.retry_count == 0
    assert dq.error is None

    # 下一轮消费：可被 process_transfer_queue 取走（进入转存链 = 修复闭环）
    run(transfer_mod.process_transfer_queue())
    assert len(transfer_env["cloudsaver"].save_calls) == 1
    assert run(read_row(db, DownloadQueue, dq_id)).status == "downloading"


def test_retry_old_state_failed_still_works(db, transfer_env, monkeypatch):
    """P1-3 回归：旧数据 state='failed'（node 为默认值 idle）重置路径不受影响。"""
    monkeypatch.setattr(transfer_mod, "async_session", db)
    monkeypatch.setattr(transfer_mod, "trigger_transfer", AsyncMock(return_value=None))

    async def seed():
        async with db() as s:
            media = Media(title="测试剧", media_type="tv", tmdb_id=42, status="tracking")
            s.add(media)
            await s.flush()
            mid = media.id
            s.add(EpisodeState(media_id=mid, episode="S01E01", state="failed",
                               retry_count=3, error="e", file_name="ep.mkv",
                               file_size=1024, share_code="sc1", updated_at=_now()))
            await s.flush()
            s.add(TransferQueue(media_id=mid, episode="S01E01", file_name="ep.mkv",
                                file_size=1024, share_code="sc1", stoken="st",
                                status="failed", error="e", updated_at=_now()))
            await s.commit()
            return mid

    mid = run(seed())

    async def do_retry():
        async with db() as s:
            es = (
                await s.execute(select(EpisodeState).where(EpisodeState.media_id == mid))
            ).scalars().first()
            return await queue_mod.retry_task(
                task_id=es.id, admin=types.SimpleNamespace(role="admin"), session=s,
            )

    assert run(do_retry()) == {"ok": True}
    es = run(get_es_by_media(db, mid))
    assert es.state == "queued" and es.node == "idle" and es.retry_count == 0


# ---------------------------------------------------------------------------
# P1-5：_resolve_done_states 达循环上限分支 → node='failed' 失败终态，可 retry
# ---------------------------------------------------------------------------

def test_done_limit_marks_failed_terminal_and_retryable(db, transfer_env, monkeypatch):
    """构造达上限场景（dq status='done' + retry_count=3，Emby 仍缺失）：

    resolve 后 dq → status='failed'（node_error 非空、node_attempt 达上限值、
    retry_count 不消耗）；随后 retry_task 可解锁（→ pending + 计数归零），
    并可被 process_transfer_queue 取走。
    """
    monkeypatch.setattr(scan_mod, "async_session", db)
    monkeypatch.setattr(transfer_mod, "async_session", db)
    monkeypatch.setattr(transfer_mod, "trigger_transfer", AsyncMock(return_value=None))
    mid, dq_id = run(_seed_done_at_limit(db, retry_count=3))

    # tv 模式：episode 仍在 Emby 缺失集 → 未入库 → done 防重判定
    run(scan_mod._resolve_done_states(types.SimpleNamespace(id=mid), {"S01E01"}, False))

    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "failed"
    assert dq.node_error == scan_mod._DONE_LIMIT_ERROR  # 非空上限文案
    assert dq.node_attempt == scan_mod._DONE_FAIL_RETRY_LIMIT  # 达上限值
    assert dq.error == scan_mod._DONE_LIMIT_ERROR
    assert dq.retry_count == 3  # 不再递增，上限语义保持

    # 可 retry：解锁（dq 以失败终态被定位 → 重置 pending）
    async def do_retry():
        async with db() as s:
            return await queue_mod.retry_task(
                task_id=dq.id, admin=types.SimpleNamespace(role="admin"), session=s,
            )

    assert run(do_retry()) == {"ok": True}
    dq2 = run(read_row(db, DownloadQueue, dq_id))
    assert dq2.status == "pending"
    assert dq2.node_attempt == 0
    assert dq2.node_error is None
    assert dq2.retry_count == 0
    assert dq2.error is None

    # 取走闭环（P1-3 + P1-5 联动后队列可消费）
    run(transfer_mod.process_transfer_queue())
    assert len(transfer_env["cloudsaver"].save_calls) == 1


def test_done_limit_resolve_is_idempotent_after_transition(db, monkeypatch):
    """P1-5：上限分支转 failed 后再次 resolve 不再改写（status 已非 done，天然幂等）。"""
    monkeypatch.setattr(scan_mod, "async_session", db)

    async def seed():
        async with db() as s:
            media = Media(title="测试剧", media_type="tv", tmdb_id=42, status="tracking")
            s.add(media)
            await s.flush()
            mid = media.id
            dq = DownloadQueue(media_id=mid, episode="S01E01", status="done",
                               retry_count=3, file_name="ep.mkv", file_size=1024,
                               share_code="sc1", stoken="st", receive_code="rc",
                               fids="[]", fid_tokens="[]", folder_id="fd",
                               updated_at=_now())
            s.add(dq)
            await s.commit()
            return mid, dq.id

    mid, dq_id = run(seed())

    run(scan_mod._resolve_done_states(types.SimpleNamespace(id=mid), {"S01E01"}, False))
    dq1 = run(read_row(db, DownloadQueue, dq_id))
    assert dq1.status == "failed" and dq1.node_error == scan_mod._DONE_LIMIT_ERROR
    first_ts = dq1.updated_at

    run(scan_mod._resolve_done_states(types.SimpleNamespace(id=mid), {"S01E01"}, False))
    dq2 = run(read_row(db, DownloadQueue, dq_id))
    assert dq2.status == "failed"
    assert dq2.updated_at == first_ts  # 第二轮 select(status='done') 不命中，无改写