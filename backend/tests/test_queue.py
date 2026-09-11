"""P3 queue 控制面 API（影视下载两队列重设计 §8.2 / §8.1 树契约）单测。

直接调用 app.routers.queue 的路由函数（绕过 HTTP 层，注入 db session 与 fake admin），
外部服务（aria2/alist/scan 触发）一律 monkeypatch；in-memory SQLite。

覆盖：
- GET /api/queue 扁平任务列表：TaskQueue ∪ DownloadQueue 活跃行合一、终态剔除、
  凭据脱敏、同集去重（promote 遗留探测快照）；type=download 下载队列扁平列表
- 整条暂停：POST /queue/download/pause|resume + GET state（in_flight）
- 单任务：cancel（dq 清 aria2+删夸克+failed+tq done / tq done）、prioritize、skip
  （dq 行 / tq 行补写防重终态）、retry（dq failed/skipped→pending、tq error→pending、
  非可重试 409）、promote（ready→pending）、probe、tasks 加集、sort（up/down/top）、
  progress（tell_status 聚合/降级）
"""
import asyncio
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  注册全部 ORM 模型
import app.routers.queue as queue_mod
import app.tasks.scan as scan_mod
import app.tasks.transfer as transfer_mod
from app.models import DownloadQueue, Media, SystemConfig, TaskQueue


def run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _admin():
    return types.SimpleNamespace(role="admin")


def _guest():
    return types.SimpleNamespace(role="guest")


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
    """fake 外部服务：aria2（tell_status/remove）、alist（remove）、scan 触发、续跑触发。"""
    fakes = {
        "aria2": types.SimpleNamespace(
            client=types.SimpleNamespace(
                remove=AsyncMock(return_value={}),
                tell_status=AsyncMock(return_value={}),
            )
        ),
        "alist": types.SimpleNamespace(remove=AsyncMock(return_value={})),
        # trigger_scan_background 为同步 fire-and-forget 函数（scan.py def）
        "scan_trigger": MagicMock(),
        "consume": AsyncMock(return_value=None),
    }
    monkeypatch.setattr(queue_mod, "aria2", fakes["aria2"])
    monkeypatch.setattr(queue_mod, "alist", fakes["alist"])
    monkeypatch.setattr(scan_mod, "trigger_scan_background", fakes["scan_trigger"])
    monkeypatch.setattr(queue_mod, "_trigger_consume", fakes["consume"])
    return fakes


@pytest.fixture()
def transfer_env(monkeypatch, db):
    """把 transfer 模块的 async_session 指向测试库（_fetch_from_task_queue 直接调用）。"""
    monkeypatch.setattr(transfer_mod, "async_session", db)
    return db


# ---------------------------------------------------------------------------
# 种子数据 / 读取
# ---------------------------------------------------------------------------

async def seed_media(db, *, title="测试剧", media_type="tv", tmdb_id=None):
    async with db() as s:
        media = Media(title=title, media_type=media_type, tmdb_id=tmdb_id, status="tracking")
        s.add(media)
        await s.flush()
        await s.commit()
        return media.id


async def seed_dq(db, mid, *, episode="S01E01", status="pending", enqueued_at=None,
                  retry_count=0, node_attempt=0, share_code="ScAaBbCcDdEe",
                  quark_path="/quark/ep.mkv", aria2_gid="gid-1", error=None):
    async with db() as s:
        dq = DownloadQueue(
            media_id=mid, episode=episode, file_name="ep.mkv", file_size=1024,
            share_code=share_code, stoken="st", receive_code="rc", fids="[]",
            fid_tokens="[]", folder_id="fd", status=status, retry_count=retry_count,
            node_attempt=node_attempt, quark_path=quark_path, aria2_gid=aria2_gid,
            node_started_at=_now(), error=error, enqueued_at=enqueued_at or _now(),
            updated_at=_now(),
        )
        s.add(dq)
        await s.flush()
        await s.commit()
        return dq.id


async def seed_tq(db, mid, *, episode="S01E01", status="ready", share_code="TqXxYyZz1234",
                  probe_attempt=1, silent_until=None, error=None, created_at=None):
    async with db() as s:
        tq = TaskQueue(
            media_id=mid, episode=episode, file_name="ep.mkv", file_size=1024,
            share_code=share_code, pwd_id="pwd", stoken="st", receive_code="rc",
            fids="[]", fid_tokens="[]", folder_id="fd", status=status,
            probe_attempt=probe_attempt, silent_until=silent_until, error=error,
            created_at=created_at or _now(), updated_at=_now(),
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


async def read_config(db, key):
    async with db() as s:
        row = await s.get(SystemConfig, key)
        return row.value if row else None


# ---------------------------------------------------------------------------
# GET /api/queue 扁平任务列表（TaskQueue ∪ DownloadQueue 活跃行，终态剔除）
# ---------------------------------------------------------------------------

def test_list_flat_union_dq_tq_with_fields(db, env):
    """扁平列表：TaskQueue ∪ DownloadQueue 活跃行合一；每行含 title/episode/status/node。"""
    mid = run(seed_media(db))
    dq_id = run(seed_dq(db, mid, episode="S01E01", status="pending"))
    tq_ready = run(seed_tq(db, mid, episode="S01E02", status="ready"))
    tq_unmatched = run(seed_tq(db, mid, episode="S01E03", status="unmatched"))

    async def _case():
        async with db() as s:
            return await queue_mod.list_queue(user=_admin(), session=s)
    res = run(_case())
    rows = res["items"]

    assert res["total"] == 3
    assert len(rows) == 3
    by_ep = {r["episode"]: r for r in rows}
    # 扁平行：无 children / 聚合字段（非影视分组树）
    assert all("children" not in r and "aggregate_status" not in r for r in rows)
    for r in rows:
        assert r["media_id"] == mid and r["title"] == "测试剧"
        assert r["file_name"] == "ep.mkv" and r["file_size"] == 1024
        assert r["updated_at"] is not None and r["enqueued_at"] is not None
    # DQ 行：status=node=执行状态
    assert by_ep["S01E01"]["status"] == "pending" and by_ep["S01E01"]["node"] == "pending"
    # TQ 行：status=探测状态，node 无节点概念 → None
    assert by_ep["S01E02"]["status"] == "ready" and by_ep["S01E02"]["node"] is None
    assert by_ep["S01E03"]["status"] == "unmatched"


def test_list_flat_excludes_terminal_states(db, env):
    """终态剔除：done/failed/skipped 不出现在显示列表；活跃态（含 quota_wait/error/probing）保留。"""
    mid = run(seed_media(db))
    run(seed_dq(db, mid, episode="S01E01", status="done"))
    run(seed_dq(db, mid, episode="S01E02", status="failed"))
    run(seed_dq(db, mid, episode="S01E03", status="skipped"))
    run(seed_dq(db, mid, episode="S01E04", status="pending"))
    run(seed_dq(db, mid, episode="S01E05", status="quota_wait"))
    run(seed_dq(db, mid, episode="S01E06", status="transferring"))
    run(seed_dq(db, mid, episode="S01E07", status="downloading"))
    run(seed_dq(db, mid, episode="S01E08", status="scrape"))
    run(seed_dq(db, mid, episode="S01E09", status="library"))
    run(seed_tq(db, mid, episode="S01E10", status="done"))
    run(seed_tq(db, mid, episode="S01E11", status="error"))
    run(seed_tq(db, mid, episode="S01E12", status="probing"))

    async def _case():
        async with db() as s:
            return await queue_mod.list_queue(user=_admin(), session=s)
    res = run(_case())
    rows = res["items"]

    episodes = {r["episode"] for r in rows}
    assert res["total"] == 8
    assert episodes == {"S01E04", "S01E05", "S01E06", "S01E07", "S01E08", "S01E09",
                        "S01E11", "S01E12"}
    for term in ("S01E01", "S01E02", "S01E03", "S01E10"):
        assert term not in episodes


def test_list_flat_dedup_promoted_snapshot(db, env):
    """扁平去重（延续树视图 Playwright 修复）：同 (media, episode) 已有 DQ 活跃行
    → 不展示 task_queue 探测快照（防「同集显示两条」）。"""
    mid = run(seed_media(db))
    dq_id = run(seed_dq(db, mid, episode="S01E01", status="pending"))
    run(seed_tq(db, mid, episode="S01E01", status="ready"))  # promote 后遗留探测快照

    async def _case():
        async with db() as s:
            return await queue_mod.list_queue(user=_admin(), session=s)
    res = run(_case())
    rows = res["items"]

    assert res["total"] == 1
    assert len(rows) == 1
    assert rows[0]["id"] == dq_id and rows[0]["episode"] == "S01E01"
    assert rows[0]["status"] == "pending"


def test_list_flat_contract_fields_and_no_credentials(db, env):
    """扁平行契约：恰好 12 字段定界（含 share_code/size_estimated，无 share_url 键）；
    admin 明文 share_code；敏感凭据与树字段一律不返回。"""
    mid = run(seed_media(db))
    run(seed_dq(db, mid, episode="S01E01", status="downloading", share_code="AbCd1234XyZq"))
    run(seed_tq(db, mid, episode="S01E02", status="unmatched", share_code="TqXxYyZz1234"))

    async def _case():
        async with db() as s:
            return await queue_mod.list_queue(user=_admin(), session=s)
    res = run(_case())
    rows = res["items"]

    assert res["total"] == 2
    assert len(rows) == 2
    by_ep = {r["episode"]: r for r in rows}
    # admin 明文 share_code（12 位），size_estimated 契约键先行（Task 2 前恒 False）
    assert by_ep["S01E01"]["share_code"] == "AbCd1234XyZq"
    assert by_ep["S01E02"]["share_code"] == "TqXxYyZz1234"
    assert all(r["size_estimated"] is False for r in rows)
    for r in rows:
        assert set(r.keys()) == {"id", "media_id", "title", "episode", "status", "node",
                                 "file_name", "file_size", "share_code", "size_estimated",
                                 "updated_at", "enqueued_at"}
        for f in ("stoken", "receive_code", "fid_tokens", "pwd_id",
                  "folder_id", "fids", "tq_status", "silent_until", "share_code_tail",
                  "error", "children", "aggregate_status", "probe_counts", "scan_tasks"):
            assert f not in r


def test_list_download_flat_type_download(db, env):
    """?type=download → 下载队列扁平列表（§8.1 Tab；活跃态过滤：终态 done 不再返回）。"""
    mid = run(seed_media(db))
    dq_id = run(seed_dq(db, mid, episode="S01E01", status="downloading"))
    run(seed_dq(db, mid, episode="S01E02", status="done"))  # 终态被活跃态过滤剔除

    async def _case():
        async with db() as s:
            return await queue_mod.list_queue(user=_admin(), session=s, type="download")
    res = run(_case())
    rows = res["items"]
    assert res["total"] == 1
    assert len(rows) == 1
    assert rows[0]["id"] == dq_id
    assert rows[0]["media_title"] == "测试剧"
    assert rows[0]["status"] == "downloading" and rows[0]["enqueued_at"] is not None
    assert rows[0]["node_attempt"] == 0
    assert rows[0]["size_estimated"] is False


def test_list_share_code_admin_vs_guest(db, env):
    """share_code 脱敏契约（Task 1）：admin 两视图明文；guest 一律 null。

    flat 契约不输出 share_url 键（仅 download 视图携带），故 guest 脱敏断言
    分视图：flat 验 share_code None + 无 share_url 键；download 验两者均 None。
    """
    mid = run(seed_media(db))
    run(seed_dq(db, mid, episode="S01E01", status="pending", share_code="AbCd1234XyZq"))
    run(seed_tq(db, mid, episode="S01E02", status="ready", share_code="TqXxYyZz1234"))
    run(seed_dq(db, mid, episode="S01E03", status="downloading", share_code="AbCd1234XyZq"))

    async def _flat(user):
        async with db() as s:
            return await queue_mod.list_queue(user=user, session=s)

    async def _download(user):
        async with db() as s:
            return await queue_mod.list_queue(user=user, session=s, type="download")

    # admin flat：DQ/TQ 均明文 share_code；flat 契约无 share_url 键
    res = run(_flat(_admin()))
    by_ep = {r["episode"]: r for r in res["items"]}
    assert by_ep["S01E01"]["share_code"] == "AbCd1234XyZq"
    assert by_ep["S01E02"]["share_code"] == "TqXxYyZz1234"
    assert all("share_url" not in r for r in res["items"])

    # admin download：明文 share_code + share_url 由 share_code 构造
    res = run(_download(_admin()))
    row = next(r for r in res["items"] if r["episode"] == "S01E03")
    assert row["share_code"] == "AbCd1234XyZq"
    assert row["share_url"] == "https://pan.quark.cn/s/AbCd1234XyZq"

    # guest flat：share_code 全 null；无 share_url 键（不泄露凭据）
    res = run(_flat(_guest()))
    assert all(r["share_code"] is None for r in res["items"])
    assert all("share_url" not in r for r in res["items"])

    # guest download：share_code / share_url 均 null
    res = run(_download(_guest()))
    assert all(r["share_code"] is None and r["share_url"] is None for r in res["items"])


# ---------------------------------------------------------------------------
# 整条下载队列暂停 / 恢复 / 状态（§8.2）
# ---------------------------------------------------------------------------

def test_pause_resume_state(db, env):
    mid = run(seed_media(db))
    a = run(seed_dq(db, mid, episode="S01E01", status="downloading"))   # 在途
    b = run(seed_dq(db, mid, episode="S01E02", status="library"))       # 在途
    c = run(seed_dq(db, mid, episode="S01E03", status="pending"))       # 不在途

    async def _pause():
        async with db() as s:
            return await queue_mod.pause_download_queue(admin=_admin(), session=s)
    assert run(_pause()) == {"paused": True}
    async def _resume():
        async with db() as s:
            return await queue_mod.resume_download_queue(admin=_admin(), session=s)
    assert run(_resume()) == {"paused": False}
    async def _pause2():
        async with db() as s:
            return await queue_mod.pause_download_queue(admin=_admin(), session=s)
    run(_pause2())

    async def _state():
        async with db() as s:
            # 单测直接调用路由函数绕过 Depends：state 已降为 get_current_user（登录可读），
            # 参数名为 user 仅满足签名，role 权限校验由 HTTP 层依赖负责（test_queue_auth.py）
            return await queue_mod.download_queue_state(user=_admin(), session=s)
    state = run(_state())
    assert state["paused"] is True
    assert state["in_flight"] == 2  # downloading + library（pending 不计在途）


# ---------------------------------------------------------------------------
# cancel（§8.2：清 aria2 + 删夸克 + failed + tq done）
# ---------------------------------------------------------------------------

def test_cancel_dq_cleans_side_effects_and_marks_failed(db, env):
    mid = run(seed_media(db))
    tq_id = run(seed_tq(db, mid, episode="S01E01", status="pending"))
    dq_id = run(seed_dq(db, mid, status="downloading"))

    async def _cancel():
        async with db() as s:
            return await queue_mod.cancel_task(task_id=dq_id, admin=_admin(), session=s)
    assert run(_cancel()) == {"ok": True}

    dq = run(read_dq(db, dq_id))
    assert dq.status == "failed" and dq.error == "人工取消" and dq.node_error == "人工取消"
    # 事务提交后 best-effort：aria2 remove + 删夸克残留（reserved 由 DB 聚合自动释放）
    env["aria2"].client.remove.assert_awaited_once_with("gid-1")
    env["alist"].remove.assert_awaited_once_with(["ep.mkv"], "/quark/")
    # task_queue 同步 done（探测视图不再入队）
    assert run(read_tq(db, tq_id)).status == "done"


def test_cancel_aria2_failure_does_not_block(db, env):
    """cancel 时 aria2 不可用 → 仅告警不阻断（failed 已落库；§8.2 幂等兜底）。"""
    mid = run(seed_media(db))
    dq_id = run(seed_dq(db, mid, status="transferring"))
    env["aria2"].client.remove = AsyncMock(side_effect=RuntimeError("aria2 挂"))

    async def _cancel():
        async with db() as s:
            return await queue_mod.cancel_task(task_id=dq_id, admin=_admin(), session=s)
    assert run(_cancel()) == {"ok": True}
    assert run(read_dq(db, dq_id)).status == "failed"


def test_cancel_tq_task_sets_done(db, env):
    """探测视图取消：task_queue → done（不再探测）。"""
    mid = run(seed_media(db))
    tq_id = run(seed_tq(db, mid, status="pending"))

    async def _cancel():
        async with db() as s:
            return await queue_mod.cancel_task(task_id=tq_id, admin=_admin(), session=s)
    assert run(_cancel()) == {"ok": True}
    assert run(read_tq(db, tq_id)).status == "done"
    env["aria2"].client.remove.assert_not_awaited()


def test_cancel_unknown_404(db, env):
    async def _cancel():
        async with db() as s:
            return await queue_mod.cancel_task(task_id=99999, admin=_admin(), session=s)
    with pytest.raises(Exception) as ei:
        run(_cancel())
    assert ei.value.status_code == 404


# ---------------------------------------------------------------------------
# prioritize / skip / sort（§8.2）
# ---------------------------------------------------------------------------

def test_prioritize_pending_moves_to_front_by_enqueued_at(db, env):
    mid = run(seed_media(db))
    base = _now()
    a = run(seed_dq(db, mid, episode="S01E01", status="pending", enqueued_at=base))
    b = run(seed_dq(db, mid, episode="S01E02", status="pending", enqueued_at=base + timedelta(minutes=1)))
    c = run(seed_dq(db, mid, episode="S01E03", status="pending", enqueued_at=base + timedelta(minutes=2)))

    async def _prio():
        async with db() as s:
            await queue_mod.prioritize_task(task_id=c, admin=_admin(), session=s)
    run(_prio())

    async def _order():
        async with db() as s:
            rows = (await s.execute(
                select(DownloadQueue).where(DownloadQueue.media_id == mid,
                                            DownloadQueue.status == "pending")
                .order_by(DownloadQueue.enqueued_at.asc(), DownloadQueue.id.asc())
            )).scalars().all()
            return [r.id for r in rows]
    assert run(_order())[0] == c  # 置顶：enqueued_at 提前至最小-1


def test_prioritize_only_pending_or_quota_wait(db, env):
    mid = run(seed_media(db))
    dq_id = run(seed_dq(db, mid, status="downloading"))

    async def _prio():
        async with db() as s:
            await queue_mod.prioritize_task(task_id=dq_id, admin=_admin(), session=s)
    with pytest.raises(Exception) as ei:
        run(_prio())
    assert ei.value.status_code == 409


def test_skip_dq_marks_skipped_and_tq_done(db, env):
    mid = run(seed_media(db))
    tq_id = run(seed_tq(db, mid, episode="S01E01", status="ready"))
    dq_id = run(seed_dq(db, mid, episode="S01E01", status="pending"))

    async def _skip():
        async with db() as s:
            await queue_mod.skip_task(task_id=dq_id, admin=_admin(), session=s)
    run(_skip())

    dq = run(read_dq(db, dq_id))
    assert dq.status == "skipped"  # 防重终态（§8.2：防 scan 重新入队）
    assert run(read_tq(db, tq_id)).status == "done"


def test_skip_tq_without_dq_writes_seed_for_dedup(db, env):
    """探测视图跳过（无 download_queue 行）→ 按 tq 快照补写 skipped 防重终态。"""
    mid = run(seed_media(db))
    tq_id = run(seed_tq(db, mid, status="ready"))

    async def _skip():
        async with db() as s:
            await queue_mod.skip_task(task_id=tq_id, admin=_admin(), session=s)
    run(_skip())

    assert run(read_tq(db, tq_id)).status == "done"
    async def _dq_rows():
        async with db() as s:
            return (await s.execute(select(DownloadQueue))).scalars().all()
    rows = run(_dq_rows())
    assert len(rows) == 1 and rows[0].status == "skipped" and rows[0].episode == "S01E01"


def test_sort_pending_up_down_top(db, env):
    mid = run(seed_media(db))
    base = _now()
    a = run(seed_dq(db, mid, episode="S01E01", status="pending", enqueued_at=base))
    b = run(seed_dq(db, mid, episode="S01E02", status="pending", enqueued_at=base + timedelta(minutes=1)))
    c = run(seed_dq(db, mid, episode="S01E03", status="pending", enqueued_at=base + timedelta(minutes=2)))

    async def _order():
        async with db() as s:
            rows = (await s.execute(
                select(DownloadQueue).where(DownloadQueue.media_id == mid,
                                            DownloadQueue.status == "pending")
                .order_by(DownloadQueue.enqueued_at.asc(), DownloadQueue.id.asc())
            )).scalars().all()
            return [r.id for r in rows]

    async def _sort(task_id, direction):
        async with db() as s:
            await queue_mod.sort_task(task_id=task_id, body=types.SimpleNamespace(direction=direction),
                                      admin=_admin(), session=s)
    # c up → 与 b 交换
    run(_sort(c, "up"))
    assert run(_order()) == [a, c, b]
    # a down → 与 c 交换
    run(_sort(a, "down"))
    assert run(_order()) == [c, a, b]
    # 再 b top → 队首
    run(_sort(b, "top"))
    assert run(_order()) == [b, c, a]


# ---------------------------------------------------------------------------
# retry（§8.2：兼容 DownloadQueue / TaskQueue）
# ---------------------------------------------------------------------------

def test_retry_dq_failed_resets_to_pending(db, env):
    mid = run(seed_media(db))
    dq_id = run(seed_dq(db, mid, status="failed", retry_count=3, error="旧失败"))

    async def _retry():
        async with db() as s:
            return await queue_mod.retry_task(task_id=dq_id, admin=_admin(), session=s)
    assert run(_retry()) == {"ok": True}

    dq = run(read_dq(db, dq_id))
    assert dq.status == "pending"
    assert dq.retry_count == 0 and dq.node_attempt == 0
    assert dq.save_task_id is None and dq.error is None
    assert dq.node_started_at is None and dq.node_finished_at is None
    env["consume"].assert_awaited_once()  # 重试后触发下载队列消费


def test_retry_dq_skipped_allowed(db, env):
    """skipped 也是可重试终态（人工反悔放回 pending）。"""
    mid = run(seed_media(db))
    dq_id = run(seed_dq(db, mid, status="skipped"))

    async def _retry():
        async with db() as s:
            await queue_mod.retry_task(task_id=dq_id, admin=_admin(), session=s)
    run(_retry())
    assert run(read_dq(db, dq_id)).status == "pending"


def test_retry_dq_pending_rejected_409(db, env):
    mid = run(seed_media(db))
    dq_id = run(seed_dq(db, mid, status="pending"))

    async def _retry():
        async with db() as s:
            await queue_mod.retry_task(task_id=dq_id, admin=_admin(), session=s)
    with pytest.raises(Exception) as ei:
        run(_retry())
    assert ei.value.status_code == 409


def test_retry_tq_error_resets_to_pending(db, env):
    mid = run(seed_media(db))
    tq_id = run(seed_tq(db, mid, status="error", probe_attempt=3, error="探测失败"))

    async def _retry():
        async with db() as s:
            return await queue_mod.retry_task(task_id=tq_id, admin=_admin(), session=s)
    assert run(_retry()) == {"ok": True}

    tq = run(read_tq(db, tq_id))
    assert tq.status == "pending"
    assert tq.probe_attempt == 0 and tq.error is None and tq.silent_until is None


# ---------------------------------------------------------------------------
# promote / probe / tasks / progress（§8.2）
# ---------------------------------------------------------------------------

def test_promote_ready_to_pending_and_tq_done(db, env):
    mid = run(seed_media(db))
    tq_id = run(seed_tq(db, mid, status="ready", share_code="PrOmOtE123456"))

    async def _promote():
        async with db() as s:
            return await queue_mod.promote_task(task_id=tq_id, admin=_admin(), session=s)
    assert run(_promote()) == {"ok": True}

    assert run(read_tq(db, tq_id)).status == "done"
    async def _dq():
        async with db() as s:
            return (await s.execute(select(DownloadQueue))).scalars().first()
    dq = run(_dq())
    assert dq is not None and dq.status == "pending" and dq.episode == "S01E01"
    env["consume"].assert_awaited_once()


def test_promote_not_ready_rejected_409(db, env):
    mid = run(seed_media(db))
    tq_id = run(seed_tq(db, mid, status="pending"))

    async def _promote():
        async with db() as s:
            await queue_mod.promote_task(task_id=tq_id, admin=_admin(), session=s)
    with pytest.raises(Exception) as ei:
        run(_promote())
    assert ei.value.status_code == 409


def test_probe_media_triggers_scan_background(db, env):
    mid = run(seed_media(db))

    async def _probe():
        async with db() as s:
            return await queue_mod.probe_media(media_id=mid, admin=_admin(), session=s)
    assert run(_probe()) == {"ok": True}
    env["scan_trigger"].assert_called_once_with(mid, manual=True)


def test_add_task_creates_pending_and_triggers_scan(db, env):
    mid = run(seed_media(db))

    async def _add():
        async with db() as s:
            return await queue_mod.add_queue_task(
                body=types.SimpleNamespace(media_id=mid, episode="S01E05"),
                admin=_admin(), session=s)
    assert run(_add()) == {"ok": True}

    async def _tq():
        async with db() as s:
            return (await s.execute(select(TaskQueue))).scalars().first()
    tq = run(_tq())
    assert tq is not None and tq.status == "pending" and tq.episode == "S01E05"
    env["scan_trigger"].assert_called_once_with(mid, manual=True)


def test_add_task_existing_resets_pending(db, env):
    mid = run(seed_media(db))
    tq_id = run(seed_tq(db, mid, status="unmatched", silent_until=_now() + timedelta(days=2)))

    async def _add():
        async with db() as s:
            await queue_mod.add_queue_task(
                body=types.SimpleNamespace(media_id=mid, episode="S01E01"),
                admin=_admin(), session=s)
    run(_add())

    tq = run(read_tq(db, tq_id))
    assert tq.status == "pending" and tq.silent_until is None


def test_progress_aggregates_tell_status_and_degrades(db, env):
    """progress：downloading 行 tellStatus 聚合（speed/progress/total）；失败行与 0 值降级 null。"""
    mid = run(seed_media(db))
    dq_id = run(seed_dq(db, mid, status="downloading", aria2_gid="gid-1"))
    run(seed_dq(db, mid, episode="S01E02", status="downloading", aria2_gid="gid-2"))
    run(seed_dq(db, mid, episode="S01E03", status="downloading", aria2_gid="gid-3"))

    async def _fake_tell(gid):
        if gid == "gid-1":
            return {"gid": gid, "totalLength": "1000", "completedLength": "250",
                    "downloadSpeed": "500"}
        if gid == "gid-3":
            # tell_status 成功但 totalLength=0（Design Doc §4 边界）：total 降级 None
            return {"gid": gid, "totalLength": "0", "completedLength": "0",
                    "downloadSpeed": "0"}
        raise RuntimeError("aria2 任务不存在")

    env["aria2"].client.tell_status = _fake_tell

    async def _progress():
        async with db() as s:
            return await queue_mod.download_progress(admin=_admin(), session=s)
    rows = run(_progress())

    assert len(rows) == 3
    by_gid = {r["gid"]: r for r in rows}
    assert by_gid["gid-1"]["progress"] == 25.0 and by_gid["gid-1"]["speed"] == 500
    # 失败行降级：gid/字段保留，speed/progress 为 null
    assert by_gid["gid-2"]["progress"] is None and by_gid["gid-2"]["speed"] is None
    # total：totalLength>0 → 真实字节；失败行降级 None
    assert by_gid["gid-1"]["total"] == 1000
    assert by_gid["gid-2"]["total"] is None
    # 0 值成功响应（totalLength=0 不显示 0 字节，Design Doc §4 边界条件）
    assert by_gid["gid-3"]["total"] is None and by_gid["gid-3"]["speed"] == 0


# ---------------------------------------------------------------------------
# queue-flow-rework Task 4：TaskQueue FIFO 取件生成 download_queue(pending)
# ---------------------------------------------------------------------------

def test_fetch_from_task_queue_fifo_generates_pending_and_done(db, env, transfer_env):
    """Task 4：TaskQueue(ready) 按 (created_at, id) FIFO 取件 → DownloadQueue(pending)。

    断言：按 created_at（同刻按 id 决胜）顺序生成 pending 行、转存凭据快照整体拷贝、
    源 TaskQueue 行置 done；同键已有 DownloadQueue 行（任意状态）的 ready 任务被跳过
    （不重复生成，防止重复下载/覆盖）。
    """
    mid = run(seed_media(db))
    base = _now()
    t1 = run(seed_tq(db, mid, episode="S01E01", created_at=base - timedelta(minutes=2),
                     share_code="FifoAaa111111"))
    t2 = run(seed_tq(db, mid, episode="S01E02", created_at=base - timedelta(minutes=1)))
    t3 = run(seed_tq(db, mid, episode="S01E03", created_at=base - timedelta(minutes=1)))  # 同刻 → id 决胜
    # 同键已有 DQ 行（pending，任意状态皆跳过）→ 该 ready 任务不生成新行
    t4 = run(seed_tq(db, mid, episode="S01E04", created_at=base))
    dq_existing = run(seed_dq(db, mid, episode="S01E04", status="pending"))

    async def _fetch():
        return await transfer_mod._fetch_from_task_queue()
    n = run(_fetch())
    assert n == 3  # S01E04 同键已存在 → 跳过

    async def _rows():
        async with db() as s:
            dqs = (await s.execute(
                select(DownloadQueue).where(DownloadQueue.media_id == mid)
                .order_by(DownloadQueue.id)
            )).scalars().all()
            tqs = (await s.execute(
                select(TaskQueue).where(TaskQueue.media_id == mid)
                .order_by(TaskQueue.created_at, TaskQueue.id)
            )).scalars().all()
            return dqs, tqs
    dqs, tqs = run(_rows())

    # 源行终态（review R1 语义）：被取件的 ready 任务置 done；同键已有 DQ 的跳过行
    # （t4）保持 ready——SQL 层排除不置 done，既有 DQ 行消失后下轮可补取，重试路径保留
    assert [t.id for t in tqs] == [t1, t2, t3, t4]
    assert {t.status for t in tqs[:3]} == {"done"} and tqs[3].status == "ready"

    # FIFO 顺序生成 pending：3 条新行按 (created_at, id) 顺序（task_queue_id 溯源）
    new_dqs = [d for d in dqs if d.task_queue_id is not None]
    assert [d.episode for d in new_dqs] == ["S01E01", "S01E02", "S01E03"]
    by_ep = {d.episode: d for d in dqs}
    assert by_ep["S01E01"].status == "pending"
    assert by_ep["S01E02"].task_queue_id == t2
    assert by_ep["S01E03"].task_queue_id == t3
    # 转存凭据快照整体拷贝（Task 2 快照字段 → DQ 同名字段）；download_name Task 7 前不填
    snap = by_ep["S01E01"]
    assert snap.task_queue_id == t1 and snap.share_code == "FifoAaa111111"
    assert snap.pwd_id == "pwd" and snap.stoken == "st" and snap.receive_code == "rc"
    assert snap.fids == "[]" and snap.fid_tokens == "[]" and snap.folder_id == "fd"
    assert snap.file_name == "ep.mkv" and snap.file_size == 1024
    assert snap.download_name is None
    # 同键已有 DQ 行未被复制：S01E04 仍只有种子行（id 不变）
    assert by_ep["S01E04"].id == dq_existing

    # 幂等：二次取件无新行、无新状态变更
    assert run(_fetch()) == 0


def test_fetch_from_task_queue_only_ready_and_num_limit(db, env, transfer_env):
    """Task 4：只取 status='ready'（pending/error/done 不取）；num 限制批大小。"""
    mid = run(seed_media(db))
    run(seed_tq(db, mid, episode="S01E01", status="pending"))
    run(seed_tq(db, mid, episode="S01E02", status="error"))
    run(seed_tq(db, mid, episode="S01E03", status="done"))
    for r in range(4, 14):
        run(seed_tq(db, mid, episode=f"S01E{r:02d}", status="ready"))  # 10 条 ready

    async def _fetch(num=None):
        return await transfer_mod._fetch_from_task_queue() if num is None \
            else await transfer_mod._fetch_from_task_queue(num=num)

    # num=5 → 只取 5 条最早的 ready；pending/error/done 一律不取
    assert run(_fetch(5)) == 5
    async def _chk():
        async with db() as s:
            dqs = (await s.execute(
                select(DownloadQueue).where(DownloadQueue.media_id == mid)
            )).scalars().all()
            tqs = (await s.execute(
                select(TaskQueue).where(TaskQueue.media_id == mid)
            )).scalars().all()
            return dqs, tqs
    dqs, tqs = run(_chk())
    assert len(dqs) == 5
    taken = {d.episode for d in dqs}
    assert taken == {"S01E04", "S01E05", "S01E06", "S01E07", "S01E08"}  # FIFO 最前 5 条
    # 已取件源行 done；未取件 ready 行保持 ready；pending/error/done 源行状态不变
    by_ep = {t.episode: t for t in tqs}
    assert by_ep["S01E04"].status == "done" and by_ep["S01E08"].status == "done"
    assert by_ep["S01E09"].status == "ready" and by_ep["S01E13"].status == "ready"
    assert by_ep["S01E01"].status == "pending" and by_ep["S01E02"].status == "error"
    assert by_ep["S01E03"].status == "done"
    # 默认 num=10：剩余 5 条 ready 全部取走
    assert run(_fetch()) == 5
    assert len(run(_chk())[0]) == 10


def test_fetch_from_task_queue_repicks_after_dq_row_removed(db, env, transfer_env):
    """Task 4 review R1：同键跳过行保持 ready（不置 done），既有 DQ 行消失后下轮补取。

    覆盖 SQL 层排除方案的语义核心：跳过只是「本轮不取」，不消费 TaskQueue 源行终态
    ——当同键 DQ 行被删（入库删除/运维清理）后，下轮取件自然重新生成，重试路径保留。
    """
    mid = run(seed_media(db))
    tq_id = run(seed_tq(db, mid, status="ready"))
    dq_id = run(seed_dq(db, mid, status="pending"))

    async def _fetch():
        return await transfer_mod._fetch_from_task_queue()
    async def _tq():
        async with db() as s:
            return await s.get(TaskQueue, tq_id)

    # 第一轮：同键已有 DQ → 不取件；源行保持 ready（绝不被置 done）
    assert run(_fetch()) == 0
    assert run(_tq()).status == "ready"

    # DQ 行消失（入库删除/运维清理）→ 下轮取件自动补生成
    async def _del_dq():
        async with db() as s:
            dq = await s.get(DownloadQueue, dq_id)
            await s.delete(dq)
            await s.commit()
    run(_del_dq())

    assert run(_fetch()) == 1
    assert run(_tq()).status == "done"
    async def _dqs():
        async with db() as s:
            return (await s.execute(select(DownloadQueue))).scalars().all()
    dqs = run(_dqs())
    assert len(dqs) == 1 and dqs[0].task_queue_id == tq_id and dqs[0].status == "pending"


def test_admit_batch_fetches_ready_before_early_exit(db, env, transfer_env, monkeypatch):
    """Task 4 review R2：_admit_batch 在 has_pending 空跑早退之前先取件。

    空 DownloadQueue + 一条 TaskQueue(ready)：若阶段 1 先做 has_pending=0 早退，
    则 ready 任务永不取件（下载停摆）。断言调用 _admit_batch（_try_admit_one 短路
    为 no_pending，GID 校验 fake 空队列）后 DQ pending 行已生成、源行置 done。
    """
    mid = run(seed_media(db))
    tq_id = run(seed_tq(db, mid, status="ready", share_code="AdmItAa111111"))

    fake_aria2 = types.SimpleNamespace(client=types.SimpleNamespace(
        tell_active=AsyncMock(return_value=[]),
        tell_waiting=AsyncMock(return_value=[]),
    ))
    monkeypatch.setattr(transfer_mod, "aria2", fake_aria2)
    monkeypatch.setattr(transfer_mod, "_try_admit_one", AsyncMock(return_value="no_pending"))

    async def _run():
        await transfer_mod._admit_batch()
    run(_run())

    async def _chk():
        async with db() as s:
            dqs = (await s.execute(select(DownloadQueue))).scalars().all()
            tq = await s.get(TaskQueue, tq_id)
            return dqs, tq
    dqs, tq = run(_chk())
    assert len(dqs) == 1 and dqs[0].status == "pending"
    assert dqs[0].task_queue_id == tq_id and dqs[0].share_code == "AdmItAa111111"
    assert tq.status == "done"