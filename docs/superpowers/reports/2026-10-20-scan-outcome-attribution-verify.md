# 验证报告：scan-outcome-attribution

- 日期：2026-10-20
- 验证模式：`full`（`comet state scale` 建议 full：13 任务 / 2 delta spec / 10 变更文件）
- 审查模式：`standard`（Build 分段审查 P1、P2 + Verify 最终集成审查）
- 执行方式：`autonomous`，TDD
- 验证轮次：第 1 轮失败（1 项 IMPORTANT）→ 回 Build 修复 → 第 2 轮通过

## 1. 总览

| 维度 | 结果 |
| --- | --- |
| Completeness | 14/14 任务完成；4 条 ADDED requirement（0 REMOVED / 0 RENAMED） |
| Correctness | 10/10 场景有实现与测试覆盖 |
| Coherence | 设计决策 D1–D6 全部落实；实施层偏离记录于 `rulings.md` R1–R8 |

## 2. 检查项

| # | 检查 | 结果 | 证据 |
| --- | --- | --- | --- |
| 1 | tasks.md 全部完成 | PASS | `openspec instructions apply` → `progress: 14/14, remaining 0` |
| 2 | 实现符合 design.md 决策 | PASS | D1/D1a/D2/D3/D4/D5/D6 逐条核对，见 §4 |
| 3 | 实现符合 Design Doc | PASS | 正式设计即 `docs/openspec/changes/scan-outcome-attribution/design.md` |
| 4 | 能力规格场景全部通过 | PASS | 10 个场景均有实现与测试，见 §3 |
| 5 | proposal 目标已满足 | PASS | 三条目标（可归因、可感知、可追溯）均有实现与测试 |
| 6 | delta spec 与 design doc 无矛盾 | PASS | Build 期间无 spec 增量修改；唯一 Spec Patch（冷却措辞）已在 design.md §Spec Patch 记录 |
| 7 | 设计文档可定位 | PASS | `design_doc` 已登记，design guard 16/16 PASS |

## 3. 规格场景覆盖

### media-pipeline

| 场景 | 实现 | 测试 |
| --- | --- | --- |
| 有已播缺集但搜不到资源 | `_skip_message("no_candidates")` + 置位条件（排除 unmatched / size_filtered / non_video / unrelated_filtered / existing_skipped） | `test_skip_reason_no_candidates_with_aired_missing`、`test_integration_no_candidates_alert_and_logs_readable` |
| 缺失集全部被已播门控剔除 | `gated_out` 计算（`missing_before` − `kept_codes`，排除取数失败季） | `test_skip_reason_gated_out_records_dropped_episodes`、`test_gated_out_message_bounded_but_detail_keeps_all` |
| 因该影视正在下载而未入队 | `downloading_skipped_keys` + `skip_reason=skip_enqueue`，不计入 `existing_skipped` | `test_skip_reason_skip_enqueue_downloading` |
| 无缺失集 | `skip_reason=no_missing` | `test_skip_reason_no_missing` |
| 用户手动触发巡检 | 受理即建 run（`trigger_source=manual`），响应含 `accepted`/`run_id`；前端按 `accepted` 分支提示 | `test_manual_trigger_creates_single_manual_run`、`test_media_scan_route_returns_run_id_and_accepted`、`MediaDetailView.test.ts`（accepted 两分支） |
| 手动触发的后台执行异常 | `_scan_one` 外壳 try/except 收敛 error 后上抛 | `test_manual_background_exception_converges_to_error`、`test_scan_one_finally_converges_run_to_error` |
| 巡检进程中断 | `_recover_stale_scan_runs()` 置于 `recover_stale_tasks` 早退之前 | `test_recovery.py -k scan_runs`（超时 / 未超时 / 无下载候选三分支） |

### notifications

| 场景 | 实现 | 测试 |
| --- | --- | --- |
| 巡检执行失败 | `_alert_scan_error` + 冷却（`media_id:reason`，24h） | `test_error_alert_contains_title_phase_and_reason`、`test_error_alert_cooldown_suppresses_same_phase` |
| 长期存在已播缺集但无资源 | `_maybe_alert_no_candidates`（连续 N=3 轮，`system_config` 可配；缺失数下降清冷却） | `test_no_candidates_alert_once_after_three_rounds`、`test_no_candidates_alert_rounds_from_system_config`、`test_no_candidates_alert_rearmed_after_missing_declines` |
| 告警降级 | 复用 `NotifierChain`，PushPlus 失败落站内 | `test_scan_alert_written_inapp_when_pushplus_unconfigured`、`test_scan_alert_pushplus_failure_falls_back_to_inapp` |

## 4. 设计一致性

| 决策 | 落实 |
| --- | --- |
| D1 归因放 `task_run.scan_detail` | 无迁移；新增 `skip_reason` / `gated_out` / `missing_aired_total` / `trigger_source` |
| D1a 短路分支必须落骨架 | 4 个提前 return 分支均写骨架；`test_scan_run_phases.py` 旧契约同步更新（R1） |
| D2 保留 `status=skipped` | `task_run.status` 枚举未变 |
| D3 告警复用 `flow_error` + 自建冷却 | 模块级 TTL 表；连续轮次查 DB 推导；未给 `notifications` 加列 |
| D4 回收先于早退 | `_recover_stale_scan_runs()` 在 `if not candidates: return 0` 之前，失败不影响下载回收 |
| D5 手动触发同步建 run | 受理即建 run 并返回 id；`_scan_one` 复用，不双建 |
| D6 `_skip_message` 承担语义文案 | `_result_message` 保持纯计数契约 |

## 5. 验证证据

| 项 | 结果 |
| --- | --- |
| 后端全量回归 | `pytest tests -q` → **908 passed, 2 warnings, 233.34s** |
| 前端单测 | `npm test`（vitest）→ exit 0 |
| 前端构建 + 类型 | `npm run build`（vue-tsc + vite build）→ exit 0 |
| Verify 证据 | `comet check run ... verify --local` 三条命令均 exit=0；后端日志 `checks/c3db7dd5-...log`，前端日志 `checks/6c55bad8-...log`、`checks/2b17acbf-...log` |
| Build 分段审查 | P1 `APPROVE`（IMPORTANT 1 已修）、P2 `APPROVE_WITH_MINOR`（IMPORTANT 1 已修） |
| 最终集成审查 | `APPROVE_WITH_MINOR`，IMPORTANT I1 已修复并复验（见 §6） |

## 6. 最终集成审查

结论：`APPROVE_WITH_MINOR`。跨工作包接缝核查通过——归因产生点、告警判据、`routers/logs.py` 读回三段口径一致；手动触发「受理 → 复用 → 异常收敛 → retry 留痕」无双建路径；回收与 `_finish` 的竞态无有害结果（回收后终态覆盖、或 WHERE 不命中）。

### 已修复

**I1（IMPORTANT）前端未消费 `accepted`，受理失败仍提示成功**
- 修复：`routers/media.py` 两个提前 return 分支补 `accepted: false` / `run_id: null`；`api/index.ts` 类型补全；`stores/media.ts` 返回 `{accepted, runId}`；`MediaDetailView.vue`、`MediaListView.vue` 按 `accepted` 分支 `success` / `warning`，失败不刷新列表。
- 复验：前端用例（accepted 两分支）、`vue-tsc --noEmit`、后端全量 908 passed。

### 接受不修（MINOR）

| 编号 | 问题 | 接受理由 |
| --- | --- | --- |
| M1 | 同一 media 定时巡检运行中手动触发，短暂出现两条 running | 各自终态正确，仅运行日志短暂可见；修复需引入 per-media 锁检测，收益低于复杂度 |
| M2 | 受理后 media 被删 → running 记录残留至回收 | 最长 30 分钟由 `_recover_stale_scan_runs` 收敛，满足 spec「不得长期停留进行中」下限 |
| M3 | 超时回收不触发告警 | spec 告警范围为「单轮巡检执行失败」；进程中断属兜底场景，留待后续 change |
| M4 | `skip_enqueue` 轮丢弃同轮计数文案 | 方向安全（避免误报「未找到资源」），信息损失轻微 |
| M5 | 多 worker 部署下模块级冷却表失效 | 当前单进程部署；design D3 已声明进程内代价 |

## 7. 安全与回归面

- 无硬编码密钥、无新增 `eval` / `shell=True` / 不安全反序列化（`git diff` 全量 grep 已核对）。
- `queue_retry` 新 task_type 已在后端 `routers/logs.py:_TASK_TYPES` 与前端 `utils/format.ts:TASK_TYPE_MAP` 双端登记。
- `existing_skipped` 语义收窄已 grep 确认无其他调用方依赖旧语义（前端不读 `scan_detail`）。
- `scan_detail` 新键对旧代码不可见；`routers/logs.py:_json_or_none` 对 `None` 与新键均容错。
- 响应契约变更（`POST /api/media/{id}/scan` 增 `accepted` / `run_id`）已同步更新 `tests/test_scan_async.py` 两处旧断言。

## 8. 已知限制（不影响本次验收）

- 未做真实外部服务（Emby / TMDB / cloudSaver / PushPlus）联网验证；集成测试为 in-memory SQLite + mock 外部依赖。
- 列表页 `episode_stats.missing` 与巡检判定的口径不统一，design 明确留给 change `missing-baseline-integrity`。
- 代码尚未 `git commit`（用户全局规则：仅在其要求时提交）。

## 9. 结论

无 CRITICAL；1 项 IMPORTANT（I1）已修复并复验；5 项 MINOR 已记录接受理由。7 项检查全部 PASS，14/14 任务完成，规格 10/10 场景覆盖。**通过验证，可进入归档。**
