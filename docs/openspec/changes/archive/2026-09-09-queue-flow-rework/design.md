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