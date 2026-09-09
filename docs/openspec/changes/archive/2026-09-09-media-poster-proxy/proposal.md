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