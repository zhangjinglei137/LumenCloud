# Comet Design Handoff

- Change: emby-library-all-poster
- Phase: design
- Mode: compact
- Context hash: 1f2491ee9517cc42bdcb20e46e8a74b7feede544cb53e50a909cb95ddd188280

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This handoff is a deterministic, source-traceable context pack, not an agent-authored summary.

## docs/openspec/changes/emby-library-all-poster/proposal.md

- Source: docs/openspec/changes/emby-library-all-poster/proposal.md
- Lines: 1-25
- SHA256: 4753a9f016d3ca53790cb7028f5637d0df210ae47a5b683084ba146bc182ab65

```md
# Proposal: emby-library-all-poster

## Why

Emby 影视库选择「全部」类型时列表为空（诊断 `GET /api/emby/library?library_id=7` 返回 `{items: [], total: 0, item_type: null}`），用户无法浏览全部媒体；同时 Emby 封面由 `<img>` 直连带 `api_key` 的 Emby 服务地址，未走后端代理缓存，整页刷新时大量并发回源导致封面加载缓慢。

## What Changes

- **修复「全部」类型空列表**：诊断并修复 library_id 聚合查询在「全部」场景返回空的问题（核查 ParentId 解析、IncludeItemTypes 映射、聚合去重逻辑与错误归一），确保全部类型能返回非空条目
- **Emby 封面改走后端代理加载**：复用/扩展海报代理通道，Emby 封面 URL 不再由前端直连（去掉 URL 内嵌 api_key 暴露），改由后端代理拉取并附带缓存头（复用进程内 TTL + 浏览器 Cache-Control），缓解整页刷新并发回源
- 保持「全部」聚合去重语义（多库条目按 emby_id 去重）与分类 Tab 行为不变

## Capabilities

### New Capabilities
- `emby-library-browse`: Emby 影视库浏览与封面加载（全部类型聚合查询、封面代理加载）

### Modified Capabilities
- `poster-proxy`: 前端统一走代理加载海报（代理范围扩展覆盖 Emby 封面源）

## Impact

- 后端：`backend/app/services/emby.py`（全部查询修复、封面 URL 生成改造）、`backend/app/routers/emby.py`（接口层如需）、`backend/app/services/poster.py` + `backend/app/routers/poster.py`（Emby 封面代理支持）
- 前端：`frontend/src/views/EmbyLibraryView.vue`（封面 src 走代理、加载兜底）、`frontend/src/utils/poster.ts`
- 依赖：Emby 服务端（Items 查询、图片接口）；无数据库变更

```

## docs/openspec/changes/emby-library-all-poster/design.md

- Source: docs/openspec/changes/emby-library-all-poster/design.md
- Lines: 1-39
- SHA256: 87ce5422c75538c08d9cb0f2bffe8d0e1abad7ad0ef5b3bc0d6f97b0189b8c6d

```md
## Context

Emby 影视库分类改造（emby-library-categories change）后，「全部」Tab 通过 store 的 libraryGroups 按 collection_type 分组逐库请求，聚合后按 emby_id 去重展示；诊断显示 `GET /api/emby/library?library_id=7` 返回 `{items: [], total: 0, item_type: null}`，即全部场景下可能因 library_id 归属、ParentId 解析或 mixed 库映射问题返回空。封面方面 `poster_url` 直接把 `api_key` 拼进 Emby 直连 URL，`<img loading="lazy">` 是唯一性能手段，整页刷新大量并发回源。现有 `poster.py` 已实现 TMDB 图床代理（10 分钟 TTL + 86400s Cache-Control，SSRF 校验 + 登录态）。见 proposal.md - Why。

## Goals / Non-Goals

**Goals:**
- 修复「全部」类型空列表，确保真实 Emby 环境返回非空
- Emby 封面改经后端代理加载，复用既有 TTL 缓存与 Cache-Control，消除前端直连与 api_key 暴露

**Non-Goals:**
- 不改 Emby 影视库分类 Tab 与去重语义（保持现有行为）
- 不引入 Emby 图片的持久化磁盘缓存（沿用进程内 + 浏览器缓存足够）
- 不处理其他图片源（TMDB 代理已存在）

## Decisions

**D1: 代理通道扩展而非新建端点体系**
- 理由：poster 代理已具备 SSRF 防护、登录态、TTL 缓存与 Cache-Control；Emby 封面作为受限来源接入，前端通过抽象工具函数统一生成代理 URL
- 实现：后端代理接受 Emby 图片请求（如 `/api/poster?p=emby/<itemId>/<imageType>` 或独立参数），校验 itemId 为合法标识并带鉴权解析，拉取后透传缓存头
- 备选：新建独立 embby 图片端点（结构化重复）→ 放弃

**D2: 「全部」空列表修复先做根因诊断，再定修复点**
- 怀疑点按序排查：`library_id=7` 是否真实存在且可访问 → ParentId/IncludeItemTypes 映射 → mixed 库聚合 → 前端 store 分组兜底
- 修复以服务层能力为主，前端仅在必要处调整取数逻辑

## Risks / Trade-offs

- [Emby 图片代理成为新的可滥用出口] → 复用既有登录态 + SSRF/路径校验 + 仅允许 emby 域内图片
- [Emby 图片接口并发仍高] → 代理内进程 TTL 可缓存热门封面；必要时后端限并发
- [全部聚合多库请求放大耗时] → 复用逐库请求最小化（并行请求库），必要时加入聚合缓存

## Migration Plan

- 后端代理扩展为增量（旧 TMDB 参数路径保持兼容），前端分阶段切换 Emby 封面 src
- 无数据库迁移

## Open Questions

- 真实环境下 library_id=7 的 collection_type 与条目分布（需 build 阶段连接真实 Emby 环境诊断确认）
```

## docs/openspec/changes/emby-library-all-poster/tasks.md

- Source: docs/openspec/changes/emby-library-all-poster/tasks.md
- Lines: 1-22
- SHA256: b743857a0efdd9a55dfed5d00616d588f608d97c58eacd28822be26b3ba9abcf

```md
# Tasks: emby-library-all-poster

## 1. 后端：全部类型空列表根因与修复

- [ ] 1.1 诊断全部场景空列表：核查 library_id 归属、ParentId 解析（服务端 /Items 调用）、IncludeItemTypes 映射、错误归一路径，定位空返回根因并记录诊断结论
- [ ] 1.2 services/emby.py 修复全部聚合查询：确保 mixed 等 collection_type 库条目被聚合（必要时调整 list_library 的 IncludeItemTypes 默认与 ParentId 解析），验证对真实 Emby 环境「全部」返回非空
- [ ] 1.3 补充后端测试：全部聚合去重、mixed 库收录、Emby 不可达/未配置错误映射，验证 `pytest` 通过

## 2. 后端：Emby 封面代理

- [ ] 2.1 services/poster.py + routers/poster.py 扩展代理支持 Emby 封面（校验 emby Image 参数合法性、复用登录态与 SSRF 防护、进程内 TTL + Cache-Control），验证代理返回图片且非法参数被拒
- [ ] 2.2 services/emby.py 封面 URL 生成改造：Emby 条目 poster_url 改为后端代理地址（不再内嵌 api_key），验证返回地址为代理格式且无 api_key 泄露
- [ ] 2.3 补充测试：代理合法/非法路径、登录态要求、api_key 不外泄，验证 `pytest` 通过

## 3. 前端：封面加载切换

- [ ] 3.1 frontend/src/utils/poster.ts 或工具函数支持 Emby 代理 URL 组装（复用现有 poster 工具约定），验证 URL 生成正确
- [ ] 3.2 EmbyLibraryView.vue 封面 img src 切换为代理地址并保留加载失败兜底逻辑，验证封面经代理显示、失败时占位不破版
- [ ] 3.3 前端测试补充：Emby 封面代理 URL 生成与兜底，验证 `vitest` 通过

## 4. 集成验证

- [ ] 4.1 真实 Emby 环境验证：全部类型列表非空、封面经代理加载且刷新无大量并发回源；`npm run build` 通过
```

## docs/openspec/changes/emby-library-all-poster/specs/emby-library-browse/spec.md

- Source: docs/openspec/changes/emby-library-all-poster/specs/emby-library-browse/spec.md
- Lines: 1-38
- SHA256: 4e39404f6efd5f250f3fd489534e7ea1cfa232f58fdcc6a96a8f3e2fac44229d

```md
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
```

## docs/openspec/changes/emby-library-all-poster/specs/poster-proxy/spec.md

- Source: docs/openspec/changes/emby-library-all-poster/specs/poster-proxy/spec.md
- Lines: 1-18
- SHA256: 24f3c7c6f83ab178c396af738d486e84138afef200d936b69c5fc2599b0c1774

```md
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
```
