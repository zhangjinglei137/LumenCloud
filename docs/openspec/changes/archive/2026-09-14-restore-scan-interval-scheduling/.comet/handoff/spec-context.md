# Comet Spec Context

- Change: restore-scan-interval-scheduling
- Phase: design
- Mode: beta
- Context hash: 69b23fc2363f9f2d227e6499246bb840458658adb82496caff6ad522bb4c10e1

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This beta context pack verbatim-projects spec files and references supporting artifacts by hash, not an agent-authored summary.

## Source References

- Source: docs/openspec/changes/restore-scan-interval-scheduling/proposal.md
- SHA256: 2f02d655aa25bf07c174a16a067b9a0b20351adb145abd04c08010a9bd33d2fa
- Source: docs/openspec/changes/restore-scan-interval-scheduling/design.md
- SHA256: 0a84c0f78141078267b625a953380c83f53a510917ad882726b16f0bddc42d7d
- Source: docs/openspec/changes/restore-scan-interval-scheduling/tasks.md
- SHA256: 6c1c8e16e6ca69d603006e32df8632dc342ccbb381bb3ed8980b4edb5e67be77
- Source: docs/openspec/changes/restore-scan-interval-scheduling/specs/media-pipeline/spec.md
- SHA256: 65dffb7e007fe0084664ac48067bd2e6e68456eace3e40cdc9a90271faa58b75

## Acceptance Projection

## docs/openspec/changes/restore-scan-interval-scheduling/specs/media-pipeline/spec.md

- Source: docs/openspec/changes/restore-scan-interval-scheduling/specs/media-pipeline/spec.md
- Lines: 1-33
- SHA256: 65dffb7e007fe0084664ac48067bd2e6e68456eace3e40cdc9a90271faa58b75

```md
## Purpose

（既有 capability，无新 Purpose。）

## MODIFIED Requirements

### Requirement: 全局统一定时巡检

系统 SHALL 按各影视的 per-media 巡检间隔（`Media.scan_interval_minutes`，缺省回退全局默认 60 分钟）执行缺集巡检：调度 job 每分钟触发一次巡检 tick，每轮仅巡检「距上次成功巡检已超过其配置间隔」或「从未巡检过」的 tracking/downloading 影视；未到期的影视 SHALL 被跳过（不执行 Emby 基线、搜索与 task_run 落库）。

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

```

Full source files remain canonical. If a required heading or scenario is missing here, regenerate the handoff or read the source spec directly. Supporting files (proposal, design, tasks) are referenced by hash only.