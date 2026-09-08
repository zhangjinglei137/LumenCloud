"""P3 queue 控制面 API（影视下载两队列重设计 §8.2 / §8.1 树契约）单测。

直接调用 app.routers.queue 的路由函数（绕过 HTTP 层，注入 db session 与 fake admin），
外部服务（aria2/alist/scan 触发）一律 monkeypatch；in-memory SQLite。

覆盖：
- GET /api/queue 树：download_queue + task_queue 聚合、probe_counts、aggregate_status、
  children 新契约字段（tq_status / silent_until / share_code_tail）；type=download 扁平
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
from app.models import DownloadQueue, Media, SystemConfig, TaskQueue


def run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _admin():
    return types.SimpleNamespace(role="admin")


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
                  probe_attempt=1, silent_until=None, error=None):
    async with db() as s:
        tq = TaskQueue(
            media_id=mid, episode=episode, file_name="ep.mkv", file_size=1024,
            share_code=share_code, pwd_id="pwd", stoken="st", receive_code="rc",
            fids="[]", fid_tokens="[]", folder_id="fd", status=status,
            probe_attempt=probe_attempt, silent_until=silent_until, error=error,
            created_at=_now(), updated_at=_now(),
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
# GET /api/queue 树（download_queue + task_queue 聚合，§8.1 契约）
# ---------------------------------------------------------------------------

def test_list_tree_groups_dq_tq_with_probe_counts(db, env):
    """树：同 media 的 dq+tq 聚合为一个父级；probe_counts 按 task_queue 计数；
    children 呈现两队列子任务。"""
    mid = run(seed_media(db))
    dq_id = run(seed_dq(db, mid, episode="S01E01", status="pending"))
    tq_ready = run(seed_tq(db, mid, episode="S01E02", status="ready"))
    tq_unmatched = run(seed_tq(db, mid, episode="S01E03", status="unmatched"))

    async def _case():
        async with db() as s:
            return await queue_mod.list_queue(user=_admin(), session=s)
    tree = run(_case())

    assert len(tree) == 1
    parent = tree[0]
    assert parent["media_id"] == mid and parent["title"] == "测试剧"
    assert parent["total_count"] == 3 and parent["done_count"] == 0
    # 探测层聚合计数（{pending,probing,ready,unmatched,error}）
    assert parent["probe_counts"] == {"pending": 0, "probing": 0, "ready": 1,
                                      "unmatched": 1, "error": 0}
    # dq pending（排队）+ tq ready/unmatched（进行中/静默） → running
    assert parent["aggregate_status"] == "running"
    by_ep = {c["episode"]: c for c in parent["children"]}
    assert by_ep["S01E01"]["node"] == "pending" and by_ep["S01E01"]["tq_status"] is None
    assert by_ep["S01E02"]["tq_status"] == "ready" and by_ep["S01E02"]["node"] is None
    assert by_ep["S01E03"]["tq_status"] == "unmatched"


def test_list_tree_child_contract_fields(db, env):
    """§8.1 契约字段：tq_status / silent_until / share_code_tail（敏感网盘凭据不返回）。"""
    mid = run(seed_media(db))
    dq_id = run(seed_dq(db, mid, status="downloading", share_code="AbCd1234XyZq"))
    silence = _now() + timedelta(days=2)
    tq_id = run(seed_tq(db, mid, episode="S01E02", status="unmatched", silent_until=silence))

    async def _case():
        async with db() as s:
            return await queue_mod.list_queue(user=_admin(), session=s)
    tree = run(_case())
    children = tree[0]["children"]
    # 注意：download_queue / task_queue 各自独立自增，id 可能重叠 → 按 episode 匹配
    dq_child = next(c for c in children if c["episode"] == "S01E01")
    tq_child = next(c for c in children if c["episode"] == "S01E02")
    # dq 执行视图：node=status，share_code_tail 缩略，无完整凭据
    assert dq_child["node"] == "downloading"
    assert dq_child["node_started_at"] is not None
    assert dq_child["share_code_tail"] == "XyZq"
    for f in ("share_code", "stoken", "receive_code", "fid_tokens", "pwd_id", "folder_id", "fids"):
        assert f not in dq_child and f not in tq_child
    # tq 探测视图：tq_status 优先语义 + silent_until 静默倒计时
    assert tq_child["tq_status"] == "unmatched"
    assert tq_child["silent_until"] == silence.isoformat()


def test_list_tree_aggregate_all_done_and_scan_tasks(db, env):
    """全 done → all_done；scan_tasks 注入最近巡检摘要。"""
    mid = run(seed_media(db))
    dq1 = run(seed_dq(db, mid, episode="S01E01", status="done"))
    dq2 = run(seed_dq(db, mid, episode="S01E02", status="done"))

    from app.models import TaskRun
    now = _now()

    async def _seed_scan():
        async with db() as s:
            s.add(TaskRun(task_type="scan_media", media_id=mid, status="success",
                          message="已入队 2 集", started_at=now - timedelta(minutes=5),
                          duration_seconds=3.0))
            await s.commit()
    run(_seed_scan())

    async def _case():
        async with db() as s:
            return await queue_mod.list_queue(user=_admin(), session=s)
    tree = run(_case())
    parent = tree[0]
    assert parent["aggregate_status"] == "all_done"
    assert parent["done_count"] == 2
    assert parent["scan_tasks"] and parent["scan_tasks"][0]["message"] == "已入队 2 集"


def test_list_download_flat_type_download(db, env):
    """?type=download → 下载队列扁平列表（§8.1 下载队列 Tab）。"""
    mid = run(seed_media(db))
    dq_id = run(seed_dq(db, mid, episode="S01E01", status="downloading"))

    async def _case():
        async with db() as s:
            return await queue_mod.list_queue(user=_admin(), session=s, type="download")
    rows = run(_case())
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == dq_id and row["media_title"] == "测试剧"
    assert row["status"] == "downloading" and row["enqueued_at"] is not None
    assert row["node_attempt"] == 0


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
            return await queue_mod.download_queue_state(admin=_admin(), session=s)
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
    env["scan_trigger"].assert_called_once_with(mid)


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
    env["scan_trigger"].assert_called_once_with(mid)


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
    """progress：downloading 行 tellStatus 聚合（speed/progress）；失败行降级 null。"""
    mid = run(seed_media(db))
    dq_id = run(seed_dq(db, mid, status="downloading", aria2_gid="gid-1"))
    run(seed_dq(db, mid, episode="S01E02", status="downloading", aria2_gid="gid-2"))

    async def _fake_tell(gid):
        if gid == "gid-1":
            return {"gid": gid, "totalLength": "1000", "completedLength": "250",
                    "downloadSpeed": "500"}
        raise RuntimeError("aria2 任务不存在")

    env["aria2"].client.tell_status = _fake_tell

    async def _progress():
        async with db() as s:
            return await queue_mod.download_progress(admin=_admin(), session=s)
    rows = run(_progress())

    assert len(rows) == 2
    by_gid = {r["gid"]: r for r in rows}
    assert by_gid["gid-1"]["progress"] == 25.0 and by_gid["gid-1"]["speed"] == 500
    # 失败行降级：gid/字段保留，speed/progress 为 null
    assert by_gid["gid-2"]["progress"] is None and by_gid["gid-2"]["speed"] is None