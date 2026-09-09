## Why

影视下载主流程当前依赖「每部影视独立巡检冷却 + 同步探测 promote + 并发数准入」的复杂链路，导致任务队列与下载队列频繁出现「迟迟不开始任务」的卡驻问题（GID 来源校验 fail-closed 整批停摆、探测静默期、冷却不一致等）。主流程参照 n8n「影视下载.json」但细节多处走偏，且 NasTools 转移完成后仅靠轮询确认入库、确认延迟不可控。

## What Changes

- **统一定时巡检**：移除每部影视独立 `scan_interval_minutes` 冷却逻辑，改为全局统一巡检间隔（默认 60 分钟，system_config 可调），每次巡检跑全部影视（替换 B 定时按 `last_scan_at` 到期过滤的机制）。
- **任务队列 = 巡检队列**：巡检只负责「搜索缺失集并收集转存所需信息（share_code/stoken/fids 等）」，落到 TaskQueue 即视为搜集完毕。任务队列展示简化为扁平的「影视名 - SxxExx」+ 任务状态列表，不再展示影视下的子集树；完成即剔除。
- **下载队列容量准入**：巡检队列每产生任务即触发下载队列按 FIFO 顺序取件，容量判断为「任务文件大小 + 在途下载文件 ≤ 网盘最大容量」才准入转存；不足则置等待队列（quota_wait），空间释放后按序继续。
- **格式化名称时机调整**：`download_name` 格式化从「推送 aria2 时」挪到「转存完成后」——转存成功落盘后即按规范化规则改名，后续下载/入库沿用该名。
- **转移 + 入库闭环**：下载完成（aria2 完成 → nastools webhook）→ NasTools 转移 → `transfer.finished` 时**主动调用 Emby 扫描媒体库** → 确认入库 done → 删除夸克中转文件、释放容量 → 继续消费下载队列排队任务（重复容量判断）。
- **修复卡驻根因**：排查并修复任务队列/下载队列迟迟不开始的具体卡点（含 GID 校验 fail-closed 误伤、探测静默、冷却不一致等）。
- **BREAKING**：任务队列前端从「影视分组树」改为扁平任务列表；`scan_interval_minutes` 每影视独立间隔语义废弃（保留全局间隔）。

## Capabilities

### New Capabilities
- `media-pipeline`: 影视下载主流程：统一巡检调度、TaskQueue（巡检队列）搜集缺失集信息、DownloadQueue 容量准入与转存下载、完成后剔除、排队续跑
- `pipeline-transfer`: NasTools 转移与入库闭环：webhook 完成信号、Emby 媒体库扫描触发、入库确认与容量释放
- `pipeline-admission`: 下载队列容量预算准入（在途 + 新任务 ≤ 网盘容量），quota_wait 排队与唤醒

### Modified Capabilities
<!-- 无既有能力被修改：项目 specs 目录尚为空（首次建立 specs）。 -->

## Impact

- **backend**: `app/tasks/scan.py`（巡检调度、enqueue 简化）、`app/tasks/transfer.py`（容量准入、格式化时机、GID 校验）、`app/tasks/library_check.py` 与 `app/routers/nastools_notify.py`（Emby 扫描触发）、`app/scheduler.py`（统一 tick）
- **frontend**: `QueueView.vue`（扁平任务列表，移除影视子集树）、`utils/format.ts`、`types/index.ts`（缩减契约）、设置项 `scan_interval_minutes` 展示
- **infra/docs**: 设计文档 `docs/影视下载两队列重设计.md` 需同步修订；可能伴随 schema 调整（删除/弃用字段）
- **tests**: test_scan_*、test_transfer.py、test_library_check.py、test_queue.py 需适配新语义