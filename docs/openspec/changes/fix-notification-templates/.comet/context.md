# Comet Spec Context

- Change: fix-notification-templates
- Phase: design
- Mode: beta
- Context hash: 3e1d494ecf394b17228d689a2126dfc3c84d7dc5a382a4c86da6506450b94528

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This beta context pack verbatim-projects spec files and references supporting artifacts by hash, not an agent-authored summary.

## Source References

- Source: docs/openspec/changes/fix-notification-templates/proposal.md
- SHA256: 74818294c4ee929e1418df27c57c6dd81bbb7401802eaa9fedb15bc64d48d415
- Source: docs/openspec/changes/fix-notification-templates/design.md
- SHA256: 94f3ccfd29a780a0f8f65caaa2cbbc3f16c8dd07247e2eb7b5db972994b546ee
- Source: docs/openspec/changes/fix-notification-templates/tasks.md
- SHA256: 29f5befd9e4237a4b0407d1ae152212755ac31b69975a2f18a66068224b5c66f
- Source: docs/openspec/changes/fix-notification-templates/specs/notifications/spec.md
- SHA256: 30e67c983f3982894149db1f7915c98e2b393675a2cfb003cdfbbe795949c5da

## Acceptance Projection

## docs/openspec/changes/fix-notification-templates/specs/notifications/spec.md

- Source: docs/openspec/changes/fix-notification-templates/specs/notifications/spec.md
- Lines: 1-74
- SHA256: 30e67c983f3982894149db1f7915c98e2b393675a2cfb003cdfbbe795949c5da

```md
## Purpose

定义站内铃铛与 PushPlus 推送的用户可见通知行为：通知触发时机、文案格式规范、事件类型的图标/颜色呈现与 PushPlus 消息格式，确保用户只看到简洁、一致、可读的中文通知，聚焦媒体名称与集数信息。

## ADDED Requirements

### Requirement: 通知触发时机
系统 SHALL 仅在以下时机产生通知：剧集/电影入库完成（Emby 确认收录）、存在待审批的想看请求、以及发生流程错误或告警（转存失败、容量告警、NasTools 同步失败等）。
系统 MUST NOT 在下载开始、开始入库、或 downloading→scrape 阶段产生通知。
「入库完成」是下载链路唯一的结果型通知。

#### Scenario: 下载开始不产生通知
- **WHEN** 一个下载任务被提交至 aria2 开始下载，或一个审批被批准后进入入库流程
- **THEN** 系统不产生任何站内通知，也不向 PushPlus 推送

#### Scenario: 下载完成但未入库不产生通知
- **WHEN** 下载完成、文件进入刮削阶段但 Emby 尚未确认收录
- **THEN** 系统不产生任何通知

#### Scenario: Emby 确认收录后产生入库完成通知
- **WHEN** 库检查确认文件已被 Emby 收录
- **THEN** 系统产生一条「入库完成」通知，并通过站内铃铛与 PushPlus 推送（如已配置）

### Requirement: 入库完成通知文案
入库完成通知 MUST 以媒体名称与集数（`SxxExx` 格式）为主体呈现，MUST NOT 在用户可见文案中出现内部媒体 id 或文件名。
剧集格式 SHALL 为「媒体 {media_name} · {episode} 已入库完成。」，其中 `{episode}` 为 `S01E01` 形式的两位季号与两位集号（三位集号保留三位）。
电影格式 SHALL 为「媒体 {title} 已入库完成。」（电影无集数概念）。

#### Scenario: 剧集入库完成通知
- **WHEN** 一部剧集的 S01E02 被 Emby 确认收录
- **THEN** 通知文案为「媒体 {剧集标题} · S01E02 已入库完成。」，且不包含 media_id 与文件名

#### Scenario: 电影入库完成通知
- **WHEN** 一部电影被 Emby 确认收录
- **THEN** 通知文案为「媒体 {电影标题} 已入库完成。」，且不包含 media_id 与文件名

### Requirement: 通知文案使用中文且风格统一
所有通知的标题与正文 SHALL 使用中文文案，MUST NOT 在用户可见文案中暴露英文 raw 事件类型（如 `transfer.fail`、`download.fail`、`transfer.finished`）。
同类通知的标题与正文格式 SHALL 保持一致：标题为短语式描述，正文承载补充信息（数值、原因、操作建议）。

#### Scenario: NaSTools 失败事件通知
- **WHEN** NaSTools 报告 `transfer.fail` 或 `download.fail` 事件
- **THEN** 通知标题与正文使用中文描述（如「转存失败」「下载失败」），不出现 `transfer.fail` 等英文事件名

#### Scenario: 通知正文包含可操作信息
- **WHEN** 系统产生失败/告警类通知
- **THEN** 正文包含失败原因或告警数值及建议操作，便于用户直接判断处理

### Requirement: PushPlus 推送格式
PushPlus 推送 SHALL 使用 HTML 模板呈现：标题加粗、正文分段、媒体名称与集数高亮，提升移动端可读性。
未配置 PushPlus token 时 SHALL 保持现有行为（跳过该通道，不影响站内通知）。

#### Scenario: PushPlus 已配置时推送 HTML 格式
- **WHEN** PushPlus token 已配置且产生一条通知
- **THEN** PushPlus 消息以 HTML 模板发送，标题加粗，正文按段落组织，媒体名称与集数高亮显示

#### Scenario: PushPlus 未配置
- **WHEN** PushPlus token 未配置
- **THEN** 系统跳过 PushPlus 通道，站内通知不受影响

### Requirement: 前端铃铛按事件类型呈现
前端通知铃铛 SHALL 按事件类型区分图标与颜色：
- 入库完成（成功类）：绿色
- 待审批请求：蓝色
- 失败/告警类：红色
每类通知条目 SHALL 在标题前展示对应的类型图标。

#### Scenario: 铃铛列表按类型展示
- **WHEN** 用户打开通知铃铛面板，其中包含入库完成、待审批、转存失败三类通知
- **THEN** 三条通知分别以绿色+成功图标、蓝色+待审批图标、红色+失败图标呈现，用户可在无正文阅读的情况下区分类型

#### Scenario: 未知事件类型的回退
- **WHEN** 通知的 event_type 不属于已知分类
- **THEN** 铃铛以默认样式（无强调色/中性图标）呈现，不阻断列表渲染

```

Full source files remain canonical. If a required heading or scenario is missing here, regenerate the handoff or read the source spec directly. Supporting files (proposal, design, tasks) are referenced by hash only.