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