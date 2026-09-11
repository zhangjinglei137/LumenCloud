# Tasks: queue-size-and-progress

## 1. 后端：精确大小来源

- [x] 1.1 核查队列大小来源：routes/queue.py 列表接口与 tasks/scan.py `_enqueue` 大小估算/回填逻辑，确认真实大小来源（转存/下载记录、cloudSaver 分享文件大小、aria2）与 size_estimated 判定
- [x] 1.2 提升精确大小获取：cloudSaver 搜索/分享信息解析带出真实文件大小；下载完成后将真实大小回填 file_size 并清除估算标记，验证新任务 default 为精确值、旧任务可回填
- [x] 1.3 补充后端测试：精确大小优先、估算兜底标注、下载后回填，验证 `pytest` 通过

## 2. 前端：大小与进度展示

- [x] 2.1 frontend/src/utils/format.ts `formatFileSize` 语义调整：estimated=true 且无真实值时保留「约」前缀，否则显示精确值，验证调用处输出符合新语义
- [x] 2.2 QueueView.vue 巡检队列大小列移除无条件「约」前缀，验证展示精确大小
- [ ] 2.3 QueueView.vue 下载队列拆列：进度（进度条 + 速度）与文件大小独立展示区域/单元格，验证下载中行进度与大小分区清晰、非下载中行仅大小
- [ ] 2.4 前端测试补充：formatFileSize 新语义、下载队列分区渲染，验证 `vitest` 通过

## 3. 集成验证

- [ ] 3.1 `npm run build` 通过；本地起服查看巡检/下载队列展示符合预期