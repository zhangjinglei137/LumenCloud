# Comet Spec Context

- Change: scan-outcome-attribution
- Phase: design
- Mode: beta
- Context hash: c34cecd63570efa101c0eaede8dee136175df4e0a86ad05c964a92693cd1adcb

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This beta context pack verbatim-projects spec files and references supporting artifacts by hash, not an agent-authored summary.

## Source References

- Source: docs/openspec/changes/scan-outcome-attribution/proposal.md
- SHA256: 682a1aae6aee298bdab1ec51dbaa3274ddca5312863a52504dcfc1c76ed88f00
- Source: docs/openspec/changes/scan-outcome-attribution/design.md
- SHA256: acad09eb171cb55b0c350800836acfdb79490835bffddc1c160a6dbe86790d15
- Source: docs/openspec/changes/scan-outcome-attribution/tasks.md
- SHA256: 275c4d0bef374548d84db2a075fbdb70b75a9fc9fd12d94dc36bd8ced6b32a45
- Source: docs/openspec/changes/scan-outcome-attribution/.openspec.yaml
- SHA256: ff4326f8b42dcf8851877f4f3989c7fecb8f97d8154686d7d74b7281b70e6b9b
- Source: docs/openspec/changes/scan-outcome-attribution/specs/media-pipeline/spec.md
- SHA256: a84f86bcc22ccf5464f61f04fc3157cfe253269652ff8c203613a23e0cd796ad
- Source: docs/openspec/changes/scan-outcome-attribution/specs/notifications/spec.md
- SHA256: 2e41a8333edd70392e33770d72d6aee997ae48bafd467700481f57d7a8955c4e

## Acceptance Projection

## docs/openspec/changes/scan-outcome-attribution/specs/media-pipeline/spec.md

- Source: docs/openspec/changes/scan-outcome-attribution/specs/media-pipeline/spec.md
- Lines: 1-49
- SHA256: a84f86bcc22ccf5464f61f04fc3157cfe253269652ff8c203613a23e0cd796ad

```md
# Spec Delta

## ADDED Requirements

### Requirement: 巡检结论必须可归因

系统 SHALL 为每一轮巡检记录可区分的结论与归因计数，不得把语义不同的结果折叠为同一种状态。归因 SHALL 至少覆盖：无缺失集、存在缺失但均未播出、存在缺失但被已播门控剔除、存在已播缺失集但未搜到资源、存在缺失但因该影视正在下载而未入队。归因结果 SHALL 持久化到该轮巡检的运行记录中，并可用于后续查询与展示。

#### Scenario: 有已播缺集但搜不到资源
- **WHEN** 某影视存在已播出的缺失集，且本轮搜索未匹配到任何候选资源
- **THEN** 运行记录 SHALL 标注为「存在已播缺集但未获取资源」，并记录缺失集数量
- **AND** 该结论 SHALL NOT 与「无缺失集」或「尚未播出」混为同一表述

#### Scenario: 缺失集全部被已播门控剔除
- **WHEN** 本轮检出的缺失集播出日期均晚于当前日期
- **THEN** 运行记录 SHALL 标注为「未播出」并记录被剔除的集数
- **AND** 系统 SHALL NOT 为该影视发起搜索或入队

#### Scenario: 因该影视正在下载而未入队
- **WHEN** 某影视状态为 downloading 且本轮检出已播缺失集
- **THEN** 运行记录 SHALL 明确标注「因正在下载未入队」及未入队集数
- **AND** 该表述 SHALL NOT 被记为「已有任务跳过」

#### Scenario: 无缺失集
- **WHEN** 本轮巡检未检出任何缺失集
- **THEN** 运行记录 SHALL 标注为「无缺失集」并记录巡检完成时间

### Requirement: 手动触发必须留痕且可追溯

系统 SHALL 在每次由用户主动触发的巡检或重试中记录触发来源，并在运行记录中可区分自动巡检与手动触发。手动触发 SHALL 在受理后立即产生一条可查询的运行记录；若后台执行失败，该记录 SHALL 反映失败原因。

#### Scenario: 用户手动触发巡检
- **WHEN** 用户对某影视点击「巡检」
- **THEN** 系统 SHALL 创建一条触发来源为手动的巡检运行记录
- **AND** 用户 SHALL 在界面上获得受理成功或失败的明确反馈

#### Scenario: 手动触发的后台执行异常
- **WHEN** 手动触发的巡检在后台执行过程中抛出未处理异常
- **THEN** 对应运行记录 SHALL 结束为失败并记录原因
- **AND** 系统 SHALL NOT 留下永久处于「进行中」的记录

### Requirement: 巡检运行记录不得长期停留在进行中

系统 SHALL 对超过合理时限仍处于「进行中」的巡检运行记录执行回收，将其结束为失败并记录回收原因。

#### Scenario: 巡检进程中断
- **WHEN** 某条巡检运行记录处于「进行中」的时长超过配置阈值
- **THEN** 回收任务 SHALL 将其结束为失败并标注超时回收
- **AND** 该影视的下一次巡检 SHALL 不被该残留记录阻塞

```

## docs/openspec/changes/scan-outcome-attribution/specs/notifications/spec.md

- Source: docs/openspec/changes/scan-outcome-attribution/specs/notifications/spec.md
- Lines: 1-22
- SHA256: 2e41a8333edd70392e33770d72d6aee997ae48bafd467700481f57d7a8955c4e

```md
# Spec Delta

## ADDED Requirements

### Requirement: 巡检异常必须产生告警

系统 SHALL 在巡检出现异常时产生告警，覆盖两类情形：单轮巡检执行失败，以及某影视连续多轮存在已播缺失集却始终未获取到资源。告警 SHALL 复用既有失败/告警通道（站内通知，PushPlus 可用时同步推送），并由系统自行维护同一影视同一原因的冷却窗口，同一影视同一原因在冷却期内 SHALL NOT 重复告警。

#### Scenario: 巡检执行失败
- **WHEN** 某轮巡检以失败结束
- **THEN** 系统 SHALL 产生一条站内告警，包含影视名称与失败原因
- **AND** 冷却期内同一影视同一原因的后续失败 SHALL 被合并，不重复推送

#### Scenario: 长期存在已播缺集但无资源
- **WHEN** 某影视连续多轮存在已播缺失集且每轮均未获取到任何资源
- **THEN** 系统 SHALL 产生一条告警，说明缺失集数量与最近一次尝试时间
- **AND** 该告警 SHALL 在缺失集数量下降后不再重复产生

#### Scenario: 告警降级
- **WHEN** PushPlus 未配置或推送失败
- **THEN** 系统 SHALL 仍写入站内通知
- **AND** SHALL 遵循既有推送失败补发规则

```

Full source files remain canonical. If a required heading or scenario is missing here, regenerate the handoff or read the source spec directly. Supporting files (proposal, design, tasks) are referenced by hash only.
Task hash policy: task-content-v1. Read tasks.md for live completion; task hashes ignore checkbox completion only.