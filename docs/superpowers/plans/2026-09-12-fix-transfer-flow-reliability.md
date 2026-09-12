# 转存下载流程可靠性修复 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

---
change: fix-transfer-flow-reliability
design-doc: docs/superpowers/specs/2026-09-12-transfer-flow-reliability-design.md
base-ref: 78edae379580c0814ec708e5a883417e99d86360
---

**Goal:** 修复转存下载流程 9 组可靠性缺陷（准入事务边界、GID 白名单逃生、webhook 文件级推进、media 状态同步、夸克残留清理、取件原子化、容量记账、P2 有界性修复），全部配套回归测试。

**Architecture:** 单 worker + SQLite（StaticPool 单连接），APScheduler 驱动；transfer.py 主链路状态机（L2 五节点 + CAS 幂等协议）不变，逐项修复边界缺陷；每项修复配套 backend/tests/ 回归测试，mock 外部服务（aria2/cloudsaver/alist/nastools/emby）。

**Tech Stack:** Python 3.11+ / FastAPI / SQLAlchemy 2.0 async / aiosqlite / pytest / APScheduler

**Spec:** `docs/openspec/changes/fix-transfer-flow-reliability/`（proposal.md / design.md / specs/pipeline-admission/spec.md / specs/pipeline-transfer/spec.md）+ `docs/superpowers/specs/2026-09-12-transfer-flow-reliability-design.md`（深度设计，任务编号 T1-T8 与 tasks.md 对齐）。

## Global Constraints

- 产物语言：zh-CN。提交信息遵循 Conventional Commits 结构、中文摘要（feat/fix/refactor/test/chore 前缀）。
- 主链路状态机拓扑与 CAS 幂等协议（`_node_failure`/`_complete_download`/`_commit_downloading` 的 retry_count/node_attempt 双快照）不得破坏——回归测试 `test_fix_p0_recovery_cleanup_transfer.py` / `test_p1_fixes.py` / `test_oracle_fixes.py` / `test_council_fixes.py` 必须保持全绿。
- 不新增 public API / schema / capability；不改前端。
- 不切换存储引擎、不引入 Redis。
- 测试风格：in-memory SQLite（`create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)`）+ `monkeypatch.setattr(mod, "async_session", db)` + fake 外部服务（`types.SimpleNamespace`）——参照 `backend/tests/test_fix_p0_recovery_cleanup_transfer.py` 的 `_patch_transfer` helper 模式。
- 运行验证命令：`cd backend && python -m pytest tests/<目标测试文件> -v`；全量回归 `python -m pytest tests/ -x -q`。
- 每个任务独立提交；任务验收 = 对应测试通过 + 提交完成 + tasks.md 勾选对应 checkbox。

---

## 测试基础设施速查（所有任务共用）

既有测试模式（`backend/tests/test_fix_p0_recovery_cleanup_transfer.py`）：

```python
import types
from unittest.mock import AsyncMock
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from app.database import Base
import app.models  # noqa: F401

@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()

def _patch_transfer(monkeypatch, db):
    fakes = {
        "alist": types.SimpleNamespace(...),
        "aria2": AsyncMock(),
        "cloudsaver": AsyncMock(),
        "capacity": types.SimpleNamespace(provider=AsyncMock()),
        "notifier": AsyncMock(),
    }
    monkeypatch.setattr(transfer_mod, "async_session", db)
    monkeypatch.setattr(transfer_mod, "alist", fakes["alist"])
    monkeypatch.setattr(transfer_mod, "aria2", types.SimpleNamespace(client=fakes["aria2"]))
    monkeypatch.setattr(transfer_mod, "cloudsaver", fakes["cloudsaver"])
    monkeypatch.setattr(transfer_mod, "capacity", types.SimpleNamespace(provider=fakes["capacity"]))
    monkeypatch.setattr(transfer_mod, "notifier", fakes["notifier"])
    return fakes
```

---

## Task 1: 准入事务边界重构（tasks.md 1.1）

**Files:**
- Modify: `backend/app/tasks/transfer.py:1093-1259`（`_try_admit_one`）
- Test: Create `backend/tests/test_admission_tx_boundary.py`

**Interfaces:**
- Consumes: `_read_reserved_in_tx(s)`（transfer.py:1262）、`capacity.provider.check(candidate_bytes)`、`_preflight_quark_mount(...)`、`_transfer_chain(...)`、`_record_alert(...)`
- Produces: `_try_admit_one(t0) -> str` 返回语义不变（`'admitted'/'no_pending'/'conflict'/'quota_wait'/'capacity_unavailable'/'retry'/'terminal_failed'`）

**改造目标（design T1）**：现准入段在 `async with s.begin()` 事务内（L1155-1229）调用 `capacity.provider.check` → `get_usage` 内部 `_persist_snapshot` 新开 session commit（嵌套提交）。拆为三段：

```
短事务A（只读）: 取最早 pending 快照（L1116-1139 保持不变，detached 快照）
锁外:           _preflight_quark_mount（L1141-1146 保持不变）
锁外:           capacity.provider.check(reserved + file_size)   ← 移出事务（网络 IO + 快照落库在无外层事务上下文）
短事务B（写）:  锁行 → _read_reserved_in_tx → CAS pending→transferring（或置 quota_wait）
```

- [x] **Step 1: 写失败测试 `test_admission_tx_boundary.py`**

```python
"""P0-1 准入事务边界回归：容量 check 期间无活动 DB 事务；CAS 条件更新仍生效。"""
import types
from unittest.mock import AsyncMock
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from app.database import Base
import app.models  # noqa: F401
import app.tasks.transfer as transfer_mod
from app.models import DownloadQueue, Media


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _seed_pending(db, *, episode="S01E01", file_size=1024):
    db.add(Media(id=1, tmdb_id=1, title="测试剧", media_type="tv", status="tracking"))
    db.add(DownloadQueue(
        id=1, media_id=1, episode=episode, file_name="测试剧.S01E01.mkv",
        file_size=file_size, share_code="sc", status="pending",
        enqueued_at=transfer_mod._now(), updated_at=transfer_mod._now(),
    ))
    await db.commit()


def test_check_runs_outside_db_transaction(db, monkeypatch):
    """容量 check 调用时不得存在活动 DB 事务（短事务 A 已提交、事务 B 未开始）。"""
    await db  # noqa: 保持 fixture 生效
    in_tx_flags = []

    async def fake_check(candidate_bytes):
        async with db() as s:
            in_tx_flags.append(s.in_transaction())
        return True

    provider = AsyncMock()
    provider.check = fake_check
    monkeypatch.setattr(transfer_mod, "async_session", db)
    monkeypatch.setattr(transfer_mod, "capacity", types.SimpleNamespace(provider=provider))
    monkeypatch.setattr(transfer_mod, "_preflight_quark_mount",
                        AsyncMock(return_value=None))
    monkeypatch.setattr(transfer_mod, "_transfer_chain",
                        AsyncMock(return_value="admitted"))
    await _seed_pending(db)

    # 清除既有 pending 再取（detached 快照在 check 前已提交）
    result = await transfer_mod._try_admit_one(0.0)
    assert result == "admitted"
    assert in_tx_flags == [False], "capacity.check 期间不得有活动 DB 事务"


def test_cas_pending_to_transferring_still_applies(db, monkeypatch):
    """事务 B 的 CAS（WHERE status='pending'）仍生效：并发方已抢占 → conflict。"""
    provider = AsyncMock()
    provider.check = AsyncMock(return_value=True)
    monkeypatch.setattr(transfer_mod, "async_session", db)
    monkeypatch.setattr(transfer_mod, "capacity", types.SimpleNamespace(provider=provider))
    monkeypatch.setattr(transfer_mod, "_preflight_quark_mount",
                        AsyncMock(return_value=None))

    captured = {}

    async def fake_chain(*args, **kwargs):
        async with db() as s:
            # 模拟并发方在 check 之后抢占了该行
            from sqlalchemy import update
            await s.execute(update(DownloadQueue).where(DownloadQueue.id == 1)
                            .values(status="transferring"))
            await s.commit()
        return "conflict"

    monkeypatch.setattr(transfer_mod, "_transfer_chain", fake_chain)
    await _seed_pending(db)
    result = await transfer_mod._try_admit_one(0.0)
    assert result == "conflict"
```

- [x] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_admission_tx_boundary.py -v`
Expected: FAIL（现有实现 check 在事务内，`in_tx_flags == [False]` 断言失败）

- [x] **Step 3: 重构 `_try_admit_one`（transfer.py:1093-1259）**

保持结构：`短事务A`（只读取 pending 快照，detached）→ `_preflight_quark_mount`（锁外）→ 锁外 `capacity.provider.check`（不再包在 `async with s.begin()` 内）→ 短事务B（锁行 + `_read_reserved_in_tx` + CAS 抢占/置 quota_wait）。要点：

- 短事务 A 结束后快照字段已在内存（现有 L1131-1139 已如此，保持）。
- 将原 L1155-1229 的「`async with s.begin()` 内 check」拆开：事务 B 内只做锁行 + `_read_reserved_in_tx(s)` + 容量复判（用「内存 usage 快照 + reserved_新」比较，**不重复调 get_usage**，避免事务内网络 IO 回潮）+ CAS。
- 容量复判公式（design T1 边界）：`usage.used_gb + reserved_新 + file_size ≤ quota`；不满足 → 置 quota_wait（同现有分支逻辑）。锁外 check 的原始结果保留用于决定是否进入事务 B；事务 B 内以重读 reserved 后的复判为最终判定。
- 保持 `_admission_lock` 包住「锁外 check + 事务 B」整段（L1153 语义不变）。
- 保持返回状态语义与 `quota_count`/告警逻辑（L1230-1251）不变。

- [x] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_admission_tx_boundary.py -v`
Expected: PASS（2 个用例）

- [x] **Step 5: 回归主链路测试**

Run: `cd backend && python -m pytest tests/test_fix_p0_recovery_cleanup_transfer.py tests/test_p1_fixes.py -q`
Expected: PASS（状态机 CAS 协议未破坏）

- [x] **Step 6: 提交**

```bash
git add backend/app/tasks/transfer.py backend/tests/test_admission_tx_boundary.py
git commit -m "fix(transfer): 准入段拆分为锁外容量检查与短事务抢占"
```

- [x] **Step 7: 勾选 tasks.md 1.1**

---

## Task 2: 容量快照移出外层事务（tasks.md 1.2）

**Files:**
- Modify: `backend/app/tasks/transfer.py:1153-1229`（沿用 Task 1 已拆分的准入段；确认 `_persist_snapshot` 不再在任何外层事务内被调用）
- Test: 追加 `backend/tests/test_admission_tx_boundary.py`

**Interfaces:**
- Consumes: Task 1 的三段式 `_try_admit_one`
- Produces: 快照落库独立提交（`_persist_snapshot` 内自有 `async with async_session()` + commit，无嵌套 session 提交）

**改造目标（design T1 验证项）**：Task 1 将 `capacity.provider.check` 移出事务后，`_persist_snapshot` 天然脱离外层事务；本任务用测试固化「无嵌套 session 提交」，并确认 `capacity.py` 无需改动。

- [x] **Step 1: 追加测试（test_admission_tx_boundary.py）**

```python
def test_persist_snapshot_commits_independently(db, monkeypatch):
    """_persist_snapshot 落库时不在任何外层 DB 事务上下文中（无嵌套提交）。"""
    provider = AsyncMock()
    provider.check = AsyncMock(return_value=True)
    provider.get_usage = AsyncMock()
    monkeypatch.setattr(transfer_mod, "async_session", db)
    monkeypatch.setattr(transfer_mod, "capacity", types.SimpleNamespace(provider=provider))
    monkeypatch.setattr(transfer_mod, "_preflight_quark_mount",
                        AsyncMock(return_value=None))
    monkeypatch.setattr(transfer_mod, "_transfer_chain",
                        AsyncMock(return_value="admitted"))
    snapshot_in_tx = []

    real_get_usage = provider.get_usage

    async def wrapped_get_usage():
        async with db() as s:
            snapshot_in_tx.append(s.in_transaction())
        return real_get_usage()

    provider.get_usage = wrapped_get_usage
    await _seed_pending(db)
    await transfer_mod._try_admit_one(0.0)
    assert snapshot_in_tx == [False], "get_usage（含 _persist_snapshot）不得在活动事务内被调用"
```

- [x] **Step 2: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_admission_tx_boundary.py -v`
Expected: PASS（Task 1 已把 check 移出事务；若红则检查 `_try_admit_one` 内是否有残留事务包裹 check）

- [x] **Step 3: 提交**

```bash
git add backend/tests/test_admission_tx_boundary.py
git commit -m "test(transfer): 固化容量快照独立提交无嵌套会话"
```

- [x] **Step 4: 勾选 tasks.md 1.2**

---

## Task 3: GID 白名单口径放宽（tasks.md 2.1）

**Files:**
- Modify: `backend/app/tasks/transfer.py:1481-1491`（`_admit_batch` GID 白名单查询）
- Test: 更新 `backend/tests/test_gid_escape.py`（新建）

**Interfaces:**
- Consumes: `aria2.client.tell_active()`、`aria2.client.tell_waiting()`
- Produces: `known_gids` 集合口径 = `SELECT aria2_gid WHERE aria2_gid IS NOT NULL`（不限 status）

**改造目标（design T2）**：现白名单只收 `status='downloading' AND aria2_gid 非空`；放宽为「DB 中 aria2_gid 非空全部行」，使回退中/在库任务不再误判陌生。

- [x] **Step 1: 更新 GID 校验测试**

```python
"""P1-2 GID 白名单逃生通道：白名单放宽 + 陌生 gid 连续跳过哨兵。"""
import types
from unittest.mock import AsyncMock
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from app.database import Base
import app.models  # noqa: F401
import app.tasks.transfer as transfer_mod
from app.models import DownloadQueue, Media


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def test_gid_whitelist_includes_non_downloading_rows(db, monkeypatch):
    """白名单口径放宽：非 downloading 但 aria2_gid 非空的行也在白名单内。"""
    db.add(Media(id=1, tmdb_id=1, title="测试剧", media_type="tv", status="tracking"))
    # 回退中的任务：status='pending' 但 aria2_gid 仍残留（recovery 回退未清 gid 的场景）
    db.add(DownloadQueue(
        id=1, media_id=1, episode="S01E01", file_name="a.mkv", file_size=1,
        share_code="sc", status="pending", aria2_gid="gid-pending-row",
        enqueued_at=transfer_mod._now(), updated_at=transfer_mod._now(),
    ))
    await db.commit()
    fake_aria2 = AsyncMock()
    fake_aria2.tell_active = AsyncMock(return_value=[{"gid": "gid-pending-row"}])
    fake_aria2.tell_waiting = AsyncMock(return_value=[])
    monkeypatch.setattr(transfer_mod, "async_session", db)
    monkeypatch.setattr(transfer_mod, "aria2", types.SimpleNamespace(client=fake_aria2))
    monkeypatch.setattr(transfer_mod, "_record_alert", AsyncMock())
    monkeypatch.setattr(transfer_mod, "_fetch_from_task_queue", AsyncMock(return_value=0))
    # 无 pending → 校验段前已空跑返回；需有 pending 才走到 GID 段，故先注入一个 pending
    db.add(DownloadQueue(
        id=2, media_id=1, episode="S01E02", file_name="b.mkv", file_size=1,
        share_code="sc", status="pending",
        enqueued_at=transfer_mod._now(), updated_at=transfer_mod._now(),
    ))
    await db.commit()
    await transfer_mod._admit_batch()
    # 未告警 = 白名单命中（旧实现 status='downloading' 限制会把该 gid 判陌生 → 告警）
    transfer_mod._record_alert.assert_not_awaited()
```

- [x] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_gid_escape.py -v`
Expected: FAIL（旧白名单按 status='downloading' 过滤，pending 行 gid 不在集合 → 触发告警）

- [x] **Step 3: 放宽白名单查询（transfer.py:1481-1491）**

```python
known_gids = {
    g for (g,) in (
        await s.execute(
            select(DownloadQueue.aria2_gid).where(
                DownloadQueue.aria2_gid.isnot(None),
            )
        )
    ).all()
}
```

- [x] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_gid_escape.py -v`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add backend/app/tasks/transfer.py backend/tests/test_gid_escape.py
git commit -m "fix(transfer): 放宽 GID 白名单口径至全部非空 gid 行"
```

- [x] **Step 6: 勾选 tasks.md 2.1**

---

## Task 4: 陌生 gid 连续跳过哨兵（tasks.md 2.2）

**Files:**
- Modify: `backend/app/tasks/transfer.py`（`_admit_batch` GID 校验段 L1492-1500 + 模块级常量区 L240 附近）
- Test: 追加 `backend/tests/test_gid_escape.py`

**Interfaces:**
- Consumes: Task 3 的放宽白名单
- Produces: `_unknown_gid_strikes: dict[str, int]`（模块级）、`_GID_STRIKE_LIMIT = 3`；哨兵触发 `aria2.client.remove(gid)` best-effort + `_record_alert`

**改造目标（design T2 双层之二）**：白名单未命中的 gid 每轮 `strikes += 1`，`≥3` 时执行一次 `aria2.client.remove(gid)` best-effort + 告警，清计数字典条目；陌生 gid 自然消失（终态）后从 dict 移除。

- [x] **Step 1: 追加哨兵测试**

```python
def test_unknown_gid_removed_after_three_strikes(db, monkeypatch):
    """孤儿 gid 连续 3 轮未命中白名单 → 触发 aria2.remove + 告警，且不永久阻断转存。"""
    fake_aria2 = AsyncMock()
    fake_aria2.tell_active = AsyncMock(return_value=[{"gid": "orphan-gid"}])
    fake_aria2.tell_waiting = AsyncMock(return_value=[])
    fake_aria2.remove = AsyncMock(return_value="OK")
    monkeypatch.setattr(transfer_mod, "async_session", db)
    monkeypatch.setattr(transfer_mod, "aria2", types.SimpleNamespace(client=fake_aria2))
    monkeypatch.setattr(transfer_mod, "_record_alert", AsyncMock())
    monkeypatch.setattr(transfer_mod, "_fetch_from_task_queue", AsyncMock(return_value=0))
    db.add(Media(id=1, tmdb_id=1, title="测试剧", media_type="tv", status="tracking"))
    db.add(DownloadQueue(
        id=1, media_id=1, episode="S01E01", file_name="a.mkv", file_size=1,
        share_code="sc", status="pending",
        enqueued_at=transfer_mod._now(), updated_at=transfer_mod._now(),
    ))
    await db.commit()
    transfer_mod._unknown_gid_strikes.clear()
    monkeypatch.setattr(transfer_mod, "_GID_STRIKE_LIMIT", 3)

    for _ in range(2):
        await transfer_mod._admit_batch()
    # 前两轮：计数未到 3，仅跳过，不 remove
    assert fake_aria2.remove.await_count == 0

    await transfer_mod._admit_batch()
    # 第 3 轮：触发一次 best-effort remove
    assert fake_aria2.remove.await_count == 1
    fake_aria2.remove.assert_awaited_with("orphan-gid")
    assert "orphan-gid" not in transfer_mod._unknown_gid_strikes


def test_known_gid_never_strikes(db, monkeypatch):
    """在库 gid（含非 downloading 行）不进入哨兵计数。"""
    fake_aria2 = AsyncMock()
    fake_aria2.tell_active = AsyncMock(return_value=[{"gid": "in-db-gid"}])
    fake_aria2.tell_waiting = AsyncMock(return_value=[])
    monkeypatch.setattr(transfer_mod, "async_session", db)
    monkeypatch.setattr(transfer_mod, "aria2", types.SimpleNamespace(client=fake_aria2))
    monkeypatch.setattr(transfer_mod, "_record_alert", AsyncMock())
    monkeypatch.setattr(transfer_mod, "_fetch_from_task_queue", AsyncMock(return_value=0))
    db.add(Media(id=1, tmdb_id=1, title="测试剧", media_type="tv", status="tracking"))
    db.add(DownloadQueue(
        id=1, media_id=1, episode="S01E01", file_name="a.mkv", file_size=1,
        share_code="sc", status="downloading", aria2_gid="in-db-gid",
        enqueued_at=transfer_mod._now(), updated_at=transfer_mod._now(),
    ))
    db.add(DownloadQueue(
        id=2, media_id=1, episode="S01E02", file_name="b.mkv", file_size=1,
        share_code="sc", status="pending",
        enqueued_at=transfer_mod._now(), updated_at=transfer_mod._now(),
    ))
    await db.commit()
    transfer_mod._unknown_gid_strikes.clear()
    for _ in range(5):
        await transfer_mod._admit_batch()
    assert fake_aria2.remove.await_count == 0
```

- [x] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_gid_escape.py -v`
Expected: FAIL（哨兵逻辑未实现）

- [x] **Step 3: 实现哨兵**

模块级（`transfer.py` 常量区，`_alert_cooldown` 定义 L240 附近）：

```python
_GID_STRIKE_LIMIT = 3
_unknown_gid_strikes: dict[str, int] = {}
```

改造 `_admit_batch` GID 校验段（L1492-1500）：对未命中白名单的 gid：

```python
for t in actives:
    gid = t.get("gid")
    if gid in known_gids:
        _unknown_gid_strikes.pop(gid, None)   # 在库 gid 自然清零
        continue
    strikes = _unknown_gid_strikes.get(gid, 0) + 1
    _unknown_gid_strikes[gid] = strikes
    if strikes >= _GID_STRIKE_LIMIT:
        _unknown_gid_strikes.pop(gid, None)   # 清计数防重复删除
        try:
            await aria2.client.remove(gid)
        except Exception as exc:  # noqa: BLE001  best-effort
            logger.warning("[transfer] 清理孤儿 aria2 任务失败 %s: %s", gid, exc)
        await _record_alert(
            None,
            f"检测到非本系统 aria2 任务 gid={gid}（连续 {_GID_STRIKE_LIMIT} 轮未在 DB 白名单），"
            f"已 best-effort 清理并告警，请人工确认 n8n 未误启动",
            category="gid",
        )
    else:
        await _record_alert(
            None,
            f"检测到非本系统 aria2 任务 gid={gid}（第 {strikes}/{_GID_STRIKE_LIMIT} 轮，暂跳过转存）",
            category="gid",
        )
```

注意：哨兵触发后不再 `return`（不永久阻断本批——本批其余任务继续准入；若需保持「本轮跳过」语义，按 design 边界：触发 remove 后继续循环）。任务在库有行（含 pending/回退中）不受影响。

- [x] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_gid_escape.py -v`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add backend/app/tasks/transfer.py backend/tests/test_gid_escape.py
git commit -m "fix(transfer): 陌生 gid 连续跳过哨兵解除转存自锁"
```

- [x] **Step 6: 勾选 tasks.md 2.2**

---

## Task 5: webhook 文件级推进（tasks.md 3.1）

**Files:**
- Modify: `backend/app/routers/nastools_notify.py:87-104`（`_advance_scrape_to_library`）+ `:119-149`（`_handle_transfer_finished` 传文件名）
- Test: 更新 `backend/tests/test_nastools_notify.py`

**Interfaces:**
- Consumes: webhook 载荷 `data.media_info`（tmdb_id/title）+ 文件名/集号字段
- Produces: `_advance_scrape_to_library(media_id, file_name=None) -> int`；无法定位 → 0 行推进 + 触发 library_check 轮询加速

**改造目标（design T3）**：现批量推进 `WHERE media_id=? AND status='scrape'` 全部行；改为从载荷提取文件名/集号（`SxxExx` / `第N集` / 文件名），与 scrape 行 `file_name`/`download_name`/`episode` 匹配，CAS 单行推进 `WHERE id=? AND status='scrape'`；无法定位 → 零推进，仅触发轮询。

- [x] **Step 1: 更新 webhook 测试（test_nastools_notify.py）**

先查看现有 `test_nastools_notify.py` 结构与既有断言，追加：

```python
async def test_single_file_webhook_advances_only_matching_row(db, monkeypatch):
    """单文件整理完成事件只推进匹配行，其余 scrape 行保持。"""
    # 构造同 media 两行 scrape：S01E01 与 S01E02
    # 调用 _advance_scrape_to_library(media_id, file_name="测试剧.S01E02.mkv")
    # 断言：S01E02 行 → library，S01E01 行仍 scrape，返回 1


async def test_webhook_cannot_locate_file_advances_nothing(db, monkeypatch):
    """载荷无法定位文件名 → 0 推进 + 触发 library_check 轮询（_check_library_background 被调用）。"""
```

- [x] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_nastools_notify.py -v`
Expected: FAIL（现实现批量推进全部 scrape 行）

- [x] **Step 3: 实现文件级推进**

`_advance_scrape_to_library(media_id, file_name=None)`：

- `file_name` 为空 → 返回 0（不推进），由 `_handle_transfer_finished` 侧仅触发 `_check_library_background`。
- 有文件名：规范化提取集号（复用 `_RE_SXXEXX`/`_RE_CN_EP` 匹配模式，参照 `library_check._episode_in_missing` 的匹配思路；可导入 `app.utils.fmt_episode`/`parse_episode_num`）；查询该 media 的 scrape 行集合，按 `episode == 规范化集号` 或 `file_name == 载荷文件名` 或 `download_name == 载荷文件名` 定位单行 → `UPDATE ... WHERE id=? AND status='scrape'`（CAS）。
- 返回推进行数（0 或 1）。

`_handle_transfer_finished`：从 `data` 提取文件名（`data.get("file_name")` / `data.get("name")` / `media_info` 内文件名字段，兼容旧版载荷缺失 → None），传入 `_advance_scrape_to_library(media, file_name)`；`advanced == 0` 时仍触发 `_check_library_background(media)` 轮询加速（不丢任务）。

- [x] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_nastools_notify.py -v`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add backend/app/routers/nastools_notify.py backend/tests/test_nastools_notify.py
git commit -m "fix(nastools): webhook 推进改为文件级单行定位，无法定位退化为轮询加速"
```

- [x] **Step 6: 勾选 tasks.md 3.1**

---

## Task 6: 推进 UPDATE 补齐节点重置（tasks.md 3.2）

**Files:**
- Modify: `backend/app/routers/nastools_notify.py`（`_advance_scrape_to_library` 的 UPDATE values）
- Test: 追加 `backend/tests/test_nastools_notify.py`

**Interfaces:**
- Consumes: Task 5 的文件级推进
- Produces: 推进 UPDATE 含 `node_attempt=0 / node_finished_at=now / node_error=None`

**改造目标（design T3）**：推进 UPDATE 从仅 `status/node_started_at/updated_at` 补齐节点重置字段，使进入 library 节点后重试计数归零、失败诊断清空。

- [x] **Step 1: 追加节点重置断言**

```python
async def test_advance_resets_node_fields(db, monkeypatch):
    """推进 scrape→library 时重置 node_attempt=0 / node_finished_at=now / node_error=None。"""
    # 预置一条 scrape 行：node_attempt=2, node_error='旧错误', node_finished_at=None
    # 调用 _advance_scrape_to_library(media_id, file_name=匹配名)
    # 断言：status='library', node_attempt=0, node_finished_at 非空, node_error is None
```

- [x] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_nastools_notify.py -v`
Expected: FAIL（现 UPDATE 不含节点重置字段）

- [x] **Step 3: 补充 UPDATE values**

```python
.values(
    status="library",
    node_started_at=now,
    node_attempt=0,
    node_finished_at=now,
    node_error=None,
    updated_at=now,
)
```

- [x] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_nastools_notify.py -v`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add backend/app/routers/nastools_notify.py backend/tests/test_nastools_notify.py
git commit -m "fix(nastools): 推进 library 时重置节点计数与失败诊断"
```

- [x] **Step 6: 勾选 tasks.md 3.2**

---

## Task 7: cancel/skip 同步 media 状态（tasks.md 4.1）

**Files:**
- Modify: `backend/app/routers/queue.py:345-386`（`cancel_task`）+ `:445-507`（`skip_task`）
- Test: 更新 `backend/tests/test_queue.py`

**Interfaces:**
- Consumes: `transfer_mod._sync_media_status(media_id, session)`（transfer.py:712）
- Produces: 置 DQ 终态后同一事务调用 `_sync_media_status`；media 无在途任务 → 回落 tracking

**改造目标（design T4）**：`cancel_task`/`skip_task` 置 DQ 终态（failed/skipped）后，同一事务内调用 `_sync_media_status(media_id, session)`——media 无任何 `_ACTIVE_STATUSES` 任务时条件回落 `tracking`；多任务在途时保持 downloading。

- [x] **Step 1: 更新 queue 测试（test_queue.py）**

```python
async def test_cancel_last_task_rolls_back_media_to_tracking(client, db):
    """取消 media 最后一个在途任务 → media.status 回落 tracking。"""
    # 预置：media(1) status='downloading'；一个 DownloadQueue(status='pending')
    # POST /api/queue/1/cancel
    # 断言：media.status == 'tracking'


async def test_cancel_with_other_inflight_keeps_downloading(client, db):
    """media 仍有其他在途任务时取消 → media.status 保持 downloading。"""
```

- [x] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_queue.py -v`
Expected: FAIL（现 cancel/skip 不调用 `_sync_media_status`）

- [x] **Step 3: 实现**

`cancel_task`（L369-374 区域）与 `skip_task`（L467-472 区域）：在 `update(DownloadQueue)...` + `update(TaskQueue)...` 之后、`await session.commit()` 之前插入：

```python
from app.tasks.transfer import _sync_media_status  # 函数内延迟导入防循环
await _sync_media_status(dq.media_id, session)
```

注意 `_sync_media_status(media_id, session)` 的 `session` 参数语义（transfer.py:712-747）：传入时复用外部事务、不自行提交——与 `cancel_task`/`skip_task` 的外层 `async with session.begin()` 或显式 `commit` 协调一致（queue.py 的 session 来自 `get_session` 依赖，当前用显式 `session.commit()` 模式；插入的调用在同一事务内，commit 前生效）。

- [x] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_queue.py -v`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add backend/app/routers/queue.py backend/tests/test_queue.py
git commit -m "fix(queue): cancel/skip 终态后同事务回落 media 状态"
```

- [x] **Step 6: 勾选 tasks.md 4.1**

---

## Task 8: 转存提交冲突清理夸克残留（tasks.md 5.1）

**Files:**
- Modify: `backend/app/tasks/transfer.py:953-1068`（`_transfer_chain`）+ `:878-950`（`_commit_downloading`）
- Test: Create `backend/tests/test_transfer_quark_cleanup.py`

**Interfaces:**
- Consumes: `_split_quark_path`（transfer.py:86）、`alist.remove(...)`
- Produces: `_transfer_chain` 将 `final_quark_path` 传入 `_commit_downloading`；`_DownloadStateChanged` 分支追加 `alist.remove(final_quark_path)` best-effort

**改造目标（design T5）**：`_commit_downloading` 的 `_DownloadStateChanged` 冲突分支（L920-935）目前只清 aria2 gid；追加清理夸克残留 `final_quark_path`（复用 `_split_quark_path` + `alist.remove(names, dir_part)`），失败仅告警。

- [ ] **Step 1: 写失败测试**

```python
"""P1-5 转存提交冲突清理夸克残留。"""
import types
from unittest.mock import AsyncMock
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from app.database import Base
import app.models  # noqa: F401
import app.tasks.transfer as transfer_mod
from app.models import DownloadQueue


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def test_conflict_removes_final_quark_path(db, monkeypatch):
    """_DownloadStateChanged 冲突分支调用 alist.remove(final_quark_path)。"""
    fake_alist = AsyncMock()
    fake_alist.remove = AsyncMock(return_value=True)
    fake_aria2 = AsyncMock()
    fake_aria2.remove = AsyncMock(return_value="OK")
    monkeypatch.setattr(transfer_mod, "async_session", db)
    monkeypatch.setattr(transfer_mod, "alist", fake_alist)
    monkeypatch.setattr(transfer_mod, "aria2", types.SimpleNamespace(client=fake_aria2))
    monkeypatch.setattr(transfer_mod, "notifier", AsyncMock())
    db.add(DownloadQueue(
        id=1, media_id=1, episode="S01E01", file_name="a.mkv", file_size=1,
        share_code="sc", status="transferring",  # 与 CAS 的 transferring 门控一致
        enqueued_at=transfer_mod._now(), updated_at=transfer_mod._now(),
    ))
    await db.commit()

    # 并发方已变动状态 → 事务 B 内 UPDATE rowcount=0 → _DownloadStateChanged
    async with db() as s:
        from sqlalchemy import update
        await s.execute(update(DownloadQueue).where(DownloadQueue.id == 1)
                        .values(status="pending"))
        await s.commit()

    result = await transfer_mod._commit_downloading(
        1, 1, "S01E01", "a.mkv", "out.mkv", "gid-1", None, 0.0,
        quark_path="/quark/新名.mkv",
    )
    assert result == "conflict"
    fake_aria2.remove.assert_awaited_with("gid-1")
    # alist.remove 以 final_quark_path 拆分后的目录与文件名调用
    fake_alist.remove.assert_awaited()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_transfer_quark_cleanup.py -v`
Expected: FAIL（现冲突分支只调 aria2.remove，不调 alist.remove）

- [ ] **Step 3: 实现**

`_commit_downloading` 的 `_DownloadStateChanged` 分支（L920-935）在 `aria2.remove(gid)` 之后追加：

```python
try:
    q_name, q_dir = _split_quark_path(quark_path)
    await alist.remove(q_name, q_dir) if q_dir else await alist.remove(q_name)
except Exception as exc:  # noqa: BLE001  清理失败仅告警
    logger.warning("[transfer] 清理夸克残留失败 %s: %s", quark_path, exc)
```

确认 `_split_quark_path` 返回结构（transfer.py:86 从 `app.utils` 导入；按实际签名调用——若返回 `(name, dir_part)` 则 `alist.remove(name, dir_part)`）。`quark_path` 参数已由调用方传入（`_transfer_chain` L1067 已传 `quark_path=final_quark_path`——确认参数透传存在；若 `_transfer_chain` 未传则在 L1067 补上）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_transfer_quark_cleanup.py -v`
Expected: PASS

- [ ] **Step 5: 回归 + 提交**

Run: `cd backend && python -m pytest tests/test_fix_p0_recovery_cleanup_transfer.py -q`
Expected: PASS

```bash
git add backend/app/tasks/transfer.py backend/tests/test_transfer_quark_cleanup.py
git commit -m "fix(transfer): 转存提交冲突时清理夸克残留文件"
```

- [ ] **Step 6: 勾选 tasks.md 5.1**

---

## Task 9: 取件-创建原子化（tasks.md 6.1）

**Files:**
- Modify: `backend/app/tasks/transfer.py:1273-1362`（`_fetch_from_task_queue`）
- Test: 更新 `backend/tests/test_transfer_queue.py`

**Interfaces:**
- Consumes: `TaskQueue(ready)`、`DownloadQueue` 模型
- Produces: 单语句条件 INSERT（`INSERT ... SELECT ... WHERE NOT EXISTS`）实现取件与 DQ 创建原子一致；撞 UNIQUE 时源行不误标 done

**改造目标（design T6）**：现实现先 CAS `ready→done`（L1322-1328）再保存点 INSERT DQ（L1343-1360）；撞 UNIQUE 时保存点回滚但 done 已在外层事务提交。改为单语句条件 INSERT + 命中行同事务置 done。

- [ ] **Step 1: 更新取件测试（test_transfer_queue.py）**

```python
async def test_fetch_conflict_keeps_source_row_ready(db, monkeypatch):
    """并发先建 DQ（同键）时：源 task_queue 行保持 ready，不误标 done。"""
    # 预置：task_queue(1) status='ready'（media=1, episode=S01E01）
    #        download_queue 已有同键行（media=1, episode=S01E01）
    # 调用 _fetch_from_task_queue()
    # 断言：task_queue(1).status == 'ready'（旧实现先置 done 会误标）
    #        download_queue 行数不变
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_transfer_queue.py -v`
Expected: FAIL（旧实现 CAS 先置 done）

- [ ] **Step 3: 实现单语句原子取件**

`_fetch_from_task_queue` 改为：对候选 rows（保留 L1298-1316 的 NOT EXISTS 预筛），对每行执行：

```python
res = await s.execute(
    insert(DownloadQueue)
    .from_select(
        [
            DownloadQueue.media_id, DownloadQueue.episode,
            DownloadQueue.task_queue_id, DownloadQueue.file_name,
            DownloadQueue.file_size, DownloadQueue.size_estimated,
            DownloadQueue.share_code, DownloadQueue.pwd_id,
            DownloadQueue.stoken, DownloadQueue.receive_code,
            DownloadQueue.fids, DownloadQueue.fid_tokens,
            DownloadQueue.folder_id, DownloadQueue.status,
            DownloadQueue.enqueued_at, DownloadQueue.updated_at,
        ],
        select(
            r.media_id, r.episode, r.id, r.file_name, r.file_size,
            r.size_estimated, r.share_code, r.pwd_id, r.stoken,
            r.receive_code, r.fids, r.fid_tokens, r.folder_id,
            literal("pending"), now, now,
        ).where(
            TaskQueue.id == r.id,
            TaskQueue.status == "ready",
            ~exists(
                select(DownloadQueue.id).where(
                    DownloadQueue.media_id == r.media_id,
                    DownloadQueue.episode == r.episode,
                )
            ),
        ),
    )
)
if res.rowcount == 1:
    # 插入成功 → 同事务置源行 done（WHERE id AND status='ready' 条件更新）
    await s.execute(
        update(TaskQueue).where(TaskQueue.id == r.id, TaskQueue.status == "ready")
        .values(status="done", updated_at=now)
    )
    fetched += 1
```

说明：`INSERT ... SELECT ... WHERE NOT EXISTS` 在 SQLite 与 Postgres 均支持（design T6 边界）。条件内嵌 `TaskQueue.status='ready'` 保证源行未被并发取件；`NOT EXISTS` 保证同键无 DQ。影响行数 0 = 撞 UNIQUE 或源行已取 → 不置 done、保持 ready，由下轮或并发路径处理。不再需要 `begin_nested()` 保存点（单语句原子）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_transfer_queue.py -v`
Expected: PASS

- [ ] **Step 5: 回归 + 提交**

Run: `cd backend && python -m pytest tests/test_fix_p0_recovery_cleanup_transfer.py tests/test_p1_fixes.py -q`
Expected: PASS

```bash
git add backend/app/tasks/transfer.py backend/tests/test_transfer_queue.py
git commit -m "fix(transfer): 取件与 DQ 创建改为单语句条件 INSERT 原子化"
```

- [ ] **Step 6: 勾选 tasks.md 6.1**

---

## Task 10: 容量记账漏计窗口（tasks.md 7.1）

**Files:**
- Modify: `backend/app/services/capacity.py`（`CapacityProvider`，L114-180）
- Modify: `backend/app/tasks/transfer.py:878-950`（`_commit_downloading` 成功路径）
- Test: 追加 `backend/tests/test_capacity.py`

**Interfaces:**
- Consumes: `CapacityProvider`（capacity.py:114）
- Produces: `CapacityProvider.invalidate_usage_cache()`（置 `_usage_cache=None`）；`_commit_downloading` 成功后调用

**改造目标（design T7）**：`CapacityProvider` 新增 `invalidate_usage_cache()`；`_commit_downloading` 成功（返回 'admitted'）后调用——文件已落盘 downloading，立即使下一轮准入反映真实 used（downloading 仍不计 reserved，防双计）。

- [ ] **Step 1: 追加测试（test_capacity.py）**

```python
async def test_invalidate_usage_cache_forces_recount(db, monkeypatch):
    """invalidate_usage_cache 后 get_usage 重新统计（monkeypatch list_dir 计数断言）。"""
    from app.services import capacity as capacity_mod

    provider = capacity_mod.CapacityProvider.__new__(capacity_mod.CapacityProvider)
    provider._fallback_quota_gb = 100.0
    provider._usage_cache = object()   # 模拟已有缓存
    provider._usage_cached_at = 9999.0  # 未过期
    provider.invalidate_usage_cache()
    assert provider._usage_cache is None


async def test_commit_downloading_invalidates_cache(db, monkeypatch):
    """_commit_downloading 成功后调用 capacity.provider.invalidate_usage_cache。"""
    # 复用 test_transfer_quark_cleanup 的 fixture 模式，mock capacity.provider
    # 断言 provider.invalidate_usage_cache 被 await 一次（'admitted' 路径）
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_capacity.py -v`
Expected: FAIL（invalidate_usage_cache 不存在）

- [ ] **Step 3: 实现**

capacity.py `CapacityProvider` 增加：

```python
def invalidate_usage_cache(self) -> None:
    """准入提交成功后使 used 缓存失效（downloading 落盘立即反映真实 used）。"""
    self._usage_cache = None
    self._usage_cached_at = 0.0
```

transfer.py `_commit_downloading` 成功路径（L950 `return "admitted"` 前）：

```python
try:
    capacity.provider.invalidate_usage_cache()
except Exception as exc:  # noqa: BLE001  缓存失效失败不影响主流程
    logger.debug("[transfer] 容量缓存失效失败: %s", exc)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_capacity.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/capacity.py backend/app/tasks/transfer.py backend/tests/test_capacity.py
git commit -m "fix(capacity): 转存提交后失效 used 缓存消除漏计窗口"
```

- [ ] **Step 6: 勾选 tasks.md 7.1**

---

## Task 11: 刮削连坐风暴退避（tasks.md 8.1）

**Files:**
- Modify: `backend/app/tasks/library_check.py`（`scrape_runner` L246-251 / `_run_scrape_once` 及刮削失败处理 L205-243）
- Test: 追加 `backend/tests/test_library_check.py`

**Interfaces:**
- Consumes: `_mark_scrape_failed`（library_check.py:205 附近，CAS 计数）
- Produces: 模块级 `_scrape_backoff: dict[int, float]`（media_id → 退避截止时间戳）；`scrape_runner` 对该 media 10min 内不重复 force sync

**改造目标（design T8.1）**：`scrape_runner` 失败后对该 media 设置进程内退避（10min 内不再触发 force sync）；同步失败与节点重试计数解耦（不批量累加全部 scrape 行 node_attempt——仅对「本 media 本轮尝试的那个任务」计数或交由 recover）。

- [ ] **Step 1: 追加测试（test_library_check.py）**

```python
async def test_scrape_failure_sets_backoff_and_stops_cascade(db, monkeypatch):
    """NasTools 故障：本 media 进入 10min 退避；不批量累加全部 scrape 行 node_attempt。"""
    # 预置同 media 两行 scrape（node_attempt=0/0），mock 刮削同步失败
    # 第一轮调用 → 仅一行 node_attempt++（不批量）+ media 退避标记
    # 第二轮立即调用 → 退避期内跳过（node_attempt 不再变化）
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_library_check.py -v`
Expected: FAIL（现实现批量累加）

- [ ] **Step 3: 实现**

- 模块级 `_SCRAPE_BACKOFF_SECONDS = 600`、`_scrape_backoff: dict[int, float] = {}`。
- `_run_scrape_once` 刮削失败的 media 处理：`_scrape_backoff[media_id] = now + 600`；`_mark_scrape_failed` 的 `pending` 参数只包含「本 media 本轮实际尝试的任务」而非该 media 全部 scrape 行。
- `scrape_runner`/`_run_scrape_once` 入口对退避期内的 media 跳过（读取 `_scrape_backoff`，过期清理）。
- 保持 CAS（`WHERE node_attempt == cur_attempt`）与终态转 failed 语义。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_library_check.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/tasks/library_check.py backend/tests/test_library_check.py
git commit -m "fix(library_check): 刮削失败按 media 进程内退避并解耦批量计数"
```

- [ ] **Step 6: 勾选 tasks.md 8.1**

---

## Task 12: aria2 add_uri options（tasks.md 8.2）

**Files:**
- Modify: `backend/app/tasks/transfer.py:1055-1059`（`_transfer_chain` 的 `aria2.add_uri` 调用）
- Test: 更新 `backend/tests/test_aria2_rpc_shapes.py`

**Interfaces:**
- Consumes: `aria2.client.add_uri(...)`
- Produces: `add_uri` options 追加 `allow-overwrite=true, auto-file-renaming=false`

**生产确认点（design T8.2/风险表）**：实施前核对生产 aria2 启动参数——若 aria2 已配置 `allow-overwrite=true`/`auto-file-renaming=false` 则跳过本任务并记录（不阻塞主修复）。

- [ ] **Step 1: 核对生产 aria2 启动参数**

检查部署配置（docker-compose / systemd / 运维文档中 aria2 启动参数）。若已配置 → 在本任务记录「已配置，跳过」，勾选 tasks.md 8.2 并提交说明性 commit（或直接进入 Step 6）。若未配置 → 继续。

- [ ] **Step 2: 更新 RPC shape 测试（test_aria2_rpc_shapes.py）**

```python
async def test_add_uri_options_shape():
    """add_uri options 含 allow-overwrite=true 与 auto-file-renaming=false。"""
    # 参照既有 shape 测试风格：捕获 _transfer_chain 内 add_uri 调用的 options 参数
    # 断言 options.get("allow-overwrite") == "true"
    # 断言 options.get("auto-file-renaming") == "false"
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_aria2_rpc_shapes.py -v`
Expected: FAIL（现 add_uri 无 options）

- [ ] **Step 4: 实现**

`_transfer_chain` L1055-1059：

```python
gid = await aria2.client.add_uri(
    link,
    out=out_name,
    comment=f"{_COMMENT_PREFIX}{media_id}:{episode}",
    options={
        "allow-overwrite": "true",
        "auto-file-renaming": "false",
    },
)
```

按既有 add_uri 包装的实际签名确认 options 参数名（若包装层为 `**kwargs` 透传 aria2 RPC，则按 RPC 契约 `options` dict）。

- [ ] **Step 5: 跑测试确认通过 + 提交**

Run: `cd backend && python -m pytest tests/test_aria2_rpc_shapes.py -v`
Expected: PASS

```bash
git add backend/app/tasks/transfer.py backend/tests/test_aria2_rpc_shapes.py
git commit -m "fix(transfer): aria2 add_uri 追加覆盖与禁重命名 options"
```

- [ ] **Step 6: 勾选 tasks.md 8.2（并记录生产核对结果）**

---

## Task 13: 集级确认 fail-open（tasks.md 8.3）

**Files:**
- Modify: `backend/app/tasks/library_check.py`（`_episode_in_missing` L271-297 及调用方 `library_check` L300+）
- Test: 更新 `backend/tests/test_library_check.py`

**Interfaces:**
- Consumes: `_episode_in_missing(episode, missing_codes)`、Emby 遗漏集
- Produces: 模块级 `_recent_empty_check: dict[str, float]`；遗漏集为空时延迟一轮复核（≥60s 才放行）；电影入库确认前校验文件大小合理性

**改造目标（design T8.3）**：`_episode_in_missing` 遗漏集为空时不再直接返回 False（立即放行）；增加延迟复核标记（进程内 dict，间隔 ≥60s 才放行）。电影（media_type='movie'）入库确认前校验文件大小合理性。

- [ ] **Step 1: 更新测试（test_library_check.py）**

```python
async def test_episode_in_missing_empty_list_delays_recheck():
    """遗漏集为空时首次返回等待，间隔未到不重复校验，≥60s 后放行。"""
    # 调用 _episode_in_missing("S01E01", set()) 首次 → True（等待复核）
    # 立即再调 → True（60s 窗口内）
    # 将 _recent_empty_check 时间戳前移 61s → 再调 → False（放行）


async def test_movie_size_check_on_confirmation(db, monkeypatch):
    """电影入库确认前校验文件大小合理性（异常大小不 finalize）。"""
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_library_check.py -v`
Expected: FAIL（现遗漏集为空直接返回 False）

- [ ] **Step 3: 实现**

```python
_RECENT_EMPTY_RECHECK_SECONDS = 60
_recent_empty_check: dict[str, float] = {}   # key = f"{media_id}:{episode}"


def _episode_in_missing(episode, missing_codes):
    episode = (episode or "").strip()
    if not episode:
        return False
    if not missing_codes:
        # fail-open 延迟复核：遗漏集为空 → 至少间隔 60s 才放行一次
        key = ...
        last = _recent_empty_check.get(key, 0.0)
        if time.monotonic() - last < _RECENT_EMPTY_RECHECK_SECONDS:
            return True
        _recent_empty_check[key] = time.monotonic()
        return False
    # ... 原有三重匹配逻辑不变
```

电影文件大小合理性：`library_check` finalize 电影前，比对 `dq.file_size` 与 Emby 侧文件/媒体大小（或配置阈值），明显不合理（如 0 字节、与预期集大小偏离过大）→ 不 finalize、告警记录。具体比对依据按 `library_check` 现有 Emby 查询结果确定（实施时确认可用字段；无法可靠获取时记录并跳过，不阻塞）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_library_check.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/tasks/library_check.py backend/tests/test_library_check.py
git commit -m "fix(library_check): 集级确认遗漏集为空延迟复核并校验电影大小"
```

- [ ] **Step 6: 勾选 tasks.md 8.3**

---

## Task 14: nastools_sync 失败通知节流（tasks.md 8.4）

**Files:**
- Modify: `backend/app/tasks/nastools_sync.py`（失败通知点）
- Test: 追加 `backend/tests/test_nastools_notify.py` 或既有 nastools 相关测试文件

**Interfaces:**
- Consumes: `_record_alert` 的节流模式参考（transfer.py:764-808）
- Produces: nastools_sync 失败通知接入模块级节流（复用 `_alert_cooldown` 模式）

**改造目标（design T8.4）**：`nastools_sync` 失败通知复用 `_alert_cooldown` 模式（模块级 dict + TTL）；节流后不重复通知。

- [ ] **Step 1: 追加测试**

```python
async def test_nastools_sync_failure_notify_throttled(db, monkeypatch):
    """同一失败类别在节流窗口内只通知一次。"""
    # 触发两次相同失败 → notifier.notify 只被调用一次（第二次节流跳过）
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_nastools_notify.py -v`
Expected: FAIL（现无节流）

- [ ] **Step 3: 实现**

`nastools_sync.py` 模块级增加 `_sync_alert_cooldown: dict[str, tuple[float, str]] = {}` + TTL 常量（复用 transfer `_ALERT_COOLDOWN_SECONDS` 语义，如 600s）；失败通知点按 (media_id/类别) 节流：窗口内同指纹跳过 `notifier.notify`，`task_run` 记录保留。可显式传入节流指纹使不同消息共享同一指纹。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_nastools_notify.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/tasks/nastools_sync.py backend/tests/test_nastools_notify.py
git commit -m "fix(nastools_sync): 失败通知接入进程内节流防刷屏"
```

- [ ] **Step 6: 勾选 tasks.md 8.4**

---

## Task 15: quota_wait 唤醒后容量预查（tasks.md 8.5）

**Files:**
- Modify: `backend/app/tasks/transfer.py`（`_admit_batch` 唤醒段 L1405-1436 之后）
- Test: 追加 `backend/tests/test_transfer_queue.py`

**Interfaces:**
- Consumes: `capacity.provider.get_usage()`、`capacity.provider._load_margin_gb`
- Produces: 唤醒后先查容量余量；余量为 0 直接返回（不进入准入循环）

**改造目标（design T8.5）**：quota_wait 唤醒后先 `capacity.get_usage` 算余量，余量为 0 直接返回（减少 N×UPDATE + 容量查询）。

- [ ] **Step 1: 追加测试**

```python
async def test_quota_wake_with_zero_room_skips_admission_loop(db, monkeypatch):
    """唤醒后余量为 0 → 不进入准入循环（_try_admit_one 不被调用）。"""
    # mock get_usage 返回 used_gb=quota（余量 0）
    # 预置 quota_wait 行 → 调用 _admit_batch
    # 断言：_try_admit_one 未调用（或调用次数 0），quota_wait 行保持 pending
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_transfer_queue.py -v`
Expected: FAIL（现唤醒后直接进入准入循环）

- [ ] **Step 3: 实现**

`_admit_batch` 唤醒段（L1430 告警之后、`_fetch_from_task_queue`/准入循环之前）：

```python
try:
    usage = await capacity.provider.get_usage()
    quota_gb = await capacity.provider._load_quota_gb()
    margin_gb = await capacity.provider._load_margin_gb(quota_gb)
    remaining_gb = max(0.0, quota_gb - usage.used_gb - margin_gb)
    if remaining_gb <= 0:
        logger.info("[transfer] 容量余量不足（%.2fG），唤醒后直接返回不进入准入循环", remaining_gb)
        return
except Exception as exc:  # noqa: BLE001  容量不可用 → 交给准入循环 fail-closed 处理
    logger.debug("[transfer] 唤醒后容量预查失败（由准入循环 fail-closed 兜底）: %s", exc)
```

注意：预查失败不阻断（由准入循环内的 fail-closed 语义兜底）；预查是优化，不是新的硬门。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_transfer_queue.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/tasks/transfer.py backend/tests/test_transfer_queue.py
git commit -m "fix(transfer): quota_wait 唤醒后先查容量余量减少写放大"
```

- [ ] **Step 6: 勾选 tasks.md 8.5**

---

## Task 16: 取件凭据完整性校验（tasks.md 8.6）

**Files:**
- Modify: `backend/app/tasks/transfer.py:1331-1339`（`_fetch_from_task_queue` 凭据兜底段）
- Test: 追加 `backend/tests/test_transfer_queue.py`

**Interfaces:**
- Consumes: Task 9 的单语句取件
- Produces: file_name/file_size/share_code 缺失 → 保持源行 ready + 告警，不建注定失败的 DQ

**改造目标（design T8.6）**：取件时校验 file_name/file_size/share_code 非空，缺失保持源行 ready + 告警。

- [ ] **Step 1: 追加测试**

```python
async def test_fetch_incomplete_credentials_keeps_source_ready(db, monkeypatch):
    """凭据缺失（file_name/file_size/share_code 任一为空）→ 源行保持 ready + 告警。"""
    # 预置 task_queue(1) status='ready'，share_code=''（缺失）
    # 调用 _fetch_from_task_queue()
    # 断言：task_queue(1).status == 'ready'（不置 done、不建 DQ）
    #        _record_alert 被调用
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_transfer_queue.py -v`
Expected: FAIL（现实现空值兜底拷贝继续建 DQ）

- [ ] **Step 3: 实现**

`_fetch_from_task_queue` 循环内、单语句 INSERT 之前：对 `file_name`/`file_size`/`share_code` 缺失的行：

```python
if not (r.file_name and (r.file_size or 0) > 0 and r.share_code):
    await _record_alert(
        r.media_id,
        f"task_queue id={r.id} media={r.media_id} episode={r.episode} 转存凭据不完整"
        f"（file_name/file_size/share_code 缺失），保持 ready 不建注定失败的 DQ",
        category="transfer",
    )
    continue   # 不置 done、不建 DQ；下轮重试（或由上游探测路径补全凭据）
```

保持 `_fetch_from_task_queue` 返回计数语义。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_transfer_queue.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/tasks/transfer.py backend/tests/test_transfer_queue.py
git commit -m "fix(transfer): 取件校验凭据完整性拒绝建注定失败的 DQ"
```

- [ ] **Step 6: 勾选 tasks.md 8.6**

---

## Task 17: save_task_id 条件化清理（tasks.md 8.7）

**Files:**
- Modify: `backend/app/tasks/transfer.py:628-641`（`_node_failure` 无条件清 save_task_id 段）
- Test: 追加 `backend/tests/test_transfer_queue.py`

**Interfaces:**
- Consumes: `_node_failure`（transfer.py:587）
- Produces: 清 save_task_id 改为 `WHERE status != 'transferring'` 条件化（不抹掉并发方新 save 的 task_id）

**改造目标（design T8.7）**：`_node_failure` 目前无条件清 `save_task_id/save_attempt_at`（L628-631 及 CAS 冲突分支 L636-641）；改为仅在 `status != 'transferring'` 时清——`transferring` 状态说明并发方已重新 save 并落库了新 task_id，不应抹掉。

- [ ] **Step 1: 追加测试**

```python
async def test_node_failure_does_not_clear_fresh_save_id(db, monkeypatch):
    """并发方已将任务转回 transferring 并新 save → _node_failure 不清 save_task_id。"""
    # 预置 dq：status='transferring', save_task_id='new-save', retry_count=0, node_attempt=0
    # 调用 _node_failure(..., clear_save=True)
    # 断言：save_task_id 仍为 'new-save'（未被无条件清空）
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_transfer_queue.py -v`
Expected: FAIL（现实现无条件清空）

- [ ] **Step 3: 实现**

`_node_failure` 的两处 `save_task_id=None if clear_save else ...`（L628-631 与 CAS 冲突兜底 L636-641）改为条件化：仅在 `DownloadQueue.status != "transferring"` 时清空。实现方式：VALUES 中 `save_task_id` 用表达式区分——简单可靠做法是拆分 UPDATE 语句，或使用 `case()`：

```python
# 主 UPDATE 的 values 中：
save_task_id=(
    None
    if (clear_save and status != "transferring")
    else DownloadQueue.save_task_id
),
```

由于 UPDATE 的 WHERE 已有 `retry_count/node_attempt` CAS，实施时确认最佳实现：可改为两条条件语句，或利用 SQL 表达式 `case((DownloadQueue.status == 'transferring', DownloadQueue.save_task_id), else_=None)`。保证：`status='transferring'` 时保留；其他状态（CAS 命中回退路径）清空。CAS 冲突兜底分支（L636-641）同样条件化。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_transfer_queue.py -v`
Expected: PASS

- [ ] **Step 5: 回归 + 提交**

Run: `cd backend && python -m pytest tests/test_fix_p0_recovery_cleanup_transfer.py tests/test_p1_fixes.py -q`
Expected: PASS

```bash
git add backend/app/tasks/transfer.py backend/tests/test_transfer_queue.py
git commit -m "fix(transfer): save_task_id 清理按状态条件化防抹并发新 save"
```

- [ ] **Step 6: 勾选 tasks.md 8.7**

---

## Task 18: 全量模式防重（tasks.md 8.8）

**Files:**
- Modify: `backend/app/tasks/scan.py`（`_enqueue` L1213-1302）
- Test: 追加 `backend/tests/test_scan_dedup.py`（或并入既有 scan 测试）

**Interfaces:**
- Consumes: `_enqueue(media_id, episode_key, file_name, file_size, share_code, payload)`（scan.py:1213）
- Produces: 入队前对 download_queue 补集号归一化防重查询（同 SxxExx 键存在则跳过）

**改造目标（design T8.8）**：全量模式防重——`_enqueue` 对 download_queue 补集号归一化防重（跨文件名同集不再重复入队）。现状 `_enqueue` 只查 task_queue 同键/同文件名；补查 download_queue：同 media 下已有该集（归一化集号匹配）任意状态的行 → 返回 'existing' 不入队。

- [ ] **Step 1: 追加测试**

```python
async def test_enqueue_skips_when_download_queue_has_same_episode(db, monkeypatch):
    """同 media 同集已在 download_queue（跨文件名）→ _enqueue 返回 existing 不入队。"""
    # 预置：download_queue 已有 (media=1, episode='S01E01', file_name='甲.mkv')
    # 调用 _enqueue(1, 'S01E01', '乙.mkv', ...)  → 返回 'existing'
    # 断言 task_queue 无新行


async def test_enqueue_normalizes_episode_key_for_dedup(db, monkeypatch):
    """文件名不同但归一化集号相同（'S1E1' vs 'S01E01'）→ 防重命中。"""
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_scan_dedup.py -v`
Expected: FAIL（现 _enqueue 不查 download_queue）

- [ ] **Step 3: 实现**

`_enqueue` 幂等检查段（L1236-1258 之后）增加 download_queue 防重查询：同 media 下按归一化集号匹配（复用 `app.utils.fmt_episode`/`parse_episode_num` 规范化，覆盖 `S01E01`/`S1E1`/`第1集`/`01` 等表示），命中任意状态行 → `return "existing"`。保持 task_queue 同键/同文件名防重不变。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_scan_dedup.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/tasks/scan.py backend/tests/test_scan_dedup.py
git commit -m "fix(scan): 入队对 download_queue 补集号归一化防重"
```

- [ ] **Step 6: 勾选 tasks.md 8.8**

---

## Task 19: NaSTools webhook token query 通道移除（tasks.md 8.9）

**Files:**
- Modify: `backend/app/routers/nastools_notify.py:68-79`（`_token_from_request`）
- Test: 更新 `backend/tests/test_nastools_notify.py`

**Interfaces:**
- Consumes: `_token_from_request(request)`
- Produces: 仅保留 header（`X-NaSTools-Token`）/ `Authorization` 通道；移除 `?token=` query 通道

**生产确认点（design T8.9/风险表）**：实施前确认是否有部署依赖 query 通道（旧版 NaSTools 插件仅支持 query token 时，移除会导致 webhook 401）。有依赖 → 保留并补文档说明；无依赖 → 移除。

- [ ] **Step 1: 确认部署依赖**

检查部署文档/既有 NaSTools 插件配置。若确认有 query 通道依赖 → 记录「保留 + 文档说明」，勾选 tasks.md 8.9 并提交文档性 commit。若确认无依赖 → 继续。

- [ ] **Step 2: 更新鉴权测试（test_nastools_notify.py）**

```python
async def test_query_token_channel_removed(client):
    """?token= query 通道不再放行（仅 header 鉴权）。"""
    # 仅带 ?token=<secret> 请求 → 401


async def test_header_token_still_works(client):
    """X-NaSTools-Token header 通道仍正常。"""
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_nastools_notify.py -v`
Expected: FAIL（现实现 query 通道放行）

- [ ] **Step 4: 实现**

`_token_from_request`（L68-79）删除 `request.query_params.get("token")` 分支，仅保留 `X-NaSTools-Token` → `Authorization` 顺序。

- [ ] **Step 5: 跑测试确认通过 + 提交**

Run: `cd backend && python -m pytest tests/test_nastools_notify.py -v`
Expected: PASS

```bash
git add backend/app/routers/nastools_notify.py backend/tests/test_nastools_notify.py
git commit -m "fix(nastools): webhook 鉴权移除 query token 通道"
```

- [ ] **Step 6: 勾选 tasks.md 8.9（并记录部署确认结果）**

---

## Task 20: 全量集成验证（tasks.md 9.1）

**Files:**
- Test: `backend/tests/` 全量

**Interfaces:**
- Consumes: Task 1-19 全部实现
- Produces: 全量 pytest 通过证据

- [ ] **Step 1: 全量运行 pytest**

Run: `cd backend && python -m pytest tests/ -x -q 2>&1 | tail -20`
Expected: PASS（含既有 + 新增回归测试；`test_fix_p0_recovery_cleanup_transfer.py` / `test_p1_fixes.py` / `test_oracle_fixes.py` / `test_council_fixes.py` 全绿——状态机 CAS 幂等协议无破坏）

若失败：按 `systematic-debugging` 流程定位，修复后重跑（回到对应任务，不直接进 Verify）。

- [ ] **Step 2: 提交（如修复产生改动）**

```bash
git add -A backend/
git commit -m "test(backend): 全量回归通过修复遗留问题"
```

- [ ] **Step 3: 勾选 tasks.md 9.1**

---

## Task 21: design Open Questions 复查（tasks.md 9.2）

**Files:**
- Modify: `docs/superpowers/specs/2026-09-12-transfer-flow-reliability-design.md`（或另立说明）
- Test: 无（文档任务）

**Interfaces:**
- Consumes: Task 12（aria2 参数确认结果）、Task 19（NaSTools query 通道确认结果）、部署 worker 数
- Produces: design.md Open Questions 处置记录更新

- [ ] **Step 1: 复查 Open Questions**

design.md「4. 技术风险与缓解」表 + 各任务生产确认点：
- 8.2 aria2 参数确认结果（Task 12 记录）
- 8.9 NaSTools query 通道确认结果（Task 19 记录）
- 部署 worker 数（单 worker 假设是否成立）

- [ ] **Step 2: 更新文档**

将确认结果写入 design.md 对应条目（或补充「处置记录」小节）；无法确认的项标注「未确认，另立后续 change」。

- [ ] **Step 3: 提交**

```bash
git add docs/superpowers/specs/2026-09-12-transfer-flow-reliability-design.md
git commit -m "docs(design): 记录 Open Questions 处置结果"
```

- [ ] **Step 4: 勾选 tasks.md 9.2**

---

## 自检清单（Self-Review）

**Spec 覆盖**：T1/T2 → tasks 1-2；T3 → 3-4；T4 → 5-6；T5 → 7；T6 → 8；T7 → 9；T8 → 10-11；T9 → 12-13；T10 → 14-15；T11 → 16；T12 → 17；T13 → 18；T14 → 19；T15 → 20；T16 → 21；T17 → 22（tasks.md 21 个 checkbox 全覆盖）。

**Placeholder 扫描**：测试代码均给出关键断言骨架；涉及生产确认的点（Task 12/19/13 的电影大小校验、Task 3 的 `_split_quark_path` 返回结构、Task 9 的 `literal` 导入）标注为实施时以实际源码签名校准，非「TBD」占位——executor 必须读取目标函数确认后落笔。

**Type 一致性**：`_try_admit_one(t0)` 返回语义、`_fetch_from_task_queue() -> int`、`_advance_scrape_to_library(media_id, file_name=None) -> int`、`_sync_media_status(media_id, session=None) -> int`、`invalidate_usage_cache() -> None` 在各任务间一致；`_GID_STRIKE_LIMIT`/`_unknown_gid_strikes`、`_scrape_backoff`、`_recent_empty_check` 命名全计划唯一。

**提交纪律**：每个任务独立提交；commit message 中文 Conventional Commits；不 commit 未验证内容。
