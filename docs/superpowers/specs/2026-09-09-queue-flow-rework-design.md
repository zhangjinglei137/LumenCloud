---
comet_change: queue-flow-rework
role: technical-design
canonical_spec: openspec
archived-with: 2026-09-09-queue-flow-rework
status: final
---

# Design Doc: queue-flow-rework — 影视下载主流程重构

日期: 2026-09-09
规格: 见 `<classic-change-dir>/specs/{media-pipeline,pipeline-admission,pipeline-transfer}/spec.md`

## 1. 目标与非目标

**目标**
- 修复任务队列 / 下载队列「迟迟不开始任务」的卡驻根因（同步探测静默、GID 校验整批停摆、冷却不一致）
- 主流程收敛为「统一巡检 → 轻量任务队列 → 容量准入下载 → 转移 → Emby 扫描 → 入库 → 续跑」闭环
- 任务队列扁平化展示（影视名 - SxxExx），完成即剔除

**非目标**
- 不重做 TMDB 搜索/匹配算法（复用 scan 搜索能力）
- 不处理海报显示（独立 change: media-poster-proxy）
- 不做多 worker / 分布式改造（保持单 worker + 进程内锁）
- 不改容量 provider 实现

## 2. 架构与数据流

```
[统一巡检] scan_all_media_job (全局间隔, 默认60min, system_config 可调)
   │  遍历全部 tracking/downloading 影视
   ▼
[搜索缺失集] per-media _scan_one（复用搜索/匹配/大小过滤）
   │  命中分享码 → share_info 收集转存凭据 (share_code/stoken/fids/fid_tokens/folder_id/file_name/file_size)
   │  失败 → 本轮记录，下轮巡检重试（无 2 天静默）
   ▼
[落任务队列] TaskQueue.insert(pending→ready)
   │  同键(media_id,episode)已存在/已完成 → 跳过
   │  入队成功后触发下载队列消费尝试（事件驱动）
   ▼
[FIFO 取件] _try_fetch_from_task_queue: TaskQueue(ready, 按 created_at,id) → 生成 DownloadQueue(pending)
   │  （对已有 DQ 行的任务跳过；CAS 防重）
   ▼
[容量准入] 已用 + 在途预留(reserved) + 本任务 ≤ 网盘容量?
   ├─ 满足 → pending→transferring (CAS) → 转存链
   └─ 不足 → quota_wait 排队（wait_since 语义保留）→ 释放后续跑
   ▼
[转存链] cloudSaver save → 等落盘可见 → **立即格式化 download_name（转存后改名）** → alist 直链 → aria2 addUri(out=download_name)
   ▼
[下载中] aria2 downloading → aria2 完成（轮询/回调）
   ▼
[转移] DownloadQueue scraping → _scrape_impl: nastools_sync(force=True) → 推进 library
   ▼
[Emby 扫描] transfer.finished(webhook) 或 scrape 完成后 → POST /Library/Refresh（全库 scan，fail 降级轮询）
   ▼
[入库确认] library_check: find_emby_id 命中 + 非遗漏集 → done
   │  → 删夸克中转 + 释放容量预留
   ▼
[续跑] 触发下载队列消费尝试（重复容量判断，quota_wait 唤醒）
```

## 3. 关键技术决策

### D1 统一巡检（移除 per-media 冷却）

- `scan_all_media` 移除 `last_scan_at + scan_interval_minutes` 到期过滤；遍历全部 `tracking/downloading` 影视。
- 全局间隔 `system_config.scan_interval_minutes`（沿用现有键名与默认 60min），由 `scan_all_media_job` 单点控制触发频率。
- `Media.scan_interval_minutes` 字段保留（兼容读取、不参与调度），设置页隐藏编辑项。

### D2 TaskQueue 状态机简化（移除静默）

- 状态收敛：`pending`（巡检写入）→ `ready`（凭据收集完毕，可下发）→ 下载队列取走生成 DQ 行后标记 `done`（源行清终态）；`error` 用于异常中断诊断。
- **删除**：`probing`（探测并入巡检内一次性完成）、`unmatched` + `silent_until`（2 天静默机制改为下轮巡检重试）。
- 后端 DTO / 前端 format.ts 相应缩减状态集。

### D3 下载队列双轨取件 + 容量准入

- 取件双轨：
  - ① 巡检入队事件 → `_try_fetch_from_task_queue()`：按 FIFO 取 TaskQueue(ready) 中「尚无 DownloadQueue 行」的任务 → 生成 pending 行。
  - ② 已有 pending → 走既有 `_admit_batch` / `_try_admit_one` 容量准入。
- 容量判断：`已用 + reserved(在途) + file_size ≤ quota`（沿用 `capacity.provider.check`，准入段 `_admission_lock` + CAS 保留）。
- quota_wait 排队 / 唤醒 / >24h 告警语义全部保留。

### D4 GID 来源校验降级

- 陌生 aria2 活动/等待任务：不再 `fail-closed` 整批停摆；改为 `_record_alert`（flow_error 通知）+ 跳过本轮（return），下轮 job/事件续跑。
- 防双转存策略保留为「告警 + 手动核查」，不再因一次性误判永久卡死。

### D5 格式化名称时机（转存后）

- `download_name` 生成从 scan 前置改为**转存链落盘成功后**（`_get_link_wait_visible(rename_to=...)` 即现状改名点，规范化语义明确化）。
- 复用 `_format_download_name`；失败重试幂等：改名成功后持久化 `download_name`，重试不再重复改名（`quark_path` 已指向新名）。
- aria2 `out`、`quark_path`、`local_path`、转移、入库全程沿用格式化名。

### D6 Emby 扫描（全库 Refresh）

- 已核实 dev.emby.media 文档：`POST /Library/Refresh` 是官方唯一库级扫描端点（`Starts a library scan`，Admin 认证）；**无按媒体库文件夹单独扫描端点**。
- 语义：`transfer.finished`（webhook）或 scrape 完成后，fire-and-forget 调用 `emby.refresh_library()` → `POST /Library/Refresh` → 随后既有 `library_check` 轮询确认入库。
- 失败降级：扫描调用异常仅告警，`library_check` 轮询（find_emby_id + 非遗漏集 + 超时回退）继续兜底。
- 节流：每次转移完成一次全库 scan；同一时段多集完成合并（扫描在执行器/锁内串行，避免并发多次全库扫描）——复用 `_scrape_lock` 与 `_background_tasks` 模式。

### D7 前端任务队列扁平化

- `GET /api/queue` 返回扁平合并视图（TaskQueue ready/done + DownloadQueue 活跃行），移除影视分组树结构；终态（done/failed/skipped）从展示剔除。
- QueueView：每行「影视名 - SxxExx」+ 状态 + 操作（取消/跳过/置顶/重试），移除子集树展开、巡检伪行、探测聚合计数；保留下载队列 tab。

## 4. 复用既有机制（不重复造轮子）

| 机制 | 复用点 |
|---|---|
| 搜索/匹配/大小过滤 | `_scan_one` 的 check/search/match 阶段 |
| 容量 provider | `capacity.provider.check` + reserved 聚合 |
| 准入原子性 | `_admission_lock` + CAS pending→transferring + 事务级锁行 |
| 转存链 | `_transfer_chain`（save 幂等 / save_attempt_at 兜底 / 失败清理） |
| 刮削/转移 | `scrape_runner` + `nastools_sync(force=True)` |
| 入库确认 | `library_check`（find_emby_id + 非遗漏集 + 超时回退） |
| webhook 加速 | `nastools_notify`（transfer.finished → scrape→library） |
| 通知 | `notifier`（download_complete/flow_error/capacity 告警） |

## 5. 数据模型与契约变更

- `TaskQueue.status` 枚举收敛为 `pending/ready/error/done`（`probing/unmatched` 弃用；`silent_until` 保留列但不再写入）。
- `Media.scan_interval_minutes` 语义废弃（兼容读取）。
- 前端 `QueueNode/TaskQueueStatus` 类型缩减；删除 `ScanRow/childStatusOf/probeCountLine` 等树形逻辑。
- **迁移**：存量 TaskQueue.unmatched 行 → 视为 error/由下轮巡检覆盖；存量 pending/probing 行继续可被取件。

## 6. 风险与对策

- [巡检内并发探测长耗时] → 逐码 500ms 间隔保留（扫描本身串行），失败本轮跳过下轮重试；巡检与下载解耦，互不阻塞。
- [GID 降级放行双转存] → 告警 + 手动核查兜底；仅用户手动强启 n8n 时发生，事件可追溯。
- [全库 Refresh 周期性触发开销] → 扫描入口串行合并（同轮多任务一次扫描）+ 失败可接受（轮询兜底）。
- [TaskQueue 无限积累] → 已生成 DQ 行即置 done，展示剔除；物理清理沿用 cleanup 定时任务。
- [BREAKING: 队列 API 结构变化] → 前端同步改造；后端 API 版本兼容（旧树形字段按需兼容或直接移除，接受一次性迁移）。

## 7. 测试策略

- 后端 pytest：`test_scan_*`（统一巡检、只写 TaskQueue、无静默重试）、`test_transfer.py`（GID 降级、容量准入、命名幂等、转存后改名）、`test_library_check.py`（Emby 扫描触发 + 确认 + 失败降级）、`test_queue.py`（扁平 API 契约、FIFO、完成剔除）、`test_capacity*.py`（排队续跑）。
- 前端：`npm run build`；手工核对扁平列表、状态标签、完成剔除。
- 端到端：添加影视 → 巡检产出缺失集 → FIFO 容量准入 → 转存后格式化 → 下载 → 转移 → Emby 扫描 → 入库 done → 释放容量续跑。

## 8. 迁移与部署

1. 后端改造顺序：调度 → enqueue 简化 → 取件 → 容量准入 → 转存后格式化 → Emby 扫描 → 续跑。
2. 前端：QueueView 扁平化 + types/format 缩减。
3. 存量数据：DownloadQueue pending/quota_wait 自然消费；存量 TaskQueue 按新语义兼容。
4. 回滚：`scan_all_media` 保留按配置开关回退到期过滤分支；前端视图层可回退树形（API 兼容层）。
