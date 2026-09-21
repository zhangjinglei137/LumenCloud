## ADDED Requirements

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
