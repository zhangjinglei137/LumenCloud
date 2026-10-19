## Why

巡检入队目前只有「大小上限」，没有「大小下限」。实际事故：一部 AI 生成剧集的单集仅约 20MB，被当成正常剧集下载入库，浪费转存/下载配额并污染媒体库。

同一事故还暴露了第二个问题：在 Emby 侧删除这条错误影视后，系统无法再次下载它。`download_queue` 是防重权威源（`UNIQUE(media_id, episode)`），而 `_enqueue()` 对同集**任意状态**（含 `done`/`failed`）一律判为已存在并跳过；巡检的 `_resolve_done_states()` 对「已完成但 Emby 仍缺失」的记录只做一次 `done→failed`，之后 `failed` 行永远不会再被巡检处理。结果是：用户在 Emby 删除后，该集被永久挡住，只能到队列页手动点「重试」。

## What Changes

- 新增「最小影视大小」过滤：单集、电影分别设置下限，全局默认 + 单影视覆盖（与现有上限同一口径：覆盖值 > 全局配置 > 代码默认）；**留空或 0 表示不限制**。
- 探测入队前统一做大小过滤：`0 < file_size < 下限` 或 `file_size > 上限` 的文件均跳过并计入 `size_filtered`；未知大小维持现有 fail-closed 跳过策略。
- 系统设置页与影视详情页新增下限配置项（与现有上限并排）。
- 修正「Emby 侧删除后无法重新下载」：巡检确认某集在 Emby 中已缺失（用户删除）后，不再把该集永久锁死；在既有宽限轮次用尽后，自动重置该集记录并重新下载，并设「自动重下」次数上限兜底，超过上限后转为终态并保留人工重试入口。

## Capabilities

### New Capabilities
- `media-size-filter`: 影视资源大小限制能力——按单集/电影分设上下限，全局默认 + 单影视覆盖，配置留空或 0 表示不限制；巡检入队前按该策略过滤资源文件。

### Modified Capabilities
- `media-pipeline`: 新增「Emby 侧删除后自动重新下载」行为——确认 Emby 缺失后解除该集防重记录并按上限自动重下，不再永久跳过。
- `media-detail-ui`: 影视详情页的大小设置新增「最小大小」输入项，与现有最大大小并排展示与保存。

## Impact

- 后端配置与 API：`backend/app/config.py`（默认值）、`backend/app/routers/settings.py`（可写白名单）、`backend/app/models/__init__.py`（media 覆盖列）、`backend/app/routers/media.py`（media patch 字段与 DTO）。
- 后端巡检：`backend/app/tasks/scan.py`（`_size_limits` / `_limit_gb` 扩展为上下限、入队循环大小过滤、`_resolve_done_states` 自动重下）。
- 数据库：`download_queue` 新增「自动重下次数」列（alembic 迁移）。
- 前端：`frontend/src/config/settingsMeta.ts`、`frontend/src/types/index.ts`、`frontend/src/views/MediaDetailView.vue`（详情页最小值输入）、设置页沿用既有元数据驱动渲染。
- 不改动：容量准入（`pipeline-admission`）、转存/下载/刮削/入库状态机（`pipeline-transfer`）、aria2 与 cloudSaver 交互、Emby 侧数据。
- 测试：`backend/tests` 巡检与队列相关用例；前端 `MediaDetailView.test.ts` / `SettingsView.test.ts`。
