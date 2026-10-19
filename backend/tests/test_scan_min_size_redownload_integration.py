"""大小下限过滤 + Emby 删除后自动重下端到端集成测试。

change media-min-size-and-emby-redownload 车道 D / 任务 4.2。

与单测（test_scan_size_bounds.py / test_scan_redownload.py /
test_queue_retry_redownload.py）的区别：这里全部走真实 scan 入队路径
（_scan_one 完整流程）与真实状态机（_resolve_done_states / queue.retry_task），
并断言真实落库结果（task_queue / download_queue / task_run），验证两项能力
在同一条巡检链路上协同正确。

覆盖：
1. 20MB 单集被下限（0.1GB）拦截 → 不写 task_queue、size_filtered+1；正常大小文件入队。
2. done 行宽限用尽（retry_count=3）且 Emby 仍缺失 → 自动重下置 pending 并清零/清空字段；
   后续巡检按次递增 auto_redownload_count，3 次用尽后转 failed 终态。
3. 对终态行人工 retry_task → pending 且 auto_redownload_count 归零。
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.database import Base
from app.models import DownloadQueue, Media, TaskQueue, TaskRun
from app.utils import now_utc_naive as _now

GB = 1024 ** 3


def run(coro):
    return asyncio.run(coro)


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


# ---------------------------------------------------------------------------
# 公共 mock / 读取辅助（对齐 test_scan_size_bounds.py 的真实入队路径）
# ---------------------------------------------------------------------------

def _share_file(name: str, size: int) -> dict:
    return {
        "fileName": name, "fileId": f"f_{name}", "fileIdToken": f"ft_{name}",
        "isFolder": False, "size": size,
    }


def _missing(*codes):
    return [{"code": c, "premiere_date": "2020-01-01"} for c in codes]


def _patch_scan(monkeypatch, db, *, missing_codes, files):
    """mock 外部 IO（Emby/搜索/分享遍历/转存），保留真实过滤与入队逻辑。"""
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    monkeypatch.setattr(scan_mod, "_emby_missing_codes", AsyncMock(return_value=missing_codes))
    monkeypatch.setattr(scan_mod, "_search_and_rank", AsyncMock(return_value=[
        {"title": "测试剧", "share_code": "sc1"},
    ]))
    monkeypatch.setattr(scan_mod, "_cloudsaver_share_info", AsyncMock(return_value={
        "pwd_id": "pd", "stoken": "st", "receive_code": "", "fileSize": 0,
    }))
    monkeypatch.setattr(scan_mod.cloudsaver, "share_list",
                        AsyncMock(return_value={"list": files}))
    monkeypatch.setattr(scan_mod, "_trigger_transfer", AsyncMock(return_value=None))
    return scan_mod


async def _seed_media(db, **overrides):
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=None, status="tracking",
                      **overrides)
        s.add(media)
        await s.commit()
        return media.id


async def _seed_done_row(db, mid, *, retry_count, auto_redownload_count):
    """宽限已用尽的 done 行（Emby 侧已删除该集），附带待清理字段。"""
    async with db() as s:
        dq = DownloadQueue(
            media_id=mid, episode="S01E01", status="done",
            retry_count=retry_count, auto_redownload_count=auto_redownload_count,
            error="旧错误", node_error="旧节点错误", node_attempt=2,
            save_task_id="save-1", file_name="S01E01.mkv", file_size=GB,
            share_code="sc", stoken="st", receive_code="rc", fids="[]",
            fid_tokens="[]", folder_id="fd", updated_at=_now(),
        )
        s.add(dq)
        await s.commit()
        return dq.id


async def _read_tq(db, mid):
    async with db() as s:
        return (
            await s.execute(
                select(TaskQueue).where(TaskQueue.media_id == mid)
                .order_by(TaskQueue.episode.asc())
            )
        ).scalars().all()


async def _last_run(db, mid):
    async with db() as s:
        return (
            await s.execute(
                select(TaskRun).where(TaskRun.media_id == mid)
                .order_by(TaskRun.id.desc())
            )
        ).scalars().first()


async def _read_dq(db, dq_id):
    async with db() as s:
        return await s.get(DownloadQueue, dq_id)


# ---------------------------------------------------------------------------
# 1) 过小文件被下限拦截，正常文件照常入队（完整 _scan_one 路径）
# ---------------------------------------------------------------------------

def test_scan_min_size_filtered_others_enqueued(db, monkeypatch):
    """20MB < 0.1GB 下限 → 不写 task_queue + size_filtered=1；1GB 正常入队。"""
    scan_mod = _patch_scan(
        monkeypatch, db, missing_codes=_missing("S01E01", "S01E02"),
        files=[
            _share_file("S01E01.mkv", 20 * 1024 * 1024),  # 过小
            _share_file("S01E02.mkv", GB),                # 正常
        ],
    )
    mid = run(_seed_media(db, min_episode_size_gb=0.1))
    rid = run(scan_mod._scan_one(mid))

    # 真实落库：task_queue 只有正常大小文件（过小文件未入队）
    tq = run(_read_tq(db, mid))
    assert [(row.episode, row.file_name, row.status) for row in tq] == [
        ("S01E02", "S01E02.mkv", "ready"),
    ]
    # 巡检结果摘要：size_filtered +1、enqueued=1
    tr = run(_last_run(db, mid))
    assert tr.id == rid and tr.status == "success"
    detail = json.loads(tr.scan_detail)
    assert detail["size_filtered"] == 1
    assert detail["enqueued"] == 1


# ---------------------------------------------------------------------------
# 2) Emby 删除后自动重下：宽限用尽 → pending 递增 → 3 次用尽转 failed 终态
# ---------------------------------------------------------------------------

async def _rearm_done(db, dq_id):
    """模拟自动重下完成（pending 被消费并落为 done），Emby 再次删除该集。

    retry_count 归零与 pending 重置/下载完成后的真实状态一致。
    """
    async with db() as s:
        dq = await s.get(DownloadQueue, dq_id)
        dq.status = "done"
        dq.retry_count = 0
        dq.error = None
        dq.node_error = None
        await s.commit()


def _burn_grace(scan_mod, db, mid, dq_id):
    """宽限期内逐轮巡检：done → failed 且 retry_count 递增到上限。"""
    for expected in range(1, scan_mod._DONE_FAIL_RETRY_LIMIT + 1):
        run(scan_mod._resolve_done_states(SimpleNamespace(id=mid), {"S01E01"}, False))
        dq = run(_read_dq(db, dq_id))
        assert dq.status == "failed"
        assert dq.retry_count == expected


def test_emby_delete_auto_redownload_until_terminal(db, monkeypatch):
    """宽限用尽 + Emby 仍缺失 → 自动重下；每次重下后再删除按次递增，3 次用尽转终态。"""
    scan_mod = _patch_scan(
        monkeypatch, db, missing_codes=_missing("S01E01"),
        files=[_share_file("S01E01.mkv", GB)],
    )
    mid = run(_seed_media(db))
    dq_id = run(_seed_done_row(db, mid, retry_count=3, auto_redownload_count=0))

    # 第一次：完整巡检路径（_scan_one 内部执行 _resolve_done_states）
    run(scan_mod._scan_one(mid))

    dq = run(_read_dq(db, dq_id))
    assert dq.status == "pending"
    assert dq.retry_count == 0
    assert dq.auto_redownload_count == 1
    # retry_task 语义的字段清理
    assert dq.save_task_id is None
    assert dq.error is None
    assert dq.node_error is None
    assert dq.node_attempt == 0
    assert dq.enqueued_at is not None

    # 后续巡检：重下完成 → Emby 再次删除 → 宽限再次用尽 → auto 按次递增
    for expected_auto in (2, scan_mod._AUTO_REDOWNLOAD_LIMIT):
        run(_rearm_done(db, dq_id))
        _burn_grace(scan_mod, db, mid, dq_id)
        run(scan_mod._resolve_done_states(SimpleNamespace(id=mid), {"S01E01"}, False))

        dq = run(_read_dq(db, dq_id))
        assert dq.status == "pending"
        assert dq.retry_count == 0
        assert dq.auto_redownload_count == expected_auto

    # 自动重下次数用尽 → 再次删除后转 failed 终态
    run(_rearm_done(db, dq_id))
    _burn_grace(scan_mod, db, mid, dq_id)
    run(scan_mod._resolve_done_states(SimpleNamespace(id=mid), {"S01E01"}, False))

    dq = run(_read_dq(db, dq_id))
    assert dq.status == "failed"
    assert dq.error == scan_mod._DONE_LIMIT_ERROR
    assert dq.node_error == scan_mod._DONE_LIMIT_ERROR
    assert dq.node_attempt == scan_mod._DONE_FAIL_RETRY_LIMIT
    assert dq.auto_redownload_count == scan_mod._AUTO_REDOWNLOAD_LIMIT


# ---------------------------------------------------------------------------
# 3) 人工重试归零自动重下计数
# ---------------------------------------------------------------------------

def test_manual_retry_resets_auto_redownload_count(db, monkeypatch):
    """终态行（failed + 上限文案）人工 retry → pending 且 auto_redownload_count=0。"""
    import app.routers.queue as queue_mod
    from app.tasks import scan as scan_mod

    # 与场景 2 产出的终态行一致
    async def _seed_terminal():
        mid = await _seed_media(db)
        async with db() as s:
            dq = DownloadQueue(
                media_id=mid, episode="S01E01", status="failed",
                retry_count=scan_mod._DONE_FAIL_RETRY_LIMIT,
                auto_redownload_count=scan_mod._AUTO_REDOWNLOAD_LIMIT,
                error=scan_mod._DONE_LIMIT_ERROR,
                node_error=scan_mod._DONE_LIMIT_ERROR,
                node_attempt=scan_mod._DONE_FAIL_RETRY_LIMIT,
                save_task_id="save-1", file_name="S01E01.mkv", file_size=GB,
                share_code="sc", stoken="st", receive_code="rc", fids="[]",
                fid_tokens="[]", folder_id="fd", updated_at=_now(),
            )
            s.add(dq)
            await s.commit()
            return dq.id

    dq_id = run(_seed_terminal())
    # 人工 retry 后的消费触发不属于本用例范围（真实实现会 fire-and-forget）
    monkeypatch.setattr(queue_mod, "_trigger_consume", AsyncMock(return_value=None))

    async def _do_retry():
        async with db() as s:
            return await queue_mod.retry_task(
                task_id=dq_id, admin=SimpleNamespace(role="admin"), session=s,
            )

    assert run(_do_retry()) == {"ok": True}

    dq = run(_read_dq(db, dq_id))
    assert dq.status == "pending"
    assert dq.auto_redownload_count == 0
    assert dq.retry_count == 0
    assert dq.save_task_id is None
    assert dq.error is None
    assert dq.node_error is None
