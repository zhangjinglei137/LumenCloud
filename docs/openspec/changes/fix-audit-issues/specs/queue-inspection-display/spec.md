## ADDED Requirements

### Requirement: 仅看活跃包含排队中任务

下载队列「仅看活跃」筛选 SHALL 与后端活跃状态集一致，包含 pending（等待准入/排队中）任务；pending 任务不得被「仅看活跃」过滤消失。

#### Scenario: 仅看活跃展示排队任务
- **WHEN** 用户开启下载队列「仅看活跃」且存在 pending 任务
- **THEN** pending 任务仍显示（属活跃排队态），不消失