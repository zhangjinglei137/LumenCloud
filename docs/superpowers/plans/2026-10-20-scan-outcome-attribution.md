---
change: scan-outcome-attribution
design-doc: docs/openspec/changes/scan-outcome-attribution/design.md
base-ref: f4d932daf511b460b00f53b61c11996e5df260d4
archived-with: 2026-10-20-scan-outcome-attribution
---

<!-- comet-task-authority: docs/openspec/changes/scan-outcome-attribution/tasks.md -->

# 实施计划：scan-outcome-attribution

设计与需求分别以 `design.md`、`proposal.md` 与 `specs/**/spec.md` 为准，本计划只记录实施方法、依赖与验收命令。

## 工作包划分

| 包 | 任务 | 文件面 | 依赖 |
| --- | --- | --- | --- |
| A 归因与文案 | 1.1 / 1.2 / 1.3 | `backend/app/tasks/scan.py` | 无 |
| B 告警 | 2.1 / 2.2 / 2.3 | `backend/app/tasks/scan.py`、`backend/app/services/notifier.py` | A（`skip_reason` 是告警判据） |
| C 回收 | 3.1 / 3.2 | `backend/app/tasks/recovery.py`、`backend/app/tasks/scan.py` | 无（可与 A 并行，但共用 `scan.py`，串行执行） |
| D 手动留痕 | 4.1 / 4.2 / 4.3 | `backend/app/tasks/scan.py`、`backend/app/routers/media.py`、`backend/app/routers/queue.py` | A（`trigger_source` 字段） |
| E 集成验证 | 5.1 / 5.2 | `backend/tests/**` | A–D 全部完成 |

执行顺序：A → C → B → D → E。A 与 C 都改 `scan.py`，不并行写。

## 全局约束

- 无数据库迁移；`task_run.scan_detail` 是 text 列存 JSON，新键只加不改。
- 不改 `task_run.status` 枚举；`skipped` 语义收窄为「本轮无需进一步动作」。
- 新配置项（无资源告警轮次、`task_run` 回收阈值）走 `system_config`，代码内提供默认值（3 / 30）。
- 每个任务先写失败测试并确认 RED 原因就是待实现行为，再实现到 GREEN。
- 文案统一简体中文，不得出现「无遗漏集」用于表达「搜不到资源」。

---

## A. 归因字段与结论文案

### 1.1 各结束分支写入归因字段

<!-- comet-task-ref:d01d3009-f64c-4279-80bd-d21307541f96 -->

- 范围：`_scan_one` 的全部结束分支。
- 做法：新增 `_result_detail_skeleton()` 返回含 `skip_reason` / `gated_out` / `missing_aired_total` / `trigger_source` 的骨架 dict；在 `_finish` 调用前保证 `scan_detail` 非空。四个提前 return 分支（`status_paused`/`status_error`、`baseline_unavailable`、`airdate_unavailable`、`no_missing`）必须写入骨架 + 对应 `skip_reason`。
- 约束：`no_candidates` 与 `skip_enqueue` 沿用现有 `scan_detail` 装配点，只补键。
- 验收：`pytest backend/tests/test_scan_outcome_attribution.py -k skeleton`，逐类分支断言键值。

### 1.2 按 `skip_reason` 生成互不混淆的文案

<!-- comet-task-ref:a765b577-70c8-4b28-916d-7d121d0dea83 -->

- 范围：新增 `_skip_message(reason, ctx)`；`_result_message` 保持纯计数契约（design D6）。
- 做法：9 类 reason 各自文案，含缺失集数与影视名占位；「因下载中未入队」不再表述为「已有任务跳过」。
- 约束：`_result_message` 不接收 `skip_reason`，避免混入语义分支。
- 验收：`pytest backend/tests/test_scan_outcome_attribution.py -k skip_message`，表驱动断言 9 条文案互不相同。

### 1.3 收窄 `skipped` 语义的兼容性确认

<!-- comet-task-ref:3c9d97cd-4b8b-40a0-8409-1a0bb74f1079 -->

- 范围：运行日志筛选、前端状态映射、清理任务对 `status=skipped` 的依赖。
- 做法：只核对与补测，不改枚举；若发现依赖 `scan_detail is None` 的代码路径，改为判空兼容。
- 验收：`pytest backend/tests -k "scan or log"` 通过。

---

## C. 运行记录回收

### 3.1 `task_run` 超时回收

<!-- comet-task-ref:dd6ffcc3-fb93-4cef-9dde-515fe7c65394 -->

- 范围：`backend/app/tasks/recovery.py`。
- 做法：新增 `_recover_stale_scan_runs()`，并在 `recover_stale_tasks` 的 `if not candidates: return 0` **之前**调用（design D4）。阈值默认 30 分钟，`system_config` 可配；回收时 `status='error'`、`message` 含超时回收标记、`finished_at` 补齐。
- 约束：不得改变 `download_queue` 回收的既有行为与返回值语义。
- 验收：`pytest backend/tests/test_recovery.py -k scan_runs`，覆盖超时回收 / 未超时不动 / 无 `download_queue` 候选仍执行三分支。

### 3.2 `_scan_one` try/finally 兜底

<!-- comet-task-ref:caeef910-38c5-4b2b-af11-46126e34dcc6 -->

- 范围：`_scan_one` 外层。
- 做法：异常逃逸时把当前 `task_run` 收敛为 `error` 并保留异常摘要；不吞异常（继续向上抛，由调用方记录）。
- 验收：`pytest backend/tests/test_scan_outcome_attribution.py -k finally`，断言异常后无 `running` 残留。

---

## B. 巡检告警

### 2.1 巡检 `error` 立即告警

<!-- comet-task-ref:dcd28dff-274c-4067-9873-bd28a3a43c08 -->

- 范围：`_finish_scan_run` 的 error 分支 → `NotifierChain` 发 `flow_error`。
- 做法：正文含影视名、失败阶段（`failed_phase`）、原因；发送失败只记日志，不影响巡检结果落库。
- 验收：`pytest backend/tests/test_scan_outcome_attribution.py -k error_alert`，断言 notifier 调用一次且正文含影视名。

### 2.2 连续 N 轮无资源告警

<!-- comet-task-ref:0bcfcaa8-95f0-420b-9a90-38c07cb6b8a5 -->

- 范围：`scan.py` 新增模块级 TTL 冷却表（`media_id:reason`，24h，对齐 `transfer._alert_cooldown` 写法）。
- 做法：连续轮次查该 media 最近 N 条 `task_run` 的 `scan_detail->>skip_reason` 推导（不依赖内存计数）；`N` 走 `system_config`（默认 3）；`missing_aired_total` 下降时清除冷却键。
- 约束：不得给 `notifications` 加列（design D3 已否决）。
- 验收：`pytest backend/tests/test_scan_outcome_attribution.py -k no_candidates_alert`，覆盖连续 3 轮触发一次、第 4 轮不重复、数量下降后可再次触发。

### 2.3 PushPlus 降级断言

<!-- comet-task-ref:123407d7-87d0-48a7-a387-32ed3eac2db0 -->

- 范围：`notifier.py` 既有降级路径。
- 做法：只补测试，不改降级规则；未配置 PushPlus 时站内通知仍落库。
- 验收：`pytest backend/tests -k notifier` 通过，含一条巡检告警的降级断言。

---

## D. 手动触发留痕

### 4.1 受理时同步建 manual 记录

<!-- comet-task-ref:1bd4e282-cb94-4963-ae14-8b8b276478f9 -->

- 范围：`trigger_scan_background`、`POST /api/media/{id}/scan`。
- 做法：`trigger_scan_background` 同步 `_create_scan_run(media_id, trigger_source="manual")` 并返回 run id；`scan_media(..., run_id=...)` 透传给 `_scan_one`，有 `run_id` 则复用、无则自建（定时路径传 `auto`）。`POST` 返回 `{run_id, accepted}`。
- 约束：不得产生两条记录（`_scan_one` 内自建逻辑必须被 `run_id` 短路）。
- 验收：`pytest backend/tests/test_scan_outcome_attribution.py -k manual_trigger`，断言调用后恰好 1 条 manual 记录。

### 4.2 队列 retry 记录来源

<!-- comet-task-ref:2e769dd1-76fc-40e9-b64c-abc09b930760 -->

- 范围：`POST /api/queue/{task_id}/retry`。
- 做法：retry 路径复用同一受理函数，写入 `trigger_source=manual`。
- 验收：接口测试断言 retry 后新记录来源为 manual。

### 4.3 异常时收敛 manual 记录

<!-- comet-task-ref:7cf3960d-ce24-4618-8841-f66da539735c -->

- 范围：后台任务异常路径。
- 做法：与 3.2 的 try/finally 共用收敛逻辑，保证 manual 记录不残留 `running`。
- 验收：单测注入异常，断言记录 `status=error` 且 `message` 含原因。

### 4.4 前端消费受理结果（Verify 集成审查 I1 补入）

<!-- comet-task-ref:0525e1b8-d8fe-4a01-b49e-3ed722a97d98 -->

- 范围：`backend/app/routers/media.py`、`frontend/src/api/index.ts`、`frontend/src/stores/media.ts`、`frontend/src/views/MediaDetailView.vue`、`frontend/src/views/MediaListView.vue`。
- 做法：响应恒含 `accepted` / `run_id`（两个提前 return 分支补 `accepted: false`）；前端类型与 store 透传；两个视图按 `accepted` 分支 `success` / `warning`，失败时不刷新列表。
- 依据：spec「用户手动触发巡检」场景要求界面给出受理成功或失败的明确反馈；原实现无条件提示成功。
- 验收：`npm test`（视图用例断言 `accepted=false` 无 success）+ `npm run build`（vue-tsc）+ 后端全量 pytest。

---

## E. 集成验证

### 5.1 端到端归因场景

<!-- comet-task-ref:bd079cc9-8a0a-49ed-9b18-ffd07dbe3288 -->

- 做法：fake Emby / cloudSaver 构造「有已播缺集 + 搜索无候选」，跑 3 轮。
- 验收：断言 `skip_reason=no_candidates`、通知表只有 1 条 `flow_error`、运行日志文案可读。

### 5.2 全量回归

<!-- comet-task-ref:53cadb8b-8294-4e71-beb2-9dd479d729c5 -->

- 验收：`pytest backend/tests` 全绿。

---

## 回退

纯代码回退；新增 `scan_detail` 键对旧代码不可见、不影响解析。配置项缺失时按默认值运行。
