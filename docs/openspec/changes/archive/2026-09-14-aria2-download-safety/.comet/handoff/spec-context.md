# Comet Spec Context

- Change: aria2-download-safety
- Phase: design
- Mode: beta
- Context hash: 059861245c86d803506f61a4aff065eb676226dee1fb4e0dcebcfa6d271b4838

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This beta context pack verbatim-projects spec files and references supporting artifacts by hash, not an agent-authored summary.

## Source References

- Source: docs/openspec/changes/aria2-download-safety/proposal.md
- SHA256: cbeb1bc31ba43179447b009d9a3fd2832392605f8b42ddec41959c5b9651853c
- Source: docs/openspec/changes/aria2-download-safety/design.md
- SHA256: a988a8cf9461141dd55c43ba7e6def37a71088b629ffc40558638bc9dd1ad48a
- Source: docs/openspec/changes/aria2-download-safety/tasks.md
- SHA256: 8110a63179b9af3dcd570d54aa84d43b1e5409a19b974ab1cd591ff1fb378131
- Source: docs/openspec/changes/aria2-download-safety/specs/notifications/spec.md
- SHA256: 4e9668f08a2cfd06daaa90215ced461473aeeba46735b598ddbfe4fcf5b3eaf5
- Source: docs/openspec/changes/aria2-download-safety/specs/pipeline-admission/spec.md
- SHA256: 69244db109a5117844be5dfef8be165a4706968d530946e335e755fa908833d0
- Source: docs/openspec/changes/aria2-download-safety/specs/quark-cleanup-safety/spec.md
- SHA256: b6bec108575f4c011c0b5efb7904c62adf2f008d5751c46e1f2b0d25381294b8

## Acceptance Projection

## docs/openspec/changes/aria2-download-safety/specs/notifications/spec.md

- Source: docs/openspec/changes/aria2-download-safety/specs/notifications/spec.md
- Lines: 1-13
- SHA256: 4e9668f08a2cfd06daaa90215ced461473aeeba46735b598ddbfe4fcf5b3eaf5

```md
## ADDED Requirements

### Requirement: 容量告警与真实容量一致

系统 SHALL 仅在实际容量数据满足告警条件（如使用率连续快照 ≥ 配置阈值）时产生「空间不足/容量使用率过高」类系统通知；容量充足（使用率低于阈值）时 MUST NOT 产生任何「空间不足」类系统通知或站内提醒。告警数值（总量/已用/使用率）SHALL 来自实时容量统计（alist），不得使用估算或与实时统计冲突的缓存数值。

#### Scenario: 容量充足不告警
- **WHEN** 网盘使用率低于告警阈值（如 40% < 90%）
- **THEN** 系统不产生任何「空间不足」类系统通知或站内提醒

#### Scenario: 容量不足才告警
- **WHEN** 网盘使用率连续快照 ≥ 配置阈值
- **THEN** 系统按既有去抖与冷却规则产生一条容量告警通知，数值与实时统计一致

```

## docs/openspec/changes/aria2-download-safety/specs/pipeline-admission/spec.md

- Source: docs/openspec/changes/aria2-download-safety/specs/pipeline-admission/spec.md
- Lines: 1-13
- SHA256: 69244db109a5117844be5dfef8be165a4706968d530946e335e755fa908833d0

```md
## MODIFIED Requirements

### Requirement: 转存来源校验与逃生

系统在准入转存前 SHALL 校验 aria2 活动/等待任务的来源；aria2 中的陌生 gid（不属于本系统 download_queue 已签发集合）不得导致转存拦截、告警或自动清理删除——用户自行添加的下载任务 SHALL 与系统任务共存。系统 SHALL 仅对自有已签发 gid 执行下载状态跟踪与完成推进。

#### Scenario: 陌生任务共存不拦截
- **WHEN** aria2 中存在 gid 不在本系统已签发集合的活动/等待任务（如用户自行下载）
- **THEN** 转存流程不受影响正常继续，不产生非本系统任务告警，不删除该任务

#### Scenario: 本系统任务正常跟踪
- **WHEN** aria2 中的任务 gid 属于本系统已签发集合
- **THEN** 系统按既有状态轮询与完成推进逻辑处理

```

## docs/openspec/changes/aria2-download-safety/specs/quark-cleanup-safety/spec.md

- Source: docs/openspec/changes/aria2-download-safety/specs/quark-cleanup-safety/spec.md
- Lines: 1-29
- SHA256: b6bec108575f4c011c0b5efb7904c62adf2f008d5751c46e1f2b0d25381294b8

```md
## Purpose

保护夸克网盘中正在被 aria2 下载或排队等待下载的源文件，避免孤儿清理等删除性操作误删下载源导致下载失败。

## ADDED Requirements

### Requirement: 清理前保护 aria2 下载中/等待下载源文件

系统执行夸克文件删除性操作（含孤儿文件兜底清理）前 SHALL 查询 aria2 活动任务与等待任务集合，解析每个任务下载的源文件（经下载 URI/文件路径归一为 /quark 下文件名），将命中集合的文件从待删除列表剔除；正在下载或排队等待下载的文件 MUST NOT 被删除。

#### Scenario: 下载中文件被保护
- **WHEN** 候选孤儿列表包含某文件，且该文件正被 aria2 活动任务下载（active）
- **THEN** 该文件不进入删除列表，其余未下载文件正常删除

#### Scenario: 等待下载文件被保护
- **WHEN** 候选孤儿列表包含某文件，且该文件对应 aria2 等待任务（waiting）
- **THEN** 该文件不进入删除列表

#### Scenario: 无下载任务时正常清理
- **WHEN** aria2 无活动/等待任务，或候选孤儿文件不在任何下载任务中
- **THEN** 系统按既有孤儿判定规则正常删除

### Requirement: aria2 状态查询失败时清理 fail-safe

清理执行前查询 aria2 活动/等待任务失败（服务不可用/网络故障）时，系统 SHALL 本次不删除任何孤儿文件并记录告警，不得在无法确认下载状态时执行删除。

#### Scenario: aria2 不可用时跳过清理
- **WHEN** 清理前查询 aria2 任务状态失败（Aria2Unavailable）
- **THEN** 本轮清理不删除任何文件，记录告警，下轮清理再试

```

Full source files remain canonical. If a required heading or scenario is missing here, regenerate the handoff or read the source spec directly. Supporting files (proposal, design, tasks) are referenced by hash only.