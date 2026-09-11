# poster-proxy Specification

## Purpose
为影视海报提供后端代理加载能力：前端通过后端代理获取 TMDB 图床图片，支持可配置图床镜像地址，解决墙内直连 TMDB 图床不可达导致的海报不显示问题。

## Requirements

### Requirement: 后端海报代理端点

系统 SHALL 提供海报代理端点（如 GET /api/poster），接受 TMDB 图床相对路径参数，后端拉取对应图片并返回；代理 SHALL 校验路径合法性（仅允许 `/t/p/...` 形态，防路径穿越）。

#### Scenario: 合法海报路径代理返回
- **WHEN** 前端请求代理端点且参数为合法 TMDB 相对路径（/t/p/...）
- **THEN** 后端返回对应海报图片内容（正确 Content-Type 与缓存头）

#### Scenario: 非法路径被拒绝
- **WHEN** 前端请求代理端点但参数含非法路径（如 ../、协议外地址）
- **THEN** 后端拒绝请求（4xx），不发起对外请求

#### Scenario: 代理端点要求登录态
- **WHEN** 未登录用户请求代理端点
- **THEN** 后端返回 401，不返回图片内容（防止开放图床代理滥用；已登录用户经同源 cookie 通道正常加载）

### Requirement: 可配置图床镜像

系统 SHALL 支持配置图床镜像根地址（system_config 优先，env 兜底）；配置镜像后代理 SHALL 从镜像地址拉取图片，未配置回退官方图床。

#### Scenario: 已配置镜像
- **WHEN** 管理员配置了图床镜像地址且海报已存在
- **THEN** 代理从镜像地址拉取海报返回给前端

#### Scenario: 未配置镜像
- **WHEN** 未配置图床镜像地址
- **THEN** 代理从官方 TMDB 图床拉取海报返回给前端

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
