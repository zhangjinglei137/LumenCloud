# 验证报告：restore-scan-interval-scheduling

- Change: restore-scan-interval-scheduling
- 日期: 2026-09-14
- 语言: zh-CN
- verify_mode: full（10 任务、1 delta spec、8 变更文件）
- 验证证据: `.venv/bin/python -m compileall -q app && .venv/bin/python -m pytest tests/ -q` → **643 passed**（exit 0）

## 验证摘要

| 维度 | 状态 |
|------|------|
| Completeness（完整性） | 10/10 任务完成，1/1 requirement 实现 |
| Correctness（正确性） | 6/6 场景有实现与测试覆盖 |
| Coherence（一致性） | 设计决策全部遵循；2 项非阻断偏差已接受并记录 |

## 1. Completeness — 任务与规格覆盖

- tasks.md：10/10 全部 `[x]` 勾选（1.1 事实核查、1.2 存量摸底、2.1-2.4 核心实现、3.1-3.2 测试、4.1 文档批注、4.2 部署后验收说明）。
- 实施计划：8 个 Task 全部步骤勾选。
- Requirement「全局统一定时巡检」（delta spec media-pipeline）：已实现——`scan_all_media` 按 per-media `scan_interval_minutes`（COALESCE 兜底 60）到期过滤，`force=True` 绕过；“全局统一”语义随文件回归为 per-media 调度。

## 2. Correctness — 需求实现与场景覆盖

### Requirement 实现映射

| Requirement | 实现位置 | 一致 |
|---|---|---|
| 按 per-media 间隔到期过滤 | `scan_all_media`（scan.py）+ `_due_filter()`（跨方言 SQL） | ✓ |
| 未到期跳过 | `_due_filter()`：`last_scan_at IS NULL OR last_scan_at <= now - 间隔` | ✓ |
| per-media 间隔生效（兜底 60） | `COALESCE(media.scan_interval_minutes, settings.SCAN_INTERVAL_MINUTES)` | ✓ |
| 手动全量绕过间隔 | `scan_all_media(force=True)` 跳过 `_due_filter()` | ✓ |
| 故障影视不被冷却 | `_scan_one` 4 个短路分支 `touch_last_scan_at=False` | ✓ |
| 新建影视立即首巡 | `last_scan_at IS NULL` → 立即进入巡检 | ✓ |

### Scenario 覆盖（6/6）

| Scenario | 测试 | 状态 |
|---|---|---|
| 到点触发该影视巡检 | `test_scan_all_media_scans_due_media` | ✓ |
| 未到期影视被跳过 | `test_scan_all_media_skips_not_due` | ✓ |
| per-media 间隔配置生效 | `test_scan_all_media_null_interval_falls_back_to_default`（NULL→60 兜底） | ✓ |
| 手动全量巡检绕过间隔 | `test_scan_all_media_force_bypasses_due` + `test_scan_all_media_force_skips_due_filter`（保持） | ✓ |
| 故障影视不被间隔冷却 | `test_scan_one_emby_error_does_not_touch_last_scan_at` + `test_scan_one_missing_required_does_not_touch_last_scan_at` | ✓ |
| 新建影视立即首巡 | `test_scan_all_media_scans_never_scanned` | ✓ |

### 全量回归

```bash
cd backend && .venv/bin/python -m pytest tests/ -q
```
→ **643 passed, 2 warnings**（starlette/anyio DeprecationWarning，与本 change 无关）。`test_settings_retired.py` 保持通过（全局键维持退休）。

## 3. Coherence — 设计一致性

### OpenSpec design.md 高层决策（D1-D6）遵循情况

| 决策 | 实现 | 一致 |
|---|---|---|
| D1 SQL 侧到期过滤 | `_due_filter()` 查询内过滤，未到期不进入 Python 层 | ✓ |
| D2 间隔取值链 | `COALESCE(media.scan_interval_minutes, settings.SCAN_INTERVAL_MINUTES=60)` | ✓ |
| D3 touch 语义（短路分支不冷却） | 4 分支改 False，无遗漏/正常完成保留 True | ✓ |
| D4 force 语义恢复 | force=True 绕过过滤，docstring 修正 | ✓ |
| D5 downloading 语义澄清 | 注释明确「防卡死 ≠ 绕过冷却」 | ✓ |
| D6 不复活全局键 | `test_settings_retired.py` 未动，无全局键引用 | ✓ |

### 最终集成代码审查（@oracle，review_mode: standard）

对整个 change 最终 diff（`ec2d6dd0..HEAD`，11 commits）执行一次集成审查，判定 **PASS**：

- 上次审查发现的 1 IMPORTANT（PG 时区基准）+ 2 MINOR（裸 session / func.concat 版本依赖）经编译验证全部消解：
  - PG 分支 `bindparam("now", _now())` → `timestamp - interval` 无隐式时区 cast，过滤端与写入端真正同源
  - 方言判定改 `engine.dialect.name`（进程级不变量），无裸 session
  - `literal("-") + cast + literal(" minutes")` 编译为 `||`，消除 SQLite ≥3.43 依赖
- touch 语义 7 个分支点逐一核对完整；边界条件（NULL 立即扫 / downloading 参与到期判断 / force 绕过）正确。
- 无 CRITICAL / IMPORTANT。

### 已接受的非阻断偏差（用户确认 2026-09-14）

| # | 级别 | 内容 | 处理 |
|---|---|---|---|
| WARNING-1 | WARNING | media 表无 `(status, last_scan_at)` 复合索引；当前 8 行规模 YAGNI 决策正确，但未来行数增长后每分钟 tick 全表扫描成本上升 | **接受偏差**：design.md 已显式记录 YAGNI 决策；留 follow-up——当 media 行数增长至千级时评估复合索引（记入 release note / 后续 change） |
| SUGGESTION-1 | SUGGESTION | 本 change Design Doc §2.T2.1 伪代码仍为审查前初版（func.now / async_session().bind / func.concat），未同步最终实现 | **接受偏差**：Design Doc 是设计意图文档，实现改进后文本滞后属正常；归档时由 `/comet-archive` 同步伪代码或标记 superseded |

## 4. proposal.md 目标满足

| 目标 | 状态 |
|---|---|
| 恢复 per-media 到期过滤 | ✓ |
| 修复短路分支 last_scan_at 更新语义 | ✓ |
| 恢复 force 语义 | ✓ |
| 明确 downloading 语义 | ✓ |
| 同步 docstring（scheduler.py 已是目标态，scan.py 已同步） | ✓ |
| 翻转并新增测试 | ✓ |
| 前端 per-media 输入框恢复生效（语义回归，无需改前端） | 部署后验证（4.2 部署后项） |

## 5. 非目标保持

- 全局 `system_config.scan_interval_minutes` 未复活（`_RETIRED_EXACT` 保持，`test_settings_retired.py` 通过）✓
- 未新增 force CLI/API 入口（仅函数语义）✓
- APScheduler 注册未改（job 保持每分钟 tick）✓
- task_run 格式、前端队列展示未改 ✓

## 结论

**所有检查通过**（Completeness 10/10、Correctness 6/6、Coherence 遵循 + 2 项已记录偏差）。642 项既有测试全绿 + 新增/翻转测试通过（全量 643 passed）。无 CRITICAL / IMPORTANT 遗留。**可进入归档**。