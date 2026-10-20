# Proposal

## Why

2026-10-19 用户发现斗破苍穹「该更新但缺 4 集」，系统没有补上，最终由用户手动处理。生产库实查显示：该剧自 10-16 起每小时巡检一次，**每一次都以 `skipped` 结束**，`missing_total=239`、`search_status=no_candidates`、`enqueued=0`，全程不产生任何通知；用户手动操作在 `task_run`、`task_queue`、`download_queue` 中**没有任何留痕**。全库 17 部 tracking 影视最近一次巡检同样全部 `skipped`。

根因不是「漏跑」，而是**结果不可解释**：`skipped` 同时代表「没有缺集」「还没播」「被已播门控剔除」「搜不到资源」「因下载中未入队」五种完全不同的结论，用户与系统都无法据此判断是否需要干预；巡检失败不告警；手动触发不落库。系统在错误前提上安静空转，用户只能靠「发现没更新」兜底。

本次改造只解决「过程与结论不可见」，不改缺失集判定来源（基线可信化见 `missing-baseline-integrity`）。

## What Changes

- `task_run.scan_detail` 新增归因字段：`skip_reason`（`no_missing` / `not_aired` / `gated_out` / `no_candidates` / `baseline_suspect` / `skip_enqueue`）、`gated_out`（被已播门控剔除的集数与集号）、`trigger_source`（`auto` / `manual`）。
- 巡检结果文案按归因分类生成，不再把「有缺集但搜不到资源」折叠为「跳过」；`task_run.status` 的 `skipped` 保留，但语义收窄为「无需处理」。
- 巡检异常接入告警：巡检 `error`、以及「存在已播缺集但连续多轮无资源」时，通过既有 `flow_error` 通道发节流通知（站内 + PushPlus 可用时）。
- `recovery` 增加 `task_run` 卡 `running` 的超时回收，标注回收原因，避免日志永久停留在「正在巡检」。
- 手动触发路径（`POST /api/media/{id}/scan`、队列 retry）必须落库并带 `trigger_source=manual`；触发失败或后台任务异常时向调用方返回明确结果，不再 fire-and-forget 静默。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `media-pipeline`：巡检结果必须可解释——每轮巡检须记录可区分的结论与归因计数，手动触发须留痕且可追溯；不得将不同失败语义折叠为同一状态。
- `notifications`：新增巡检异常通知时机——巡检错误与「长期存在已播缺集但无资源」须产生节流告警，且受既有去重与脱敏约束。

## Impact

- 后端：`backend/app/tasks/scan.py`（归因、文案、告警触发）、`backend/app/tasks/recovery.py`（task_run 回收）、`backend/app/services/notifier.py`（事件与节流复用）、`backend/app/routers/media.py` 与 `backend/app/routers/queue.py`（手动触发留痕与反馈）。
- 数据：`task_run.scan_detail` 为 JSON 字段，新增键不涉及表结构变更；`task_run` 现有记录保持可读（缺字段按未知处理）。
- 前端：本次不改展示层；新增字段由后续 `media-detail-update-diagnosis` 消费。
- 测试：`backend/tests/` 下 scan、recovery、notifier 相关用例需同步补充。
- 外部依赖：无新增；PushPlus 未配置时按既有行为降级为站内通知。
