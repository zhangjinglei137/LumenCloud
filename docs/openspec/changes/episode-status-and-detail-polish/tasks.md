# Tasks: episode-status-and-detail-polish

## 1. 后端：Emby 已入库集数聚合

- [x] 1.1 services/emby.py 新增查询影视已入库集数能力（按影视关联库/ProviderId 统计已入库 SxxExx 集合），复用 _get/_check_config/错误归一，验证未配置/不可达抛 EmbyUnavailable
- [x] 1.2 services/emby.py 为已入库集数查询加进程内 TTL 缓存，验证重复查询命中缓存且配置变化后失效
- [x] 1.3 routers/media.py 的 _stats 聚合扩展「已有」口径：Emby 已入库集号 ∪ episode_state/download_queue/task_queue 已完成集号（去重），缺失 = TMDB 全集 − 已有，验证现有三表聚合逻辑仍工作且 Emby 维度计入
- [ ] 1.4 routers/media.py 详情接口新增 active_tasks 字段：从 download_queue/task_queue 取进行中状态集（过滤完成/失败/跳过）并附带 season/episode/status/air_date，验证只返回进行中任务
- [ ] 1.5 补充后端测试：已有口径（入库∪已完成去重）、Emby 未配置回退、active_tasks 过滤（终态剔除、未到首播日剔除），验证 `pytest` 通过

## 2. 前端：列表与详情展示

- [ ] 2.1 frontend/src/types/index.ts 扩展 MediaItem/MediaDetail 类型（已有/缺失统计、active_tasks 字段），验证类型契约与后端一致
- [ ] 2.2 MediaListView.vue episodeText 改为「已有 N 缺失 M」文案（可用 available/total 或新增字段换算），验证列表卡展示正确、电影仍回退状态文案
- [ ] 2.3 MediaDetailView.vue 新增「当前进行中任务」区块：基于 detail.active_tasks 渲染集号 + 状态标签（复用 DOWNLOAD_QUEUE_STATUS_MAP），验证仅展示进行中任务、空态正常
- [ ] 2.4 MediaDetailView.vue 状态下拉 el-select 显式设置宽度（min-width），验证选中后文本完整可见
- [ ] 2.5 前端测试补充：episodeText 新文案、active_tasks 区块渲染与空态、下拉宽度，验证 `vitest` 通过

## 3. 集成验证

- [ ] 3.1 `npm run build` 与后端启动无报错，列表/详情页在本地环境正常展示（后端无真实 Emby 时回退不报错）