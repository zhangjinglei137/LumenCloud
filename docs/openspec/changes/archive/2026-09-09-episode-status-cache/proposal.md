## Why

影视列表的「已有 0/xx 集」与影视详情的「集数状态(0)」全部显示为 0，无法反映真实已入库集数；集数信息依赖实时 TMDB 查询（慢且易失败）；标记状态（已在库/巡检中/异常/未开播）与实际不符；集大小显示错误（默认等同第一个文件大小）。需要一套正确的集数状态数据源、数据库缓存与定时刷新机制。

## What Changes

- **修复集数统计全为 0**：修正影视列表 episode_stats 与影视详情 episode_state 的聚合统计，使「已有 x/xx 集」与「集数状态」显示真实已入库集数
- **新增集数信息数据库缓存**：将 TMDB 集信息（每集标题、首播日期、季/集号）入库缓存，新增每日定时刷新任务，避免每次查询都打 TMDB
- **修正标记状态机**：统一「已在库 / 巡检中 / 异常（开播但未下载成功，含未搜索到资源）/ 未开播」的判定逻辑，影视下载入库完成后自动更新集数状态信息
- **修正集大小展示**：集大小读取每个文件的真实大小，不再默认等同第一个文件大小
- 涉及数据模型变更：新增集信息缓存表（**BREAKING** schema 变更，alembic 迁移）

## Capabilities

### New Capabilities
- `episode-cache`: TMDB 集信息数据库缓存与每日定时刷新能力，为集数状态展示提供持久化数据源
- `media-status`: 集数状态统计（已有 x/xx 集、集数状态）的正确聚合；标记状态机（已在库/巡检中/异常/未开播）；集大小真实值展示；影视下载入库完成后自动更新集数状态

### Modified Capabilities
<!-- 集数状态统计与队列调度职责分离：media-pipeline 聚焦巡检/任务/下载队列，集数统计、标记状态机、大小与入库更新归属新增的 media-status，故无既有 capability 的需求级变更。 -->

## Impact

- 后端：`app/routers/media.py`（列表/详情集数聚合）、`app/models/`（新缓存表）、`app/tasks/`（每日集信息刷新任务）、`app/services/tmdb.py`（集信息获取）、alembic 迁移
- 前端：`frontend/src/views/MediaListView.vue`（episodeText）、`frontend/src/views/MediaDetailView.vue`（集数状态行展示）、`frontend/src/utils/format.ts`（状态映射）
- 配置：system_config 新增集信息缓存刷新间隔（可配置）
- 依赖：PostgreSQL（新表）；TMDB API（每日刷新）
