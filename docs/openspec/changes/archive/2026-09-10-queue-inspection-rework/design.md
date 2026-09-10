# Design: queue-inspection-rework

## Context

现状（参见 proposal.md - Why）：
- `QueueView.vue` 两个 Tab：`task`（任务队列/扁平列表）+ `download`（下载队列/扁平列表）
- 后端 `_list_flat`（task_queue ∪ download_queue 扁平行）：`order_by(updated_at.desc(), id.desc())`，`_list_download`：`order_by(enqueued_at.desc(), id.desc())` —— 均为倒序，非用户要求的创建时间正序
- 前端 store `fetchPage`/`fetchDownloadPage` 用 limit/offset 追加式加载（hasMore + loadMore），非 el-pagination 标准分页；QueueView 有 `loadMore()`「展开更多」交互
- 大小：`_list_flat` 行 `file_size` 来自 task_queue/download_queue 行；前端 `formatSize` 按字节展示。用户反馈「每行都一样」，疑似数据填充/展示占位问题，需调查确认（与 episode-status-cache 的大小修正同源，本 change 聚焦队列行数据来源）
- 分享码：`_list_download` 返回 `share_code`（明文，后端仅 admin 可见？需核查 `_list_download` 的权限脱敏），`_list_flat` 无分享码字段或仅有 tail
- 入库确认：`library_check` 轮询 status='library'，依赖 Emby 收录 + 遗漏集判定 finalize；存在迟迟不入库，需调查根因

## Goals / Non-Goals

**Goals**
- 任务队列改名「巡检队列」
- 巡检队列展示字段：影视名称 / SxxExx / 分享码明文 / 状态 / 真实大小 / 更新时间
- 巡检与下载队列：标准分页 + 默认创建时间正序
- 下载队列分享码明文可点击跳夸克
- 列表默认全显（去 loadMore）
- 调查并修复入库确认卡死

**Non-Goals**
- 集信息缓存表设计（episode-status-cache 负责）
- 影视详情页 UI（media-detail-ui 负责）
- 时区底层修正（docker-timezone 负责；本 change 时间字段展示复用其入口）
- 巡检/下载调度状态机语义重构（沿用现有两队列模型，仅修展示与卡死）

## Decisions

### D1：巡检队列字段与数据源

**决策**：巡检队列行 = 原扁平任务列表（task_queue ∪ download_queue），新增/补全字段：`media_title`（join media.title）、`episode`（SxxExx）、`share_code`（明文，admin）、`file_size`（真实）、`status`、`updated_at`。分享码明文仅 admin 返回（沿用 §9.1 脱敏约定）。

**理由**：扁平行已含大部分字段，补 media_title 与明文 share_code 即可满足展示需求；脱敏沿用现有契约避免越权。

### D2：分页与排序——后端 order + 前端 el-pagination

**决策**：后端 `_list_flat`/`_list_download` 的排序改为 `created_at ASC, id ASC`（创建时间从小到大），保留 limit/offset 分页；前端由 loadMore 追加式改为 el-pagination 标准分页（page/pageSize/total），列表默认加载第一页全量展示。

**理由**：用户明确「默认按照创建时间 从小到大」与「分页展示」「默认显示全部（去掉展开更多）」。el-pagination 是 Element Plus 标准分页组件，符合「分页展示」要求；「默认显示全部」= 去掉 loadMore 追加交互，改为页码翻页，首屏即全量可查。

**备选**：无限滚动 → 用户明确否定「下拉后还得再点击展开更多」，否决。

### D3：大小真实值——调查数据源

**决策**：先调查「每行大小都一样」根因（可能：file_size 未随任务更新/共享占位/前端 format 错误），确认后修复为逐行真实 file_size。调查路径：
1. 检查 task_queue/download_queue 建行时 file_size 来源（转存后是否更新真实大小）
2. 检查 `_list_flat`/`_list_download` 输出与前端展示链路
3. 若为数据层未更新 → 在转存/探测完成处回填真实大小

**理由**：大小问题横跨数据写入与展示，需先归因再修；本 change 至少保证队列行展示「来自本行的真实 file_size」，不读取首行占位。

### D4：下载队列分享码链接

**决策**：下载队列 `share_code` 明文展示（admin），前端渲染为可点击链接；分享地址 = 后端新增 `share_url` 字段（由夸克分享码构造，`https://pan.quark.cn/s/<code>`）或前端拼装。后端统一提供 share_url 更安全（避免前端猜域名）。

**理由**：用户要求「点击可以跳转到夸克分享地址」；后端直出 URL 避免硬编码域名。

### D5：入库确认卡死——调查与修复

**决策**：`library_check` 卡死根因调查，候选方向：
1. `find_emby_id` 未命中（tmdb_id 缺失 / Emby 名称不匹配）→ 本轮 continue 且不消耗超时窗口，可能无限等待
2. Emby 遗漏集 `_episode_in_missing` 恒 True（episode 与 missing code 盲匹配误判）→ 永不 finalize
3. 入库超时配置缺失/过大 → 长期不超时
4. scheduler 中 library_check 未注册/被禁用 → 根本不轮询

**修复策略**：逐一验证；至少保证「卡死可被诊断」——日志明确记录 continue 原因，并让超时判定覆盖「Emby 命中但遗漏集判定」路径（避免无限等待）；若根因是配置/注册，则修正调度。

**理由**：卡死是用户最关心的运营问题（第 7 点），需真实归因；spec 要求「能正确判定卡死原因」。

## Risks / Trade-offs

- [排序改为 created_at ASC 影响「最新在前」操作习惯] → 用户明确要求正序，按需求执行；正序符合「新任务排在后面待处理」语义
- [分享码明文展示安全] → 仅 admin 可见（沿用脱敏），guest 不返回
- [share_url 拼装域名假设] → 后端统一构造，域名集中一处
- [大小根因调查耗时] → 先做最小保证（队列行真实 file_size），再顺带修复数据回填；不阻塞其他任务
- [入库确认修复需真实 Emby 环境验证] → 单测覆盖判定逻辑 + 部署后观察日志

## Migration Plan

1. 后端：`_list_flat`/`_list_download` 排序 + 补字段（media_title/share_url）+ share_code 明文（admin）+ file_size 来源核查
2. 后端：library_check 卡死修复（log continue 原因 + 超时覆盖遗漏集路径）
3. 前端：QueueView 改名巡检队列 + el-pagination + 分享码链接 + 去 loadMore + 字段展示
4. 验证：单测（排序/分页/脱敏/分享URL）+ 部署观察队列与入库
5. 回滚：还原前端与后端改动；无数据库迁移

## Open Questions

- 巡检队列中 download_queue 行（执行层）与 task_queue 行（探测层）是否同屏混合展示？现有 `_list_flat` 已混合（isTaskQueue 区分），默认保持混合；若用户期望仅探测层归巡检队列，需再确认 —— 当前按「巡检队列 = 原任务队列（扁平混合列表）」理解，与用户「任务队列改名为巡检队列」表述一致，不阻塞。
