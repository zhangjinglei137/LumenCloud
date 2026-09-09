# Comet Design Handoff

- Change: queue-flow-rework
- Phase: design
- Mode: compact
- Context hash: 1c34010ba024aab41e5b0e814a95f156e6da0309c0b5f07356bf21e6c99058da

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This handoff is a deterministic, source-traceable context pack, not an agent-authored summary.

## docs/openspec/changes/queue-flow-rework/proposal.md

- Source: docs/openspec/changes/queue-flow-rework/proposal.md
- Lines: 1-29
- SHA256: 8ae9c2e5689b16985bc9fb894770161460cba4465e3fc9717a854f66bb65e12e

```md
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
```

## docs/openspec/changes/queue-flow-rework/design.md

- Source: docs/openspec/changes/queue-flow-rework/design.md
- Lines: 1-80
- SHA256: 78dce8f21f7f19294e2b7fface060a3500345fd58df7cf9f72f3c07a6ee72d6e

```md
## Context

现状是「两队列」架构：TaskQueue（巡检/探测层）+ DownloadQueue（执行层），scan 巡检时同步 share_info 探测并自动 promote 双写 download_queue(pending)，transfer 每分钟 tick 消费。用户反馈的故障与需求（见 proposal.md - Why）：任务队列/下载队列「迟迟不开始」、巡检存在每影视独立冷却、任务队列展示过重（影视子集树）、格式化名称时机、Emby 确认入库延迟。主流程对标本项目曾用的 n8n「影视下载.json」。

## Goals / Non-Goals

**Goals**
- 巡检侧收敛为「统一全局调度 + 收集缺失集转存信息落 TaskQueue」，巡检不负责下载推进
- 下载侧以容量为唯一准入约束（在途 + 待下载 ≤ 容量），FIFO 取件，quota_wait 排队
- 命名格式化统一在转存成功落盘后完成，贯穿下载/转移/入库
- 转移完成 → 主动触发 Emby 扫描 → 入库确认 → 释放容量 → 续跑（全事件/信号驱动闭合）

**Non-Goals**
- 不重做 TMDB 搜索/匹配算法本身（保留 scan 搜索能力）
- 不处理海报显示（拆分为 media-poster-proxy change）
- 不做多 worker/分布式部署改造（保持单 worker + 进程内锁现状）
- 不改变网盘容量统计 provider 的实现（容量读取代价由 provider 提供）

## Decisions

### D1: 巡检从「每影视冷却 + 同步探测 promote」改为「统一巡检 + 收集落任务队列」

现状 `scan_all_media` 按 `media.last_scan_at + media.scan_interval_minutes` 到期过滤，且 enqueue 阶段同步做 share_info 探测并直接双写 download_queue。

改为：
- 移除 per-media 到期过滤与 `scan_interval_minutes` 语义；全局 `scan_all_media_job` 按统一间隔（默认 60min，`system_config.scan_interval_minutes` 可调）遍历全部 tracking/downloading 影视。
- 巡检产出「缺失集 + 转存凭据」落 TaskQueue；对已有同键（media_id, episode）TaskQueue/DownloadQueue 已存在或已完成的跳过。
- 不再在巡检内同步 promote 双写 DownloadQueue——DownloadQueue 由**准入端**按序从 TaskQueue 取件生成（事件驱动：巡检每产生新任务触发一次消费尝试；兜底：transfer job 定时 tick）。

> 备选：保留现状同步探测 promote。否决：把探测慢路径留在巡检链路正是「巡检/下载迟迟不开始」与静默期卡驻的温床（探测 500ms/码串行 + unmatched 2 天静默），且任务队列展示被迫承载完整子集树。收敛后任务队列即为轻量「巡检结果队列」。

**关键处理**：探测（share_info 取转存凭据）挪到哪个阶段？
- 决策：**仍在巡检内完成收集**（搜索命中分享码即调 share_info 取凭据落 TaskQueue），因为「缺失集的信息（包含转存需要的信息）落在任务队列上 就算搜集完毕了」是用户明确意图。探测只做单分享码一次，失败不静默重探 2 天——本轮失败记录，下轮巡检重新尝试（无静默态，或静默期收紧为较短周期）。
- 这样 TaskQueue 任务到达 DownloadQueue 时即携带完整凭据，准入转存不再有探测环节。

### D2: 下载队列准入重构——FIFO 取件 + 容量唯一约束

现状 `_try_admit_one` 已实现容量 check（reserved + file_size ≤ quota）与 quota_wait，但取件源是 DownloadQueue.pending；准入前还有 GID 来源校验 fail-closed（非本系统 aria2 活动任务→整批停摆）、/quark 挂载预检等节流。

改为：
- **取件源双轨**：① 巡检入队触发时，从 TaskQueue 按 (created_at, id) FIFO 取「尚未生成 DownloadQueue 行」的任务生成 pending 行；② 已有 pending 行走原容量准入流转。两者共用同一准入（容量判断 + CAS 抢占）。
- **GID 校验降级为警告不阻断**：陌生 aria2 任务不再整批 fail-closed 停摆，改为记录 flow_error 通知 + 跳过该轮（避免一次性误伤永久卡死，下轮续跑）。
- 容量判断口径保持「已用 + 在途预留 + 新任务 ≤ 容量」，quota_wait 排队、唤醒后在途完成时触发续跑。

### D3: 格式化名称时机——转存完成后立即格式化

现状 download_name 在 scan promote 时生成（入库时），transfer 链 `_get_link_wait_visible(rename_to=download_name)` 已实现转存落盘后改名。

决策：**保留并规范化这一时机语义**——转存（cloudSaver save → 落盘可见）成功即按规则格式化名称（剧名 - SxxExx - 第 N 集.ext），后续 aria2 out、quark_path、转移、入库全程沿用该名。将 `download_name` 生成从「scan promote 前置」调整为「转存链成功后置」（避免 share 文件名与格式化名分叉），并保证失败重试时幂等（同一文件名只格式化一次）。

### D4: 转移 → Emby 扫描 → 入库确认 → 续跑闭环

现状已有：nastools webhook（transfer.finished → scrape→library）+ library_check 轮询（Emby find_emby_id 命中 → done + 删夸克）。

新增（用户选择「调用 Emby 扫描媒体库」）：
- `transfer.finished` 或 polling 确认 download 完成 → 触发 NasTools 转移（已有）→ 收到 `transfer.finished` 后，**调用 Emby 媒体库扫描接口**（`POST /emby/Library/Refresh` 或等价，限定该影视所在媒体库）→ 再走既有 library_check 确认逻辑（find_emby_id + 非遗漏集）→ done + 删夸克 + 释放容量 → 触发下载队列续跑。
- Emby 扫描失败不阻断：先轮询兜底再扫，node_attempt 重试与超时回退逻辑沿用。

### D5: 任务队列前端扁平化

QueueView 从「影视分组树（父级 + children 分集 + 巡检伪行）」改为**扁平任务列表**：每行「影视名 - SxxExx / movie:<title>」+ 状态（待取件/下载中/等待容量/完成）+ 操作（取消/跳过/置顶）。完成即从列表剔除（列表数据源不包含 done 终态）。后端 queue API 返回扁平 TaskQueue+DownloadQueue 合并视图；保留 download tab 现状。

## Risks / Trade-offs

- [探测仍留在巡检内，可能单轮耗时变长] → 巡检与下载解耦：巡检慢不阻塞下载推进；探测失败本轮跳过下轮重试，不做 2 天静默。
- [GID 校验从 fail-closed 降级可能放行双转存] → 保留通知告警 + 跳过该轮（非永久停摆）；双转存仅在用户手动强启 n8n 时发生，事件可追溯。
- [TaskQueue 无限积累历史] → 完成/跳过即删除或标记终态由列表过滤；归档策略在 tasks 中明确（删除远比目前保留 2 天静默查询更可控）。
- [Emby 扫描触发 API 契约随 Emby 版本变化] → 扫描调用失败降级为轮询确认，不影响主链路完成。
- [移除 scan_interval_minutes 字段属 BREAKING（前端设置/后端字段）] → migration 中做兼容读取（字段保留但不再参与调度），设置页隐藏。

## Migration Plan

1. 后端：改 `scan_all_media` 调度（移除 per-media 冷却）→ 调整 enqueue 只写 TaskQueue → 新准入取件逻辑 → 转存链格式化时机后置 → Emby 扫描接入 → 续跑触发。
2. 前端：QueueView 扁平化改造，settingsMeta 隐藏 scan_interval_minutes。
3. 存量 DownloadQueue.pending/quota_wait 数据由新准入逻辑自然消费；存量 TaskQueue 数据按新语义（完成剔除）兼容展示。
4. 回滚：保留旧 `scan_all_media` 到期过滤分支由配置开关（`scan_interval_minutes` 兼容读取）控制；前端扁平列表可回退树形（不改 API，仅视图层）。

## Open Questions

- Emby 扫描接口的具体端点/媒体库定位方式（Emby API 版本差异）→ 实现时可适配，不需要改 spec。
- 任务队列历史清理策略（完成多久后从展示剔除/物理删除）→ 默认：终态直接不展示，物理清理沿用现有 cleanup 定时任务。
```

## docs/openspec/changes/queue-flow-rework/tasks.md

- Source: docs/openspec/changes/queue-flow-rework/tasks.md
- Lines: 1-37
- SHA256: d58fdccf1745e64cb4fdcf1ae1f1babf629101da95879e5b7a295a20f5a025d2

```md
## 1. 统一巡检调度

- [ ] 1.1 修改 scan_all_media：移除 per-media last_scan_at/scan_interval_minutes 到期过滤，改为全局统一间隔遍历全部 tracking/downloading 影视；验证 `test_scan_run_phases.py` / `test_scan_baseline.py` 适配后通过
- [ ] 1.2 设置页隐藏 scan_interval_minutes 编辑项，后端 MediaPatch 保留兼容读取但不参与调度；验证 GET /api/media 不再回传该字段或标注废弃

## 2. 任务队列 = 巡检结果队列

- [ ] 2.1 调整 enqueue：巡检产出缺失集 + 转存凭据只写 TaskQueue（移除同步双写 DownloadQueue.pending）；验证 `test_scan_*` 入队语义更新后通过
- [ ] 2.2 移除 unmatched 长期静默（silent_until 2 天机制收紧为下轮巡检重试）；验证 test_scan_silent_filter 更新后通过
- [ ] 2.3 下载队列新增「从 TaskQueue 按 FIFO（created_at,id）取尚未生成 DownloadQueue 的任务 → 生成 pending 行」的取件逻辑；验证 test_media_two_queue / test_queue 覆盖取件顺序与去重

## 3. 容量准入与排队续跑

- [ ] 3.1 GID 来源校验从 fail-closed 整批停摆降级为「告警 + 跳过本轮」；验证 test_transfer.py 中 GID 校验用例更新后通过（陌生任务不再永久卡死）
- [ ] 3.2 巡检入队 + 下载完成两处事件触发「下载队列消费尝试」（任务产生即触发）；容量判断保留「已用 + 在途 + 新任务 ≤ 容量」，不足 quota_wait 排队；验证 test_capacity.py / test_capacity_alert.py 通过
- [ ] 3.3 下载完成释放容量后自动续跑等待队列（重复容量判断）；验证容量释放→续跑集成用例（test_library_check 或新增）

## 4. 转存后格式化名称

- [ ] 4.1 将 download_name 生成时机从 scan promote 前置改为「转存链落盘成功后」格式化（复用 _format_download_name），并保证失败重试幂等（同一文件只格式化一次）；验证 test_transfer.py 中命名用例通过
- [ ] 4.2 aria2 out / quark_path / 转移 / 入库全程沿用格式化名；验证端到端命名一致性用例（test_fix_p0_recovery_cleanup_transfer 适配）

## 5. 转移 → Emby 扫描 → 入库确认 → 续跑闭环

- [ ] 5.1 nastools webhook transfer.finished 后调用 Emby 媒体库扫描接口（限定该影视媒体库）；验证 test_nastools_notify / test_emby_series_status 适配后通过
- [ ] 5.2 Emby 扫描失败降级轮询确认（node_attempt 重试与超时回退沿用）；验证 library_check 超时/Rerun 用例通过
- [ ] 5.3 入库 done 后删除夸克、释放容量预留并触发下载队列续跑；验证 test_fix_p0_recovery_cleanup_transfer 中「删夸克 + 释放」断言通过

## 6. 前端任务队列扁平化

- [ ] 6.1 后端 queue API 返回扁平任务列表视图（TaskQueue + DownloadQueue 合并，终态剔除）；验证 test_queue.py API 契约更新后通过
- [ ] 6.2 QueueView 从影视分组树改为扁平列表（影视名 - SxxExx + 状态 + 取消/跳过/置顶），移除子集树与巡检伪行；验证前端构建通过且手工核对列表展示
- [ ] 6.3 完成即剔除：列表刷新后终态不显示；验证手工场景（下载完成后行消失）

## 7. 集成验证

- [ ] 7.1 全量后端测试通过（pytest backend/tests），前端 npm run build 通过
- [ ] 7.2 端到端演练：添加影视 → 统一巡检产出缺失集落任务队列 → 下载队列 FIFO 容量准入 → 转存后格式化 → 下载 → nastools 转移 → Emby 扫描 → 入库 done → 释放容量续跑
```

## docs/openspec/changes/queue-flow-rework/specs/media-pipeline/spec.md

- Source: docs/openspec/changes/queue-flow-rework/specs/media-pipeline/spec.md
- Lines: 1-52
- SHA256: 02aaf6af561cc7adc5030e767c0c9f96b51962cc0bc7b3bf331658f82f87e88d

```md
## Purpose

为影视下载提供统一调度与任务队列能力：全局定时巡检发现缺失集并搜集转存信息，任务以扁平列表形式呈现候选下载项，完成后自动剔除。

## ADDED Requirements

### Requirement: 全局统一定时巡检

系统 SHALL 按全局统一巡检间隔（默认 60 分钟，system_config 可调）遍历全部 tracking/downloading 影视执行缺集巡检，每次巡检跑全部影视；不得依赖各影视独立的巡检间隔或冷却时长做跳过滤。

#### Scenario: 到点触发全局巡检
- **WHEN** 距上次全局巡检达到配置间隔
- **THEN** 系统对全部 tracking/downloading 影视逐个执行缺集搜索并产出缺失集任务

#### Scenario: 配置全局间隔
- **WHEN** 管理员修改全局巡检间隔配置
- **THEN** 后续巡检按新间隔执行，且无需逐个影视设置

### Requirement: 巡检产出缺失集任务落任务队列

巡检发现缺失集（剧集 SxxExx）或缺失影视（电影）时，SHALL 将缺失集信息（含转存所需信息：分享码、stoken、fids、fid_tokens、folder_id 等）写入任务队列；搜索并收集完资料后，该影视该轮巡检即视为完成，巡检侧不再继续执行转存/下载。

#### Scenario: 巡检发现缺失集并搜集资料
- **WHEN** 巡检搜索到某影视的缺失集且成功收集分享信息
- **THEN** 任务队列新增一条该影视缺失集任务，携带完整转存所需信息，巡检任务标记完成

#### Scenario: 缺失集搜索无结果
- **WHEN** 巡检对某缺失集搜索后未找到可用分享资源
- **THEN** 该缺失集不入任务队列，并在巡检结果中记录未找到源

### Requirement: 任务队列扁平展示与完成剔除

任务队列 SHALL 以扁平列表展示候选任务（每条：影视名 - 集号 + 任务状态），不展示影视下子集树；已完成（入库完成或跳过）的任务 SHALL 从队列移除。

#### Scenario: 展示扁平任务列表
- **WHEN** 用户查看任务队列
- **THEN** 看到扁平的任务列表，每行体现「影视名 - SxxExx」与任务状态，无影视子集树展开

#### Scenario: 完成即剔除
- **WHEN** 某任务完成入库或被跳过
- **THEN** 该任务从任务队列中移除，不再展示

### Requirement: 巡检队列任务供下载队列按序取件

下载队列 SHALL 从巡检队列按 FIFO 顺序取件；巡检每产生新任务 SHALL 触发一次下载队列消费尝试。

#### Scenario: 新任务触发下载队列
- **WHEN** 巡检向任务队列新增一条任务
- **THEN** 系统触发下载队列消费，按序尝试取件

#### Scenario: 任务排队等待
- **WHEN** 下载队列已有多个待取任务
- **THEN** 按入队先后顺序（FIFO）逐一取件处理
```

## docs/openspec/changes/queue-flow-rework/specs/pipeline-admission/spec.md

- Source: docs/openspec/changes/queue-flow-rework/specs/pipeline-admission/spec.md
- Lines: 1-40
- SHA256: 3c9ae7f3d04f03fdd9c66828a0dd1cf352e8fffb7d089c9abd5152ecae2c2223

```md
## Purpose

为下载队列提供容量感知的准入控制：以网盘容量为唯一准入约束，在途下载与新任务合计不超网盘容量的任务才能开始下载，否则进入等待队列，容量释放后按序续跑。

## ADDED Requirements

### Requirement: 容量判断准入

下载队列 SHALL 以「任务文件大小 + 在途下载文件合计 ≤ 网盘最大容量」作为开始下载的唯一准入条件；准入通过后任务进入转存下载流程。

#### Scenario: 容量充足直接下载
- **WHEN** 下载队列取到任务且（任务大小 + 在途下载合计）小于等于网盘容量
- **THEN** 该任务进入转存下载流程

#### Scenario: 容量不足进入等待
- **WHEN** 下载队列取到任务且（任务大小 + 在途下载合计）大于网盘容量
- **THEN** 该任务进入等待状态，不开始下载，按入队顺序排队

### Requirement: 等待队列排队与唤醒

容量不足的任务 SHALL 在等待队列按序排队；当任一在途任务完成释放容量后，系统 SHALL 重新评估等待队列首位任务，容量满足则续跑下载。

#### Scenario: 下载完成释放容量后续跑
- **WHEN** 某下载任务完成并释放容量预留
- **THEN** 系统重新按序评估等待队列，容量满足的任务进入下载流程

#### Scenario: 多个任务排队依序推进
- **WHEN** 连续释放容量
- **THEN** 等待队列按入队顺序逐个推进，直至容量再次不足

### Requirement: 转存完成后再格式化落盘名称

系统 SHALL 在转存成功完成后依据命名规则格式化落盘名称（影视名 - SxxExx），后续下载以该格式化名称落盘；不得在推送 aria2 下载时才临时格式化。

#### Scenario: 转存后即格式化
- **WHEN** 转存成功落盘
- **THEN** 系统立即按规则生成格式化名称并用于后续 aria2 下载

#### Scenario: 格式化名称贯穿下载入库
- **WHEN** 下载任务以格式化名称落盘
- **THEN** 后续下载跟踪、转移、入库确认均沿用该名称，命名全程一致
```

## docs/openspec/changes/queue-flow-rework/specs/pipeline-transfer/spec.md

- Source: docs/openspec/changes/queue-flow-rework/specs/pipeline-transfer/spec.md
- Lines: 1-36
- SHA256: d80ffefe1828c37a1d2027b93022996e0f943a135be7b476fc20df7cbe3cafb3

```md
## Purpose

为影视下载提供转移与入库闭环：下载完成后交给 NasTools 转移整理，以 Webhook 完成信号触发 Emby 媒体库扫描，确认入库后标记完成并释放容量，继续消费下载队列等待任务。

## ADDED Requirements

### Requirement: 下载完成进入转移

下载任务完成后，系统 SHALL 通过 NasTools 完成转移整理；以 nastools Webhook（transfer.finished）或其他等价信号判断转移是否完成。

#### Scenario: 判定转移完成
- **WHEN** 下载任务完成且收到 NasTools 转移完成 Webhook
- **THEN** 系统推进该任务进入入库确认阶段

### Requirement: 转移完成后触发 Emby 扫描

系统 SHALL 在判定转移完成后调用 Emby 媒体库扫描（对应媒体库文件夹），确保新文件被 Emby 及时收录。

#### Scenario: 转移完成触发扫描
- **WHEN** NasTools 报告某影视转移完成
- **THEN** 系统调用 Emby 扫描媒体库接口触发该媒体库刷新

#### Scenario: 扫描失败回退轮询确认
- **WHEN** Emby 扫描调用失败或不可达
- **THEN** 系统保留任务在入库确认状态，由后续轮询兜底再扫描并确认

### Requirement: 入库确认与容量释放

任务在 Emby 确认收录后 SHALL 标记完成，删除夸克中转文件并释放容量预留，随后继续消费下载队列等待任务（重复容量判断流程）。

#### Scenario: 入库成功完成闭环
- **WHEN** Emby 已确认收录该集/该片
- **THEN** 任务标记完成、删除夸克中转、释放容量预留，并触发下载队列继续按序取件

#### Scenario: 入库超时处理
- **WHEN** 任务在入库确认状态停留超过超时阈值仍未被 Emby 收录
- **THEN** 系统按回退策略处理（重试或失败），不永久卡驻队列
```
