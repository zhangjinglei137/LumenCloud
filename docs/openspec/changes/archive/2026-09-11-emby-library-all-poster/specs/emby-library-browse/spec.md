# emby-library-browse Specification

## Purpose

提供 Emby 影视库的「全部」聚合浏览与封面代理加载能力：全部类型跨库查询去重展示，封面经后端代理缓存加载避免整页并发回源直连 Emby。

## ADDED Requirements

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

系统 SHALL 为 Emby 条目封面提供后端代理加载：前端不直连 Emby、不暴露 api_key；代理响应带缓存（进程内 TTL + 浏览器 Cache-Control）。

#### Scenario: 封面经代理显示
- **WHEN** Emby 条目存在封面
- **THEN** 前端通过代理地址加载封面并正常显示

#### Scenario: 封面失败降级
- **WHEN** 代理拉取封面失败
- **THEN** 前端按既有海报缺失兜底逻辑显示占位，不破版

#### Scenario: 不暴露 api_key
- **WHEN** 前端请求 Emby 封面
- **THEN** 请求地址不携带 Emby api_key，安全鉴别由代理/服务端通道承载