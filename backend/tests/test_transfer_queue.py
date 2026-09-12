"""取件-创建原子化（fix-transfer-flow-reliability T6）与取件契约单测。

T6（design §T6）：_fetch_from_task_queue 由「先 CAS ready→done + 保存点 INSERT DQ」
改为「单语句条件 INSERT（INSERT...SELECT...WHERE NOT EXISTS）+ 命中行同事务置 done」。
旧实现撞 UNIQUE 时保存点回滚但 done 已在外层事务提交 → 源行被误标终态；
新实现影响行数 0（撞 UNIQUE 或源行已被并发取件）→ 不置 done、保持 ready。

保留契约：只取 status='ready'；NOT EXISTS 同键排除（保持 ready 不占 LIMIT 名额）；
按 (created_at, id) FIFO；返回生成行数；num=10；download_name 不填。

fixture/模式参照 test_transfer.py 既有体系：独立 in-memory SQLite（StaticPool 共享
连接），不连任何真实外部服务/数据库。
"""
import asyncio
import time as _time
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import Select, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  注册全部 ORM 模型
import app.tasks.transfer as transfer_mod
from app.models import DownloadQueue, Media, TaskQueue


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
def transfer_env(monkeypatch, db):
    """把 transfer 模块的 async_session 指向测试库（_fetch_from_task_queue 直接调用）。"""
    monkeypatch.setattr(transfer_mod, "async_session", db)
    return db


# ---------------------------------------------------------------------------
# 种子数据 / 读取
# ---------------------------------------------------------------------------

async def seed_media(db, *, title="测试剧", media_type="tv"):
    async with db() as s:
        media = Media(title=title, media_type=media_type, tmdb_id=None, status="tracking")
        s.add(media)
        await s.flush()
        await s.commit()
        return media.id


async def seed_tq(db, mid, *, episode="S01E01", status="ready", share_code="TqXxYyZz1234",
                  created_at=None, file_name="ep.mkv", file_size=1024):
    async with db() as s:
        tq = TaskQueue(
            media_id=mid, episode=episode, file_name=file_name, file_size=file_size,
            share_code=share_code, pwd_id="pwd", stoken="st", receive_code="rc",
            fids="[]", fid_tokens="[]", folder_id="fd", status=status,
            probe_attempt=1, silent_until=None, error=None,
            created_at=created_at or _now(), updated_at=_now(),
        )
        s.add(tq)
        await s.flush()
        await s.commit()
        return tq.id


async def seed_dq(db, mid, *, episode="S01E01", status="pending", task_queue_id=None):
    async with db() as s:
        dq = DownloadQueue(
            media_id=mid, episode=episode, task_queue_id=task_queue_id,
            file_name="ep.mkv", file_size=1024, share_code="DqAaBbCcDdEe",
            pwd_id="pwd", stoken="st", receive_code="rc", fids="[]",
            fid_tokens="[]", folder_id="fd", status=status,
            enqueued_at=_now(), updated_at=_now(),
        )
        s.add(dq)
        await s.flush()
        await s.commit()
        return dq.id


async def read_tq(db, tq_id):
    async with db() as s:
        return await s.get(TaskQueue, tq_id)


async def read_dq(db, dq_id):
    async with db() as s:
        return await s.get(DownloadQueue, dq_id)


async def count_dq(db, *, media_id=None):
    async with db() as s:
        stmt = select(DownloadQueue)
        if media_id is not None:
            stmt = stmt.where(DownloadQueue.media_id == media_id)
        return len((await s.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# T6：取件-创建原子化
# ---------------------------------------------------------------------------

def test_fetch_conflict_during_loop_keeps_source_row_ready(db, monkeypatch, transfer_env):
    """T6 核心：取件循环期间另一路径抢先建同键 DQ → 源行保持 ready，不误标 done。

    构造：预筛候选 SELECT 通过后（循环开始前）注入同键 DQ 行，模拟另一条 DQ 写入
    路径在「预筛与创建之间」抢先落库。旧实现此时已 CAS ready→done，保存点 INSERT
    撞 UNIQUE 回滚但 done 已在外层事务提交 → 源行被误标 done（RED）；新实现单语句
    条件 INSERT 的 NOT EXISTS 看到注入行 → 影响 0 行 → 不置 done（GREEN）。

    生产代码中被此测试捕获的缺陷：CAS 置 done 与 DQ 创建未原子化（撞 UNIQUE 时
    源行终态误标）。
    """
    mid = run(seed_media(db))
    tq_id = run(seed_tq(db, mid, episode="S01E01", status="ready"))

    real_execute = AsyncSession.execute
    injected = False

    async def _execute(self, stmt, *args, **kwargs):
        nonlocal injected
        result = await real_execute(self, stmt, *args, **kwargs)
        # hook：_fetch_from_task_queue 内首个 SELECT（候选预筛）返回后，在同一事务内
        # 注入同键 DQ 行——模拟并发 DQ 写入路径在预筛通过后抢先建 DQ。
        if not injected and isinstance(stmt, Select):
            injected = True
            await real_execute(self, insert(DownloadQueue).values(
                media_id=mid, episode="S01E01", task_queue_id=tq_id,
                file_name="ep.mkv", file_size=1024, share_code="Conflict111111",
                pwd_id="pwd", stoken="st", receive_code="rc", fids="[]",
                fid_tokens="[]", folder_id="fd", status="pending",
                enqueued_at=_now(), updated_at=_now(),
            ))
        return result

    monkeypatch.setattr(AsyncSession, "execute", _execute)

    n = run(transfer_mod._fetch_from_task_queue())

    tq = run(read_tq(db, tq_id))
    # 影响 0 行 → 不置 done：源行必须保持 ready（等待下轮或并发路径处理）
    assert n == 0
    assert tq.status == "ready"
    # DQ 行数不变（仅注入行，_fetch 未重复生成）
    assert run(count_dq(db, media_id=mid)) == 1


def test_fetch_conflict_keeps_source_row_ready(db, monkeypatch, transfer_env):
    """同键 DQ 已存在（任意来源，如另一 DQ 写入路径先行落库）→ 该 ready 任务被
    SQL 层排除：源行保持 ready（不误标 done）、不生成新行、返回计数不包含该行。"""
    mid = run(seed_media(db))
    tq_id = run(seed_tq(db, mid, episode="S01E01", status="ready"))
    dq_id = run(seed_dq(db, mid, episode="S01E01", status="pending"))

    n = run(transfer_mod._fetch_from_task_queue())

    tq = run(read_tq(db, tq_id))
    assert n == 0
    assert tq.status == "ready"
    assert run(count_dq(db, media_id=mid)) == 1


def test_fetch_generates_pending_and_done_fifo(db, monkeypatch, transfer_env):
    """正常路径：ready 行按 (created_at, id) FIFO 取件 → DownloadQueue(pending) +
    源行同事务置 done；返回生成行数。确保原子化改造不破坏取件主链路契约。"""
    mid = run(seed_media(db))
    base = _now()
    t1 = run(seed_tq(db, mid, episode="S01E01", created_at=base - timedelta(minutes=2),
                     share_code="FifoAaa111111"))
    t2 = run(seed_tq(db, mid, episode="S01E02", created_at=base - timedelta(minutes=1)))

    n = run(transfer_mod._fetch_from_task_queue())

    assert n == 2

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

    # FIFO 顺序生成 pending，task_queue_id 溯源；download_name 取件阶段不填
    assert [d.episode for d in dqs] == ["S01E01", "S01E02"]
    assert all(d.status == "pending" for d in dqs)
    assert dqs[0].task_queue_id == t1 and dqs[1].task_queue_id == t2
    assert all(d.download_name is None for d in dqs)
    # 取件源行同事务置 done（防重复取件）
    assert all(t.status == "done" for t in tqs)

    # 幂等：二次取件无新行、无新状态变更
    assert run(transfer_mod._fetch_from_task_queue()) == 0
    assert run(count_dq(db, media_id=mid)) == 2


# ---------------------------------------------------------------------------
# T8.5：quota_wait 唤醒后容量预查（design §T8.5，Task 15）
# ---------------------------------------------------------------------------

def test_quota_wake_with_zero_room_skips_admission_loop(db, monkeypatch, transfer_env):
    """T8.5 主路径：唤醒 quota_wait 后容量余量=0 → 不进入准入循环。

    预置 quota_wait 行；mock capacity.provider.get_usage 返回 used_gb == quota_gb
    （余量 0）。_admit_batch 唤醒 quota_wait→pending 后容量预查直接 return——不再
    走取件之后的 GID 校验 / 准入循环。旧实现会进准入循环把刚唤醒的 pending 行逐
    个容量 check 拒绝再置回 quota_wait：N×UPDATE + 容量查询的写放大（T8.5 优化目标）。

    RED（无预查）：_try_admit_one 被调用 ≥1 次（call_count 断言失败）；
    GREEN（有预查）：_try_admit_one 调用 0 次，quota_wait 行被唤醒后保持 pending。
    """
    mid = run(seed_media(db))
    dq_id = run(seed_dq(db, mid, status="quota_wait"))

    # 容量余量 = 0：used_gb == quota_gb（margin 0）
    provider = types.SimpleNamespace(
        get_usage=AsyncMock(return_value=types.SimpleNamespace(
            total_gb=100.0, used_gb=100.0, source="alist")),
        _load_quota_gb=AsyncMock(return_value=100.0),
        _load_margin_gb=AsyncMock(return_value=0.0),
    )
    monkeypatch.setattr(transfer_mod, "capacity", types.SimpleNamespace(provider=provider))
    # RED 路径需让 GID 校验通过（空活动队列）→ 才能走到准入循环证明旧行为
    monkeypatch.setattr(transfer_mod, "aria2", types.SimpleNamespace(
        client=types.SimpleNamespace(tell_active=AsyncMock(return_value=[])),
    ))
    try_admit = AsyncMock(return_value="no_pending")
    monkeypatch.setattr(transfer_mod, "_try_admit_one", try_admit)

    run(transfer_mod._admit_batch())

    # 余量 0 → 唤醒后直接返回，不进入准入循环
    assert try_admit.call_count == 0
    # 唤醒段已把 quota_wait → pending；未进准入循环 → 保持 pending（不回 quota_wait）
    assert run(read_dq(db, dq_id)).status == "pending"


def test_quota_wake_precheck_failure_falls_back_to_admission_loop(db, monkeypatch, transfer_env):
    """T8.5 失败兜底：容量预查异常 → 不阻断，由准入循环 fail-closed 兜底。

    预查是优化不是新硬门：get_usage 抛异常时不得 return——仍进准入循环
    （_try_admit_one 被调用），与改造前行为一致（容量不可用由 _try_admit_one 内
    的 fail-closed 语义处理：保持 pending + 告警）。
    """
    mid = run(seed_media(db))
    run(seed_dq(db, mid, status="quota_wait"))

    provider = types.SimpleNamespace(
        get_usage=AsyncMock(side_effect=RuntimeError("alist 不可用")),
        _load_quota_gb=AsyncMock(return_value=100.0),
        _load_margin_gb=AsyncMock(return_value=0.0),
    )
    monkeypatch.setattr(transfer_mod, "capacity", types.SimpleNamespace(provider=provider))
    monkeypatch.setattr(transfer_mod, "aria2", types.SimpleNamespace(
        client=types.SimpleNamespace(tell_active=AsyncMock(return_value=[])),
    ))
    try_admit = AsyncMock(return_value="no_pending")
    monkeypatch.setattr(transfer_mod, "_try_admit_one", try_admit)

    run(transfer_mod._admit_batch())

    # 预查失败不阻断 → 准入循环仍被调用（fail-closed 语义在 _try_admit_one 内兜底）
    assert try_admit.call_count >= 1


# ---------------------------------------------------------------------------
# T8.6：取件凭据完整性校验（design §T8.6，Task 16）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "missing_field,seed_kwargs",
    [
        ("file_name", {"file_name": ""}),
        ("file_size", {"file_size": 0}),
        ("share_code", {"share_code": ""}),
    ],
)
def test_fetch_incomplete_credentials_keeps_source_ready(
    db, monkeypatch, transfer_env, missing_field, seed_kwargs
):
    """T8.6 核心：取件时校验 file_name/file_size/share_code 完整性，任一缺失（为空/0）
    → 源行保持 ready（不置 done、不建 DQ）+ _record_alert 告警留痕。

    构造：task_queue(1) status='ready'，对应字段缺失（share_code='' 为首例目标）；
    调用 _fetch_from_task_queue()。断言：返回计数不含该行、源行 status 保持 ready、
    不生成 DownloadQueue、_record_alert 被调用且 category="transfer"。

    RED（现实现空值兜底拷贝）：缺失行仍进单语句 INSERT 建 DQ、源行置 done、无告警
    → n==0 / status=='ready' / count==0 / alert.assert_awaited() 全部失败；
    GREEN（本任务校验+continue）：缺失行跳过 INSERT，源行保持 ready，告警留痕。
    """
    mid = run(seed_media(db))
    tq_id = run(seed_tq(db, mid, episode="S01E01", status="ready", **seed_kwargs))
    alert = AsyncMock()
    monkeypatch.setattr(transfer_mod, "_record_alert", alert)

    n = run(transfer_mod._fetch_from_task_queue())

    tq = run(read_tq(db, tq_id))
    # 不建注定失败的 DQ：返回计数为 0
    assert n == 0
    # 源行保持 ready（不置 done，下轮重试或由上游探测路径补全凭据）
    assert tq.status == "ready"
    # 未生成 DownloadQueue
    assert run(count_dq(db, media_id=mid)) == 0
    # 告警留痕（category="transfer"）
    alert.assert_awaited_once()
    alert_call = alert.await_args
    assert alert_call is not None
    assert alert_call.kwargs.get("category") == "transfer"


# ---------------------------------------------------------------------------
# T8.7：save_task_id 条件化清理（design §T8.7，Task 17）
# ---------------------------------------------------------------------------

async def seed_dq_failure(db, mid, *, status, save_task_id=None, save_attempt_at=None,
                          retry_count=0, node_attempt=0):
    """预置 _node_failure 条件化清理测试用 DownloadQueue 行（显式计数，CAS 快照可控）。"""
    async with db() as s:
        dq = DownloadQueue(
            media_id=mid, episode="S01E01", task_queue_id=None,
            file_name="ep.mkv", file_size=1024, share_code="DqAaBbCcDdEe",
            pwd_id="pwd", stoken="st", receive_code="rc", fids="[]",
            fid_tokens="[]", folder_id="fd", status=status,
            save_task_id=save_task_id, save_attempt_at=save_attempt_at,
            retry_count=retry_count, node_attempt=node_attempt,
            enqueued_at=_now(), updated_at=_now(),
        )
        s.add(dq)
        await s.flush()
        await s.commit()
        return dq.id


def test_node_failure_keeps_save_id_when_transferring(db, monkeypatch, transfer_env):
    """T8.7 主路径：并发方已将行置 transferring 并新 save（save_task_id='new-save'）
    → _node_failure(clear_save=True) 不得抹掉该新 task_id（否则下一轮跳过 save 重复转存）。

    构造：转移链失败时序下，另一条 save 提交路径先行落库（WHERE status='transferring'，
    不改 retry_count/node_attempt），随后的 _node_failure 主 UPDATE CAS 仍命中——
    无条件清空会抹掉 'new-save'（RED）；条件化后保留（GREEN）。
    """
    mid = run(seed_media(db))
    dq_id = run(seed_dq_failure(
        db, mid, status="transferring",
        save_task_id="new-save", save_attempt_at=_now(),
        retry_count=0, node_attempt=0,
    ))

    out = run(transfer_mod._node_failure(
        dq_id, mid, "S01E01", "ep.mkv", 0, 0, "转存失败: 模拟直链超时",
        clear_save=True, t0=_time.monotonic(),
    ))

    assert out == "retry"                  # 非终态回退语义不变
    dq = run(read_dq(db, dq_id))
    assert dq.save_task_id == "new-save"   # 关键：并发新 save 不被无条件清空
    assert dq.save_attempt_at is not None
    # 回退动作照常执行（仅清空被条件化）
    assert dq.status == "pending"
    assert dq.node_attempt == 1
    assert dq.retry_count == 1


def test_node_failure_clears_save_id_when_not_transferring(db, monkeypatch, transfer_env):
    """T8.7 对照：非 transferring（无并发新 save，如孤立的旧 save_task_id）
    → _node_failure(clear_save=True) 正常清空，保持 P0-1 防盲等语义。"""
    mid = run(seed_media(db))
    dq_id = run(seed_dq_failure(
        db, mid, status="pending",
        save_task_id="old-save", save_attempt_at=_now(),
        retry_count=0, node_attempt=0,
    ))

    out = run(transfer_mod._node_failure(
        dq_id, mid, "S01E01", "ep.mkv", 0, 0, "模拟失败",
        clear_save=True, t0=_time.monotonic(),
    ))

    assert out == "retry"
    dq = run(read_dq(db, dq_id))
    assert dq.save_task_id is None         # 非 transferring 正常清空
    assert dq.save_attempt_at is None
    assert dq.status == "pending"
    assert dq.node_attempt == 1


def test_node_failure_cas_conflict_keeps_save_id_when_transferring(
    db, monkeypatch, transfer_env
):
    """T8.7 CAS 冲突兜底分支：主 UPDATE rowcount=0（并发方已推进计数）走兜底 UPDATE
    → 行仍 transferring 且已新 save 时，兜底分支同样不得抹掉 'new-save'。

    构造：预置 retry_count/node_attempt=5，快照传 0,0 → 主 UPDATE CAS 不命中 → 兜底
    分支执行。旧实现兜底无条件清空（RED）；条件化后保留（GREEN）。
    """
    mid = run(seed_media(db))
    dq_id = run(seed_dq_failure(
        db, mid, status="transferring",
        save_task_id="new-save", save_attempt_at=_now(),
        retry_count=5, node_attempt=5,
    ))

    out = run(transfer_mod._node_failure(
        dq_id, mid, "S01E01", "ep.mkv", 0, 0, "转存失败: 模拟",
        clear_save=True, t0=_time.monotonic(),
    ))

    assert out == "retry"                  # CAS 冲突分支返回语义不变
    dq = run(read_dq(db, dq_id))
    assert dq.save_task_id == "new-save"   # 兜底分支同样条件化保留
    assert dq.save_attempt_at is not None
    # CAS 冲突语义：不计数、不转移状态
    assert dq.retry_count == 5 and dq.node_attempt == 5
    assert dq.status == "transferring"


def test_node_failure_cas_conflict_clears_save_id_when_not_transferring(
    db, monkeypatch, transfer_env
):
    """T8.7 CAS 冲突兜底分支对照：非 transferring 时兜底分支仍正常清空（P0-1 防盲等）。"""
    mid = run(seed_media(db))
    dq_id = run(seed_dq_failure(
        db, mid, status="quota_wait",
        save_task_id="old-save", save_attempt_at=_now(),
        retry_count=5, node_attempt=5,
    ))

    out = run(transfer_mod._node_failure(
        dq_id, mid, "S01E01", "ep.mkv", 0, 0, "模拟失败",
        clear_save=True, t0=_time.monotonic(),
    ))

    assert out == "retry"
    dq = run(read_dq(db, dq_id))
    assert dq.save_task_id is None         # 兜底分支非 transferring 清空
    assert dq.save_attempt_at is None
