## Why

巡检任务当前由 APScheduler 硬编码每分钟触发 `scan_all_media_job`，每轮直接遍历全部 tracking/downloading 影视逐一执行完整巡检（Emby 基线 + 缺失集搜索 + 每条 task_run 落库）。用户设置中的 per-media「巡检间隔」（`Media.scan_interval_minutes`）自 queue-flow-rework 起被移出调度逻辑，仅作兼容字段保留——设置不生效、每分钟全量巡检造成真实的外部 API 压力与 task_run 写入量（约 N×1440 条/天）。用户期望巡检按设置的间隔执行。

## What Changes

- 恢复 per-media 到期过滤：`scan_all_media` 按各 media 的 `scan_interval_minutes`（兜底 `settings.SCAN_INTERVAL_MINUTES=60`）与 `last_scan_at` 判断到期，仅巡检到期 media；job 保持每分钟 tick，未到期 media 以轻量 SQL 过滤跳过。
- 修复 `_scan_one` 短路分支的 `last_scan_at` 更新语义：仅真正完成巡检主流程的分支更新 `last_scan_at`；Emby 故障 / 未收录等短路分支不更新，使故障 media 下轮继续重试（不因故障被冷却一个间隔）。
- 恢复 `force` 语义：`force=True` 绕过到期过滤，恢复 CLI/手动全量巡检入口契约。
- 明确 `downloading` 语义：不排除出巡检集合（防卡死）≠ 绕过冷却。
- 同步修复文档债：`scheduler.py` 模块 docstring 与 `register_jobs` docstring 目前描述「按各 media last_scan_at 到期过滤」，与实现一致化（恢复后该描述重新为真）。
- 同步测试：翻转 `test_scan_baseline.py` / `test_oracle_fixes.py` 中固化「未到冷却期也巡检」的断言；新增「未到期跳过」「last_scan_at NULL 立即扫」「force 绕过过滤」「故障/未收录下轮重试」用例。
- 前端 `MediaDetailView.vue` 的 per-media「巡检间隔」输入框恢复生效（语义回归，无需改前端代码）。

**非目标**：
- 不复活已退休的全局 `system_config.scan_interval_minutes` 配置键（保持 `_RETIRED_EXACT` 退役状态；全局统一间隔作为演进路径另行评估）。
- 不改 task_run 记录格式与前端队列展示。
- 不做 per-media 之外的新调度机制。

## Capabilities

### New Capabilities

（无——本 change 不引入新能力）

### Modified Capabilities

- `media-pipeline`: 「全局统一定时巡检」requirement 从「按全局统一间隔遍历全部影视、不得依赖 per-media 间隔」修改为「按各 media 的 `scan_interval_minutes` 到期过滤调度；job 保持每分钟 tick，每轮仅巡检到期 media；Emby 故障/未收录短路分支不更新 `last_scan_at`，下轮继续重试」。

## Impact

- **代码**：`backend/app/tasks/scan.py`（`scan_all_media` 到期过滤、`_scan_one` touch 语义、`force` 语义恢复）、`backend/app/scheduler.py`（docstring 一致化）。
- **测试**：`backend/tests/test_scan_baseline.py`、`backend/tests/test_oracle_fixes.py` 断言翻转与新增；`backend/tests/test_settings_retired.py` 保持不动（全局键维持退休）。
- **行为**：巡检频率从每分钟全量变为按 per-media 间隔（默认 60 分钟）；新剧发现延迟从 ≤1min 变为 ≤interval；task_run 写入量约降 60×；Emby/cloudSaver 外部 API 调用量同比例下降。
- **文档**：`backend/app/scheduler.py` docstring、本 change 的 design.md 记录「偏离归档 queue-flow-rework 全局间隔设计」的决定。
