## 1. 统一巡检调度

- [x] 1.1 修改 scan_all_media：移除 per-media last_scan_at/scan_interval_minutes 到期过滤，改为全局统一间隔遍历全部 tracking/downloading 影视；验证 `test_scan_run_phases.py` / `test_scan_baseline.py` 适配后通过
- [x] 1.2 设置页隐藏 scan_interval_minutes 编辑项，后端 MediaPatch 保留兼容读取但不参与调度；验证 GET /api/media 不再回传该字段或标注废弃

## 2. 任务队列 = 巡检结果队列

- [x] 2.1 调整 enqueue：巡检产出缺失集 + 转存凭据只写 TaskQueue（移除同步双写 DownloadQueue.pending）；验证 `test_scan_*` 入队语义更新后通过
- [x] 2.2 移除 unmatched 长期静默（silent_until 2 天机制收紧为下轮巡检重试）；验证 test_scan_silent_filter 更新后通过
- [x] 2.3 下载队列新增「从 TaskQueue 按 FIFO（created_at,id）取尚未生成 DownloadQueue 的任务 → 生成 pending 行」的取件逻辑；验证 test_media_two_queue / test_queue 覆盖取件顺序与去重

## 3. 容量准入与排队续跑

- [x] 3.1 GID 来源校验从 fail-closed 整批停摆降级为「告警 + 跳过本轮」；验证 test_transfer.py 中 GID 校验用例更新后通过（陌生任务不再永久卡死）
- [x] 3.2 巡检入队 + 下载完成两处事件触发「下载队列消费尝试」（任务产生即触发）；容量判断保留「已用 + 在途 + 新任务 ≤ 容量」，不足 quota_wait 排队；验证 test_capacity.py / test_capacity_alert.py 通过
- [x] 3.3 下载完成释放容量后自动续跑等待队列（重复容量判断）；验证容量释放→续跑集成用例（test_library_check 或新增）

## 4. 转存后格式化名称

- [x] 4.1 将 download_name 生成时机从 scan promote 前置改为「转存链落盘成功后」格式化（复用 _format_download_name），并保证失败重试幂等（同一文件只格式化一次）；验证 test_transfer.py 中命名用例通过
- [x] 4.2 aria2 out / quark_path / 转移 / 入库全程沿用格式化名；验证端到端命名一致性用例（test_fix_p0_recovery_cleanup_transfer 适配）

## 5. 转移 → Emby 扫描 → 入库确认 → 续跑闭环

- [x] 5.1 nastools webhook transfer.finished 后调用 Emby 媒体库扫描接口（限定该影视媒体库）；验证 test_nastools_notify / test_emby_series_status 适配后通过
- [x] 5.2 Emby 扫描失败降级轮询确认（node_attempt 重试与超时回退沿用）；验证 library_check 超时/Rerun 用例通过
- [x] 5.3 入库 done 后删除夸克、释放容量预留并触发下载队列续跑；验证 test_fix_p0_recovery_cleanup_transfer 中「删夸克 + 释放」断言通过

## 6. 前端任务队列扁平化

- [x] 6.1 后端 queue API 返回扁平任务列表视图（TaskQueue + DownloadQueue 合并，终态剔除）；验证 test_queue.py API 契约更新后通过
- [x] 6.2 QueueView 从影视分组树改为扁平列表（影视名 - SxxExx + 状态 + 取消/跳过/置顶），移除子集树与巡检伪行；验证前端构建通过且手工核对列表展示
- [x] 6.3 完成即剔除：列表刷新后终态不显示；验证手工场景（下载完成后行消失）

## 7. 集成验证

- [x] 7.1 全量后端测试通过（pytest backend/tests，401 passed / 6 failed 均 pre-existing 归因 Task 2 legacy），前端 npm run build 通过；证据见 `.comet/verify-evidence.md`
- [ ] 7.2 端到端演练（真实服务联调，移交团队按 runbook 手工演练）：添加影视 → 统一巡检产出缺失集落任务队列 → 下载队列 FIFO 容量准入 → 转存后格式化 → 下载 → nastools 转移 → Emby 扫描 → 入库 done → 释放容量续跑