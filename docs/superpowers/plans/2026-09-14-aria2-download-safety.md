---
change: aria2-download-safety
design-doc: docs/superpowers/specs/2026-09-14-aria2-download-safety-design.md
base-ref: bc8b15e8f70a9ce7fbf24368c00f36d0d2e21da6
---

# aria2 下载链路安全 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 移除陌生 aria2 gid 对转存的拦截/告警/强删（用户自行下载任务与系统共存）、排查并消除空间不足误报、孤儿清理前保护 aria2 正在下载/等待下载的源文件。

**Architecture:** ① 删除 `_admit_batch` 段 2 的 GID 来源校验（含 `_unknown_gid_strikes` / `_GID_STRIKE_LIMIT` / flow_error 告警 / `aria2.remove` 强删）；② 证据排查容量/空间通知触发路径，确认与真实容量一致；③ `Aria2Client` 扩展 `tell_active`/`tell_waiting` 返回 `files`，新增 `list_source_basenames()` 供 `release_space_cleanup_job` 求差保护，aria2 查询失败 fail-safe。

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy / pytest（后端）。

**Spec:** `docs/openspec/changes/aria2-download-safety/specs/{pipeline-admission,notifications,quark-cleanup-safety}/spec.md`
**Design Doc:** `docs/superpowers/specs/2026-09-14-aria2-download-safety-design.md`

## Global Constraints

- 产物语言 zh-CN；注释/通知文案保持中文
- 阶段 A（`_poll_downloading_tasks` / `trigger_download_complete`）对本系统已签发 gid 的轮询、进度、完成推进、recovery 清理逻辑**不得改动**
- `queue.py` `_cleanup_cancel_side_effects` 对自有 gid 的 `aria2.remove`（用户主动取消语义）保留
- `aria2.tell_active`/`tell_waiting` 方法签名保留（cleanup 保护需要）；`list_source_basenames` 新方法挂在 `Aria2Client` 上
- cleanup 的 `files[].path`（aria2 本地落盘路径）**不使用**，源文件名只从 `files[].uris[].uri` 的 URL path 末段解析
- aria2 查询失败 → 本轮清理不删任何孤儿（fail-safe），不得误删
- 后端测试命令：`cd backend && .venv/bin/pytest -q`（venv 已存在于 `backend/.venv`）

---

### Task 1: 移除 GID 来源校验（pipeline-admission 放行）

**Files:**
- Modify: `backend/app/tasks/transfer.py:240-247`（删除 `_GID_STRIKE_LIMIT` / `_unknown_gid_strikes` 常量）
- Modify: `backend/app/tasks/transfer.py:1625-1692`（删除 `_admit_batch` 段 2 整段）
- Modify: `backend/tests/test_transfer.py`（GID 校验相关用例改放行语义）
- Modify: `backend/tests/test_capacity.py:400-412`（aria2 fake 的 tell_active/tell_waiting 注释更新）

**Interfaces:**
- Consumes: `_admit_batch`（现有结构）、`aria2.client.tell_active()` / `tell_waiting()`（保留，D3 使用）
- Produces: `_admit_batch` 不再调用 tell_active/tell_waiting、不再产生「非本系统 aria2 任务」告警；`_GID_STRIKE_LIMIT` / `_unknown_gid_strikes` 从模块中移除

- [x] **Step 1: 删除 `_GID_STRIKE_LIMIT` 与 `_unknown_gid_strikes` 常量**

`backend/app/tasks/transfer.py:240-247` 的整段注释 + 两行常量删除：

```python
# fix-transfer-flow-reliability Task 4（design T2 双层之二）：陌生 gid 连续跳过哨兵。
# 白名单未命中（不在 DB 任何行）的 aria2 活动/等待任务每轮 strikes += 1，连续
# _GID_STRIKE_LIMIT(3) 轮未消失 → best-effort aria2.remove 清理 + 告警并清计数——
# 防 recovery 回退时 aria2.remove 失败遗留的孤儿 gid 永久阻断转存（自锁）；remove
# 成功后下轮 actives 不再含该 gid → 自动恢复转存。计数为进程内共享状态（单 worker
# 部署可靠；重启即清零，重启后至多多计数 3 轮，不影响正确性）。
_GID_STRIKE_LIMIT = 3
_unknown_gid_strikes: dict[str, int] = {}
```

→ 全部删除（这段注释 + 常量均属于被移除的哨兵逻辑，无其他引用后删除）。

验证：`grep -n "_GID_STRIKE_LIMIT\|_unknown_gid_strikes" backend/app/tasks/transfer.py` 无输出。

- [x] **Step 2: 删除 `_admit_batch` 段 2 整段（陌生 gid 校验）**

`backend/app/tasks/transfer.py:1625-1692` 整段（从「# 2) GID 来源校验兜底（§12.2）」注释到 `return` 为止）删除。删除后 `_admit_batch` 从「1b) 无 pending 空跑」直接进入「3) 准入循环」。保留 `# 2)` 段之前的空行与后续注释的连续性。

验证：`grep -n "GID 来源校验\|unknown_gid\|非本系统 aria2" backend/app/tasks/transfer.py` 无输出；`python -c "import ast; ast.parse(open('backend/app/tasks/transfer.py').read())"` 语法通过。

- [x] **Step 3: 更新 test_transfer.py 的 GID 校验用例为放行语义**

删除或改写以下用例（`backend/tests/test_transfer.py`）：

1. `test_gid_source_check_skips_round_then_resumes`（:637）：**删除**（陌生 gid 不再跳过转存）。
2. `test_gid_check_failure_blocks_round`（:679）：**删除**（tell_active 故障不再阻断转存）。
3. `test_gid_whitelist_includes_non_downloading_rows`（:698）：**删除**（白名单口径不存在了）。
4. `test_unknown_gid_removed_after_three_strikes`（:744）：**删除**（强删行为移除）。
5. `test_known_gid_never_strikes`（:784）：**删除**（哨兵计数移除）。
6. `test_gid_source_check_accepts_known_gid`（:664）：**改写为「陌生 gid 共存不拦截」**——aria2 actives 含陌生 gid 时转存正常执行：

```python
def test_unknown_gid_coexists_with_transfer(db, env, monkeypatch):
    """陌生 aria2 活动任务（用户自行下载，不在系统已签发集合）不再拦截转存：
    转存正常继续、不产生 flow_error 告警、不调用 aria2.remove。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_pending(db))
    env["aria2"].actives = [{"gid": "user-gid", "status": "active", "comment": ""}]

    run(transfer_mod.process_transfer_queue())

    dq = run(read_row(db, DownloadQueue, dq_id))
    assert dq.status == "downloading"            # 转存正常准入
    assert len(env["cloudsaver"].save_calls) == 1
    assert env["aria2"].removed == []            # 不强删用户任务
    assert not any(e.event_type == "flow_error" for e in env["notifier"].events)
```

同时更新文件顶部 docstring（:15-17 附近）对 GID 校验的描述为放行语义。

验证：`cd backend && .venv/bin/pytest -q tests/test_transfer.py -k "gid or unknown or whitelist or strike"` 通过且无收集错误。

- [x] **Step 4: 运行 test_transfer.py 相关子集确认阶段 A 不受影响**

验证：`cd backend && .venv/bin/pytest -q tests/test_transfer.py` 全绿（阶段 A 轮询/完成推进/recovery 相关用例仍通过）。

- [x] **Step 5: 全量回归 + 提交**

验证：`cd backend && .venv/bin/pytest -q` 全绿（test_capacity.py / test_queue.py 等 aria2 fake 提供 tell_active/tell_waiting 的测试不受影响——方法保留，仅不再被 transfer 调用）。

```bash
git add backend/app/tasks/transfer.py backend/tests/test_transfer.py backend/tests/test_capacity.py
git commit -m "feat(aria2-download-safety): 移除陌生 gid 拦截告警与强删，放行用户自行下载任务

- 删除 _admit_batch 段 2 GID 来源校验（_unknown_gid_strikes/_GID_STRIKE_LIMIT/flow_error/aria2.remove 强删）
- n8n 已停用，白名单口径无法区分用户自行下载与误启动，彻底放行
- 保留阶段 A 对本系统已签发 gid 的轮询/进度/完成推进/recovery 清理"
```

---

### Task 2: 空间不足提示来源排查与口径修复（notifications）

**Files:**
- Inspect: `backend/app/services/capacity.py`（`check_capacity_alert` / `_load_alert_threshold` / `CapacityProvider.check`）
- Inspect: `backend/app/tasks/transfer.py`（`_record_alert(category="capacity")` 触发点：`_try_admit_one`「容量不足已累计 N 次」、`_admit_batch`「容量不足已持续超过 24 小时」）
- Inspect: `backend/app/tasks/capacity_alert.py`（job 编排）
- Inspect: `frontend/src/views/QueueView.vue:134-139`（`quotaWaitText`）
- Modify: `backend/tests/test_capacity_alert.py`（补充「使用率低于阈值不告警」显式用例，如缺）

**Interfaces:**
- Consumes: `check_capacity_alert()`、`CapacityProvider.check()`、`_record_alert`、`quotaWaitText`
- Produces: 排查结论（是否真实存在容量充足时误报路径）；如确认无误报则不改产品代码，仅补测试/证据

- [ ] **Step 1: 证据排查并记录结论**

核对以下路径（用数据库查询 + 代码审读，产出结构化结论）：

1. `check_capacity_alert`：门槛 = 连续 2 条快照 ≥ `capacity_alert_threshold`（system_config=0.9）+ 30min 冷却。实测 `quark_capacity_log` 峰值 40.2%（84.5/210 GB）→ 永不触发。核对 `_load_alert_threshold` 读取正常。
2. `_record_alert(category="capacity")` 触发点：仅在 `CapacityProvider.check()` 返回 False（真实容量不足）或 `quota_wait` 滞留 >24h 时触发。核对 `_load_quota_gb`（system_config=210）、`_load_margin_gb`（=quota×5%）、`check()` 预算公式在当前数据下必然通过。
3. 前端 `quotaWaitText`：仅当任务 `status=quota_wait` 展示；当前 DB 无 quota_wait 行 → 前端不显示「等待容量」。
4. 通知表：查询确认无容量类 flow_error（body 不含「容量/空间」）。

结论写入设计文档对应章节（或作为验证证据存档）。若排查发现真实误报路径，进入 Step 2；否则跳过 Step 2 进入 Step 3。

验证：产出排查结论记录（在任务完成说明中列出 4 项核对结果）。

- [ ] **Step 2: 若发现容量充足时误触发路径则修复（条件执行）**

仅当 Step 1 发现实际误报路径时执行：修复对应触发条件或数据源，确保与真实容量一致（spec：容量充足 MUST NOT 产生空间不足通知）。修复后补充回归测试（放入 `backend/tests/test_capacity_alert.py` 或对应模块测试）。

验证：对应测试覆盖「使用率低于阈值不告警」场景且通过。若 Step 1 确认无误报路径，本步标记为「条件未触发，跳过」。

- [ ] **Step 3: 补充/确认「容量充足不告警」测试**

`backend/tests/test_capacity_alert.py` 已有 `test_alert_not_fired_when_recent_snapshot_below_threshold`（:104，80% < 90% 不告警）。补充一个显式对齐本 change 场景的用例（40% 使用率不告警，模拟当前生产数据）：

```python
def test_alert_not_fired_at_40_percent_usage():
    # 当前生产数据（210G 总量、40% 使用率）：远低于默认阈值 90% → 绝不告警
    rows = [_log(210.0, 84.5), _log(210.0, 84.4)]
    alerted, session, n = _aligned_check(rows)

    assert alerted is False
    n.notify.assert_not_called()
```

验证：`cd backend && .venv/bin/pytest -q tests/test_capacity_alert.py -k "40_percent"` 通过。

- [ ] **Step 4: 全量回归 + 提交**

验证：`cd backend && .venv/bin/pytest -q` 全绿。

```bash
git add backend/tests/test_capacity_alert.py
git commit -m "test(aria2-download-safety): 补充容量充足（40%）不告警回归用例

证据排查确认：快照峰值 40.2%、check_capacity_alert 门槛 90%+连续2次从未触发、
_record_alert(capacity) 仅在真实不足/滞留>24h 触发、通知表无容量类告警、
前端等待容量仅 quota_wait 展示（当前无 quota_wait 行）——系统无容量误报路径"
```

---

### Task 3: 清理保护 aria2 下载源（quark-cleanup-safety）

**Files:**
- Modify: `backend/app/services/aria2.py:167-201`（`tell_active` / `tell_waiting` keys 增加 `"files"`）
- Modify: `backend/app/services/aria2.py`（新增 `list_source_basenames()`）
- Modify: `backend/app/tasks/cleanup.py:45-128`（`release_space_cleanup_job` 加入下载源保护）
- Modify: `backend/tests/test_aria2_rpc_shapes.py`（新增 list_source_basenames 单测）
- Create: `backend/tests/test_cleanup_aria2_protection.py`（清理保护 4 场景测试）

**Interfaces:**
- Consumes: `aria2.client.list_source_basenames() -> set[str]`（新增）
- Produces: `release_space_cleanup_job` 的孤儿判定变为 `present − referenced − aria2_sources`；aria2 不可用时本轮不删

- [ ] **Step 1: 扩展 `tell_active` / `tell_waiting` 的 keys 增加 `"files"`**

`backend/app/services/aria2.py`：

- `tell_active`（:175-178）：keys 从 `["gid", "status", "comment", "totalLength", "completedLength"]` 改为追加 `"files"`。
- `tell_waiting`（:193-200）：keys 同样追加 `"files"`。
- 两个方法的 docstring 补充：`files[].uris[].uri` 供清理保护解析下载源文件名（`files[].path` 是本地落盘路径，不使用）。

验证：`grep -n '"files"' backend/app/services/aria2.py` 出现 2 处（tell_active / tell_waiting keys 内）。

- [ ] **Step 2: 新增 `list_source_basenames()`**

在 `backend/app/services/aria2.py` 的 `remove` 方法之后、模块单例之前新增：

```python
async def list_source_basenames(self) -> set[str]:
    """清理保护用：返回 aria2 活动 + 等待任务下载的源文件名集合（/quark basename）。

    组合 tell_active + tell_waiting，从每任务 `files[].uris[].uri`（下载源 URL）解析
    URL path 末段并 URL-decoded 得到文件名。`files[].path` 是 aria2 本地落盘路径
    （download_dir + out），不是夸克源文件，不用于本方法。单条 uri 解析失败仅
    debug 日志跳过（降级）；aria2 不可用/整体失败向上抛 Aria2Unavailable，由
    调用方 fail-safe（不删任何孤儿）。
    """
    tasks = list(await self.tell_active() or [])
    tasks += list(await self.tell_waiting() or [])
    names: set[str] = set()
    for t in tasks:
        for f in (t.get("files") or []):
            for u in (f.get("uris") or []):
                uri = (u.get("uri") or "").strip()
                if not uri:
                    continue
                try:
                    path = urlsplit(uri).path.rstrip("/")
                    base = unquote(path.rsplit("/", 1)[-1]) if path else ""
                except ValueError:
                    logger.debug("[aria2] 下载源 uri 解析失败（跳过）: %r", uri)
                    continue
                if base:
                    names.add(base)
    return names
```

顶部 import 增加：`from urllib.parse import unquote, urlsplit`。

验证：`python -c "import ast; ast.parse(open('backend/app/services/aria2.py').read())"` 通过。

- [ ] **Step 3: 新增 `list_source_basenames` 单测**

`backend/tests/test_aria2_rpc_shapes.py` 新增：

```python
def test_list_source_basenames_parses_uris():
    """从 tell_active/tell_waiting 的 files[].uris[].uri 解析下载源 basename：
    URL query 不影响、中文文件名 URL 解码、files[].path 不参与。"""
    from app.services.aria2 import Aria2Client

    client = Aria2Client()
    active = [{
        "gid": "a1",
        "files": [{"path": "/download/本地名.mkv", "uris": [
            {"uri": "http://alist:5244/d/quark/190.mkv?sign=abc&ts=123", "status": "used"}]}],
    }]
    waiting = [{
        "gid": "w1",
        "files": [{"uris": [{"uri": "http://alist:5244/d/quark/%E4%B8%AD%E6%96%87.mkv", "status": "used"}]}],
    }]

    async def fake_tell_active():
        return active

    async def fake_tell_waiting():
        return waiting

    client.tell_active = fake_tell_active
    client.tell_waiting = fake_tell_waiting

    import asyncio
    names = asyncio.run(client.list_source_basenames())
    assert names == {"190.mkv", "中文.mkv"}   # query 剥离 + 中文 URL 解码；无本地路径名
```

验证：`cd backend && .venv/bin/pytest -q tests/test_aria2_rpc_shapes.py -k "list_source_basenames"` 通过。

- [ ] **Step 4: `release_space_cleanup_job` 加入下载源保护 + fail-safe**

`backend/app/tasks/cleanup.py`：

1. 模块顶部引入 aria2（延迟导入防循环，风格同库内惯例）：

```python
def _aria2_client():
    from app.services import aria2  # noqa: PLC0415 延迟导入防循环
    return aria2.client
```

2. `release_space_cleanup_job` 在 Step 2（引用集）之后、Step 3（孤儿求差）之前插入：

```python
    # 2b) aria2 下载源保护（quark-cleanup-safety）：查询 aria2 活动 + 等待任务
    #     下载的源文件名，与引用集一起从待删除列表剔除（用户自行下载的源文件
    #     不在 download_queue 引用集内，仅靠本层保护）。查询失败 → fail-safe：
    #     本轮不删除任何孤儿（无法确认下载状态时绝不误删），记 error 下轮再试。
    try:
        aria2_sources = await _aria2_client().list_source_basenames()
    except Exception as exc:  # noqa: BLE001  Aria2Unavailable 等 → fail-safe
        logger.warning("[cleanup] aria2 下载源查询失败，本轮跳过清理（fail-safe）: %s", exc)
        async with async_session() as s:
            await record_task_run(
                s, "cleanup", "error", f"aria2 下载源查询失败，本轮跳过清理: {exc}",
                duration_seconds=time.monotonic() - t0,
            )
            await s.commit()
        return
    referenced |= aria2_sources
```

（将 aria2_sources 并入 `referenced` 集合，Step 3 的 `orphans = sorted(present - referenced)` 自然生效，无需改孤儿求差行。）

验证：`python -c "import ast; ast.parse(open('backend/app/tasks/cleanup.py').read())"` 通过。

- [ ] **Step 5: 新增清理保护测试（4 场景）**

Create `backend/tests/test_cleanup_aria2_protection.py`：

```python
"""quark-cleanup-safety：release_space_cleanup_job 对 aria2 下载源的保护与 fail-safe。

场景：active 下载源保护 / waiting 下载源保护 / 无下载任务正常清理 / aria2 查询失败不删。
mock：cleanup_mod.alist（list_dir/remove）、cleanup_mod._aria2_client（list_source_basenames）。
"""
import asyncio

import pytest

from app.tasks import cleanup as cleanup_mod
from app.models import DownloadQueue, Media
from app.utils import now_utc_naive as _now


def run(coro):
    return asyncio.run(coro)


class _FakeAlist:
    def __init__(self, names):
        self.names = names
        self.remove_calls = []

    async def list_dir(self, path):
        return [{"name": n, "is_dir": False, "size": 1} for n in self.names]

    async def remove(self, names, dir):
        self.remove_calls.append((list(names), dir))
        return {}


def _patch_env(monkeypatch, db, alist, aria2_sources=None, aria2_error=None):
    monkeypatch.setattr(cleanup_mod, "async_session", db)
    monkeypatch.setattr(cleanup_mod, "alist", alist)
    fake_aria2 = type("FakeAria2", (), {})()
    if aria2_error is not None:
        async def _boom():
            raise aria2_error
        fake_aria2.list_source_basenames = _boom
    else:
        async def _sources():
            return set(aria2_sources or [])
        fake_aria2.list_source_basenames = _sources
    monkeypatch.setattr(cleanup_mod, "_aria2_client", lambda: fake_aria2)


def test_cleanup_protects_active_download_source(db, monkeypatch):
    """aria2 正在下载（active）的源文件不进入删除列表，其余孤儿正常删除。"""
    async def _seed():
        async with db() as s:
            s.add(Media(title="测试剧", media_type="tv", tmdb_id=None, status="tracking"))
            await s.commit()
    run(_seed())
    alist = _FakeAlist(["下载中.mkv", "孤儿.mkv"])
    _patch_env(monkeypatch, db, alist, aria2_sources={"下载中.mkv"})
    run(cleanup_mod.release_space_cleanup_job())
    removed = {n for calls in alist.remove_calls for n in calls[0]}
    assert "下载中.mkv" not in removed
    assert "孤儿.mkv" in removed


def test_cleanup_protects_waiting_download_source(db, monkeypatch):
    """aria2 等待（waiting）队列中的源文件同样受保护。"""
    async def _seed():
        async with db() as s:
            s.add(Media(title="测试剧", media_type="tv", tmdb_id=None, status="tracking"))
            await s.commit()
    run(_seed())
    alist = _FakeAlist(["排队中.mkv", "孤儿.mkv"])
    _patch_env(monkeypatch, db, alist, aria2_sources={"排队中.mkv"})
    run(cleanup_mod.release_space_cleanup_job())
    removed = {n for calls in alist.remove_calls for n in calls[0]}
    assert "排队中.mkv" not in removed
    assert "孤儿.mkv" in removed


def test_cleanup_without_downloads_removes_orphans(db, monkeypatch):
    """aria2 无下载任务（保护集为空）→ 按既有孤儿判定正常清理。"""
    async def _seed():
        async with db() as s:
            s.add(Media(title="测试剧", media_type="tv", tmdb_id=None, status="tracking"))
            await s.commit()
    run(_seed())
    alist = _FakeAlist(["孤儿1.mkv", "孤儿2.mkv"])
    _patch_env(monkeypatch, db, alist, aria2_sources=set())
    run(cleanup_mod.release_space_cleanup_job())
    assert alist.remove_calls and len(alist.remove_calls[0][0]) == 2


def test_cleanup_failsafe_when_aria2_unavailable(db, monkeypatch):
    """aria2 查询失败 → 本轮不删除任何孤儿（fail-safe），记 task_run error。"""
    async def _seed():
        async with db() as s:
            s.add(Media(title="测试剧", media_type="tv", tmdb_id=None, status="tracking"))
            await s.commit()
    run(_seed())
    alist = _FakeAlist(["孤儿.mkv"])
    _patch_env(monkeypatch, db, alist, aria2_error=RuntimeError("aria2 RPC 不可用"))
    run(cleanup_mod.release_space_cleanup_job())
    assert alist.remove_calls == []  # 绝不删除
```

（若既有 cleanup 测试文件的 helper（`seed_done_state` 等）可复用，可改用之；以上为自包含版本。）

验证：`cd backend && .venv/bin/pytest -q tests/test_cleanup_aria2_protection.py` 4 个用例全绿。

- [ ] **Step 6: 既有 cleanup 测试回归**

`test_fix_p0_recovery_cleanup_transfer.py` / `test_oracle_fixes.py` / `test_library_check.py` 中调用 `release_space_cleanup_job` 的用例需要 `_aria2_client` 的 mock（否则真实 aria2 RPC 调用）。按测试文件既有风格在对应 fixture/monkeypatch 中补 `monkeypatch.setattr(cleanup_mod, "_aria2_client", lambda: SimpleNamespace(list_source_basenames=AsyncMock(return_value=set())))`。

验证：`cd backend && .venv/bin/pytest -q tests/test_fix_p0_recovery_cleanup_transfer.py tests/test_oracle_fixes.py tests/test_library_check.py tests/test_library_check_timeout.py` 全绿。

- [ ] **Step 7: 全量回归 + 提交**

验证：`cd backend && .venv/bin/pytest -q` 全绿。

```bash
git add backend/app/services/aria2.py backend/app/tasks/cleanup.py backend/tests/test_aria2_rpc_shapes.py backend/tests/test_cleanup_aria2_protection.py backend/tests/test_fix_p0_recovery_cleanup_transfer.py backend/tests/test_oracle_fixes.py backend/tests/test_library_check.py backend/tests/test_library_check_timeout.py
git commit -m "feat(aria2-download-safety): 孤儿清理前保护 aria2 下载中/等待下载的源文件

- tell_active/tell_waiting 返回 keys 增加 files；新增 list_source_basenames() 从 uris 解析源文件名
- release_space_cleanup_job 将 aria2 下载源并入保护集求差后再删
- aria2 查询失败 fail-safe：本轮不删任何孤儿，记 error 下轮重试"
```

---

### Task 4: 收尾验证

**Files:**
- Inspect: 全部变更文件 + spec delta 场景

- [ ] **Step 1: 全量测试**

验证：`cd backend && .venv/bin/pytest -q` 全绿（无回归）。

- [ ] **Step 2: spec 场景覆盖核对**

逐条核对三个 delta spec 场景均有对应测试：

| spec | 场景 | 覆盖测试 |
|---|---|---|
| pipeline-admission | 陌生任务共存不拦截 | test_transfer.py `test_unknown_gid_coexists_with_transfer` |
| pipeline-admission | 本系统任务正常跟踪 | test_transfer.py 阶段 A 轮询/完成推进用例 |
| notifications | 容量充足不告警 | test_capacity_alert.py `test_alert_not_fired_at_40_percent_usage` |
| notifications | 容量不足才告警 | test_capacity_alert.py `test_alert_fires_when_two_consecutive_snapshots_over_threshold` |
| quark-cleanup-safety | 下载中文件被保护 | test_cleanup_aria2_protection.py `test_cleanup_protects_active_download_source` |
| quark-cleanup-safety | 等待下载文件被保护 | test_cleanup_aria2_protection.py `test_cleanup_protects_waiting_download_source` |
| quark-cleanup-safety | 无下载任务正常清理 | test_cleanup_aria2_protection.py `test_cleanup_without_downloads_removes_orphans` |
| quark-cleanup-safety | aria2 不可用时跳过清理 | test_cleanup_aria2_protection.py `test_cleanup_failsafe_when_aria2_unavailable` |

验证：上表全部场景有对应测试且通过；在任务完成说明中列出核对结果。
