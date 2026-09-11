# Design: emby-library-categories

## Context

现状（参见 proposal.md - Why）：
- 前端 `EmbyLibraryView.vue` 用固定 Tab（电影/剧集/动漫），走后端 `GET /api/emby/library?item_type=...&status=...&anime=true`
- 后端 `list_library` 用 `item_type/status/anime` 过滤 Emby `/Items`，动漫识别靠库名关键词（`_find_anime_library_item_id` → `ANIME_LIBRARY_KEYWORDS`）
- `GET /api/emby/libraries` 已存在（基于 /Library/VirtualFolders），但仅用于动漫识别，前端未展示媒体库
- 列表前端分页（client slice）造成浏览不便

外部调研（@librarian 核实 Emby 官方文档 dev.emby.media/reference/RestAPI.html）：
- Emby 官方推荐范式：① `GET /Library/MediaFolders`（或 `/Users/{UserId}/Views`）拿媒体库列表，每条含 `CollectionType` 与 `Id`；② `GET /Users/{UserId}/Items?ParentId=<ViewId>&IncludeItemTypes=Movie|Series&SeriesStatus=Continuing|Ended` 查库内条目
- 官方 API **没有 `collectionType` query 参数**，也没有 `ListLibrary` 端点；CollectionType 只在媒体库列表返回中携带
- `CollectionType` 取值：movies/tvshows/music/mixed/null 等；mixed/null 表示混合库
- 现有 `item_type/status` 需映射为 `IncludeItemTypes`/`SeriesStatus`（首字母大写）

## Goals / Non-Goals

**Goals**
- 去分页：Emby 影视库全量展示
- 媒体库列表接口（含 CollectionType）落地，前端基于真实媒体库分类
- 按媒体库（ParentId）+ IncludeItemTypes 查询条目，替代 item_type 猜测
- 动漫库库级识别

**Non-Goals**
- 订阅/入库流程改动（沿用现有 POST /api/media + scan）
- TMDB 匹配逻辑改动
- 后端 API 向后兼容旧 item_type 参数（仅前端升级，旧参数清理）

## Decisions

### D1：媒体库列表数据源采用 /Library/MediaFolders

**决策**：媒体库列表改用 `GET /Library/MediaFolders`（用户视图，含 Id/CollectionType/Name），而非现有 /Library/VirtualFolders。

**理由**：MediaFolders 返回的条目可直接作为 `/Items` 的 ParentId（VirtualFolders 的 ItemId 需额外关联）；含 CollectionType；官方「Browsing the Library」教程即此范式。

**备选**：保留 VirtualFolders → 字段更全（Locations/RefreshStatus）但需要 ItemId↔Id 映射，且无用户视图语义，否决。

### D2：后端新增「媒体库 + 库内条目」查询契约

**决策**：后端新增 `GET /api/emby/libraries`（返回 id/name/collection_type）与改造 `GET /api/emby/library`（新增 `library_id` 参数，内部映射 IncludeItemTypes/SeriesStatus；`item_type/status` 旧参数废弃移除）。

**理由**：后端封装 Emby 两步范式，前端无需感知 ParentId/IncludeItemTypes 细节；错误归一（_to_http_exc）复用。

**备选**：前端直连 Emby API → 凭据暴露、错误处理重复，否决。

### D3：前端 Tab 基于媒体库分类

**决策**：EmbyLibraryView 顶部改为「电影 / 剧集 / 动漫 / 全部」分类，数据源为媒体库列表：
- 电影 = CollectionType==movies 的库，查 Movie
- 剧集 = CollectionType==tvshows 且非动漫库，查 Series（可带 SeriesStatus 过滤）
- 动漫 = tvshows 库中库名含动漫关键词（或显式配置）的库
- 全部 = 混合浏览（mixed 库或全部库合并）
- 每个分类下可选具体媒体库（若同类型多库）

**理由**：分类与 Emby 真实库结构对齐；动漫库级识别优于全局 Name 匹配；多库场景可下钻。

**备选**：继续固定 Tab + 全局关键词 → 多库/混合库分类必然错乱，否决。

### D4：去分页实现

**决策**：后端列表接口不限制 Limit（或使用 Emby 默认全量），前端移除分页组件；条目多时浏览器滚动。

**理由**：用户明确要求去分页；Emby 库条目量级（数百）全量返回可接受。

## Risks / Trade-offs

- [MediaFolders 受用户权限影响（某些库不可见）] → 使用系统 api_key 调用属管理端可见性；若需用户级视图可换 /Users/{UserId}/Views，当前不阻塞
- [mixed 混合库分类归属] → 「全部」分类统一承载 mixed/null 库，避免错分
- [旧 item_type/status 参数清理影响存量前端] → 本 change 同时升级前端调用方，接口单版本化（不保留兼容层）
- [动漫识别仍依赖库名关键词] → 关键词集已存在且可扩展；库级识别（限定 tvshows 库范围内匹配）已比全局匹配精确

## Migration Plan

1. 后端：emby service 新增 list_library_folders（MediaFolders）+ 改造 list_library（library_id/IncludeItemTypes）
2. 后端：router 暴露新参数，移除旧 item_type/status（前端同步改）
3. 前端：EmbyLibraryView 分类重构 + 去分页 + store/api/types 契约更新
4. 验证：真实 Emby 环境多库分类正确、全量展示无分页
5. 回滚：还原前端与后端旧契约（无数据库变更，回滚成本低）

## Open Questions

- 动漫库是否需要「管理员配置库 ID 白名单」替代关键词？默认保留关键词库级识别（现有 ANIME_LIBRARY_KEYWORDS），白名单可作为后续增强，不阻塞本 change。
