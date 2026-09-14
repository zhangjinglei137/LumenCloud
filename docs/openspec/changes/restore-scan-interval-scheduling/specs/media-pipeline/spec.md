## Purpose

（既有 capability，无新 Purpose。）

## MODIFIED Requirements

### Requirement: 全局统一定时巡检

系统 SHALL 按各影视的 per-media 巡检间隔（`Media.scan_interval_minutes`，缺省回退全局默认 60 分钟）执行缺集巡检：调度 job 每分钟触发一次巡检 tick，每轮仅巡检「距上次成功巡检已超过其配置间隔」或「从未巡检过」的 tracking/downloading 影视；未到期的影视 SHALL 被跳过（不执行 Emby 基线、搜索与 task_run 落库）。

#### Scenario: 到点触发全局巡检
> **已废弃（restore-scan-interval-scheduling）**：本场景由「到点触发该影视巡检」取代——调度恢复 per-media 间隔，不再按全局统一间隔对全部影视逐部巡检。
- **WHEN** 距上次全局巡检达到配置间隔
- **THEN** 系统对全部 tracking/downloading 影视逐个执行缺集搜索并产出缺失集任务

#### Scenario: 配置全局间隔
> **已废弃（restore-scan-interval-scheduling）**：全局巡检间隔配置键已退休（`_RETIRED_EXACT`，不复活）；间隔改由各影视详情页 per-media 配置（见「per-media 间隔配置生效」）。
- **WHEN** 管理员修改全局巡检间隔配置
- **THEN** 后续巡检按新间隔执行，且无需逐个影视设置

#### Scenario: 到点触发该影视巡检
- **WHEN** 某影视距上次巡检已超过其配置间隔（或从未巡检过）
- **THEN** 系统对该影视执行缺集搜索并产出缺失集任务，并更新其最近巡检时间

#### Scenario: 未到期影视被跳过
- **WHEN** 某影视距上次巡检未超过其配置间隔
- **THEN** 该影视本轮不执行巡检，不产生缺失集任务与巡检记录

#### Scenario: per-media 间隔配置生效
- **WHEN** 管理员在影视详情页修改某影视的巡检间隔
- **THEN** 该影视后续按新间隔执行巡检，其余影视不受影响

#### Scenario: 手动全量巡检绕过间隔
- **WHEN** 用户手动触发全量巡检（CLI/手动入口）
- **THEN** 全部 tracking/downloading 影视立即执行巡检，不受各影视间隔限制

#### Scenario: 故障影视不被间隔冷却
- **WHEN** 某影视巡检时 Emby 故障或未收录（巡检未真正完成主流程）
- **THEN** 该影视不更新其最近巡检时间，下轮 tick 继续尝试，而非等待一个完整间隔

#### Scenario: 新建影视立即首巡
- **WHEN** 某影视从未巡检过（`last_scan_at` 为空）
- **THEN** 该影视进入下轮 tick 立即执行首巡
