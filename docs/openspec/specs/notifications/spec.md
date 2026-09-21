# notifications Specification

## Purpose
定义站内铃铛与 PushPlus 推送的用户可见通知行为：通知触发时机、文案格式规范、事件类型的图标/颜色呈现与 PushPlus 消息格式，确保用户只看到简洁、一致、可读的中文通知，聚焦媒体名称与集数信息。

## Requirements

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

### Requirement: 容量告警与真实容量一致

系统 SHALL 仅在实际容量数据满足告警条件（如使用率连续快照 ≥ 配置阈值）时产生「空间不足/容量使用率过高」类系统通知；容量充足（使用率低于阈值）时 MUST NOT 产生任何「空间不足」类系统通知或站内提醒。告警数值（总量/已用/使用率）SHALL 来自实时容量统计（alist），不得使用估算或与实时统计冲突的缓存数值。

#### Scenario: 容量充足不告警
- **WHEN** 网盘使用率低于告警阈值（如 40% < 90%）
- **THEN** 系统不产生任何「空间不足」类系统通知或站内提醒

#### Scenario: 容量不足才告警
- **WHEN** 网盘使用率连续快照 ≥ 配置阈值
- **THEN** 系统按既有去抖与冷却规则产生一条容量告警通知，数值与实时统计一致

### Requirement: 站内通知分页

站内通知列表 SHALL 支持分页（limit/offset）并返回总条数；通知接口不得一次性返回全部历史通知，避免长期累积后单次查询与响应体积无界增长。

#### Scenario: 通知列表分页返回
- **WHEN** 用户通知数量超过单页上限
- **THEN** 通知接口返回当前页数据与总条数，前端可翻页浏览

### Requirement: 站内通知定期清理

系统 SHALL 对站内通知执行定期清理（如按创建时间保留 N 天），防止 notifications 表无限增长；已读/历史通知超出保留期 SHALL 被清除。

#### Scenario: 过期通知被清理
- **WHEN** 定期清理任务运行且存在超过保留期的通知记录
- **THEN** 超期通知被删除，表体积受控

#### Scenario: 保留期内通知不受影响
- **WHEN** 定期清理任务运行且通知未超过保留期
- **THEN** 通知保留不变

### Requirement: PushPlus 推送失败降级站内告警

PushPlus 通道推送失败时，系统 SHALL 向站内通知补发一条「推送失败」告警（或记录到可观察的任务记录），使用户能感知 PushPlus 通道失效；不得静默吞掉失败。

#### Scenario: 推送失败产生站内告警
- **WHEN** PushPlus token 已配置但推送接口返回失败
- **THEN** 系统产生一条站内通知告警，提示 PushPlus 推送失败

### Requirement: 通知文案脱敏

通知正文 SHALL 对异常/错误信息进行截断与脱敏（去除 URL 中的 userinfo 与 query token、不原样透传含凭据的原始异常字符串），MUST NOT 通过站内通知或 PushPlus 泄露服务凭据。

#### Scenario: 异常文案不含凭据
- **WHEN** NasTools 等外部服务异常信息包含 URL 或请求详情
- **THEN** 通知正文仅包含错误类型/状态码等安全信息，不含凭据或敏感 query

### Requirement: 容量告警冷却跨进程一致

容量告警的冷却（去抖）状态 SHALL 不依赖单进程内存状态即可保持基本一致；多 worker 部署下不得因每个进程独立冷却导致告警成倍重复推送。若保留进程内冷却，SHALL 以配置注释明示多 worker 下的重复推送 trade-off。

#### Scenario: 多 worker 不重复告警
- **WHEN** 系统以多 worker 部署且容量连续超阈值
- **THEN** 告警推送次数与单 worker 一致（或差异有明确定义），不按 worker 数倍放
