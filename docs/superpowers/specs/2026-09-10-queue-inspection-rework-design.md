---
comet_change: queue-inspection-rework
role: technical-design
canonical_spec: openspec
---

# Design Doc: queue-inspection-rework

> 深度技术设计，是对 open 阶段 `design.md` 高层方案的细化。OpenSpec delta spec（`specs/queue-inspection-display/spec.md`）为 canonical spec，本文件不取代它。

## 1. 背景与目标

任务队列存在多个使用问题：命名语义不清、字段不全、大小显示错误、排序不符、分享码不明文不可跳转、需「展开更多」、入库确认卡死。本 change 修复展示与卡死问题，范围 =

- 巡检队列（原任务队列）：改名、字段补全、真实大小、分页排序、去 loadMore
- 下载队列：分享码明文可跳转、真实大小、分页排序
- 入库确认卡死修复（`status='library'` 长期不 finalize）

## 2. 关键现状与根因（勘察结论）

### 2.1 后端 `queue.py`

| 函数 | 现状 | 问题 |
|---|---|---|
| `_list_flat` (queue.py:91-168) | TQ∪DQ 合并、内存排序切片 `updated_at desc, id desc`，返回无 share_code | 排序键不符、全量拉取内存切片、无 share_code |
| `_list_download` (queue.py:171-203) | SQL JOIN Media，`enqueued_at desc, id desc`，**原文返回 share_code（未脱敏）** | 排序不符、**guest 越权**（`GET /queue` 是 `get_current_user`） |

### 2.2 入库确认卡死根因（library_check.py）

- `library_check` 30s 轮询 `status='library'`
- 卡死点：**Emby 命中但遗漏集判定 continue 的分支不经过 `_mark_timeout_if_expired`**——若 Emby 持续返回该集在遗漏集，永不 finalize
- 其余 continue 分支（缺 tmdb_id / find_emby_id 异常 / get_missing 异常）同样不消耗超时窗口，且无明确日志

### 2.3 file_size 同值根因（scan.py）

- cloudSaver share-list 不返回单文件 size → `_walk_share` (scan.py:650-660) 对 size_unknown 文件按「分享总大小/文件数」**均摊估算** → 同分享所有行同值
- 全链路无 aria2 真实下载量回填（`_complete_download`（transfer.py:541-552）不写 file_size）

## 3. 技术设计

### 3.1 后端：队列列表排序、分页、字段（queue.py）

**排序键**（创建时间升序）：
- `_list_flat`：TQ 按 `created_at ASC, id ASC`；DQ 按 `enqueued_at ASC, id ASC`（DQ 无 created_at 列，使用 enqueued_at 作为创建时间语义）。合并后按统一时间键升序 —— 具体实现：两查询各自排序后以时间字段合并（保持与现有内存合并结构兼容），或新增统一时间列方案对比后取优。
- `_list_download`：`enqueued_at ASC, id ASC`

**分页**：
- 接口改为返回 `{"items": [...], "total": N}`，保留 limit/offset 入参（与前端 el-pagination page/pageSize 换算）
- `_list_flat` 需要正确的 total：TQ/DQ 各自查询计数后相加减去去重数；或查询内在 SQL 层排序 + limit/offset + count。实现时先确认两表聚合语义（去重规则：同 (media_id, episode) DQ 活跃行存在时跳过 TQ 行），保证 total 与 items 一致。

**巡检队列行字段**（`_list_flat` 输出）：
- `media_title`：join media.title（现有已查 media_map，补字段即可）
- `episode`、`status`、`file_size`、`updated_at`、`created_at`（排序展示用）
- `share_code`：**仅 admin 返回明文**；guest 不返回该字段（修复现有越权口子）
- `size_estimated`：布尔，标注该行 file_size 为估算值（前端加「约」前缀）。来源：scan 已写入 `f["size_estimated"]`，需核查其是否落库；若未落库则扩展 TaskQueue/DownloadQueue 字段或按探测来源推断（见 3.4，需评估是否引入迁移）

**下载队列行字段**（`_list_download` 输出）：
- 现有字段 + 补 `share_url`：`https://pan.quark.cn/s/<share_code>`，域名集中常量（如 `app/constants.py` 或现有配置）；无 share_code 返回 null
- `share_code`：**仅 admin 返回明文**；guest 不返回（修复现有越权——当前原样返回给 guest）
- `size_estimated`：同上

**权限**：`GET /queue` 维持 `get_current_user`，但字段层面脱敏：guest 无 share_code/share_url；admin 明文 share_code + share_url。

### 3.2 后端：入库确认卡死修复（library_check.py）

- **遗漏集 continue 分支纳入超时窗口**：Emby 命中但 `_episode_in_missing` 判定在遗漏集时，不直接无限 continue——检查 `node_started_at + timeout`，超时则按既定超时处理（`_mark_timeout_if_expired` 逻辑统一），并在日志中记录「遗漏集持续命中，超时」原因
- **所有 continue 分支补明确日志**：缺 tmdb_id / find_emby_id 异常 / get_missing 异常 / 遗漏集命中，分别记录不 finalize 的具体原因（含 media_id、episode、具体原因）
- 超时判定覆盖「Emby 命中但遗漏集判定」路径，避免无限等待

### 3.3 后端：file_size 真实值回填（transfer.py）

- `_complete_download`（transfer.py:526-567，downloading→scrape 推进处）：用 aria2 tellStatus 的 `totalLength`/真实大小回填 `DownloadQueue.file_size`（更新 SQL 增加 file_size 字段）
- 若 aria2 侧大小不可得，保留原值；回填后同时清除/标记估算标志（若有）
- 顶层不做共享占位修正（scan 均摊估算保留为兜底，展示层用「约」标注）

### 3.4 前端：巡检队列改名与展示（QueueView.vue / stores/queue.ts / api / types）

- Tab「任务队列」→「巡检队列」
- 巡检队列行：影视名称（media_title）/ SxxExx（episode）/ 分享码明文（admin，`formatSize` null 兜底）/ 状态 / 大小（`formatBytes` + 约标注）/ 更新时间（`formatTime`）
- 下载队列行：分享码明文（admin）渲染为 `<a :href="share_url" target="_blank">`；无 share_url 显示纯文本不可点击
- 分页：移除 loadMore「加载更多」交互，改 el-pagination（page/pageSize/total）；store `fetchPage`/`fetchDownloadPage` 改为读取 `{items, total}`，契约同步（QueueTaskItem 补 `media_title`/`share_code`/`size_estimated`；DownloadQueueItem 补 `share_url`/`size_estimated`）；分页默认第一页全量展示（pageSize 默认如 50，与后端 limit 对齐）
- 大小「约」标注：`size_estimated=true` 时 `formatSize` 前加「约 」前缀（utils/format.ts 新增 helper）
- 时间字段沿用现有东八区 `formatTime`/`timeAgo`

### 3.5 测试

**后端 pytest**（backend/tests/）：
- 排序断言：构造多行不同 created_at/enqueued_at，验证 ASC 顺序
- 脱敏：admin/guest 对 share_code/share_url 的返回差异
- share_url 构造：有/无 share_code
- library_check：遗漏集命中超时终态、各 continue 分支日志、finalize 正常路径
- file_size 回填：_complete_download 后 DQ.file_size 更新

**前端 vitest**（frontend/src/）：
- queue store：fetchPage/fetchDownloadPage 读取 {items,total}、分页状态
- QueueView：巡检队列行字段渲染（media_title/分享码/约大小）、分享码链接可点击/不可点击
- format：约标注 helper 用例

## 4. 边界条件与风险

- **`_list_flat` total 正确性**：去重语义（同 media_id+episode DQ 优先）下 total 计算需与 items 一致，避免翻页总量跳动。风险中，实现时先用现有语义核对
- **`size_estimated` 数据来源**：scan.py `size_estimated` 当前仅存在于 walk 返回 dict，未确认落库。若需落库使用则引入迁移（TaskQueue/DownloadQueue 加布尔列）；否则前端无法区分估算值。已决策：展示估算+约标注 → 需要落库信号。评估最小迁移（一条 ALTER，迁移 0016）
- **share_code 明文安全**：仅 admin 可见，guest 不返回；沿用「登录即管理员?」需确认列表路由的 admin 判定方式与 media.py 一致（is_admin 传参）。风险低
- **卡死修复依赖真实 Emby**：单测覆盖判定逻辑；部署后观察日志验证
- **反复拉取分页 vs 全量**：非目标；保持服务端分页

## 5. Spec Patch 摘要

回写 `specs/queue-inspection-display/spec.md`：

1. **明文分享码权限边界**：明确「share_code 明文仅 admin 返回，guest 不返回」；下载队列 share_url 可点击跳转
2. **大小估算标注**：估算值显示「约」前缀；下载完成后 aria2 真实值回填
3. **入库确认超时覆盖遗漏集**：Emby 命中但持续在遗漏集 → 超时判定不无限等待