# Rulings — scan-outcome-attribution

记录实施层自主决定（不改变规格与验收），供复查与归档时追溯。

## R1 更新 `test_scan_run_phases.py` 的旧契约断言

- 关联任务：1.1（`d01d3009-f64c-4279-80bd-d21307541f96`）
- 决定：`backend/tests/test_scan_run_phases.py:216` 由 `assert runs[0].scan_detail is None` 改为断言 `skip_reason == "status_paused"`。
- 依据：design D1a 要求所有短路分支必须落 `scan_detail` 骨架，旧断言正是本次要废除的契约。属计划允许清单外的测试文件，改动 4 行。
- 影响：仅测试契约同步，无生产行为变化。

## R2 `not_aired` 与 `gated_out` 的划分口径

- 关联任务：1.1、1.2
- 决定：`gated_out` = 常规路径下被已播门控剔除的集；`not_aired` = 全量兜底模式下 TMDB 全集均无已播集。两者都不并入 `no_missing`。
- 依据：design 只规定 9 类枚举，未细化这两者的边界；若合并，用户无法区分「本来没缺」与「缺的还没播」。

## R3 `airdate_unavailable` 复用于两类取数失败

- 关联任务：1.1、1.2
- 决定：TMDB 季取数失败与「全量兜底缺 tmdb_id / 全集取数失败」共用 `airdate_unavailable`，靠文案 `detail` 区分。
- 依据：design 固定 9 类枚举，不为取数失败再拆枚举；语义同为「无法判定播出日期」。

## R4 `existing_skipped` 语义收窄

- 关联任务：1.1、1.2
- 决定：`scan_detail.existing_skipped` 不再计入「因 downloading 本轮未入队」的集；后者改为按集去重计入 `downloading_skipped_keys`，并以 `skip_reason=skip_enqueue` + 独立文案表达。
- 依据：design D6 与任务 1.2 明确要求「因下载中未入队」不得表述为「已有任务跳过」。

## R5 `no_candidates` 的置位条件收紧（主会话复查后修正）

- 关联任务：1.1
- 决定：仅当 `enqueued==0` 且 `missing_aired_total>0` 且 `existing_skipped==0` 且 `unmatched==0` 且 `size_filtered==0` 且 `non_video==0` 时置 `no_candidates`。
- 依据：`unmatched` / `size_filtered` / `non_video` 均表示已找到过资源，只是未入队。若误置 `no_candidates`，P2 的「连续 N 轮无资源」告警会把「被大小过滤」误报成「资源尚未更新」，重犯本 change 要消灭的误导。

## R7 `queue_retry` 作为新的 `task_run.task_type`

- 关联任务：4.2（`2e769dd1-76fc-40e9-b64c-abc09b930760`）
- 决定：队列 retry 不伪造一次巡检，而是写 `task_type="queue_retry"`、`status="success"`、`scan_detail.trigger_source="manual"` 的受理记录；并同步登记到 `routers/logs.py::_TASK_TYPES` 与 `frontend/src/utils/format.ts::TASK_TYPE_MAP`。
- 依据：spec 原文为「每次由用户主动触发的巡检**或重试**中记录触发来源」，retry 不是巡检，写 `scan_media` 记录会让巡检统计失真；但不登记类型会让记录无法筛选，削弱可追溯性，故两处登记属需求内必要动作，不算前端展示层改造。

## R8 冷却清除后的再告警语义保持「能重新告警」而非「仅恶化时告警」

- 关联任务：2.2（`0bcfcaa8-95f0-420b-9a90-38c07cb6b8a5`）
- 决定：`missing_aired_total` 下降时清除冷却键并抑制当轮告警；下一轮只要连续轮次仍成立即重新告警，不额外要求数量再次上升。
- 依据：design D3 的表述是「缺失集数量下降时清除冷却键，使再次恶化能重新告警」——要求的是「可重新告警」，未限定必须恶化。当前行为在持平场景会多发一条，方向是「不掩盖问题」，优于漏报。

## R6 结论文案中的集号列表截断

- 关联任务：1.2
- 决定：`_skip_message` 的 `no_candidates` / `gated_out` 只列前 10 个集号，超出追加「等 N 集」；`scan_detail.gated_out.episodes` 保留完整列表。
- 依据：生产实例斗破苍穹缺失 239 集，全量 join 会让 `task_run.message` 达数千字符，撑爆运行日志与通知。
