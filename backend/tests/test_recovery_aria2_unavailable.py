"""D11 aria2 故障解耦：aria2 故障期间不触发 downloading 任务回退循环。

背景（OpenSpec pipeline-transfer「下载跟踪与 aria2 故障解耦」，tasks.md 5.9）：
aria2 长时间故障（> episode_state_timeout_hours）时，downloading 任务因服务不可用
而「无进展」——若 recovery 照常回退 pending，transfer 会重新准入并再次转存
（cloudsaver.save + alist 轮询 300s×3 轮），aria2 仍故障 → 再次回退，每轮消耗
retry_count 且浪费转存。故 downloading 超时回退前先探活 aria2：

- aria2 不可用（Aria2Unavailable）→ 本轮跳过回退：任务保持 downloading、
  retry_count 不变、不清理残留（不触发重新转存链路），等 aria2 恢复后继续跟踪；
- aria2 可用但任务确无进展 → 维持原回退逻辑（回归锚点）。

探活只影响「是否跳过本轮回退」，不改变任务状态机其他部分；transferring/scrape/
library 的回退逻辑不受影响（不对它们探活）。

全部使用 fake 依赖（monkeypatch recovery 模块内的 alist/aria2），in-memory SQLite。
"""
import asyncio
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  注册全部 ORM 模型
import app.tasks.recovery as recovery_mod
from app.models import DownloadQueue, Media
from app.services.aria2 import Aria2Unavailable


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
    """fake 外部依赖（alist/aria2）。get_global_stat 默认可用（aria2 正常）。"""
    fakes = {
        "alist": types.SimpleNamespace(remove=AsyncMock(return_value={})),
        "aria2": types.SimpleNamespace(
            client=types.SimpleNamespace(
                remove=AsyncMock(return_value={}),
                get_global_stat=AsyncMock(return_value={}),
            )
        ),
    }
    monkeypatch.setattr(recovery_mod, "alist", fakes["alist"])
    monkeypatch.setattr(recovery_mod, "aria2", fakes["aria2"])
    return fakes


def patch_db(monkeypatch, db):
    monkeypatch.setattr(recovery_mod, "async_session", db)


# ---------------------------------------------------------------------------
# 种子数据 / 读取
# ---------------------------------------------------------------------------

async def seed_dq(db, *, episode="S01E01", status="downloading", file_name="ep.mkv",
                  quark_path="/quark/ep.mkv", gid="gid-1", save_task_id="st-1",
                  retry_count=0, node_attempt=1, updated_at=None, enqueued_at=None):
    """media + download_queue(指定 status)。返回 (mid, dq_id)。"""
    ts = updated_at or (_now() - timedelta(hours=10))  # 默认远超阈值（超时）
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=None, status="downloading")
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


# ---------------------------------------------------------------------------
# D11：aria2 故障 → downloading 不回退
# ---------------------------------------------------------------------------

def test_downloading_aria2_unavailable_skips_revert(db, env, monkeypatch):
    """aria2 抛 Aria2Unavailable → downloading 超时任务不回退（状态与 retry_count 不变）。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_dq(db, status="downloading", updated_at=_now() - timedelta(hours=3)))
    env["aria2"].client.get_global_stat = AsyncMock(side_effect=Aria2Unavailable("aria2 服务不可用"))

    count = run(recovery_mod.recover_stale_tasks())

    assert count == 0                        # 本轮不回退
    dq = run(read_dq(db, dq_id))
    assert dq.status == "downloading"        # 保持 downloading，等 aria2 恢复继续跟踪
    assert dq.retry_count == 0               # 不消耗重试计数（不触发回退重置）
    # 探活确实发生（判定基于异常类型而非返回值）
    env["aria2"].client.get_global_stat.assert_awaited_once()
    # 不触发任何副作用：不清夸克残留、不移除 aria2 任务
    env["alist"].remove.assert_not_awaited()
    env["aria2"].client.remove.assert_not_awaited()


def test_downloading_aria2_available_reverts_normally(db, env, monkeypatch):
    """回归锚点：aria2 可用 + downloading 超时 → 照常回退 pending + 计数自增。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_dq(db, status="downloading", updated_at=_now() - timedelta(hours=3)))

    count = run(recovery_mod.recover_stale_tasks())

    assert count == 1
    dq = run(read_dq(db, dq_id))
    assert dq.status == "pending"
    assert dq.retry_count == 1


def test_downloading_aria2_unavailable_no_resave_spawn(db, env, monkeypatch):
    """aria2 故障期间不下发重新转存：不回退即不清理、不触发转存链路的任何动作。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_dq(db, status="downloading", updated_at=_now() - timedelta(hours=3)))
    env["aria2"].client.get_global_stat = AsyncMock(side_effect=Aria2Unavailable("aria2 服务不可用"))

    run(recovery_mod.recover_stale_tasks())

    # recovery 侧不直接调 cloudSaver；重新转存由 transfer 在重新准入（pending）时
    # 触发。断言 CAS 未执行（状态保持 downloading → 不会重新准入）且 B-3 清理副作用
    # 未发生（alist.remove / aria2.remove 均不调用）即保证重新转存链路不触发。
    dq = run(read_dq(db, dq_id))
    assert dq.status == "downloading"
    env["alist"].remove.assert_not_awaited()
    env["aria2"].client.remove.assert_not_awaited()


# ---------------------------------------------------------------------------
# 边界：探活只作用于 downloading，其余状态回退逻辑不变
# ---------------------------------------------------------------------------

def test_transferring_unaffected_by_aria2_failure(db, env, monkeypatch):
    """aria2 故障时 transferring 超时任务仍照常回退（探活不对非 downloading 状态生效）。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_dq(db, status="transferring", updated_at=_now() - timedelta(hours=3)))
    env["aria2"].client.get_global_stat = AsyncMock(side_effect=Aria2Unavailable("aria2 服务不可用"))

    count = run(recovery_mod.recover_stale_tasks())

    assert count == 1
    assert run(read_dq(db, dq_id)).status == "pending"
    # 无 downloading 候选 → 不做 aria2 探活
    env["aria2"].client.get_global_stat.assert_not_awaited()
