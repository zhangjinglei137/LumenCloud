# Comet Design Handoff

- Change: media-poster-proxy
- Phase: design
- Mode: compact
- Context hash: 41ed8effbf458a3bd90d1a12f006e16065e9c0a39f27f39311e567c3be89bb87

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This handoff is a deterministic, source-traceable context pack, not an agent-authored summary.

## docs/openspec/changes/media-poster-proxy/proposal.md

- Source: docs/openspec/changes/media-poster-proxy/proposal.md
- Lines: 1-23
- SHA256: 08a09811fa69dd4f7ae38d08e033d3d5acc4f1fd5727553f23d58f03589a5b06

```md
## Why

影视库所有影视的 `poster_path` 均有值，但海报图片全部不显示——前端直连 `https://image.tmdb.org/t/p/w500` 图床在墙内网络不可达（无任何图床代理/镜像配置）。

## What Changes

- **后端新增海报代理端点**：`GET /api/poster` 代理拉取 TMDB 图床图片（校验 `poster_path` 合法性，防路径穿越；带基础缓存降低回源压力）。
- **前端海报地址改走后端代理**：`TMDB_POSTER_BASE` 统一切换为后端代理地址；所有使用点（影视库卡片/表格、影视详情、TMDB 搜索、审批列表、Emby 库订阅）走代理加载。
- **可配置图床镜像地址**：沿用 `tmdb_proxy` 配置模式，新增图床镜像根地址配置项（`tmdb_poster_proxy`）；配置镜像后代理从此镜像取图，未配置回退官方图床。
- **优雅降级**：代理不可达时返回占位/404，前端保持现有海报加载失败兜底（标题占位）不破页面。

## Capabilities

### New Capabilities
- `poster-proxy`: 海报图床代理：后端代理端点、镜像地址配置、相对路径合法性校验、基础缓存、前端统一代理地址加载

### Modified Capabilities
<!-- 无既有能力被修改：项目 specs 目录尚为空（首次建立 specs）。 -->

## Impact

- **backend**: 新增海报代理路由（`app/routers/` 或并入现有 router）、`app/config.py` 新增 `tmdb_poster_proxy` 配置、缓存机制（复用 tmdb_cache 表或内存 TTL）
- **frontend**: `types/index.ts` 的 `TMDB_POSTER_BASE`、`views/MediaListView.vue`、`views/MediaDetailView.vue`、`components/TmdbSearch.vue`、`views/ApprovalsView.vue`、`views/EmbyLibraryView.vue`、`views/MediaAddView.vue`
- **tests**: 新增代理端点测试（路径校验、回源/镜像/降级三类场景）
```

## docs/openspec/changes/media-poster-proxy/design.md

- Source: docs/openspec/changes/media-poster-proxy/design.md
- Lines: 1-58
- SHA256: d3fbfb78f1f8922d85b2ed7d280f4db6afd47cd3c1da377dc467aca5e59ecad9

```md
## Context

影视库 8 部影视 poster_path 全部有值，前端 `TMDB_POSTER_BASE = https://image.tmdb.org/t/p/w500` 直连图床，墙内网络不可达导致海报全部不显示。无任何图床代理/镜像配置。参见 proposal.md - Why。

## Goals / Non-Goals

**Goals**
- 提供一个后端海报代理端点，前端所有海报显示经后端转发
- 支持配置图床镜像根地址（沿用 `tmdb_proxy` 的 DB 优先/env 兜底模式）
- 保留现有前端海报加载失败兜底（标题占位）

**Non-Goals**
- 不改后端 TMDB 元数据获取逻辑（poster_path 落库逻辑不变）
- 不做图片持久化缓存存储（仅内存/短期缓存或依赖浏览器缓存）
- 不处理 Emby 库自身海报（Emby 页已有独立 poster_url，仅统一其 TMDB 兜底场景）

## Decisions

### D1: 代理端点设计 `GET /api/poster?p=<path>`

- 输入 `p` = TMDB 相对路径（`/t/p/w500/xxx.jpg`）。
- 校验：必须以 `/t/p/` 开头、无 `..`、无协议段（http:// 等）→ 否则 400，不发起对外请求（防 SSRF/路径穿越）。
- 后端 `httpx.get(base + p)`，超时沿用 TMDB 客户端配置；成功即流式返回图片 + `Content-Type` + `Cache-Control: public, max-age=86400`（浏览器缓存兜底，减少回源）。
- 失败：524/5xx → 返回 502 或空占位；日志告警（节流）。

### D2: 图床镜像配置 `tmdb_poster_proxy`

- 新增配置键 `tmdb_poster_proxy`（system_config 优先，env `TMDB_POSTER_PROXY` / settings 兜底）。
- 语义与 `tmdb_proxy` 一致：填镜像反代根地址（如 `https://tmdb-image.example.com`）；空则回退官方 `https://image.tmdb.org`。
- 复用现有 `config_store.get` 模式，保存即生效（每次请求读一次，函数内读取）。

### D3: 前端统一改造

- `types/index.ts` 的 `TMDB_POSTER_BASE` 或封装 `utils/poster.ts` 提供 `posterUrl(path)`：返回 `/api/poster?p=${encodeURIComponent(path)}`。
- 统一替换 6 处使用点：MediaListView（卡片+表格）、MediaDetailView、TmdbSearch、MediaAddView、ApprovalsView、（EmbyLibraryView 订阅 TMDB 兜底场景）。
- 保持 `poster_path` 契约不变（仍返回相对路径）。

### D4: 缓存策略

- 首选浏览器 HTTP 缓存（响应头 max-age）+ axios 默认行为；后端不做持久化缓存，避免引入新存储。
- 可选：进程内 TTL 缓存 dict（如 10 分钟）避免高频列表页重复回源；用简单 `{path: (expire, bytes, content_type)}`。

## Risks / Trade-offs

- [代理成为图片访问唯一路径，回源慢时列表页变慢] → 图片并发拉取（httpx 复用连接/信号量限流）+ 响应缓存头；必要时后端内存缓存。
- [镜像地址误填（同 tmdb_proxy 的代理端口误填问题）] → 复用 tmdb.py 的防御校验，schema 缺失时报明确错误提示。
- [SSRF 风险] → 严格路径白名单校验（D1），只允许 `/t/p/...`，拒绝任何 host 参数。
- [后端内存占用] → 内存缓存设上限与 TTL（如 50 条 * 500KB），超限不缓存回源直出。

## Migration Plan

1. 后端：新增 poster 路由 + 配置键 + 校验。
2. 前端：新增 poster.ts 工具并替换所有使用点。
3. 部署：无迁移依赖；旧 `TMDB_POSTER_BASE` 常量保留兼容（未被引用即删）。
4. 回滚：前端改回直连常量即可，后端路由保留无害。

## Open Questions

无。技术细节（缓存大小/超时值）实现时定。
```

## docs/openspec/changes/media-poster-proxy/tasks.md

- Source: docs/openspec/changes/media-poster-proxy/tasks.md
- Lines: 1-19
- SHA256: 46dff0e4657249025986e44f944412e286fe9bba83b31e66d6060bd0b55c9f63

```md
## 1. 后端海报代理端点

- [ ] 1.1 新增 GET /api/poster 路由：校验 p 参数必须以 /t/p/ 开头且无 ../ 与协议段，非法返回 400；验证新增代理端点单测（合法路径/非法路径/SSRF 向量）通过
- [ ] 1.2 代理回源逻辑：httpx 拉取官方图床，成功返回图片 + Content-Type + Cache-Control max-age；失败返回 502 并节流告警；验证回源成功/失败用例通过

## 2. 图床镜像配置

- [ ] 2.1 新增配置键 tmdb_poster_proxy（config_store 优先，env TMDB_POSTER_PROXY 兜底），复用 tmdb_proxy 的误填防御校验；验证配置读写与防御校验用例通过
- [ ] 2.2 代理端点支持镜像地址（配置后从镜像拉取，未配置回退官方）；验证镜像/回退切换用例通过

## 3. 前端统一走代理

- [ ] 3.1 新增 utils/poster.ts 封装 posterUrl(path) 返回 /api/poster?p=...；验证单元测试覆盖编码与 null 处理
- [ ] 3.2 替换全部使用点：MediaListView（卡片+表格）、MediaDetailView、TmdbSearch、MediaAddView、ApprovalsView、EmbyLibraryView 订阅场景；验证前端 npm run build 通过
- [ ] 3.3 保留海报加载失败兜底（imgErrors 标题占位）；验证手工场景（代理不可达时不破页面）

## 4. 缓存与性能

- [ ] 4.1 可选内存 TTL 缓存（条目上限 + 过期），高频列表页减少回源；验证缓存命中/过期用例通过
- [ ] 4.2 全量后端测试 + 前端构建通过；端到端验证影视库海报经代理正常显示
```

## docs/openspec/changes/media-poster-proxy/specs/poster-proxy/spec.md

- Source: docs/openspec/changes/media-poster-proxy/specs/poster-proxy/spec.md
- Lines: 1-44
- SHA256: b0946cf9818de11ef133eb8772c238d9017730477a67aedd7b0a9f105a231c1a

```md
## Purpose

为影视海报提供后端代理加载能力：前端通过后端代理获取 TMDB 图床图片，支持可配置图床镜像地址，解决墙内直连 TMDB 图床不可达导致的海报不显示问题。

## ADDED Requirements

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

前端所有海报展示（影视库卡片/表格、影视详情、TMDB 搜索、审批列表、Emby 库订阅）SHALL 通过后端代理地址加载，不再直连 image.tmdb.org。

#### Scenario: 影视库海报经代理显示
- **WHEN** 用户打开影视库且影视存在海报
- **THEN** 海报经后端代理地址加载并正常显示

#### Scenario: 代理不可达优雅降级
- **WHEN** 代理后端不可达或拉取失败
- **THEN** 前端按既有海报加载失败兜底逻辑展示标题占位，页面不破
```
