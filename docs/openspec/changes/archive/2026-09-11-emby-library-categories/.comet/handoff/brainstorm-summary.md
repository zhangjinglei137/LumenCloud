# Brainstorm Summary

- Change: emby-library-categories
- Date: 2026-09-10
- Status: **CONFIRMED**（用户已于 2026-09-10 确认设计方案）

## 确认的技术方案

### 契约层（已确认）
1. `GET /api/emby/library` 改造：`library_id`（必选）+ `item_type`（保留，movie/series）+ `status`（保留，continuing/ended）均保留；**移除 `anime` 旧参数**。tasks 2.2 的「移除 item_type/status/anime」表述与 tasks 2.1 及 spec「剧集状态过滤」场景矛盾，裁决为：只移除 anime，保留 item_type/status（需回写 delta spec）
2. `GET /api/emby/libraries` 数据源换为 `/Library/MediaFolders`：每项返回 `{id, name, collection_type, is_anime}`；**is_anime 标记由后端判定**（tvshows 库且库名含 ANIME_LIBRARY_KEYWORDS），前端不维护关键词集
3. 分类语义：
   - 电影 = CollectionType==movies 的库，查 item_type=movie
   - 剧集 = CollectionType==tvshows 且 is_anime=false 的库，查 item_type=series
   - 动漫 = is_anime=true 的库，查全部（Movie,Series，沿用现 anime 模式忽略 item_type 过滤的语义）
   - 全部 = CollectionType==mixed/null 的混合库，查全部
4. 分类 Tab 默认视图：**聚合同类库**（前端遍历该类下所有库逐库请求、合并去重展示），可下钻到具体单库
5. 设置页契约同步升级：`EmbyLibraryFolder` 字段 `item_id` → `id`（值来自 MediaFolders 的 Id）；存量 `emby_series_library_ids` 配置按 Id 语义复用；Q2 白名单逻辑保留
6. Q2 白名单新语义：`emby_series_library_ids` 非空时，**/api/emby/libraries 仅返回白名单内的 tvshows 库**（其他类型库仍全量）；前端自然只能看到/下钻到可见剧集库

### 后端实现
- `list_library_folders()`：调 `/Library/MediaFolders`，解析 Items/CollectionType/Id，附加 is_anime 判定；复用 `_get`/`_check_config`/错误归一
- `list_library(library_id, item_type, status)`：以 library_id 为 ParentId，保留 IncludeItemTypes/SeriesStatus 映射，删除 anime 分支（`_find_anime_library_item_id` 移除）；`_fetch_items` 分页拉取逻辑保留（全量语义，Limit 不放开，靠翻页兜底）
- router：`/libraries` 按白名单过滤剧集库；`/library` 参数改为 library_id 必选 + item_type/status 可选

### 前端实现
- types：`EmbyLibraryQuery` 改为 `{library_id, itemType?, status?}`；`EmbyLibraryFolder` 字段 item_id→id；新增 `is_anime`；`EmbyLibraryResponse.item_type` 回显保留
- api：`listEmbyLibraryApi` 传 library_id；`listEmbyLibrariesApi` 返回含 is_anime
- store：`fetchLibrary(library_id, itemType, status)`；新增分类派生逻辑（按 collection_type + is_anime 分组）
- EmbyLibraryView：4 分类 Tab 基于媒体库列表生成；聚合视图前端合并去重；移除分页组件（pageSize/currentPage/pagedItems 删除）；保留关键字/纳入状态本地筛选与订阅/TMDB 搜索/批量订阅

## 关键取舍与风险

- [MediaFolders 受用户权限影响] → 系统 api_key 调用属管理端可见性，沿用现状；白名单只过滤剧集库
- [混合库归「全部」] → 混合库条目不再进入电影/剧集 Tab，避免错分
- [存量 item_id 配置复用 Id] → 需在真实 Emby 环境验证 MediaFolders Id 与旧配置兼容；若不一致仅影响剧集库可见性，可通过设置页重新选择修复
- [动漫库若在白名单外则不可见] → 白名单配置即控制，可接受
- [分类聚合多请求] → 媒体库数量级个位数，逐库请求可接受；Emby 服务端压力可控
- [动漫 Tab 查全部类型] → 沿用现 anime 模式语义，剧场版电影不丢

## 测试策略

- 后端：MediaFolders 解析（含 Id/CollectionType/is_anime 判定）、IncludeItemTypes/SeriesStatus 映射、library_id ParentId 正确、白名单过滤剧集库、错误归一（未配置/不可达）→ `pytest`
- 前端：分类生成（多库/混合库/动漫库/is_anime 标记）、聚合合并去重、去分页数据流 → `vitest`
- 手动：真实 Emby 环境多库分类、按库查询、白名单可见性、无分页全量展示

## Spec Patch

1. **tasks.md 2.2**：修正「移除 item_type/status/anime 旧参数」→「移除 anime 旧参数，保留 item_type/status」（与 2.1 和 spec 一致）
2. **spec.md**：媒体库列表 Requirement 补充 is_anime 字段说明；新增验收场景：
   - 混合库归「全部」分类
   - 动漫库 is_anime 标记正确
   - 白名单配置非空时剧集库被过滤
3. **spec.md**：按库查询 Requirement 明确 library_id 为必选参数
