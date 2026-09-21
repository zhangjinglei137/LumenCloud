## ADDED Requirements

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
