# Tasks: emby-library-categories

## 1. 后端：媒体库列表接口（含 CollectionType）

- [x] 1.1 services/emby.py 新增 `list_library_folders()`：调用 `GET /Library/MediaFolders` 返回 [{id, name, collection_type}]（复用 _get/_check_config/错误归一），验证 MediaFolders 返回解析正确且未配置/不可达抛 EmbyUnavailable
- [x] 1.2 routers/emby.py 改造 `GET /api/emby/libraries`：返回 `{libraries: [{id, name, collection_type}], total}`，验证响应契约与前端类型一致

## 2. 后端：按媒体库查询库内条目

- [x] 2.1 services/emby.py 改造 `list_library(library_id, item_type, status)`：以 library_id 为 ParentId 调用 `/Items`，IncludeItemTypes 映射（movie→Movie / series→Series），status 映射 SeriesStatus=Continuing|Ended，移除 anime 全局关键词路径，验证参数映射与 /Items 调用正确
- [x] 2.2 routers/emby.py 更新 `GET /api/emby/library`：新增必选 `library_id` 参数，移除 `anime` 旧参数（保留 `item_type`/`status`，前端同步），验证接口契约更新
- [x] 2.3 补充后端测试：MediaFolders 解析、IncludeItemTypes/SeriesStatus 映射、错误归一（未配置/不可达），验证 `pytest` 通过

## 3. 前端：分类重构与去分页

- [x] 3.1 types/index.ts + api 更新 EmbyLibraryQuery（library_id 替代 item_type/status/anime）与 EmbyLibraryItem/Library 类型，验证类型契约与后端一致
- [x] 3.2 stores/emby.ts 新增 fetchLibraries（媒体库列表）并调整 fetchLibrary(library_id)，验证 store 状态与错误码处理保留
- [x] 3.3 EmbyLibraryView 分类重构：顶部「电影/剧集/动漫/全部」基于媒体库 CollectionType 生成，动漫库库级识别（tvshows 内关键词），电影/剧集库可下钻具体库，验证分类与真实媒体库一致
- [x] 3.4 EmbyLibraryView 移除分页组件，列表全量展示，验证无分页控件且滚动浏览正常
- [x] 3.5 前端测试补充：分类生成（多库/混合库/动漫库）、去分页数据流用例，验证 `vitest` 通过

## 4. 验证

- [ ] 4.1 手动验证真实 Emby 环境：多媒体库分类正确、按库查询条目正确、无分页全量展示，验证 `npm run build` 与后端启动无报错
