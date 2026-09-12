## Context

巡检调度现处于「半废弃」状态：`scan_all_media_job` 硬编码 `IntervalTrigger(minutes=1)` 每分钟 tick，`scan_all_media()` 每轮直接遍历全部 tracking/downloading 影视逐一巡检（scan.py:1442-1463）；per-media `scan_interval_minutes` / `last_scan_at` 字段仍存在且 `_finish_scan_run` 仍更新 `last_scan_at`，但不再被读取过滤。前端 `MediaDetailView.vue` 的 per-media「巡检间隔」输入框可编辑保存但不生效。全局 `system_config.scan_interval_minutes` 已被 remove-deprecated-settings 退休（`_RETIRED_EXACT`）。动机详见 proposal.md - Why（用户期望按设置的间隔巡检；每分钟全量造成真实 API/task_run 开销）。

方案选择经多模型 council 评审：主推方案 a（恢复 per-media 到期过滤），方案 b（全局统一间隔，需复活已退休的全局键 + 动态 reschedule job）保留为演进路径，方案 c（保持每分钟全量）不推荐为终态。

## Goals / Non-Goals

**Goals:**
- 恢复 `scan_all_media` 的 per-media 到期过滤，使 `Media.scan_interval_minutes` 重新生效（兜底全局默认 60 分钟）。
- 修复 `_scan_one` 短路分支（Emby 故障 / 未收录）错误更新 `last_scan_at` 导致故障 media 被冷却的问题。
- 恢复 `force` 语义（绕过过滤的全量入口）。
- 使 `scheduler.py` docstring 与实现一致（文档债）。

**Non-Goals:**
- 不复活已退休的全局 `system_config.scan_interval_minutes`（保持 `_RETIRED_EXACT` 退役；全局统一间隔留作演进路径，未来若产品明确要全库统一节奏，再从 a 平滑演进到 b：移除数据层过滤 + job.reschedule）。
- 不改 task_run 记录格式、前端队列展示。
- 不改 APScheduler 注册（job 保持每分钟 tick，过滤在 `scan_all_media` 内）。

## Decisions

**D1: 过滤位置——SQL 侧（scan_all_media 查询内）**
在 `scan_all_media` 的 `select(Media)` 查询中按到期条件过滤（`last_scan_at IS NULL OR last_scan_at <= :threshold`），threshold 由每 media 间隔计算。SQL 侧过滤使未到期 media 不进入 Python 层，tick 成本趋近于一次轻量索引查询。
- 备选：Python 层过滤（拉全量再跳过）——media 数大时浪费；不采用。
- 时区基准：`_now()`（naive UTC）与 `last_scan_at` 写入同源（`_finish_scan_run` 用 `_now()` 写入），禁止混用本地时间。

**D2: 间隔取值链——`media.scan_interval_minutes ?? settings.SCAN_INTERVAL_MINUTES(60)`**
历史数据可能为 NULL（迁移脚本用 `or 60` 兜底），应用层必须显式兜底，不能直接读 NULL。全局默认 60 分钟沿用现状。

**D3: `_scan_one` touch 语义——仅「真正完成主流程」分支更新 `last_scan_at`**
现状 `touch_last_scan_at=True` 用于 Emby 故障 error（scan.py:1598）与未收录 skipped（1611）等短路分支——恢复过滤后这些分支会推迟下轮重试（故障剧被冷却一个间隔）。改为：短路分支 `touch_last_scan_at=False`（保留日志与重试计数，下轮 tick 继续重试），仅正常完成主流程的分支 touch=True。
- 这是对 council「保留旧行为 vs 激进修复」分歧的裁决：采纳 glm 的激进修复，更符合 Task 3「自然重试」意图。
- 需逐分支审查 `_scan_one` 全部 `_finish` 调用点确认 touch 取值。

**D4: `force` 语义恢复**
`force=True` 绕过到期过滤（CLI/手动全量入口），恢复既有调用契约；`scan_all_media` docstring 同步修正「force 不再改变遍历行为」的过时描述。

**D5: downloading 语义澄清**
现有注释「downloading 不跳过」指不排除出巡检集合（防卡死），**不是**绕过冷却。恢复过滤后 downloading media 仍按各自间隔巡检、仍不入队（scan.py:1693 逻辑不变），注释需写清避免再次误读。

**D6: 不做全局键复活（方案 b 排除）**
全局 `system_config.scan_interval_minutes` 已被显式退休并有 `test_settings_retired.py` 固化。方案 a 不依赖该键（per-media 字段 + `settings.SCAN_INTERVAL_MINUTES` 兜底即可），保持退役状态更干净。若未来走方案 b 需单独评估「复活退役键」的工程面。

## Risks / Trade-offs

- [新剧发现延迟] 从每分钟全量（≤1min）变为按间隔（默认 ≤60min）→ 依赖快速追更的用户是感知变化；release note 说明，并引导把关注剧集间隔调小。这是 council 点名的产品决策，实施前需用户确认默认 60min 是否可接受。
- [touch 语义回归] 短路分支 touch=False 属行为回归 → 列入回归测试（新增「故障/未收录下轮重试」用例）+ release note。
- [存量间隔值爆炸] 老库可能存在用户配过的小值（如 5min），恢复后这些值重新生效 → 实施前摸底存量 `scan_interval_minutes` 值分布；如存在异常小值需与用户确认。
- [测试翻转范围] `test_scan_baseline.py` / `test_oracle_fixes.py` 固化「未到冷却期也巡检」断言 → 逐用例核对翻转，确认无隐含依赖「未到期也扫」的其它用例。
- [单轮耗时退化] 大库下「单轮全量扫描耗时 vs 1 分钟间隔」的节奏退化 → `task_run.duration_seconds`（Q8 已落库）监控；过滤后每轮只扫到期 media，风险已大幅收敛。
- [Task 3 重试闭环] council 提示核实「自然重试」对每分钟全量的代码依赖 → 实施前核实 `_scan_one` 的搜索/重试路径是否依赖下轮必扫；若依赖，per-media 间隔下缺失集重试延迟为 ≤interval，符合「自然重试」语义但节奏变化需确认。

## Migration Plan

- 纯后端行为变更，无 schema 迁移（字段已存在）。
- 部署后：修改某剧详情页间隔为 5 分钟并保存 → 5 分钟内该剧被扫（前端输入框重新生效）；`scheduler.py` docstring 一致化无需数据迁移。
- 回滚：还原 `scan_all_media` 过滤与 `_scan_one` touch 语义即可恢复每分钟全量行为。

## Open Questions

- 默认 60 分钟的新剧发现延迟是否可接受（产品确认，实施前）。
- 存量 `scan_interval_minutes` 值分布摸底结果（实施前）。
- Task 3「自然重试」对每分钟全量的确切代码依赖（实施前核实）。
