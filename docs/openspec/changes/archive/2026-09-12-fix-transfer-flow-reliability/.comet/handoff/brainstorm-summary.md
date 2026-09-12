# Brainstorm Summary

- Change: fix-transfer-flow-reliability
- Date: 2026-09-12
- Status: 用户已确认设计方案（2026-09-12）

## 确认的技术方案

基于 council 三方评审缺陷清单（P0+P1+P2 全部纳入，单一 change）。open 阶段 design.md 已确立 8 项核心决策：

- **D1 准入事务重构（P0-1）**：`_try_admit_one` 拆为「短事务 A 读 pending 快照 → 锁外容量 check → 短事务 B（锁行+重读 reserved+CAS 抢占/置 quota_wait）」；容量快照落库移出外层事务。
- **D2 GID 白名单逃生通道（P1-2）**：白名单放宽为「DB 中 aria2_gid 非空全部行」+ 陌生 gid 连续跳过 ≥3 轮哨兵触发 `aria2.remove`。
- **D3 webhook 文件级推进（P1-1/P1-3）**：`_advance_scrape_to_library` 按文件名/集号定位单行推进，无法定位仅触发轮询加速；推进补齐 `node_attempt=0/node_finished_at/node_error`。
- **D4 取件-创建原子化（P1-6）**：单语句条件 INSERT（`INSERT...SELECT...WHERE NOT EXISTS`）。
- **D5 转存提交冲突清理夸克残留（P1-5）**：`final_quark_path` 传入 `_commit_downloading`，冲突分支追加 best-effort `alist.remove`。
- **D6 容量记账漏计窗口（P1-7）**：`CapacityProvider.invalidate_usage_cache()`，准入提交成功后失效 used 缓存。
- **D7 cancel/skip 同步 media 状态（P1-4）**：置 DQ 终态后同一事务调 `_sync_media_status`。
- **D8 P2 有界性**：刮削退避+计数解耦、aria2 allow-overwrite options、集级确认 fail-open 延迟复核、通知节流、quota_wait 唤醒有界、凭据完整性校验、save_task_id 条件化清理、全量模式集号归一化防重、token query 通道处置。

## 关键取舍与风险

- 拆事务后 CAS 窗口变化 → 事务 B 内锁行+重读 reserved 兜底，容量不超发。
- 哨兵误删正常任务 → 仅对不在库 gid 操作 + 连续 N 轮 + 删除前告警。
- 按文件名匹配失败 → 降级为仅触发轮询，不丢任务。
- 条件 INSERT 跨方言 → SQLite/PG 均支持，无方言分支。
- 退避/节流进程内状态重启即失 → 影响有限，长期选项落库。

## 测试策略

- 每项缺陷修复配套 backend/tests/ 回归测试（事务边界、webhook 推进、GID 逃生、容量记账、取件原子化、media 状态回落等），全量 pytest 通过后进入 verify。

## Spec Patch

候选（待用户确认后回写）：
- `pipeline-transfer` delta 补「集级确认 fail-open」验收场景：Emby 遗漏集列表为空时不得立即视为已收录，延迟一轮复核。
