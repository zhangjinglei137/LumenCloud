---
comet_change: scan-outcome-attribution
role: technical-design
canonical_spec: openspec
archived-with: 2026-10-20-scan-outcome-attribution
status: final
---

# Design

## Context

见 `proposal.md` 的 Why。设计相关现状（行号基于本轮代码核对）：

- 巡检主流程在 `backend/app/tasks/scan.py` 的 `_scan_one`（`scan.py:1789`）中以 5 个阶段（check / search / match / enqueue / finish）推进；入口 `_create_scan_run`（`scan.py:1725`）先落 `status='running'` 的 `task_run`，结束由 `_finish_scan_run`（`scan.py:1748`）原地 UPDATE 为终态。`scan_detail` 为 text 列（JSON 字符串）。
- `_result_message`（`scan.py:211`）是**纯计数函数**，按 `enqueued/unmatched/size_filtered/...` 生成文案；`skip_reason` 语义无法在其中表达。
- `_scan_one` 的 skipped 分支共 9 处语义，其中 `paused/error`（1839）、`baseline_unavailable`（1868）、`airdate_unavailable`（1884）、`no_missing`（1937）四个分支在 `scan_detail` 装配（1929）之前 return，`scan_detail` 为 `None`。
- 通知统一走 `backend/app/services/notifier.py`：`NotifierChain([InAppNotifier, PushPlusNotifier])`（`notifier.py:197`）。**没有通用节流 API**——只有 PushPlus 降级专用的 `_pushplus_alert_cooldown`（全局 key，无 media 维度）。`InAppNotifier.notify` 只落 `recipient/event_type/title/body`，`NotifyEvent.extra` 中的 `media_id` **不入库**。
- `recover_stale_tasks`（`recovery.py:144`）只处理 `download_queue`，且在第 200 行 `if not candidates: return 0` 早退。
- 手动触发 `trigger_scan_background`（`scan.py:1562`）以 `asyncio.create_task` fire-and-forget 执行，不返回 run id；`_scan_one` 内部自建 run。

## Goals / Non-Goals

**Goals:**

- 每轮巡检都留下可区分、可查询、可用于展示的结论与归因计数。
- 巡检异常与长期无资源能被用户感知（告警），且不制造噪音。
- 用户手动触发的动作可追溯，不再出现「用户确信操作过、系统查无记录」。

**Non-Goals:**

- 不改变缺失集的判定来源与算法（仍以 Emby Missing + 已播门控为准）。
- 不改变 `task_run.status` 的枚举集合，不改变前端展示层。
- 不统一列表页 `episode_stats.missing` 与巡检判定的口径（留给 `missing-baseline-integrity`）。

## Decisions

### D1：归因信息放 `task_run.scan_detail`，`skip_reason` 枚举按实际分支取 9 个值

在 `scan_detail` 中新增：

- `skip_reason`：`status_paused` / `status_error` / `baseline_unavailable` / `airdate_unavailable` / `no_missing` / `not_aired` / `gated_out` / `no_candidates` / `skip_enqueue`
- `gated_out`：被已播门控剔除的集数与集号
- `trigger_source`：`auto` / `manual`
- `missing_aired_total`：本轮已播缺失集数

`baseline_suspect` 只预留键位，由 `missing-baseline-integrity` 填充。

替代方案：新增 `task_run` 列或独立归因表。否决原因：`scan_detail` 已是该轮巡检的结构化载体，加列需迁移且对旧记录无收益。

### D1a：所有短路分支必须落 `scan_detail` 骨架

`_finish` 之前统一保证 `scan_detail` 非空（`_result_detail_skeleton()` + `skip_reason`）。四个提前 return 的分支（1839 / 1868 / 1884 / 1937）当前写 `None`，若不改，归因无处可放、前端与告警都读不到。

### D2：保留 `status=skipped`，用 `skip_reason` 区分语义

`skipped` 继续表示「本轮无需进一步动作」，但必须同时带 `skip_reason`。不新增 `status` 枚举值：`task_run.status` 已被运行日志筛选、前端映射与清理任务依赖，扩展枚举的兼容成本高于收益。

### D3：告警冷却自建，连续 N 轮从 `task_run` 历史推导

新增两个触发点：

1. 巡检以 `error` 结束 → 立即告警（含失败阶段与原因）。
2. 连续 N 轮（默认 3，`system_config` 可配）`skip_reason=no_candidates` 且 `missing_aired_total>0` → 告警一次。

**冷却**：`notifier` 没有可复用的通用节流设施（见 Context），因此在 `scan.py` 维护模块级 TTL 表，key = `media_id:reason`，窗口 24h，实现模式对齐 `transfer._alert_cooldown`。**连续轮次**不靠内存计数，而是查该 media 最近 N 条 `task_run` 的 `scan_detail->>skip_reason` 推导，保证进程重启后仍准确。缺失集数量下降时清除冷却键，使再次恶化能重新告警。

已知代价：冷却表在进程内，重启后最坏多推一条同类告警。接受该代价以换取零迁移。

替代方案：给 `notifications` 加 `media_id` 列做持久去重。否决原因：为一个告警冷却引入迁移与回填，成本高于收益；且 `extra.media_id` 不入库是既有设计，本次不扩大范围。

### D4：`task_run` 卡死回收独立于 `download_queue` 早退

在 `recover_stale_tasks` 开头、`if not candidates: return 0` 之前调用独立的 `_recover_stale_scan_runs()`：`task_run` 处于 `running` 且超过阈值（默认 30 分钟，`system_config` 可配）→ 结束为 `error` 并标注超时回收。

放在早退之后会导致「无下载超时任务时永不回收」，这是 draft 设计里的实际缺陷。同时保留 `_scan_one` 外层 try/finally 兜底，覆盖同进程异常逃逸。

### D5：手动触发同步建 run，后台复用该记录

`trigger_scan_background` 改为受理时同步 `_create_scan_run(media_id, trigger_source="manual")` 并返回 run id；`scan_media(media_id, *, manual, run_id=None)` → `_scan_one`：`run_id` 非空则复用，为空则自建（定时路径）。

不能按 draft 直接在 `trigger_scan_background` 再建一条——`_scan_one:1819` 已经会建，会变成两条记录。

`_create_scan_run` 增加 `trigger_source` 参数并写入 `scan_detail` 骨架；定时路径传 `auto`。

### D6：文案由 `_skip_message` 承担，`_result_message` 保持纯计数契约

新增 `_skip_message(reason, ctx)`，在 `_finish` 调用处按 `skip_reason` 选文案。`_result_message` 只保留「有搜索/入队计数」的路径，不接收 reason。

替代方案：在 `_result_message` 里加 `skip_reason` 分支。否决原因：该函数当前是纯计数函数，混入 9 类语义分支后既难测也难读。

## Test Strategy

- 单测 `test_scan_outcome_attribution.py`：
  - `_skip_message` 表驱动覆盖 9 个 reason，断言文案互不相同且不含「无遗漏集」误报
  - `_create_scan_run` 写入 `trigger_source`；`_scan_one(run_id=...)` 复用不新建（断言只产生 1 条 `task_run`）
  - 四个短路分支的 `scan_detail` 非空且带 `skip_reason`
  - `_recover_stale_scan_runs`：超时回收 / 未超时不动 / 无 `download_queue` 候选时仍执行
  - 告警：构造连续 3 条 `no_candidates` 记录 → 只发 1 条 `flow_error`；第 4 轮仍不重复；缺失数下降后清除冷却
- 回归：`backend/tests/test_recovery.py`、`test_recovery_aria2_unavailable.py`、`test_fix_p0_recovery_cleanup_transfer.py`
- 集成：fake Emby/cloudSaver 让某 media 稳定产出 `no_candidates`，跑 3 轮断言通知表只有 1 条

## Risks / Trade-offs

- [告警噪音] → 3 轮阈值 + 24h 冷却键；均可配置。
- [重启丢冷却] → 最坏多推一条同类告警；连续轮次判定仍准确（查 DB）。
- [旧记录缺字段] → 读取侧将缺失的 `skip_reason` / `trigger_source` 视为「未知」，不回填。
- [回收阈值误杀] → 30 分钟远大于当前最长巡检（约 30 秒）。
- [分支多易漏改] → 以「`_finish` 前 `scan_detail` 非空」作为收口断言，并用单测覆盖四个短路分支。
- [口径仍不统一] → 列表页 `missing` 与巡检判定差异本次不处理，在 `missing-baseline-integrity` 中解决。

## Migration Plan

- 无数据库结构变更，无需迁移。
- 新增配置项（无资源告警轮次阈值、`task_run` 回收阈值）在代码内提供默认值，缺失时按默认值运行。
- 回滚：回退代码即可；已写入的 `scan_detail` 新键对旧代码不可见但不影响解析。

## Spec Patch

`specs/notifications/spec.md`：「并受既有去重与冷却约束」→「并由系统自行维护同一影视同一原因的冷却窗口」。原因：`notifier` 不存在可复用的通用冷却设施，原表述引用了不存在的既有能力。

## Open Questions

- 无。告警轮次阈值与回收阈值均可在实现时按实际噪音调整，不改变本设计。
