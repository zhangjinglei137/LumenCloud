---
change: restore-scan-interval-scheduling
design-doc: docs/superpowers/specs/2026-09-14-restore-scan-interval-scheduling-design.md
base-ref: ec2d6dd03d4bec292f9a6daa53a4c1fcab0435f5
---

# 恢复 per-media 巡检间隔调度 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 恢复 `scan_all_media` 的 per-media 到期过滤（`Media.scan_interval_minutes` 重新生效，兜底 60 分钟）、`force` 绕过语义与 `_scan_one` 短路分支 touch 语义，并同步 docstring 与测试断言。

**Architecture:** 纯后端行为恢复。调度 job 保持每分钟 tick，到期过滤在 `scan_all_media` 的 `select(Media)` 查询内以 SQL 表达式完成（跨 SQLite/PostgreSQL 方言分支）；`_scan_one` 四个「未真正完成主流程」短路分支 `touch_last_scan_at=False`，使故障 media 下轮继续重试。

**Tech Stack:** Python 3.14 / FastAPI / SQLAlchemy 2.x async / APScheduler / pytest（SQLite in-memory StaticPool 测试，生产可 PostgreSQL）。

**Spec:** `docs/openspec/changes/restore-scan-interval-scheduling/specs/media-pipeline/spec.md` + `docs/superpowers/specs/2026-09-14-restore-scan-interval-scheduling-design.md`（本计划的论证来源，执行者须同时阅读两者）。

## Global Constraints

- 产物语言：zh-CN（docstring / commit message 用中文）。
- 不复活全局 `system_config.scan_interval_minutes`（保持 `_RETIRED_EXACT` 退役，`test_settings_retired.py` 不动）。
- 不改 APScheduler 注册（job 保持 `IntervalTrigger(minutes=1)`）。
- 不新增 force 的 CLI/API 入口（仅恢复函数语义）。
- 时间基准：`app.utils.now_utc_naive as _now`（naive UTC），禁止混用本地时间。
- 间隔取值链：`COALESCE(media.scan_interval_minutes, settings.SCAN_INTERVAL_MINUTES)`，`settings.SCAN_INTERVAL_MINUTES = 60`。
- 测试命令：`cd backend && .venv/bin/python -m pytest <file> -x -q`；全量回归：`cd backend && .venv/bin/python -m pytest tests/ -x -q`。
- 提交信息遵循 Conventional Commits 结构 + 中文描述（如 `feat(scan): 恢复 per-media 到期过滤`）。

---

### Task 1: 事实核查与存量摸底（无代码）

**Files:**
- Modify: `docs/openspec/changes/restore-scan-interval-scheduling/design.md`（Open Questions 处置补记）
- Modify: `docs/openspec/changes/restore-scan-interval-scheduling/tasks.md`（勾选 1.1 / 1.2）

**Interfaces:**
- Consumes: 无
- Produces: 核查结论（写入 design.md Open Questions 处置 + 本 change 记录），供 Task 2 的设计前提

- [x] **Step 1: 核实 Task 3「自然重试」对每分钟全量的代码依赖**

审查 `backend/app/tasks/scan.py` 的 `_scan_one` 搜索/重试路径（`_search_and_rank` 无候选 → 正常完成 touch=True → 缺集下轮按 interval 重试）。预期结论：queue-flow-rework Task 3 已移除 silent 静默机制，缺集重试不依赖每分钟全量；恢复过滤后重试延迟为 ≤interval，符合「自然重试」语义。

核实要点（只读审查，不改代码）：
- `_search_and_rank` 搜索失败是否抛 `ScanSearchUnavailable`（→ error 分支，touch=False 后下轮重试）——是。
- 搜索成功但无候选 → 正常完成 `success/skipped`（touch=True）→ 缺集下轮到期重试，延迟 ≤interval。

- [x] **Step 2: 记录核查结论到 design.md Open Questions 处置**

编辑 `docs/openspec/changes/restore-scan-interval-scheduling/design.md` 的 Open Questions 段，追加：

```markdown
- Task 3「自然重试」对每分钟全量的代码依赖：**已核实（2026-09-14 build）**——重试不依赖每分钟全量必扫；
  queue-flow-rework Task 3 已移除 silent 静默机制，恢复过滤后缺失集重试延迟为 ≤interval（默认 ≤60min），
  符合「自然重试」语义，节奏变化记入 release note。
```

- [x] **Step 3: 勾选 tasks.md 的 1.1 与 1.2**

编辑 `<classic-change-dir>/tasks.md`，将 `- [ ] 1.1 ...` 与 `- [ ] 1.2 ...` 改为 `- [x]`（1.2 存量摸底已在设计阶段完成：DB 查询 8 行 media 间隔全为 60/NULL、last_scan_at 无 NULL，无异常小值）。

- [x] **Step 4: 提交**

```bash
git add docs/openspec/changes/restore-scan-interval-scheduling/design.md docs/openspec/changes/restore-scan-interval-scheduling/tasks.md
git commit -m "docs(scan): 记录重试依赖核查结论并勾选摸底任务"
```

---

### Task 2: scan_all_media 到期过滤 + force 语义（含新测试）

**Files:**
- Modify: `backend/app/tasks/scan.py:1497-1518`（`scan_all_media`）
- Test: `backend/tests/test_scan_baseline.py`（统一调度区新增 3 用例）

**Interfaces:**
- Consumes: `Media` 模型（`scan_interval_minutes` / `last_scan_at` / `status`）、`settings.SCAN_INTERVAL_MINUTES`、`_now()`（`app.utils.now_utc_naive`）、`async_session`
- Produces: `scan_all_media(force: bool = False)` 到期过滤行为；模块内私有 helper `_due_filter()`（本 change 内使用）；新增测试用例名 `test_scan_all_media_skips_not_due` / `test_scan_all_media_scans_never_scanned` / `test_scan_all_media_force_bypasses_due`

- [x] **Step 1: 新增到期过滤 helper 与测试**

在 `backend/app/tasks/scan.py` 的 `scan_all_media` 上方新增私有函数：

```python
def _due_filter():
    """per-media 到期过滤条件（跨方言）：last_scan_at IS NULL OR last_scan_at <= now - 间隔。

    间隔 = COALESCE(media.scan_interval_minutes, settings.SCAN_INTERVAL_MINUTES)；
    时间基准与 _finish_scan_run 写入 last_scan_at 同源（_now()，naive UTC）。
    SQLite（测试）用 func.datetime 修饰符；PostgreSQL（生产）用 now() - interval。
    """
    from sqlalchemy import String, func, or_, text

    minutes = func.coalesce(Media.scan_interval_minutes, settings.SCAN_INTERVAL_MINUTES)
    bind = async_session().bind
    if bind is not None and bind.dialect.name == "postgresql":
        due_expr = Media.last_scan_at <= (
            func.now() - (minutes * text("interval '1 minute'"))
        )
    else:
        now_iso = _now().strftime("%Y-%m-%d %H:%M:%S")
        modifier = func.concat("-", minutes.cast(String), " minutes")
        due_expr = Media.last_scan_at <= func.datetime(now_iso, modifier)
    return or_(Media.last_scan_at.is_(None), due_expr)
```

说明：`func.concat` 在 SQLite 编译器下生成 `||` 拼接，跨 SQLite/PostgreSQL 安全；`func.datetime` 仅 SQLite 分支使用。

- [x] **Step 2: 改造 scan_all_media 增加到期过滤与 force 语义**

将 `scan_all_media`（scan.py:1497-1518）改为：

```python
async def scan_all_media(force: bool = False) -> None:
    """遍历到期 tracking/downloading 影视巡检（downloading 不跳过，防卡死，§3.1）。

    恢复 per-media 到期过滤（restore-scan-interval-scheduling）：每轮 tick 仅巡检
    「距上次成功巡检已超过其配置间隔（scan_interval_minutes，兜底全局默认 60 分钟）」
    或「从未巡检过（last_scan_at 为 NULL）」的 tracking/downloading 影视；未到期
    影视以轻量 SQL 过滤跳过（不执行 Emby 基线、搜索与 task_run 落库）。调度 job
    保持每分钟 tick（scan_all_media_job 的 APScheduler IntervalTrigger），过滤在本
    函数内完成。

    force（手动全量/CLI 入口契约）：force=True 绕过 per-media 到期过滤，立即巡检
    全部 tracking/downloading 影视。本 change 不新增 force 调用入口（仅恢复函数语义）。
    """
    async with async_session() as s:
        stmt = select(Media).where(Media.status.in_(("tracking", "downloading")))
        if not force:
            stmt = stmt.where(_due_filter())
        rows = (await s.execute(stmt)).scalars().all()
    for media in rows:
        try:
            await scan_media(media.id)
        except Exception:
            logger.exception("[scan] media=%s 巡检异常", media.id)
```

同时将 `scan_all_media_job` docstring（scan.py:1468-1479）同步为：

```python
    """定时 tick 包装（B 定时：每分钟；调度粒度由本 job 触发周期控制）。

    restore-scan-interval-scheduling：job 保持每分钟 tick；到期过滤在
    scan_all_media 内部完成——每轮仅巡检已到期的 tracking/downloading 影视
    （per-media scan_interval_minutes，兜底 60 分钟），未到期影视轻量 SQL 跳过。

    M1（Oracle Gate2）：与同模块其它 job 一致（transfer.process_transfer_queue_job 等），
    DB 读取/执行异常时记录 task_run(error) 兜底而不是静默抛出——APScheduler 会吞
    任务内异常，若不记录则作业持续失败运维不可见。task_type 用 "scan_all_media"
    （作业级，对齐 transfer/cleanup/notify 的作业级短名惯例；区别于单部巡检的
    "scan_media"）。
    """
```

- [x] **Step 3: 新增测试用例（test_scan_baseline.py 统一调度区）**

在 `backend/tests/test_scan_baseline.py` 的「统一巡检调度」段（test_scan_all_media_scans_only_tracking_downloading 之后）新增：

```python
def test_scan_all_media_skips_not_due(db, monkeypatch):
    """未到期跳过：last_scan_at=now（60 间隔未到期）的 media 不进入巡检。"""
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    routed: list[int] = []

    async def fake_scan_one(media_id, *, manual=False):
        routed.append(media_id)
        return 1

    monkeypatch.setattr(scan_mod, "_scan_one", fake_scan_one)

    async def seed():
        async with db() as s:
            media = Media(
                title="测试剧", media_type="tv", status="tracking",
                scan_interval_minutes=60, last_scan_at=_now(),
            )
            s.add(media)
            await s.commit()
            return media.id

    mid = run(seed())
    run(scan_mod.scan_all_media())

    assert routed == []  # 未到期 → 跳过，不调用 _scan_one


def test_scan_all_media_scans_never_scanned(db, monkeypatch):
    """last_scan_at 为 NULL 的 media 立即首巡（新建影视下轮 tick 即扫）。"""
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    routed: list[int] = []

    async def fake_scan_one(media_id, *, manual=False):
        routed.append(media_id)
        return 1

    monkeypatch.setattr(scan_mod, "_scan_one", fake_scan_one)

    async def seed():
        async with db() as s:
            media = Media(title="测试剧", media_type="tv", status="tracking", last_scan_at=None)
            s.add(media)
            await s.commit()
            return media.id

    mid = run(seed())
    run(scan_mod.scan_all_media())

    assert routed == [mid]  # 从未巡检 → 立即巡检


def test_scan_all_media_force_bypasses_due(db, monkeypatch):
    """force=True 绕过到期过滤：未到期 media 也立即巡检（对照 force=False 跳过）。"""
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    routed: list[int] = []

    async def fake_scan_one(media_id, *, manual=False):
        routed.append(media_id)
        return 1

    monkeypatch.setattr(scan_mod, "_scan_one", fake_scan_one)

    async def seed():
        async with db() as s:
            media = Media(
                title="测试剧", media_type="tv", status="tracking",
                scan_interval_minutes=60, last_scan_at=_now(),  # 未到期
            )
            s.add(media)
            await s.commit()
            return media.id

    mid = run(seed())
    run(scan_mod.scan_all_media(force=True))

    assert routed == [mid]  # force 全量：未到期也巡检
```

- [x] **Step 4: 运行新增测试验证通过**

```bash
cd backend && .venv/bin/python -m pytest tests/test_scan_baseline.py -x -q
```

Expected: 新增 3 用例 + 既有用例（除待 Task 5 翻转的 `test_scan_all_media_scans_within_interval_cooldown` 外）通过。`within_interval_cooldown` 此刻会失败（断言未到期也扫，与恢复语义冲突）——记录该预期失败，Task 5 翻转。

- [x] **Step 5: 提交**

```bash
git add backend/app/tasks/scan.py backend/tests/test_scan_baseline.py
git commit -m "feat(scan): 恢复 per-media 到期过滤与 force 绕过语义"
```

---

### Task 3: _scan_one 短路分支 touch 语义（含新测试）

**Files:**
- Modify: `backend/app/tasks/scan.py:1650-1654, 1663-1667, 1727-1731, 1743-1746`
- Test: `backend/tests/test_scan_baseline.py`（_scan_one 端到端区新增 2 用例）

**Interfaces:**
- Consumes: `_scan_one` 现有分支结构、`_finish(..., touch_last_scan_at=...)` 闭包
- Produces: 4 个短路分支 `touch_last_scan_at=False` 的新行为；新增测试用例名 `test_scan_one_emby_error_does_not_touch_last_scan_at` / `test_scan_one_missing_required_does_not_touch_last_scan_at`

- [x] **Step 1: 改四个短路分支 touch 取值**

在 `backend/app/tasks/scan.py` 中，将以下 4 处 `_finish(...)` 调用的 `touch_last_scan_at=True` 改为 `False`（或省略参数——默认即 False；为可读性显式写 `False`）：

1. Emby 故障 error（1650-1654）：
   ```python
   return await _finish(
       "error",
       f"Emby 故障，fail-safe 暂停新缺集发现: {exc}",
       touch_last_scan_at=False,
   )
   ```
2. Emby 未收录 skipped（1663-1667）：
   ```python
   return await _finish(
       "skipped",
       "Emby 未收录该剧集，防重基线强制（scan_baseline_required=True），本轮跳过",
       touch_last_scan_at=False,
   )
   ```
3. 搜索异常 error（1727-1731）：
   ```python
   return await _finish(
       "error",
       f"搜索服务异常（cloudSaver 不可达或超时），未能查找缺失集资源，请稍后重试: {exc}",
       touch_last_scan_at=False,
   )
   ```
4. 上限读取失败 error（1743-1746）：
   ```python
   return await _finish(
       "error", f"巡检中止: 大小过滤上限读取失败: {exc}",
       touch_last_scan_at=False,
   )
   ```

不动的分支：paused/error 跳过（1634-1638，默认 False）、无遗漏 skipped（1711-1714，touch=True 保留——巡检正常完成）、正常完成 success/skipped（1936-1939，touch=True 保留）。

- [x] **Step 2: 新增测试用例（test_scan_baseline.py _scan_one 端到端区）**

在 `test_scan_one_tv_missing_required_skips` 之后新增：

```python
def test_scan_one_emby_error_does_not_touch_last_scan_at(db, monkeypatch):
    """Emby 故障 error 分支不更新 last_scan_at：下轮 tick 继续重试，不被冷却。"""
    scan_mod = _patch_scan_env(monkeypatch, db, find_emby_id=None)
    monkeypatch.setattr(scan_mod, "_emby_missing_codes", AsyncMock(side_effect=RuntimeError("Emby down")))

    mid = run(_seed_media(db))
    run(scan_mod._scan_one(mid))

    async def _read_media():
        async with db() as s:
            m = await s.get(Media, mid)
            return m.last_scan_at
    last = run(_read_media())
    assert last is None  # 故障分支未 touch → last_scan_at 保持 NULL（下轮重试）


def test_scan_one_missing_required_does_not_touch_last_scan_at(db, monkeypatch):
    """Emby 未收录（强防重 skipped）分支不更新 last_scan_at：下轮继续重试。"""
    _mock_baseline_config(monkeypatch, "true")
    scan_mod = _patch_scan_env(monkeypatch, db, find_emby_id=None)

    mid = run(_seed_media(db))
    run(scan_mod._scan_one(mid))

    async def _read_media():
        async with db() as s:
            m = await s.get(Media, mid)
            return m.last_scan_at
    last = run(_read_media())
    assert last is None  # 未收录 skipped 分支未 touch
```

- [x] **Step 3: 运行新增测试验证通过**

```bash
cd backend && .venv/bin/python -m pytest tests/test_scan_baseline.py::test_scan_one_emby_error_does_not_touch_last_scan_at tests/test_scan_baseline.py::test_scan_one_missing_required_does_not_touch_last_scan_at -x -q
```

Expected: 2 用例 PASS。

- [x] **Step 4: 提交**

```bash
git add backend/app/tasks/scan.py backend/tests/test_scan_baseline.py
git commit -m "fix(scan): 短路分支不 touch last_scan_at，故障下轮重试"
```

---

### Task 4: downloading 语义注释与 docstring 一致化

**Files:**
- Modify: `backend/app/tasks/scan.py`（`scan_all_media` 内 downloading 注释、模块顶部 docstring）

**Interfaces:**
- Consumes: Task 2 已改写的 `scan_all_media` / `scan_all_media_job` docstring
- Produces: 无新接口（纯注释/docstring）

- [x] **Step 1: 澄清 downloading 语义注释**

在 `scan_all_media` 的 docstring（Task 2 已写）基础上，确认下载相关注释明确「downloading 不排除出巡检集合（防卡死）≠ 绕过冷却」。检查 `scan_all_media` 主体注释（如有 `# downloading 不跳过` 类注释）与 `_scan_one` 中 `skip_enqueue = media.status == "downloading"`（scan.py:1748）附近注释，补充/订正为：

```python
    # downloading 不排除出巡检集合（防卡死），但按其各自间隔到期判断（≠ 绕过冷却）；
    # 仅本轮不入队（skip_enqueue），遗漏检查照常执行。
```

- [x] **Step 2: 同步模块顶部 docstring**

`scan.py` 模块顶部（scan.py:13 附近）：

```python
- scan_all_media()       遍历到期 tracking/downloading 影视（per-media 间隔过滤）
```

- [x] **Step 3: 运行既有测试确认无回归**

```bash
cd backend && .venv/bin/python -m pytest tests/test_scan_baseline.py -x -q
```

Expected: 除 `within_interval_cooldown`（Task 5 翻转）外全部通过。

- [x] **Step 4: 提交**

```bash
git add backend/app/tasks/scan.py
git commit -m "docs(scan): 澄清 downloading 防卡死语义与模块 docstring"
```

---

### Task 5: 翻转固化「未到冷却期也巡检」断言

**Files:**
- Modify: `backend/tests/test_scan_baseline.py`（`test_scan_all_media_scans_within_interval_cooldown`）
- Modify: `backend/tests/test_oracle_fixes.py`（`test_scan_all_media_scans_all_tracking`）

**Interfaces:**
- Consumes: Task 2 的到期过滤行为
- Produces: 翻转后的断言（未到期跳过语义）

- [x] **Step 1: 翻转 test_scan_baseline.py 断言**

将 `test_scan_all_media_scans_within_interval_cooldown`（277-310）整体改写为「未到期跳过」：

```python
def test_scan_all_media_skips_within_interval_cooldown(db, monkeypatch):
    """未到期跳过：last_scan_at=now（60 间隔未到期）的 media 不进入巡检。

    restore-scan-interval-scheduling：恢复 per-media 到期过滤——未到期的
    tracking/downloading 影视以 SQL 过滤跳过，不执行巡检（test_scan_all_media_skips_not_due
    的同语义规范用例；本用例保留在统一调度段作为基线）。"""
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    routed: list[int] = []

    async def fake_scan_one(media_id, *, manual=False):
        routed.append(media_id)
        return 1

    monkeypatch.setattr(scan_mod, "_scan_one", fake_scan_one)

    async def seed():
        async with db() as s:
            media = Media(
                title="测试剧", media_type="tv", status="tracking",
                scan_interval_minutes=60, last_scan_at=_now(),  # 刚巡检过（未到期）
            )
            s.add(media)
            await s.commit()
            return media.id

    mid = run(seed())
    run(scan_mod.scan_all_media())

    assert routed == []  # 未到 per-media 冷却 → 跳过
```

若与 Task 2 的 `test_scan_all_media_skips_not_due` 重复度过高，可保留本用例并调整 Task 2 用例命名差异（本用例强调「冷却语义翻转」历史，Task 2 用例强调规范语义）。二选一保留亦可，但至少一处覆盖「未到期跳过」。

- [x] **Step 2: 翻转 test_oracle_fixes.py 断言**

将 `test_scan_all_media_scans_all_tracking`（563-595）改写为「到期才扫、未到期跳过」：

```python
def test_scan_all_media_scans_due_tracking(db, monkeypatch):
    """恢复 per-media 到期过滤：A（从未巡检）/ B（已到期）巡检，C（未到期）跳过。"""
    from datetime import timedelta

    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    routed = []

    async def fake_scan_media(media_id):
        routed.append(media_id)
        return 1

    monkeypatch.setattr(scan_mod, "scan_media", fake_scan_media)
    now = _now()

    async def seed():
        async with db() as s:
            a = Media(title="A 未巡检", media_type="tv", status="tracking",
                      scan_interval_minutes=1, last_scan_at=None)
            b = Media(title="B 已到期", media_type="tv", status="downloading",
                      scan_interval_minutes=1, last_scan_at=now - timedelta(minutes=2))
            c = Media(title="C 未到期", media_type="tv", status="tracking",
                      scan_interval_minutes=1, last_scan_at=now)
            s.add_all([a, b, c])
            await s.commit()
            return [m.id for m in (a, b, c)]

    ids = run(seed())
    run(scan_mod.scan_all_media())

    # 到期过滤：A（从未巡检）、B（已到期）巡检；C（未到期）跳过
    assert sorted(routed) == sorted([ids[0], ids[1]])

    # 对照：force=True 全部巡检
    routed.clear()
    run(scan_mod.scan_all_media(force=True))
    assert sorted(routed) == sorted(ids)
```

核对 `test_scan_all_media_force_skips_due_filter`（632-659）：其 force=True 全量断言与恢复语义一致，**保持不动**（如运行通过无需编辑）。

- [x] **Step 3: 逐用例核对无隐含依赖**

搜索 `backend/tests/` 下引用 `scan_all_media` 的全部用例（`test_scan_baseline.py` / `test_oracle_fixes.py` / `test_task_run_duration.py`），确认无其它断言依赖「未到期也扫」或「force 不再改变行为」：

```bash
cd backend && grep -rn "scan_all_media" tests/ | grep -v "job\|异常\|error"
```

- [x] **Step 4: 运行两个测试文件验证全绿**

```bash
cd backend && .venv/bin/python -m pytest tests/test_scan_baseline.py tests/test_oracle_fixes.py -x -q
```

Expected: 全部 PASS（含翻转后用例与 force 用例）。

- [x] **Step 5: 提交**

```bash
git add backend/tests/test_scan_baseline.py backend/tests/test_oracle_fixes.py
git commit -m "test(scan): 翻转未到期跳过断言并补 force 对照"
```

---

### Task 6: 全量回归

**Files:**
- 无（仅运行验证）

**Interfaces:**
- Consumes: Task 2-5 全部改动
- Produces: 全量回归证据

- [x] **Step 1: 运行全量测试**

```bash
cd backend && .venv/bin/python -m pytest tests/ -x -q
```

Expected: 全部 PASS（含巡检相关与转存相关、`test_settings_retired.py` 保持通过）。

- [x] **Step 2: 确认 test_settings_retired.py 未被改动且通过**

如回归通过，无需提交（无文件改动）；若 Step 1 有失败，按 `systematic-debugging` 技能调查根因后修复（禁止未查明根因直接改码）。

---

### Task 7: 批注偏离设计并完成 Open Questions 处置

**Files:**
- Modify: `docs/superpowers/specs/2026-09-09-queue-flow-rework-design.md`（偏离批注）
- Modify: `docs/superpowers/specs/2026-09-14-restore-scan-interval-scheduling-design.md`（Open Questions 处置回填）

**Interfaces:**
- Consumes: Task 1 的核查结论、本 change design.md 的 Open Questions
- Produces: 偏离记录 + 处置完成标记

- [x] **Step 1: 批注 queue-flow-rework 偏离设计**

在 `docs/superpowers/specs/2026-09-09-queue-flow-rework-design.md` 文件头部（标题下）追加批注：

```markdown
> **偏离设计（2026-09-14，restore-scan-interval-scheduling）**：本文档原定「全局统一间隔、
> 移除 per-media 冷却」未落地为全局统一调度，已改回 per-media 到期过滤（scan_all_media 按
> `Media.scan_interval_minutes` 兜底 60 分钟到期判断；job 保持每分钟 tick）。决策依据与
> 实现见 `docs/superpowers/specs/2026-09-14-restore-scan-interval-scheduling-design.md`。
```

- [x] **Step 2: 回填本 change design.md Open Questions 处置**

在 `docs/superpowers/specs/2026-09-14-restore-scan-interval-scheduling-design.md` 第 6 节 Open Questions 处置表追加：

```markdown
| Task 3「自然重试」对每分钟全量的代码依赖 | **已核实**（Task 1）：不依赖每分钟全量必扫，重试延迟 ≤interval，符合「自然重试」语义 |
```

（若 Task 1 已在 design.md Open Questions 段记录结论，此处仅保持两处一致，不重复堆叠。）

- [x] **Step 3: 勾选 tasks.md 4.1**

编辑 `<classic-change-dir>/tasks.md`，勾选 `4.1`。

- [x] **Step 4: 提交**

```bash
git add docs/superpowers/specs/2026-09-09-queue-flow-rework-design.md docs/superpowers/specs/2026-09-14-restore-scan-interval-scheduling-design.md docs/openspec/changes/restore-scan-interval-scheduling/tasks.md
git commit -m "docs(scan): 批注偏离 queue-flow-rework 设计并处置 Open Questions"
```

---

### Task 8: 验收确认（需要用户手动操作）

**Files:**
- Modify: `<classic-change-dir>/tasks.md`（勾选 4.2，用户确认后）

**Interfaces:**
- Consumes: Task 2-6 的实现与回归
- Produces: 验收证据（用户观察结果）→ tasks.md 4.2 勾选

- [x] **Step 1: 向用户说明验收步骤**

向用户说明（不改代码）：

> 部署后验收：
> 1. 在影视详情页把某剧的「巡检间隔」改为 5 分钟并保存 → 5 分钟内该剧被扫（前端输入框重新生效）。
> 2. 观察一天 task_run 写入量回落至约 N×24（原为 N×1440/天）。
> 3. 新剧发现延迟从 ≤1 分钟变为 ≤60 分钟（默认间隔；已确认可接受，可调小关注剧集间隔）。

- [x] **Step 2: 用户确认后勾选 4.2**

用户确认验收完成后，编辑 `<classic-change-dir>/tasks.md` 勾选 `4.2`，并提交：

```bash
git add docs/openspec/changes/restore-scan-interval-scheduling/tasks.md
git commit -m "docs(scan): 确认 4.2 验收完成"
```

---

## 任务依赖图

```
Task 1（核查，无代码）──┐
Task 2（过滤+force+测试）┤── Task 4（注释）── Task 5（翻转）── Task 6（回归）── Task 7（文档）
Task 3（touch+测试）────┘                                                       └── Task 8（验收）
```

Task 1/2/3 可部分并行（Task 2 与 Task 3 改动同文件不同区域，建议顺序执行避免冲突）；Task 5 依赖 Task 2；Task 6 依赖 2/3/4/5；Task 7 依赖 1；Task 8 依赖 6。

## 自检记录

- **Spec 覆盖**：Scenario「到点触发」→ Task 2/3；「未到期跳过」→ Task 2+5；「per-media 间隔配置生效」→ Task 2（间隔链）；「手动全量绕过」→ Task 2（force）；「故障不被冷却」→ Task 3；「新建影视立即首巡」→ Task 2（NULL 立即扫）。全部覆盖。
- **占位符扫描**：无 TBD/TODO；每个代码步骤含完整实现。
- **类型一致**：`_due_filter()` 名称在 Task 2 定义与使用一致；测试用例名在 Task 2/5 中命名区分（`skips_not_due` vs `skips_within_interval_cooldown`）；`_patch_scan_env` / `_seed_media` / `_mock_baseline_config` 沿用现有测试辅助。
