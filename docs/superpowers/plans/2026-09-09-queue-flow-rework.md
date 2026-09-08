# queue-flow-rework 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 重构影视下载主流程：统一全局巡检 → 巡检产出缺失集落 TaskQueue → 下载队列 FIFO 容量准入 → 转存后格式化名称 → NasTools 转移 → Emby 全库 Refresh → 入库确认 → 释放容量续跑；修复「任务/下载队列迟迟不开始」卡驻根因。

**Architecture:** 保持两队列（TaskQueue=巡检结果队列 / DownloadQueue=执行队列）架构。巡检侧收敛为「统一调度 + 搜集缺失集转存凭据落 TaskQueue」；下载侧以网盘容量为唯一准入约束（已用+在途+新任务≤容量），事件触发消费 + 定时 job 兜底；转移完成后调用 Emby `POST /Library/Refresh` 全库扫描，`library_check` 轮询确认入库。

**Tech Stack:** Python 3.12 + FastAPI + SQLAlchemy(async) + APScheduler；Vue3 + Element Plus + Pinia；pytest。

**Spec:** `docs/openspec/changes/queue-flow-rework/specs/{media-pipeline,pipeline-admission,pipeline-transfer}/spec.md`、`docs/superpowers/specs/2026-09-09-queue-flow-rework-design.md`

**Plan 文件头元数据：**
```yaml
---
change: queue-flow-rework
design-doc: docs/superpowers/specs/2026-09-09-queue-flow-rework-design.md
base-ref: 11dd1beadebbbc40a109cbaf1702dd7948ce8e00
---
```

## Global Constraints

- 产物评论/提交信息语言：中文（Comet 配置 language=zh-CN）；type 用 Conventional Commits（feat/fix/refactor/docs/chore），描述中文。
- Python: `backend/app/`，测试 `backend/tests/`（pytest 命令：`cd backend && python -m pytest tests/<file> -q`）。
- 前端：`frontend/src/`，构建命令 `npm run build`（outDir=backend/static）。
- 不破坏现有 CAS 并发协议（pending→transferring 条件更新）；不删除存量列（`scan_interval_minutes`、`silent_until` 字段保留但语义废弃）。
- 所有异步 DB 写操作通过 `async_session()`，时间源 `_now()`（naive UTC）。
- 每任务结束必须运行相关测试并通过后再提交。
- commit 粒度：每个任务一个 commit；提交前 `git add` 只含本任务文件。

---

### Task 1: 统一巡检调度（移除 per-media 冷却）

**Files:**
- Modify: `backend/app/tasks/scan.py`（`scan_all_media` ~L1287、`scan_all_media_job` ~L1262）
- Test: `backend/tests/test_scan_baseline.py`、`backend/tests/test_scan_run_phases.py`

**Interfaces:**
- Consumes: `Media.status.in_(("tracking","downloading"))`；`settings.SCAN_INTERVAL_MINUTES`；`system_config "scan_interval_minutes"`。
- Produces: `scan_all_media(force: bool = False)` —— 不再按 per-media `last_scan_at + interval` 到期过滤，遍历全部影视；全局间隔仅由 job 触发周期控制。

- [x] **Step 1: 写失败测试**：在 `test_scan_baseline.py` 增加用例「未到 per-media 冷却也巡检」：创建 media 记 `last_scan_at=now`，调用 `scan_all_media()`，断言该 media 仍被巡检（mock `_scan_one` 被调用）。
- [x] **Step 2: 运行确认失败**：`cd backend && python -m pytest tests/test_scan_baseline.py -q -k interval`
- [x] **Step 3: 修改实现**：删除 `scan_all_media` 内 `last_scan_at + timedelta(minutes=interval) > now: continue` 到期过滤分支及 `_scan_interval_minutes` 使用点（保留字段兼容读取）。`scan_all_media` 直接遍历全部 tracking/downloading 影视调用 `scan_media`。
- [x] **Step 4: 更新受影响测试**：`test_scan_run_phases.py` 中依赖冷却跳过的断言按新语义调整（不再跳过）。运行全量 `test_scan_baseline.py` + `test_scan_run_phases.py` 通过。
- [x] **Step 5: 前端隐藏设置项**：`frontend/src/config/settingsMeta.ts` 删除或标注 `scan_interval_minutes`（该键在 `MediaPatch` 保留兼容），`frontend/src/types/index.ts` 的 `MediaPatch.scan_interval_minutes` 改为注释废弃。运行 `npm run build` 通过。
- [x] **Step 6: Commit**

```bash
git add backend/app/tasks/scan.py backend/tests/test_scan_baseline.py backend/tests/test_scan_run_phases.py frontend/src/config/settingsMeta.ts frontend/src/types/index.ts
git commit -m "refactor(scan): 巡检改为全局统一调度，移除每影视冷却过滤"
```

### Task 2: enqueue 只写 TaskQueue（移除同步 promote 双写）

**Files:**
- Modify: `backend/app/tasks/scan.py`（enqueue→`_enqueue`/promote 双写段 ~L950-1049）
- Test: `backend/tests/test_scan_baseline.py`、`backend/tests/test_scan_run_phases.py`

**Interfaces:**
- Consumes: 巡检搜索/匹配结果（media_id, episode_key, file_name, file_size, share_code, payload转存凭据）。
- Produces: `_enqueue(...)` → 只写 `TaskQueue(status='ready', ...)`，**不再**在同事务插入 `DownloadQueue(pending)`；返回 `'enqueued'/'existing'/'conflict'`。巡检产生的任务随后由下载队列取件（Task 3）。

- [x] **Step 1: 写失败测试**：在 `test_scan_baseline.py` 增加用例「巡检入队只写 task_queue」：跑 `_scan_one` 或 enqueue 后断言 `task_queue` 有该键行、`download_queue` 无该键行。
- [x] **Step 2: 运行确认失败**：`cd backend && python -m pytest tests/test_scan_baseline.py -q -k enqueue`
- [x] **Step 3: 修改实现**：enqueue 段删除 `tx.add(DownloadQueue(...))` 分支；`TaskQueue` 写入时 `status="ready"`（凭据收集完毕），保留 `probe_attempt=0`。同步更新 `_scan_one` 结果统计语义（`enqueued` 计数对齐新行为）。
- [x] **Step 4: 适配测试**：`test_scan_run_phases.py` 中「enqueue 后 download_queue 存在」的断言改为断言 task_queue。全量运行 `test_scan_baseline.py` + `test_scan_run_phases.py` 通过。
- [x] **Step 5: Commit**

```bash
git add backend/app/tasks/scan.py backend/tests/test_scan_baseline.py backend/tests/test_scan_run_phases.py
git commit -m "refactor(scan): 巡检入队只写 task_queue，移除同步 promote 双写"
```

### Task 3: 移除 unmatched 静默机制

**Files:**
- Modify: `backend/app/tasks/scan.py`（`_mark_unmatched` 静默逻辑 ~L1076-1138、`scan_media(manual)` 相关分支）
- Test: `backend/tests/test_scan_silent_filter.py`

**Interfaces:**
- Consumes: 现有 `_mark_unmatched` 调用点。
- Produces: 缺失集搜索失败后**不再**写 `unmatched` 状态或 `silent_until`；本轮跳过、下轮巡检自然重试。`TaskQueue` 状态集收敛为 `pending/ready/error/done`。

- [x] **Step 1: 写失败测试**：改写 `test_scan_silent_filter.py`：断言静默过滤不再生效（缺失集可在下轮巡检重新入队），`silent_until` 不被写入。
- [x] **Step 2: 运行确认失败**：`cd backend && python -m pytest tests/test_scan_silent_filter.py -q`
- [x] **Step 3: 修改实现**：删除/短路 `_mark_unmatched` 中写 `status="unmatched"+silent_until` 的分支（函数保留为空操作或直接移除调用点）；`probing` 状态不再产生。
- [x] **Step 4: 全量运行** `tests/test_scan_silent_filter.py` + `test_scan_baseline.py` + `test_scan_run_phases.py` 通过。
- [x] **Step 5: Commit**

```bash
git add backend/app/tasks/scan.py backend/tests/test_scan_silent_filter.py
git commit -m "refactor(scan): 移除 unmatched 静默机制，失败下轮巡检重试"
```

### Task 4: 下载队列从 TaskQueue FIFO 取件生成 pending

**Files:**
- Modify: `backend/app/tasks/transfer.py`（新增 `_fetch_from_task_queue`，接入 `_admit_batch`）
- Test: `backend/tests/test_queue.py`、`backend/tests/test_media_two_queue.py`

**Interfaces:**
- Consumes: `TaskQueue(status='ready')` 行（含完整转存凭据快照）。
- Produces: `async def _fetch_from_task_queue(num: int = 10) -> int` —— 按 `(created_at, id)` FIFO 取 TaskQueue(ready) 中「尚无同键 DownloadQueue 行」的任务，拷贝转存凭据生成 `DownloadQueue(status='pending')`，同事务把 TaskQueue 行置 `status='done'`（源行终态，防重复取件）；返回生成行数。

- [x] **Step 1: 写失败测试**：在 `test_queue.py` 增加用例「FIFO 取件生成 download_queue」：插入多条 TaskQueue(ready)，调用 `_fetch_from_task_queue()`，断言按 created_at 顺序生成 DownloadQueue pending 行、TaskQueue 源行置 done、已有 DQ 同键行被跳过。
- [x] **Step 2: 运行确认失败**：`cd backend && python -m pytest tests/test_queue.py -q -k fetch_from_task_queue`
- [x] **Step 3: 修改实现**：在 `transfer.py` 新增 `_fetch_from_task_queue`（复用 Task 2 的 TaskQueue 快照字段：share_code/stoken/pkg/fids/fid_tokens/folder_id/file_name/file_size → DQ 同名字段；`download_name` 暂不填，Task 7 转存后生成）。在 `_admit_batch` 阶段 1 的 `has_pending` 检查后调用它（有 ready 任务先取件生成 pending 再准入）。
- [x] **Step 4: 适配测试**：`test_media_two_queue.py` 同步语义后运行全量 `test_queue.py`+`test_media_two_queue.py` 通过。
- [x] **Step 5: Commit**

```bash
git add backend/app/tasks/transfer.py backend/tests/test_queue.py backend/tests/test_media_two_queue.py
git commit -m "feat(transfer): 下载队列按 FIFO 从 task_queue 取件生成 pending 任务"
```

### Task 5: GID 来源校验降级（告警 + 跳过本轮）

**Files:**
- Modify: `backend/app/tasks/transfer.py`（`_admit_batch` GID 校验段 ~L1258-1297）
- Test: `backend/tests/test_transfer.py`

**Interfaces:**
- Consumes: `aria2.client.tell_active()/tell_waiting()`、`DownloadQueue.aria2_gid`。
- Produces: 陌生 aria2 任务 → `_record_alert(category="gid", ...)` 后返回（跳过本轮），不再作为整批停摆原因。行为从 fail-closed 改为「告警 + 本轮跳过 + 下轮续跑」。

- [x] **Step 1: 写失败测试**：在 `test_transfer.py` 修改 GID 校验用例：断言陌生任务时 `_admit_batch` 返回（非抛错挂起），且后续轮次在陌生任务消失后可继续准入。
- [x] **Step 2: 运行确认失败**：`cd backend && python -m pytest tests/test_transfer.py -q -k gid`
- [x] **Step 3: 修改实现**：GID 校验循环中命中陌生 gid 时保留 `_record_alert` + `return`（跳过本轮），移除「视为整批失败停摆」的路径注释与 fail-closed 语义；逻辑主体不变。
- [x] **Step 4: 全量运行** `tests/test_transfer.py` 通过。
- [x] **Step 5: Commit**

```bash
git add backend/app/tasks/transfer.py backend/tests/test_transfer.py
git commit -m "refactor(transfer): GID 来源校验降级为告警+跳过本轮，不再整批停摆"
```

### Task 6: 事件触发下载队列消费 + 容量释放续跑

**Files:**
- Modify: `backend/app/tasks/scan.py`（enqueue 成功后触发）、`backend/app/tasks/library_check.py`（done 后触发）、`backend/app/tasks/__init__.py` 或事件 helper
- Test: `backend/tests/test_capacity.py`、`backend/tests/test_capacity_alert.py`

**Interfaces:**
- Consumes: `_fetch_from_task_queue`（Task 4）、`_admit_batch`（既有）、下载完成/入库 done 事件。
- Produces: `async def trigger_transfer_consume() -> None` —— 调度一次下载队列消费尝试（内部调 `_fetch_from_task_queue` + `_admit_batch` 有界循环）；供 scan enqueue 成功、library done、quota 释放时 fire-and-forget 调用（复用 `_background` 强引用集合模式）。

- [x] **Step 1: 写失败测试**：在 `test_capacity.py` 增加用例「入库完成释放容量后触发续跑」：置一个 quota_wait 行、mock 容量充足，调用消费入口，断言 quota_wait → pending → 被取件。
- [x] **Step 2: 运行确认失败**：`cd backend && python -m pytest tests/test_capacity.py -q -k resume`
- [x] **Step 3: 修改实现**：新增消费触发 helper（并发安全：`asyncio.Lock` 防重入；单 worker）。在 Task 2 的 enqueue 成功路径与 `library_check._finalize_done` 成功后调用它；`transfer.py` 现有 `process_transfer_queue_job`（每分钟兜底）保持不变。
- [x] **Step 4: 全量运行** `tests/test_capacity.py`+`test_capacity_alert.py`+`test_library_check.py` 通过。
- [x] **Step 5: Commit**

```bash
git add backend/app/tasks/scan.py backend/app/tasks/library_check.py backend/app/tasks/transfer.py backend/tests/test_capacity.py
git commit -m "feat(queue): 巡检入队与入库完成事件触发下载队列消费，容量释放续跑"
```

### Task 7: 转存后格式化名称（下载名称后置生成 + 幂等）

**Files:**
- Modify: `backend/app/tasks/scan.py`（移除 enqueue 时生成 download_name）、`backend/app/tasks/transfer.py`（`_transfer_chain`/`_commit_downloading` 转存落盘后生成）
- Test: `backend/tests/test_transfer.py`

**Interfaces:**
- Consumes: `_format_download_name(file_name, title, media_type, episode_key)`（transfer.py L101 既有）；`_get_link_wait_visible(rename_to=...)`。
- Produces: `download_name` 在**转存链 `_get_link_wait_visible` 落盘可见后**生成：`dq.download_name` 为空时按 `_format_download_name` 计算并落库（CAS 防重写），后续 aria2 `out`/`quark_path`/`local_path` 沿用。重试幂等：已生成则跳过。

- [x] **Step 1: 写失败测试**：在 `test_transfer.py` 修改命名相关用例：断言 `download_name` 仅在转存落盘成功后写入 DQ（转存前 DQ 行 `download_name` 为 NULL）；重试路径不重复改名。
- [x] **Step 2: 运行确认失败**：`cd backend && python -m pytest tests/test_transfer.py -q -k naming`
- [x] **Step 3: 修改实现**：
  1. `scan.py` enqueue 段删除 `download_name` 计算与落库（Task 2 后此处已无 DQ 写入，同步移除变量）。
  2. `transfer.py _transfer_chain`：`_get_link_wait_visible(file_name, ..., rename_to=download_name)` 前，若 `download_name` 为空 → 查 media.title/type → `_format_download_name` 生成 → CAS 更新 DQ (`WHERE status='transferring'`) 落 `download_name`；此时 quark 文件以该名改名。
  3. 失败回退 `_fail_transfer` 清理用最终名（沿用 `final_quark_path` 语义）。
- [x] **Step 4: 全量运行** `tests/test_transfer.py`+`test_fix_p0_recovery_cleanup_transfer.py` 通过。
- [x] **Step 5: Commit**

```bash
git add backend/app/tasks/scan.py backend/app/tasks/transfer.py backend/tests/test_transfer.py
git commit -m "refactor(transfer): 下载名称改为转存落盘后格式化，重试幂等"
```

### Task 8: Emby 全库 Refresh 接入

**Files:**
- Modify: `backend/app/services/emby.py`（新增 `refresh_library`）、`backend/app/tasks/library_check.py`（scrape 完成后触发）、`backend/app/routers/nastools_notify.py`（transfer.finished 后触发）
- Test: `backend/tests/test_emby_series_status.py`、`backend/tests/test_nastools_notify.py`

**Interfaces:**
- Consumes: `_base_url()`/`_serialized_headers()`/认证（emby.py 既有内部结构，按需复用 `_get`/`httpx` 模式）。
- Produces: `async def refresh_library() -> None` — `POST {base}/Library/Refresh`（管理端认证，Admin 角色），成功/失败均返回；失败抛 `EmbyUnavailable`（调用方降级）。`trigger_emby_refresh()` fire-and-forget helper：互斥锁防并发全库扫描，调用 `refresh_library()` 失败仅告警（轮询兜底）。

- [x] **Step 1: 写失败测试**：在 `test_emby_series_status.py` 增加用例：mock `httpx` 断言请求 `POST .../Library/Refresh` 且带认证头；非 2xx 抛 `EmbyUnavailable`。
- [x] **Step 2: 运行确认失败**：`cd backend && python -m pytest tests/test_emby_series_status.py -q -k refresh`
- [x] **Step 3: 修改实现**：
  1. `emby.py` 新增 `refresh_library()`（复用 `_base_url`/token 逻辑；`timeout` 放宽到 60s——全库扫描异步返回）。
  2. `library_check.py` 新增 `trigger_emby_refresh()`（互斥锁 + fire-and-forget），在 `_scrape_impl` 成功推进 scrape→library 后调用。
  3. `nastools_notify.py` `_handle_transfer_finished` 成功推进后同样调用 `trigger_emby_refresh()`。
- [x] **Step 4: 全量运行** `test_emby_series_status.py`+`test_nastools_notify.py`+`test_library_check.py` 通过。
- [x] **Step 5: Commit**

```bash
git add backend/app/services/emby.py backend/app/tasks/library_check.py backend/app/routers/nastools_notify.py backend/tests/test_emby_series_status.py
git commit -m "feat(emby): 转移完成后触发 Emby 全库 Refresh 加速入库"
```

### Task 9: 后端 queue API 扁平任务列表 + 完成剔除

**Files:**
- Modify: `backend/app/routers/queue.py`（`list_queue`/`_list_tree`），`backend/app/routers/media.py`（如引用树结构）
- Test: `backend/tests/test_queue.py`

**Interfaces:**
- Consumes: `DownloadQueue`、`TaskQueue`、`Media`。
- Produces: `GET /api/queue` → 扁平列表，每行 `{id, media_id, title, episode, status, node, file_name, file_size, updated_at, enqueued_at}`；**终态（done/failed/skipped）不返回**；排序按最新 updated_at 倒序（或保留 FIFO 语义）。`type=download` 分支保留现有扁平 DQ 列表。

- [x] **Step 1: 写失败测试**：在 `test_queue.py` 改写 list 用例：断言返回扁平行（含 title/episode/status）、终态行被剔除、`type=download` 仍返回 DQ 全量（含终态可选）。
- [x] **Step 2: 运行确认失败**：`cd backend && python -m pytest tests/test_queue.py -q -k list`
- [x] **Step 3: 修改实现**：`list_queue` 主分支改查扁平合并视图：`TaskQueue`(pending/ready/error) ∪ `DownloadQueue`(活跃态：pending/transferring/downloading/scrape/library/quota_wait)，join Media 取 title，终态剔除；返回扁平 dict 列表。保留 `limit/offset` 分页。
- [x] **Step 4: 适配前端 API 契约引用**：确认 `frontend/src/types/index.ts` QueueMediaTask 兼容扁平行（保留 `[key:string]:unknown`）。全量运行 `test_queue.py` 通过。
- [x] **Step 5: Commit**

```bash
git add backend/app/routers/queue.py backend/tests/test_queue.py
git commit -m "refactor(queue): 任务队列 API 改为扁平列表并剔除终态"
```

### Task 10: 前端 QueueView 扁平化改造

**Files:**
- Modify: `frontend/src/views/QueueView.vue`、`frontend/src/stores/queue.ts`、`frontend/src/types/index.ts`、`frontend/src/utils/format.ts`、`frontend/src/api/index.ts`

**Interfaces:**
- Consumes: Task 9 扁平 API；既有 `listDownloadQueueApi`/容量/暂停接口。
- Produces: QueueView 任务 tab 以扁平列表渲染「影视名 - SxxExx」+ 状态标签（待取件/下载中/等待容量/异常/完成即消失）+ 操作（取消/跳过/置顶/重试/手动入队显隐调整）；移除影视分组树、巡检伪行、探测聚合、子集详情树相关代码路径。

- [ ] **Step 1: 重构 store/types**：`queue.ts` 移除 `normalizeQueueTree` 树归一化，直接存扁平行；`types/index.ts` 缩减 `QueueMediaTask/QueueChildTask/ScanTask*`（保留 `[key:string]:unknown` 兜底），`format.ts` 状态 label 收敛（移除 unmatched 中文标签，保留下载状态）。
- [ ] **Step 2: 重构视图**：`QueueView.vue` 任务 tab 改 `el-table` 扁平行渲染（列：任务/状态/大小/更新时间/操作）；删除分组树展开、巡检详情 drawer、子集流程链、手动探测/加集入口（或按需保留加集）；保留下载队列 tab 与暂停开关。
- [ ] **Step 3: 构建验证**：`cd frontend && npm run build` 通过。
- [ ] **Step 4: 手工核对**：列表显示「影视名 - SxxExx」、完成任务刷新后消失、操作按钮可用。
- [ ] **Step 5: Commit**

```bash
git add frontend/src/views/QueueView.vue frontend/src/stores/queue.ts frontend/src/types/index.ts frontend/src/utils/format.ts frontend/src/api/index.ts
git commit -m "feat(frontend): 任务队列列表扁平化展示，完成即剔除"
```

### Task 11: 集成验证（全量测试 + 构建）

**Files:**
- Test: `backend/tests/`（全部）、`cd frontend && npm run build`

**Interfaces:**
- Consumes: Task 1-10 全部实现。

- [ ] **Step 1: 全量后端测试**：`cd backend && python -m pytest tests/ -q` 全部通过。
- [ ] **Step 2: 前端构建**：`cd frontend && npm run build` 通过。
- [ ] **Step 3: 静态检查**：`grep -rn "unmatched\|silent_until" backend/app | grep -v tests` 验证运行代码无残留静默逻辑（字段定义/迁移除外）。
- [ ] **Step 4: 手动端到端演练清单**（记录到 runbook 或团队验证）：添加影视 → 统一巡检产出缺失集落任务队列 → 下载队列 FIFO 容量准入 → 转存后格式化 → 下载 → nastools 转移 → Emby 全库 Refresh → 入库 done → 释放容量续跑。
- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "test(queue): 全量测试与构建通过，端到端演练验证"
```

---

## 自检记录（writing-plans self-review）

- **Spec 覆盖**：media-pipeline（统一定时巡/落任务队列/扁平展示/完成剔除/FIFO 取件→T1,T2,T4,T9,T10）、pipeline-admission（容量准入/quota_wait 排队/转存后格式化→T4,T6,T7）、pipeline-transfer（下载完成转移/Emby 扫描/入库确认与容量释放→T6,T8）全覆盖。
- **占位符**：无 TBD/TODO；每步含文件、测试命令与验证。
- **类型一致性**：`_fetch_from_task_queue`、`trigger_transfer_consume`、`refresh_library`、`trigger_emby_refresh` 跨任务签名统一（T4→T6、T8）。