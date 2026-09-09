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