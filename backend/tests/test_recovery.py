"""P3 recovery 全节点超时回退（影视下载两队列重设计 §4.2）单测。

操作对象：download_queue 单表（status∈transferring/downloading/scrape/library）。
阈值分级：transferring/downloading=episode_state_timeout_hours(2h) / scrape=
scrape_revert_timeout_hours(4h) / library=library_revert_timeout_hours(6h) 且
回退前 Emby 收录确认（已收录 → finalize done 不回退；Emby 故障 → 本轮跳过）。

全部使用 fake 依赖（monkeypatch recovery 模块内的 async_session/alist/aria2/emby
与 library_check._finalize_done），不连真实外部服务；in-memory SQLite。

验证场景：
- transferring 超时 → pending + retry_count++ + node_attempt++ + save_task_id=None
  + error 超时原因；CAS 成功后清夸克残留 + aria2.remove（B-3 后置）
- downloading 沿用 2h 阈值；scrape/library 各自独立阈值（未到阈值不回退）
- library 已收录（find_emby_id 命中且不在遗漏集）→ finalize done 不回退
- library 未收录 / Emby 故障 → 分别回退 / 跳过不误判
- CAS 冲突（retry_count 被并发推进）→ 跳过不覆盖
- 幂等（回退后 status=pending 不再命中）
- system_config 阈值覆盖 / updated_at 缺失用 enqueued_at 兜底
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
import app.tasks.library_check as library_check_mod
import app.tasks.recovery as recovery_mod
from app.models import DownloadQueue, Media, SystemConfig, TaskRun


def run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
    """fake 外部依赖（alist/aria2/emby/library_check._finalize_done）。"""
    fakes = {
        "alist": types.SimpleNamespace(remove=AsyncMock(return_value={})),
        "aria2": types.SimpleNamespace(
            client=types.SimpleNamespace(remove=AsyncMock(return_value={}))
        ),
        "emby": types.SimpleNamespace(
            find_emby_id=AsyncMock(return_value=None),
            get_missing_episodes=AsyncMock(return_value=[]),
        ),
        "finalize_done": AsyncMock(return_value=None),
    }
    monkeypatch.setattr(recovery_mod, "alist", fakes["alist"])
    monkeypatch.setattr(recovery_mod, "aria2", fakes["aria2"])
    monkeypatch.setattr(recovery_mod, "emby", fakes["emby"])
    monkeypatch.setattr(library_check_mod, "_finalize_done", fakes["finalize_done"])
    return fakes


def patch_db(monkeypatch, db):
    monkeypatch.setattr(recovery_mod, "async_session", db)


# ---------------------------------------------------------------------------
# 种子数据 / 读取
# ---------------------------------------------------------------------------

async def seed_dq(db, *, episode="S01E01", status="downloading", file_name="ep.mkv",
                  quark_path="/quark/ep.mkv", gid="gid-1", save_task_id="st-1",
                  retry_count=0, node_attempt=1, updated_at=None, enqueued_at=None,
                  tmdb_id=None, media_type="tv", title="测试剧"):
    """media + download_queue(指定 status)。返回 (mid, dq_id)。

    tmdb_id 默认 None（media.tmdb_id 有 UNIQUE 约束，多次 seed 须显式传不同值或 None）；
    library 收录确认分支调用方自行传非 None。
    """
    ts = updated_at or (_now() - timedelta(hours=10))  # 默认远超全部阈值（超时）
    async with db() as s:
        media = Media(title=title, media_type=media_type, tmdb_id=tmdb_id, status="downloading")
        s.add(media)
        await s.flush()
        dq = DownloadQueue(
            media_id=media.id, episode=episode, file_name=file_name, file_size=1024,
            share_code="sc123", stoken="st", receive_code="rc", fids="[]",
            fid_tokens="[]", folder_id="fd", status=status,
            node_attempt=node_attempt, node_started_at=ts, node_finished_at=ts,
            node_error="节点诊断", retry_count=retry_count,
            quark_path=quark_path, aria2_gid=gid, save_task_id=save_task_id,
            error="旧错误", enqueued_at=enqueued_at or ts, updated_at=ts,
        )
        s.add(dq)
        await s.flush()
        await s.commit()
        return media.id, dq.id


async def read_dq(db, dq_id):
    async with db() as s:
        return await s.get(DownloadQueue, dq_id)


async def set_config(db, key: str, value: str):
    async with db() as s:
        s.add(SystemConfig(key=key, value=value))
        await s.commit()


async def get_task_runs(db):
    async with db() as s:
        return (await s.execute(select(TaskRun))).scalars().all()


# ---------------------------------------------------------------------------
# transferring / downloading：沿用 2h 阈值
# ---------------------------------------------------------------------------

def test_recover_transferring_timeout_reverts_pending(db, env, monkeypatch):
    """transferring 超时 → pending + 计数自增 + save_task_id 清空 + B-3 后置清理。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_dq(db, status="transferring", updated_at=_now() - timedelta(hours=3)))

    count = run(recovery_mod.recover_stale_tasks())

    assert count == 1
    dq = run(read_dq(db, dq_id))
    assert dq.status == "pending"
    assert dq.retry_count == 1            # CAS 语义：本轮 +1
    assert dq.node_attempt == 2           # node_attempt++（回退后由 transfer 重新取件）
    assert dq.save_task_id is None        # 防「已受理未落盘」盲等
    assert dq.node_started_at is None and dq.node_finished_at is None
    assert "超时回退" in (dq.error or "") and "超时回退" in (dq.node_error or "")
    # B-3：CAS 成功后事务提交再清理副作用
    env["alist"].remove.assert_awaited_once_with(["ep.mkv"], "/quark/")
    env["aria2"].client.remove.assert_awaited_once_with("gid-1")


def test_recover_downloading_timeout_reverts_pending(db, env, monkeypatch):
    """downloading 沿用 episode_state_timeout_hours（2h）阈值。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_dq(db, status="downloading", updated_at=_now() - timedelta(hours=3)))

    count = run(recovery_mod.recover_stale_tasks())

    assert count == 1
    assert run(read_dq(db, dq_id)).status == "pending"


def test_recover_downloading_not_expired_kept(db, env, monkeypatch):
    """downloading 未超 2h → 不回退。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_dq(db, status="downloading", updated_at=_now() - timedelta(minutes=30)))

    count = run(recovery_mod.recover_stale_tasks())

    assert count == 0
    assert run(read_dq(db, dq_id)).status == "downloading"
    env["alist"].remove.assert_not_awaited()


# ---------------------------------------------------------------------------
# scrape：独立阈值（默认 4h）
# ---------------------------------------------------------------------------

def test_recover_scrape_uses_independent_threshold(db, env, monkeypatch):
    """scrape 超 4h → 回退；仅超 2h（downloading 阈值内）→ 不回退。"""
    patch_db(monkeypatch, db)
    mid1, stale_id = run(seed_dq(db, episode="S01E01", status="scrape",
                                  updated_at=_now() - timedelta(hours=5)))
    mid2, fresh_id = run(seed_dq(db, episode="S01E02", status="scrape",
                                  updated_at=_now() - timedelta(hours=2)))

    count = run(recovery_mod.recover_stale_tasks())

    assert count == 1
    assert run(read_dq(db, stale_id)).status == "pending"   # 超 scrape 阈值 → 回退
    assert run(read_dq(db, fresh_id)).status == "scrape"    # 未超 scrape 阈值 → 保持


def test_recover_scrape_threshold_configurable(db, env, monkeypatch):
    """system_config scrape_revert_timeout_hours 覆盖默认 4h。"""
    patch_db(monkeypatch, db)
    run(set_config(db, "scrape_revert_timeout_hours", "1"))
    mid, dq_id = run(seed_dq(db, status="scrape", updated_at=_now() - timedelta(hours=2)))

    count = run(recovery_mod.recover_stale_tasks())

    assert count == 1
    assert run(read_dq(db, dq_id)).status == "pending"


# ---------------------------------------------------------------------------
# library：独立阈值（默认 6h）+ 回退前 Emby 收录确认
# ---------------------------------------------------------------------------

def test_recover_library_confirmed_emby_finalizes_done(db, env, monkeypatch):
    """library 超时但 Emby 已收录（命中且不在遗漏集）→ finalize done，不回退。

    T8.3 语义变更：遗漏集为空改为延迟复核（recovery 低频路径同样生效）——此处
    显式用非空遗漏集（Emby 已收录当前集、仍缺失其他集）表达「已确认不在遗漏集」。
    """
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_dq(db, status="library", tmdb_id=42, updated_at=_now() - timedelta(hours=8)))
    env["emby"].find_emby_id = AsyncMock(return_value="emby-1")
    env["emby"].get_missing_episodes = AsyncMock(return_value=[{"code": "S01E02"}])

    count = run(recovery_mod.recover_stale_tasks())

    assert count == 0                    # 已收录不计入回退
    dq = run(read_dq(db, dq_id))
    assert dq.status == "library"        # 未被回退（finalize 由 fake 处理）
    env["finalize_done"].assert_awaited_once()
    # 已收录行绝不清夸克 / 不移除 aria2（副作用仅对回退行）
    env["alist"].remove.assert_not_awaited()
    env["aria2"].client.remove.assert_not_awaited()


def test_recover_library_not_collected_reverts(db, env, monkeypatch):
    """library 超时且 Emby 未收录（find_emby_id 未命中）→ 回退 pending。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_dq(db, status="library", tmdb_id=42, updated_at=_now() - timedelta(hours=8)))
    env["emby"].find_emby_id = AsyncMock(return_value=None)

    count = run(recovery_mod.recover_stale_tasks())

    assert count == 1
    assert run(read_dq(db, dq_id)).status == "pending"
    env["finalize_done"].assert_not_awaited()


def test_recover_library_episode_still_missing_reverts(db, env, monkeypatch):
    """library 超时且当前集仍在 Emby 遗漏集 → 未收录，回退 pending。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_dq(db, status="library", episode="S01E10", tmdb_id=42,
                             updated_at=_now() - timedelta(hours=8)))
    env["emby"].find_emby_id = AsyncMock(return_value="emby-1")
    env["emby"].get_missing_episodes = AsyncMock(return_value=[{"code": "S01E10"}])

    count = run(recovery_mod.recover_stale_tasks())

    assert count == 1
    assert run(read_dq(db, dq_id)).status == "pending"


def test_recover_library_emby_error_skips(db, env, monkeypatch):
    """Emby 故障 → 本轮跳过不误判（保持 library，交给 library_check 轮询）。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_dq(db, status="library", tmdb_id=42, updated_at=_now() - timedelta(hours=8)))
    env["emby"].find_emby_id = AsyncMock(side_effect=RuntimeError("Emby 不可用"))

    count = run(recovery_mod.recover_stale_tasks())

    assert count == 0
    dq = run(read_dq(db, dq_id))
    assert dq.status == "library"        # 不误判回退
    env["finalize_done"].assert_not_awaited()
    env["alist"].remove.assert_not_awaited()


def test_recover_library_not_expired_kept(db, env, monkeypatch):
    """library 未超 6h（但已超 downloading 阈值）→ 不回退（独立阈值生效）。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_dq(db, status="library", tmdb_id=42, updated_at=_now() - timedelta(hours=3)))

    count = run(recovery_mod.recover_stale_tasks())

    assert count == 0
    assert run(read_dq(db, dq_id)).status == "library"


# ---------------------------------------------------------------------------
# CAS 并发协议 / 幂等 / updated_at 兜底
# ---------------------------------------------------------------------------

def test_recover_cas_conflict_skips_row(db, env, monkeypatch):
    """并发方已推进 retry_count → CAS 不命中 → 跳过不覆盖（与 transfer P2-5 同协议）。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_dq(db, status="transferring", updated_at=_now() - timedelta(hours=3)))

    async def _concurrent_bump():
        async with db() as s:
            await s.execute(
                update(DownloadQueue).where(DownloadQueue.id == dq_id)
                .values(retry_count=5, updated_at=_now())
            )
            await s.commit()
    run(_concurrent_bump())

    count = run(recovery_mod.recover_stale_tasks())

    assert count == 0                   # CAS 冲突不计
    dq = run(read_dq(db, dq_id))
    assert dq.status == "transferring"  # 不覆盖并发方
    assert dq.retry_count == 5
    # B-3：冲突行绝不清夸克 / 不移除 aria2
    env["alist"].remove.assert_not_awaited()
    env["aria2"].client.remove.assert_not_awaited()


def test_recover_idempotent_repeat_run(db, env, monkeypatch):
    """回退后 status=pending 不再命中 → 重复执行零回退。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_dq(db, status="downloading", updated_at=_now() - timedelta(hours=3)))

    assert run(recovery_mod.recover_stale_tasks()) == 1
    assert run(recovery_mod.recover_stale_tasks()) == 0
    dq = run(read_dq(db, dq_id))
    assert dq.status == "pending" and dq.retry_count == 1


def test_recover_null_updated_at_falls_back_to_enqueued_at(db, env, monkeypatch):
    """updated_at=NULL（老数据）→ 用 enqueued_at 兜底判定；仍超时则回退。"""
    patch_db(monkeypatch, db)
    old_ts = _now() - timedelta(hours=5)
    mid, dq_id = run(seed_dq(db, status="downloading", updated_at=None,
                             enqueued_at=old_ts))

    count = run(recovery_mod.recover_stale_tasks())

    assert count == 1
    assert run(read_dq(db, dq_id)).status == "pending"


def test_recover_records_task_run(db, env, monkeypatch):
    """record_task_run 落库（recover success 记录，含回退统计）。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_dq(db, status="transferring", updated_at=_now() - timedelta(hours=3)))

    run(recovery_mod.recover_stale_tasks())

    runs = run(get_task_runs(db))
    assert runs and runs[-1].task_type == "recover"
    assert runs[-1].status == "success"
    assert "恢复 1 条" in (runs[-1].message or "")


def test_recover_no_candidates_returns_zero(db, env, monkeypatch):
    """无可回退候选（含终态/排队态不在候选集）→ 空跑零开销。"""
    patch_db(monkeypatch, db)
    run(seed_dq(db, status="pending", updated_at=_now() - timedelta(hours=100)))
    run(seed_dq(db, status="done", updated_at=_now() - timedelta(hours=100)))

    assert run(recovery_mod.recover_stale_tasks()) == 0
    env["alist"].remove.assert_not_awaited()