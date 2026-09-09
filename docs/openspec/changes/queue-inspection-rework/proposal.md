## Why

任务队列存在多个使用问题：名称「任务队列」语义不清（实际是巡检探测层）；列表字段不全（缺影视名称/SxxExx/分享码明文/大小/更新时间直观展示）；大小显示错误（每行都等同第一个文件大小）；分页默认排序不符合预期（需创建时间正序）；下载队列分享码未明文且不可点击跳转夸克；列表需「展开更多」才能看全部；且存在「入库确认」状态迟迟不入库的问题，需调查修复。

## What Changes

- **任务队列改名「巡检队列」**：原任务队列 Tab 更名为巡检队列，语义聚焦「搜索到资源即完成」的巡检目标
- **巡检队列字段补充**：显示影视名称、影片信息（SxxExx）、分享码（明文）、状态、大小、更新时间
- **大小真实值**：巡检队列与下载队列的大小展示每个任务的真实文件大小，不再全部等同第一个文件（与 episode-status-cache 的大小修正同源，此处聚焦队列行）
- **分页与排序**：巡检队列、下载队列均分页展示，默认按创建时间从小到大（升序）
- **下载队列分享码**：明文展示，点击可跳转到夸克分享地址
- **巡检目标收窄**：巡检队列目标 = 搜索到资源即完事，其余交给下载队列；任务队列展示一个「完成」即可
- **列表默认全显**：列表默认展示全部条目，去掉「下拉/展开更多」交互
- **修复入库确认迟迟不入库**：调查并修复 download_queue status='library' 长期不 finalize 的问题

## Capabilities

### New Capabilities
- `queue-inspection-display`: 巡检队列（原任务队列）的改名、字段展示、分页排序、分享码明文与跳转、列表全显能力

### Modified Capabilities
- `media-pipeline`: 巡检队列目标收窄（搜索到资源即完成，后续交下载队列）；入库确认（library 节点）正常完成不入库卡死

## Impact

- 前端：`frontend/src/views/QueueView.vue`（Tab 改名/字段/分页排序/分享码链接/去展开更多）、`frontend/src/stores/queue.ts`、`frontend/src/api`、`frontend/src/types`（QueueTaskItem/DownloadQueueItem 契约）、`frontend/src/utils/format.ts`
- 后端：`backend/app/routers/queue.py`（分页/排序/分享码字段）、`backend/app/tasks/library_check.py`（入库确认卡死根因修复）、`backend/app/tasks/transfer.py`
- 依赖：episode-status-cache 的大小修正原则（真实 file_size）；docker-timezone 的时间展示（分页时间字段东八区展示）
- 无数据库迁移（可能仅数据修复/状态修正逻辑）
