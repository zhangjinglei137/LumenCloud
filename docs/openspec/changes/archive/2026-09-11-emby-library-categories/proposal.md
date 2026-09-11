## Why

Emby 影视库当前采用固定 Tab（电影/剧集/动漫）用 `item_type/status` 参数筛选，分类不准确：无法按真实媒体库（CollectionType）区分内容，动漫库识别依赖库名关键词匹配，且 Emby 原生推荐「先列媒体库（含 CollectionType）→ 再按媒体库查条目」的范式未被采用；列表分页造成浏览不便。

## What Changes

- **去掉分页**：Emby 影视库列表不再分页，一次性展示全部条目
- **媒体库类型接口**：采用 Emby 官方范式，新增「获取媒体库列表（含 CollectionType）」能力：
  - `GET /Library/MediaFolders`（用户视图，含 CollectionType/Id）作为媒体库列表数据源
  - 按媒体库 `CollectionType`（movies/tvshows/mixed 等）识别库类型
- **按媒体库类型获取影片详情**：以媒体库 Id 为 ParentId，`IncludeItemTypes`（Movie/Series）查询库内条目，替代现有 `item_type/status` 参数映射
- **修正分类**：前端 Tab 基于真实媒体库（CollectionType）分类，动漫库按库级识别而非仅库名关键词

## Capabilities

### New Capabilities
- `emby-library-categories`: Emby 影视库按媒体库类型（CollectionType）分类展示、库内条目查询与去分页能力

### Modified Capabilities
<!-- 现有 Emby 影视库展示（前端固定 Tab + 后端 item_type 映射）归类到新 capability；该能力此前无独立 spec，故无既有 capability 需求级变更。 -->

## Impact

- 后端：`backend/app/services/emby.py`（list_libraries/list_library 重构为媒体库类型查询）、`backend/app/routers/emby.py`（接口参数调整）
- 前端：`frontend/src/views/EmbyLibraryView.vue`（Tab 基于媒体库分类、去分页）、`frontend/src/stores/emby.ts`、`frontend/src/api`、`frontend/src/types`（EmbyLibraryQuery/Item 契约）
- 依赖：Emby 服务端（需支持 /Library/MediaFolders 与 /Items）；无数据库变更
