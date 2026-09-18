## ADDED Requirements

### Requirement: 容量积压预估口径正确

容量状态的「未消费积压」（pending 预估）SHALL 基于当前生效的下载队列模型统计（DownloadQueue 中 pending 行），不得查询已废弃的旧表；积压预估口径 SHALL 与前端容量条展示语义一致，不得长期恒为 0 或失真。

#### Scenario: 积压预估反映真实排队
- **WHEN** 下载队列存在 pending（等待准入）任务且容量接口返回积压预估
- **THEN** 积压预估值包含这些 pending 任务的体积合计，与真实排队一致

### Requirement: 等待准入冲突处理

下载队列准入流程中，若并发方已将任务从 pending 推进（CAS 未命中），系统 SHALL 将该冲突显式记为并发冲突并跳过该任务，不得按「容量不足」错误语义处理导致误报配额告警或错误状态返回。

#### Scenario: 并发推进后不误报配额等待
- **WHEN** 准入尝试时任务已被并发方推进出 pending（CAS 未命中）
- **THEN** 系统将本次视为冲突跳过，不返回「配额等待」并停止本批准入的错误语义

### Requirement: 手动加集不打断探测中任务

手动向下载队列添加剧集时，SHALL 对既有任务行加状态门控：仅允许重置终态/可重置态任务，探测中（probing）等运行中状态 SHALL 不被重置；重置不得破坏探测结果落库的一致性。

#### Scenario: 探测中任务不被重置
- **WHEN** 某行处于 probing（探测中）时收到手动加集请求
- **THEN** 该行保持探测状态不被重置为 pending，探测结果正常落库

#### Scenario: 终态任务可重置
- **WHEN** 某行处于终态（failed/skipped 等）且收到手动加集请求
- **THEN** 该行可被重置入队，不报错
