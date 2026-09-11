## MODIFIED Requirements

### Requirement: Emby 封面经代理加载

系统 SHALL 为 Emby 条目封面提供后端代理加载：后端生成的 Emby 封面代理地址 SHALL 能被代理路径校验通过（含 `/emby/<itemId>/Primary` 前缀与合法 itemId），前端不直连 Emby、不暴露 api_key；代理响应带缓存（进程内 TTL + 浏览器 Cache-Control）。

#### Scenario: 封面经代理显示
- **WHEN** Emby 条目存在封面
- **THEN** 前端通过代理地址加载封面并正常显示（后端构造的代理路径通过校验，不返回 400）

#### Scenario: 封面失败降级
- **WHEN** 代理拉取封面失败
- **THEN** 前端按既有海报缺失兜底逻辑显示占位，不破版

#### Scenario: 不暴露 api_key
- **WHEN** 前端请求 Emby 封面
- **THEN** 请求地址不携带 Emby api_key，安全鉴别由代理/服务端通道承载

## ADDED Requirements

### Requirement: 加入订阅仅管理员可见

Emby 影视库条目「加入订阅」按钮 SHALL 仅对管理员（admin）角色展示；非管理员（guest）用户浏览 Emby 影视库时不展示「加入订阅」/「匹配 TMDB 后订阅」等订阅入口，仅可浏览与查看。

#### Scenario: 管理员可见订阅入口
- **WHEN** 管理员查看 Emby 影视库中未收录条目
- **THEN** 条目展示「加入订阅」按钮，可发起订阅

#### Scenario: 访客不显示订阅入口
- **WHEN** 非管理员（guest）查看 Emby 影视库
- **THEN** 条目不展示「加入订阅」/「匹配 TMDB 后订阅」按钮及订阅相关入口，仅浏览与查看
