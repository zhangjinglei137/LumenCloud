---
comet_change: restore-scan-interval-scheduling
role: technical-design
canonical_spec: openspec
archived-with: 2026-09-14-restore-scan-interval-scheduling
status: final
---

# 恢复 per-media 巡检间隔调度 — 深度技术设计

> 上游事实源：`docs/openspec/changes/restore-scan-interval-scheduling/`（proposal.md / design.md / specs/）。本文件是对 open 阶段高层决策（D1-D6）的深度技术细化：实现方案、边界条件、测试策略与技术风险。brainstorming 确认记录见 `.comet/handoff/brainstorm-summary.md`。

## 1. 实现环境约束（设计前提）

- **调度结构**：`scan_all_media_job`（APScheduler `IntervalTrigger(minutes=1)`，scheduler.py:110）每分钟 tick → `scan_all_media()`。恢复后 job 保持每分钟 tick 不变，到期过滤在 `scan_all_media` 内部完成（Non-Goal 确认）。
- **数据库**：单 worker；测试用 SQLite（`sqlite+aiosqlite`，`StaticPool` 单连接），生产可切换 `DATABASE_URL` 至 PostgreSQL（`postgresql+asyncpg://`）→ **到期过滤表达式必须跨方言**（SQLite / PostgreSQL 两侧语义一致）。
- **时间基准**：`_now()` = `app.utils.now_utc_naive`（naive UTC，`datetime.now(timezone.utc).replace(tzinfo=None)`），与 `_finish_scan_run` 写 `last_scan_at` 同源（scan.py:1567）。禁止混用本地时间/aware datetime。
- **间隔取值链**：`COALESCE(media.scan_interval_minutes, settings.SCAN_INTERVAL_MINUTES)`，`settings.SCAN_INTERVAL_MINUTES = 60`（config.py:103）。历史数据可能 NULL（server_default=60 但老行可能为空），应用层显式兜底。
- **全局键退休**：`system_config.scan_interval_minutes` 已由 remove-deprecated-settings 退休并固化于 `test_settings_retired.py`，**不复活**（D6）。
- **force 入口现状**：`scan_all_media(force=True)` 当前无真实调用方（仅测试）。用户确认**仅恢复函数语义 + docstring，不新增 CLI/API 入口**。
- **存量摸底（已执行）**：media 表 8 行，`scan_interval_minutes` 全为 60 或 NULL（无非 60 值），`last_scan_at` 无 NULL → 「存量间隔值爆炸」「首巡立即触发」两类风险面均收敛。
- **docstring 现状**：scheduler.py 模块 docstring 与 `register_jobs` docstring 已描述恢复后的到期过滤行为（恢复后重新为真，**无需改动**）；需改的是 scan.py 内 `scan_all_media` / `scan_all_media_job` docstring 与 downloading 注释。

## 2. 实现设计（按任务组细化）

### T2.1 到期过滤（scan_all_media SQL 侧）

**现状**（scan.py:1497-1518）：`select(Media).where(Media.status.in_(("tracking", "downloading")))` 拉全量后逐一 `scan_media`。

**改造**：查询增加到期条件，`force=False` 时生效：

```sql
WHERE status IN ('tracking','downloading')
  AND (last_scan_at IS NULL
       OR last_scan_at <= <now 减 per-media 间隔>)
```

- threshold 是**逐行计算的表达式**（每 media 间隔不同），不能用单一绑定参数；以 `COALESCE(scan_interval_minutes, 60)` 参与表达式。
- **跨方言实现**：封装私有 helper `_due_filter(now)`（scan.py 内），按 `async_session().bind.dialect.name` 分支：
  - SQLite：`Media.last_scan_at <= func.datetime(now_iso, '-' || func.coalesce(Media.scan_interval_minutes, 60) || ' minutes')`（`now_iso = _now().strftime('%Y-%m-%d %H:%M:%S')`）
  - PostgreSQL：`Media.last_scan_at <= func.now() - (func.coalesce(Media.scan_interval_minutes, 60) * text("interval '1 minute'"))`
  - 两分支同一语义；helper 返回 `or_(Media.last_scan_at.is_(None), <到期比较>)`。
- `force=True`：跳过整个到期条件，恢复「遍历全部 tracking/downloading」的原契约。
- **不建新索引**：`media` 表仅 8 行规模（`status`/`last_scan_at` 现有索引可覆盖），YAGNI；若未来行数增长再评估 `(status, last_scan_at)` 复合索引。

**边界**：
- `last_scan_at IS NULL` → 立即首巡（新建影视下轮 tick 即扫，对应 Scenario「新建影视立即首巡」）。
- `downloading` 状态 media **参与到期判断**（有各自间隔），仍不排除出巡检集合——「防卡死」语义是「不跳过 downloading」，不是「绕过冷却」（D5，仅注释澄清，行为不变）。
- 到期但 `paused`/`error`：不在 `status IN (...)` 集合内（`_scan_one` 状态预检兜底，行为不变）。
- tick 与上次巡检同一分钟内的边界：`<=` 语义，间隔内最后一次扫描与 `now - interval` 恰好相等视为到期（对齐「已超过配置间隔」的 SHALL 语义；容差在 SQL 时间精度内可忽略）。

### T2.2 恢复 force 语义

- `scan_all_media(force: bool = False)` 签名不变；`force=True` 时查询条件仅保留 `status IN (...)`，跳过到期过滤。
- docstring 同步：删除「force 不再改变遍历行为」过时描述，改为「force=True 绕过 per-media 到期过滤，全量巡检 tracking/downloading（CLI/手动全量入口契约保留，本 change 不新增入口）」。
- 调用契约：`scan_all_media_job` 保持 `scan_all_media()`（force 默认 False）。

### T2.3 _scan_one 短路分支 touch 语义

`_finish_scan_run`（scan.py:1544）在 `touch_last_scan_at=True` 时 `update(Media).values(last_scan_at=_now())`。逐分支核对（用户确认：全部「未真正完成主流程」分支改 False）：

| 分支 | 位置 | 现状 | 改为 | 理由 |
|---|---|---|---|---|
| paused/error 跳过 | 1634-1638 | False | 保留 False | 状态预检跳过，未开始主流程 |
| Emby 故障 error | 1650-1654 | True | **False** | fail-safe，未完成基线 |
| Emby 未收录 skipped | 1663-1667 | True | **False** | 基线不可用，未探明遗漏 |
| 无遗漏 skipped | 1711-1714 | True | 保留 True | 巡检正常完成（基线 OK 且无缺集） |
| 搜索异常 error | 1727-1731 | True | **False** | 搜索服务故障，未完成主流程 |
| 上限读取失败 error | 1743-1746 | True | **False** | fail-closed 中止，未完成主流程 |
| 正常完成 success/skipped | 1936-1939 | True | 保留 True | 主流程走完（有/无入队均 touch） |

- 改 False 的分支**保留日志与重试计数**（`_finish` message / phases / scan_detail 逻辑不动），仅不更新 `last_scan_at` → 下轮 tick 继续尝试，不被冷却一个完整间隔。
- paused/error 分支本身不 touch（现状即 False），无行为变化。

### T2.4 docstring / 注释一致化（scan.py）

- `scan_all_media` docstring：恢复「按 per-media 间隔到期过滤；force=True 绕过」描述；删「移除 per-media 冷却过滤」「字段不再参与调度」等过时句。
- `scan_all_media_job` docstring：同步为「每分钟 tick；scan_all_media 内部按各 media last_scan_at 到期过滤」。
- `scan_all_media` downloading 相关注释：明确「downloading 不排除出巡检集合（防卡死）≠ 绕过冷却（仍按各自间隔到期判断）」。
- 模块顶部 docstring（scan.py:13 附近 `scan_all_media()` 一行说明）：同步为「遍历到期 tracking/downloading 影视」。
- scheduler.py：**不改**（模块 docstring 与 `register_jobs` docstring 已是目标态；若发现描述与恢复后行为仍有出入，仅做最小订正，不扩大范围）。

### T1.1 事实核查（build 阶段首步）

- 核实 `_scan_one` 搜索/重试路径（`_search_and_rank` 无候选 → 正常完成 touch=True → 缺集下轮按 interval 重试）对「每分钟全量必扫」的依赖：**结论预期**——queue-flow-rework Task 3 已移除 silent 静默机制，缺集重试本就不依赖每分钟全量，恢复过滤后重试延迟为 ≤interval，符合「自然重试」语义（节奏变化记入 release note）。核实后把结论写入本 design.md Open Questions 处置与 tasks.md 勾选。

### T1.2 存量摸底（已完成）

- 已执行 DB 查询：`scan_interval_minutes != 60` 计数 = 0；NULL 计数 = 0；`last_scan_at IS NULL` 计数 = 0。无异常小值需上报 → 任务 1.2 直接勾选，无需用户确认。

## 3. 测试策略

### 翻转既有断言（逐用例核对）

- `test_scan_baseline.py::test_scan_all_media_scans_within_interval_cooldown`（277）：原断言「last_scan_at=now（60 间隔）仍被扫」→ 翻转为「未到期跳过」，断言 `routed == []`。
- `test_oracle_fixes.py::test_scan_all_media_scans_all_tracking`（563）：A(never)/B(已到期 2min>1min)/C(未到期 now) → 翻转断言 `routed == [A, B]`（C 跳过）。
- `test_oracle_fixes.py::test_scan_all_media_force_skips_due_filter`（632）：force=True 全量语义**保持**（本测试与恢复后行为一致，仅核对无需翻转）。
- `test_scan_baseline.py::test_scan_all_media_scans_only_tracking_downloading`（313）：paused 不触及——保持（过滤仅收窄到期的 tracking/downloading，paused 本就不在集合）。
- 逐用例搜索测试文件内其它隐含依赖「未到期也扫」的断言（如 `_scan_one` 直测用例不涉及 `scan_all_media` 过滤，不受影响）。

### 新增用例

1. **未到期跳过**：60 间隔 + `last_scan_at=now` → `scan_all_media()` 不调用 `_scan_one`。
2. **last_scan_at NULL 立即扫**：`last_scan_at=None` → 下轮 tick 进入巡检（补「新建影视立即首巡」场景覆盖）。
3. **force 绕过过滤**：`force=True` 时未到期 media 也巡检（对照 force=False 跳过）。
4. **故障/未收录下轮重试**：mock `_emby_missing_codes` 抛异常 / 返回 None → `_finish` 后 `Media.last_scan_at` 不变（touch=False 断言，补「故障影视不被间隔冷却」场景覆盖）。

### 回归

- `cd backend && .venv/bin/python -m pytest tests/ -x -q` 全量回归，确认含巡检相关与转存相关全部通过、无跨模块破坏。
- `test_settings_retired.py` 保持不动（全局键维持退休）。

## 4. 技术风险与缓解

| 风险 | 缓解 |
|---|---|
| 新剧发现延迟 ≤1min → ≤60min（默认）| 已确认可接受；release note 引导调小关注剧集间隔（T4.2 验收项） |
| touch=False 行为回归（故障 media 每分钟重试打 API）| 属预期行为（自然重试）；回归测试固化；Emby 故障 fail-safe 已暂停新缺集发现，重试仅消耗基线调用 |
| 跨方言 SQL 表达式差异 | 封装 `_due_filter` 按 dialect 分支，两分支同语义；测试覆盖 SQLite 路径，PG 分支经代码走查 |
| 测试翻转隐含依赖 | 逐用例核对 + 全量回归兜底 |
| 每分钟重试节奏对 Task 3 重试闭环的影响 | T1.1 事实核查前置核实，结论入 Open Questions 处置 |
| docstring 与实现再漂移 | T2.4 一次性对齐 scan.py 全部相关 docstring/注释 |

## 5. 非目标（明确不做）

- 不复活全局 `system_config.scan_interval_minutes`（保持 `_RETIRED_EXACT` 退役；全局统一间隔 = 演进路径 b，另行评估）。
- 不新增 force 的 CLI/API 全量入口（用户确认，仅恢复函数语义）。
- 不改 APScheduler 注册（job 保持每分钟 tick）。
- 不改 task_run 记录格式、前端队列展示、前端代码（per-media 输入框语义回归自动恢复）。
- 不建新数据库索引（当前规模 YAGNI）。

## 6. Open Questions 处置

| 问题 | 处置 |
|---|---|
| 默认 60 分钟新剧发现延迟是否可接受 | **已确认**（brainstorming 决策 2）；release note 引导 |
| 存量 scan_interval_minutes 值分布 | **已摸底**（8 行全 60/NULL，无异常值） |
| Task 3「自然重试」对每分钟全量的代码依赖 | **已核实**（Task 1）：不依赖每分钟全量必扫，重试延迟 ≤interval，符合「自然重试」语义 |
