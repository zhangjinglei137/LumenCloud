## Why

影视详情页当前布局与功能不符合使用习惯：转存队列区块冗余；大小与巡检设置位置分散；集数状态展示空间不足且全集数查找困难（无分组导航）；无法直观看到每集的 TMDB 名称。需要重设计详情页，让信息层级清晰、集数浏览高效。

## What Changes

- **移除转存队列区块**：影视详情不再展示转存队列（该能力已由下载/任务队列承接）
- **布局调整**：大小与巡检设置移到 lc-panel detail-header 最右侧并排展示
- **集数状态最大显示**：集数状态区获得最大显示空间
- **集数分组导航**：集数按每 100 集一个 tag 分组（如 350 集 → 1-100 / 101-200 / 201-300 / 301-350），点击 tag 过滤展示对应范围
- **TMDB 集名称展示**：集数行展示 TMDB 集信息名称（依赖 episode-status-cache 的集信息缓存数据）
- 纯前端 UI 变更，不涉及后端 API 契约改动

## Capabilities

### New Capabilities
- `media-detail-ui`: 影视详情页的信息层级、布局与集数分组导航展示能力

### Modified Capabilities
<!-- 纯前端布局与交互调整，无 spec 级行为契约变更（集数状态数据来自 episode-status-cache 的 media-status capability），故无既有 capability 的需求级变更。 -->

## Impact

- 前端：`frontend/src/views/MediaDetailView.vue`（布局/集数分组/转存队列移除）、`frontend/src/stores/media.ts`（如需字段）、`frontend/src/utils/format.ts`（分组辅助）
- 依赖：episode-status-cache 提供集信息缓存数据（tmdb_episodes 名称）；无后端 API 改动
- 无数据库、无配置变更
