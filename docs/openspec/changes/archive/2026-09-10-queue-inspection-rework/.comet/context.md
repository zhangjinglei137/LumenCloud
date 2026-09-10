# Comet Design Handoff

- Change: queue-inspection-rework
- Phase: design
- Mode: compact
- Context hash: 962f062c0fc99824ba195249e4db88234a412f4b450d8ce8d740f851323a0e61

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This handoff is a deterministic, source-traceable context pack, not an agent-authored summary.

## docs/openspec/changes/queue-inspection-rework/proposal.md

- Source: docs/openspec/changes/queue-inspection-rework/proposal.md
- Lines: 1-29
- SHA256: 10cd0c201170b3ccc3ef04c3f634caaeddbc30e16b5010706b6e981cdc59014f

```md
## Why

任务队列存在多个使用问题：名称「任务队列」语义不清（实际是巡检探测层）；列表字段不全（缺影视名称/SxxExx/分享码明文/大小/更新时间直观展示）；大小显示错误（每行都等同第一个文件大小）；分页默认排序不符合预期（需创建时间正序）；下载队列分享码未明文且不可点击跳转夸克；列表需「展开更多」才能看全部；且存在「入库确认」状态迟迟不入库的问题，需调查修复。

## What Changes

- **任务队列改名「巡检队列」**：原任务队列 Tab 更名为巡检队列，语义聚焦「搜索到资源即完成」的巡检目标
- **巡检队列字段补充**：显示影视名称、影片信息（SxxExx）、分享码（明文）、状态、大小、更新时间
- **大小真实值**：巡检队列与下载队列的大小展示每个任务的真实文件大小，不再全部等同第一个文件（与 episode-status-cache 的大小修正同源，此处聚焦队列行）
- **分页与排序**：巡检队列、下载队列均分页展示，默认按创建时间从小到大（升序）
- **下载队列分享码**：明文展示，点击可跳转到夸克分享地址
- **巡检目标收窄**：巡检队列目标 = 搜索到资源即完事，其余交给下载队列；任务队列展示一个「完成」即可
- **列表默认全显**：列表默认展示全部条目，去掉「下拉/展开更多」交互
- **修复入库确认迟迟不入库**：调查并修复 download_queue status='library' 长期不 finalize 的问题

## Capabilities

### New Capabilities
- `queue-inspection-display`: 巡检队列（原任务队列）的改名、字段展示、分页排序、分享码明文与跳转、列表全显能力

### Modified Capabilities
- `media-pipeline`: 巡检队列目标收窄（搜索到资源即完成，后续交下载队列）；入库确认（library 节点）正常完成不入库卡死

## Impact

- 前端：`frontend/src/views/QueueView.vue`（Tab 改名/字段/分页排序/分享码链接/去展开更多）、`frontend/src/stores/queue.ts`、`frontend/src/api`、`frontend/src/types`（QueueTaskItem/DownloadQueueItem 契约）、`frontend/src/utils/format.ts`
- 后端：`backend/app/routers/queue.py`（分页/排序/分享码字段）、`backend/app/tasks/library_check.py`（入库确认卡死根因修复）、`backend/app/tasks/transfer.py`
- 依赖：episode-status-cache 的大小修正原则（真实 file_size）；docker-timezone 的时间展示（分页时间字段东八区展示）
- 无数据库迁移（可能仅数据修复/状态修正逻辑）

```

## docs/openspec/changes/queue-inspection-rework/design.md

- Source: docs/openspec/changes/queue-inspection-rework/design.md
- Lines: 1-90
- SHA256: 3a8ba5d87f8e9ffd2f1f9878f7d380cfcffd241ebb543766be53b123f11669ae

[TRUNCATED]

```md
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

```

Full source: docs/openspec/changes/queue-inspection-rework/design.md

## docs/openspec/changes/queue-inspection-rework/tasks.md

- Source: docs/openspec/changes/queue-inspection-rework/tasks.md
- Lines: 1-33
- SHA256: b9c6fc443cca99282d015f8fedc8053b00fdf4baf65b25aaf9f1a6941fb9bb27

```md
# Tasks: queue-inspection-rework

## 1. 后端：队列列表排序/字段/分享码

- [ ] 1.1 `_list_flat` 排序改为 `created_at ASC, id ASC`（创建时间从小到大），保留 limit/offset，验证排序断言（构造多行不同 created_at）
- [ ] 1.2 `_list_download` 排序改为 `enqueued_at ASC, id ASC`，验证排序断言
- [ ] 1.3 巡检队列扁平行补 `media_title`（join media.title）与明文 `share_code`（仅 admin；guest 不返回，沿用 §9.1 脱敏），验证 admin/guest 返回差异
- [ ] 1.4 下载队列输出补 `share_url`（夸克分享地址，由 share_code 构造，域名集中常量），无 share_code 返回 null，验证 URL 构造正确
- [ ] 1.5 补充后端测试：排序/分页/脱敏/分享 URL 用例，验证 `pytest` 通过

## 2. 后端：大小真实值调查与修复

- [ ] 2.1 调查「队列每行大小都一样」根因（核查 task_queue/download_queue 建行与转存后 file_size 是否回填真实值、`_list_flat`/`_list_download` 输出链路），记录根因结论
- [ ] 2.2 按根因修复：队列行展示来自本行的真实 file_size（若为数据回填缺失则在对应流程回填），验证多任务大小各异时逐行真实

## 3. 后端：入库确认卡死调查与修复

- [ ] 3.1 调查「status=library 迟迟不入库」根因（find_emby_id 未命中/遗漏集盲匹配/超时配置/scheduler 注册缺失，逐项核查日志与代码），记录根因结论
- [ ] 3.2 修复：library_check 的 continue 分支补充明确日志（记录不 finalize 的具体原因），并让超时判定覆盖「Emby 命中但遗漏集判定」路径（避免无限等待），验证卡死可诊断/可超时
- [ ] 3.3 补充测试：遗漏集判定终态、超时路径用例，验证 `pytest` 通过

## 4. 前端：巡检队列改名与展示

- [ ] 4.1 QueueView 原「任务队列」Tab 改名为「巡检队列」，验证页面 Tab 命名更新
- [ ] 4.2 巡检队列行展示 影视名称 / SxxExx / 分享码明文（admin）/ 状态 / 真实大小 / 更新时间，验证字段齐全
- [ ] 4.3 下载队列分享码明文展示且可点击跳转 share_url（无地址则不可点击），验证点击行为
- [ ] 4.4 巡检/下载队列改 el-pagination 标准分页（page/pageSize/total），移除 loadMore「展开更多」交互，默认加载第一页全量展示，验证翻页正常且无展开更多按钮
- [ ] 4.5 stores/queue.ts 与 api/types 契约同步（total/page 字段、share_url、media_title），验证类型一致
- [ ] 4.6 前端测试补充：队列行字段渲染、分享码链接、分页数据流用例，验证 `vitest` 通过

## 5. 验证

- [ ] 5.1 手动验证：巡检队列命名/字段/正序分页、下载队列分享码跳转、无展开更多、入库确认不再长期卡死，验证 `npm run build` 与后端启动无报错

```

## docs/openspec/changes/queue-inspection-rework/specs/queue-inspection-display/spec.md

- Source: docs/openspec/changes/queue-inspection-rework/specs/queue-inspection-display/spec.md
- Lines: 1-109
- SHA256: b4bce3c69a1c962ff2ac94bb828b00fcaf5cc1f3b13f361a7724c6f402048108

[TRUNCATED]

```md
## Purpose

为巡检队列（原任务队列）与下载队列提供清晰的展示与正确的任务流：巡检队列改名并展示影视/集/分享码/状态/大小/更新时间，分页按创建时间升序，分享码明文可跳转夸克；巡检目标收窄为「搜索到资源即完成」；入库确认正常完成不卡死。

## ADDED Requirements

### Requirement: 任务队列改名为巡检队列

任务队列界面 SHALL 更名为「巡检队列」，语义为巡检探测层任务（搜索到资源即完成），与下载队列区分。

#### Scenario: 巡检队列命名
- **WHEN** 用户打开队列页面
- **THEN** 原「任务队列」Tab 显示为「巡检队列」，其余 Tab 命名不变

### Requirement: 巡检队列展示字段

巡检队列列表 SHALL 展示每行：影视名称、影片信息（SxxExx）、分享码（明文）、状态、大小、更新时间；分享码明文可见（有权限用户）。

#### Scenario: 巡检队列行字段
- **WHEN** 用户查看巡检队列列表
- **THEN** 每行显示影视名称、SxxExx、明文分享码、状态、真实大小、更新时间

#### Scenario: 分享码明文展示
- **WHEN** 管理员（admin）用户查看巡检队列
- **THEN** 分享码以明文完整显示，不再只显示尾号

#### Scenario: 分享码权限边界
- **WHEN** 非管理员（guest）用户查看巡检队列或下载队列
- **THEN** 不返回分享码明文（不做展示），分享码与分享地址不可见

### Requirement: 队列大小真实值

巡检队列与下载队列的每行大小 SHALL 展示该任务真实文件大小（对应文件记录），不得等同第一个文件或共享占位值。

#### Scenario: 逐行真实大小
- **WHEN** 队列中多个任务文件大小不同
- **THEN** 每行展示各自真实大小，不全部相同

#### Scenario: 大小缺失
- **WHEN** 某任务尚无文件大小记录
- **THEN** 显示「—」，不填充虚假值

#### Scenario: 估算大小标注
- **WHEN** 任务的文件大小为均摊估算值（探测源未返回真实大小）
- **THEN** 展示时带「约」前缀（如「约 1.9 GB」），不冒充精确真实值

#### Scenario: 下载后真实值回填
- **WHEN** 任务的转存下载完成，且可获取到真实文件大小
- **THEN** 队列行与集状态展示使用回填后的真实大小，不再显示估算值

### Requirement: 队列分页与默认排序

巡检队列与下载队列 SHALL 分页展示，默认按创建时间从小到大（升序）排列。

#### Scenario: 分页展示
- **WHEN** 队列条目超过一页
- **THEN** 列表分页展示，可翻页浏览

#### Scenario: 默认创建时间升序
- **WHEN** 用户打开队列页面
- **THEN** 列表默认按创建时间从早到晚排列

### Requirement: 下载队列分享码可点击跳转

下载队列的分享码 SHALL 以明文显示，且点击后跳转到对应的夸克分享地址。

#### Scenario: 分享码明文链接
- **WHEN** 用户点击下载队列某行的分享码
- **THEN** 浏览器打开该任务的夸克分享地址

#### Scenario: 无分享地址降级
- **WHEN** 任务无分享码或无法构造分享地址
- **THEN** 该行不显示可点击链接（或不可点击），不报错

### Requirement: 巡检队列目标收窄

巡检队列的完成判定 SHALL 为「搜索到可用资源并收集转存信息即完成」，不再在巡检阶段执行转存/下载；后续转存下载由下载队列承接。

#### Scenario: 搜索到资源即完成
- **WHEN** 巡检为某缺失集搜索到可用资源并收集分享信息

```

Full source: docs/openspec/changes/queue-inspection-rework/specs/queue-inspection-display/spec.md
