# emby-library-browse Specification

## Purpose
提供 Emby 影视库的「全部」聚合浏览与封面代理加载能力：全部类型跨库查询去重展示，封面经后端代理缓存加载避免整页并发回源直连 Emby。

## Requirements

### Requirement: 全部类型聚合查询非空

系统 SHALL 在 Emby 影视库选择「全部」时聚合可浏览媒体库的条目并按 emby_id 去重返回；不得因库类型（movies/tvshows/mixed 等）或 ParentId 映射问题返回空列表。

#### Scenario: 全部类型返回条目
- **WHEN** 用户选择「全部」且 Emby 中存在可浏览条目
- **THEN** 列表返回去重后的全部条目，非空

#### Scenario: mixed 类型库被聚合
- **WHEN** 存在 collection_type 为 mixed 的媒体库且内含条目
- **THEN** 全部类型聚合包含该库条目，不因类型判定缺失而遗漏

#### Scenario: Emby 不可达错误提示
- **WHEN** Emby 服务不可达或未配置
- **THEN** 返回明确错误/空态提示而非静默空列表，前端展示可操作提示

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

### Requirement: 加入订阅仅管理员可见

Emby 影视库条目「加入订阅」按钮 SHALL 仅对管理员（admin）角色展示；非管理员（guest）用户浏览 Emby 影视库时不展示「加入订阅」/「匹配 TMDB 后订阅」等订阅入口，仅可浏览与查看。

#### Scenario: 管理员可见订阅入口
- **WHEN** 管理员查看 Emby 影视库中未收录条目
- **THEN** 条目展示「加入订阅」按钮，可发起订阅

#### Scenario: 访客不显示订阅入口
- **WHEN** 非管理员（guest）查看 Emby 影视库
- **THEN** 条目不展示「加入订阅」/「匹配 TMDB 后订阅」按钮及订阅相关入口，仅浏览与查看

### Requirement: 配置变更后连接缓存失效

管理员修改 Emby 服务地址等连接配置后，系统 SHALL 使已缓存的 Emby server_id / user_id 等连接态缓存失效并重新获取，详情链接、库列表等 SHALL 指向新配置的服务器，不得继续指向旧服务器。

#### Scenario: 切换 Emby 服务器后指向新服务器
- **WHEN** 管理员修改 Emby 基础地址配置且新服务器可用
- **THEN** 后续详情链接与库列表基于新服务器生成，不再使用旧服务器 id

#### Scenario: 未变更配置时缓存复用
- **WHEN** Emby 连接配置未变化
- **THEN** 既有连接缓存正常复用，不额外发起重取

### Requirement: 状态筛选与 Emby 契约对齐

Emby 影视库状态筛选（在更/完结）参数 SHALL 与 Emby API 契约保持一致（含大小写）；筛选值不匹配 SHALL 导致对应状态条目正确返回，不得因参数形态不符而静默失效。

#### Scenario: 在更筛选返回在更条目
- **WHEN** 用户在 Emby 影视库选择「在更」状态筛选
- **THEN** 返回结果包含在更状态条目，筛选生效

#### Scenario: 完结筛选返回完结条目
- **WHEN** 用户在 Emby 影视库选择「完结」状态筛选
- **THEN** 返回结果包含完结状态条目，筛选生效
