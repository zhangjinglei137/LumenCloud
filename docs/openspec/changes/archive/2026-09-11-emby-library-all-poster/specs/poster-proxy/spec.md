# poster-proxy Delta Spec

## MODIFIED Requirements

### Requirement: 前端统一走代理加载海报

前端所有海报展示（影视库卡片/表格、影视详情、TMDB 搜索、审批列表、Emby 库订阅、Emby 影视库条目封面）SHALL 通过后端代理地址加载，不再直连 image.tmdb.org 或 Emby 服务端。

#### Scenario: 影视库海报经代理显示
- **WHEN** 用户打开影视库且影视存在海报
- **THEN** 海报经后端代理地址加载并正常显示

#### Scenario: Emby 封面经代理显示
- **WHEN** 用户浏览 Emby 影视库且条目存在封面
- **THEN** 封面经后端代理地址加载并正常显示，URL 不携带 Emby api_key

#### Scenario: 代理不可达优雅降级
- **WHEN** 代理后端不可达或拉取失败
- **THEN** 前端按既有海报加载失败兜底逻辑展示标题占位，页面不破