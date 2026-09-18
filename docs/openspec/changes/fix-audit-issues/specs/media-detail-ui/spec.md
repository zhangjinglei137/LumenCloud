## ADDED Requirements

### Requirement: 非法媒体标识防御

影视详情页 SHALL 在路由参数不是合法媒体 id（非数字或 NaN）时不发起后端详情请求，并回退到影视列表页或展示可操作的空态，不得向 `GET /api/media/<非法值>` 发起请求。

#### Scenario: 非法路由参数回退列表
- **WHEN** 用户访问 `/media/abc` 等非数字媒体 id 路由
- **THEN** 页面不发起非法详情请求，并跳转到影视列表页或展示可操作空态

#### Scenario: 合法媒体 id 正常加载
- **WHEN** 路由参数为合法数字媒体 id
- **THEN** 详情页正常加载该影视数据
