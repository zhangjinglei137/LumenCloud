# Brainstorm Summary

- Change: scan-outcome-attribution
- Date: 2026-10-20
- 状态：用户已确认

## 确认的技术方案

归因字段放 `task_run.scan_detail`（`skip_reason` 9 值 / `gated_out` / `trigger_source` / `missing_aired_total`），`status=skipped` 保留并用 `skip_reason` 区分；所有短路分支统一落 `scan_detail` 骨架；告警复用 `flow_error` 通道，冷却自建模块级 TTL 表（key=`media_id:reason`，24h），连续 N 轮由 `task_run` 历史推导；`task_run` 卡 running 由 `_recover_stale_scan_runs()` 在 `recover_stale_tasks` 早退前回收；手动触发由 `trigger_scan_background` 同步建 run 并返回 id，`_scan_one` 复用；文案由新增 `_skip_message` 承担，`_result_message` 保持纯计数契约。

## 关键取舍与风险

- 核对代码后修正 draft 的 5 处硬冲突：枚举不全（6→9）、四个短路分支 `scan_detail=None`、notifier 无通用冷却且 `extra.media_id` 不入库、recovery 早退在 200 行、手动触发会双建记录。
- 冷却选进程内 TTL 表（零迁移），代价：重启后最坏多推一条。
- 旧 `task_run` 无 `skip_reason`，读取侧按 unknown，不回填。

## 测试策略

pytest：`_skip_message` 表驱动 9 个 reason；`run_id` 复用只产生 1 条记录；四个短路分支骨架非空；`_recover_stale_scan_runs` 三分支；连续 3 轮 `no_candidates` 只告警一次、第 4 轮不重复、缺失数下降清冷却。回归 `test_recovery*.py` 三个文件。集成用 fake Emby/cloudSaver 跑 3 轮断言通知表仅 1 条。

## Spec Patch

`specs/notifications/spec.md`：「受既有去重与冷却约束」→「由系统自行维护同一影视同一原因的冷却窗口」（已回写）。原因：notifier 无通用冷却设施。
