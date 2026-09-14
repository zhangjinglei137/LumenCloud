# Design: 修复通知模板

## Context

现有通知体系（见 proposal.md - Why）共 4 类事件、12 处生成点，散布在 `approvals.py`、`transfer.py`、`library_check.py`、`notification_scan.py`、`capacity.py`、`nastools_sync.py`、`nastools_notify.py`。通知文案均为各调用点内联 f-string，无统一模板层。PushPlus 通道固定 `template="txt"`。前端铃铛（`MainLayout.vue`）仅渲染 `title`/`message`/`time`，无类型区分。

关键约束：
- `NotifyEvent` 结构（event_type/title/body/recipient/extra）与 `notifications` 表结构不变，避免 schema 与 API 契约变动
- 站内去重依赖 body 前缀（`wr#` / `tq#`），`notification_scan.py` 的 body 前缀不可移除
- `episode` 键已标准化：剧集=`S01E01`（`fmt_episode`），电影=`movie:<title>`（model 注释确认）
- 各处通知点均持有 `media_id`，可通过 `Media.title` 取得媒体名称

## Goals / Non-Goals

**Goals:**
- 用户可见文案统一为「媒体名 + SxxExx」风格，不含 media_id/文件名
- 消除下载开始/下载完成阶段的噪音通知
- 用户可见文本不再出现英文 raw 事件名
- PushPlus 改用 HTML 模板提升可读性
- 前端铃铛按事件类型区分图标与颜色

**Non-Goals:**
- 不改 `notifications` 表结构、不改 `GET /api/notifications` 响应字段
- 不重构通知通道架构（NotifierChain/InAppNotifier/PushPlusNotifier 保持）
- 不改去重/节流机制（body 前缀、冷却窗口）
- 不新增第三方通知渠道

## Decisions

### D1: 引入通知文案工厂函数（backend/app/services/notify_templates.py）
所有通知生成点不再内联拼文案，统一调用文案工厂生成 `(title, body)`。
- 理由：12 处生成点分散，统一入口可强制「媒体名+集数」「中文描述」规范，杜绝再次漂移；改动集中在单文件，测试可单测。
- 替代方案：仅逐处改 f-string —— 无强制一致性，后续易再次发散；在 `NotifyEvent` 上加模板字段 —— 侵入数据模型，过度设计。
- 工厂函数按事件类型划分：`approval_pending(...)`、`download_complete(...)`（入库完成）、`flow_error_*` 系列。其中 `download_complete` 需要 `media_name` 入参（调用方 JOIN 或查询 `Media.title`）。

### D2: 媒体名称获取：通知点就近查询 Media.title
入库完成通知点在 `library_check.py`，其作用域内已有 `media_id` 与 session；在该处补充一次 `Media.title` 查询（join 现有查询或独立 select）取得媒体名。
- 理由：避免改动 `NotifyEvent` 契约；该点已有 session，成本可忽略。
- 电影判定：`episode` 键以 `movie:` 前缀开头时视为电影，文案用「媒体 {title} 已入库完成。」；否则视为剧集，`episode` 已是 `SxxExx`。

### D3: 移除 download_started / 下载完成通知
删除 `approvals.py:211-220`（开始入库）、`transfer.py:998-1007`（下载开始）、`transfer.py:528-535`（下载完成）三处 `notifier.notify` 调用。
- `EVENT_DOWNLOAD_STARTED` 常量保留（避免破坏 `notifier.py` 事件表与既有测试引用），但不再有调用点；`download_complete` 事件类型仍保留给「入库完成」使用。
- `transfer.py` 原「下载完成」处的刮削触发逻辑（`_spawn(scrape_runner)`）保留，仅移除通知调用。

### D4: 英文事件名映射表
`nastools_notify.py` 中 `_EVENT_TRANSFER_FINISHED`/`_EVENT_TRANSFER_FAIL`/`_EVENT_DOWNLOAD_FAIL` 三个 raw 事件名在用户可见文案处替换为中文：`transfer.finished`→「转存完成」、`transfer.fail`→「转存失败」、`download.fail`→「下载失败」。
- 事件内部识别仍用英文常量（不影响 webhook 契约），仅展示层映射为中文。

### D5: PushPlus HTML 模板
`PushPlusClient.send` 增加 `template` 参数透传（已存在，默认 `txt`）；通知侧统一以 `html` 调用。
- 文案工厂输出结构化片段：标题用 `<b>`/`<strong>` 加粗，正文按段落 `<p>` 分隔，媒体名与集数用 `<span style="color:#…">` 高亮。
- 站内 `body` 仍用纯文本（铃铛渲染不需要 HTML）；PushPlus 推送侧再包一层 HTML 外壳。即：`NotifyEvent.body` 保持纯文本，`PushPlusNotifier.notify` 内部将纯文本转换为 HTML（标题加粗、按换行分段、识别「媒体 X · SxxExx」模式高亮）。
- 理由：站内与 PushPlus 共用同一 `body` 字段，若直接存 HTML 会污染铃铛纯文本渲染；转换放在 PushPlus 通道内是职责最清晰的位置。
- 替代方案：工厂直接产出两份文案 —— 增加调用点复杂度，且站内/推送文案语义本应一致。

### D6: 前端铃铛类型映射（frontend/src/utils/format.ts 或 MainLayout.vue）
新增 `event_type → {label, icon, color}` 映射：
- `download_complete` → 成功（绿色，如 `CircleCheck`）
- `approval_pending` → 待审批（蓝色，如 `Bell`/`Clock`）
- `flow_error` → 失败/告警（红色，如 `Warning`）
- 未知类型 → 默认中性样式回退
- 铃铛条目在标题前渲染图标 + 强调色 dot；保留现有 `unread` 高亮与 `全部已读`。
- 图标来自 element-plus 现有 icon 集，不新增依赖。

## Risks / Trade-offs

- [测试断言受影响] 现有测试断言通知标题/触发次数（如 `test_library_check.py` download_complete 断言、`test_approval_dup.py` 审批通知断言、Nastools 通知测试）→ 本 change 内同步更新断言以匹配新文案与触发时机
- [PushPlus HTML 兼容性] 个别渠道对 HTML 支持差异 → 保持简单标签（b/p/span），避免复杂样式；txt 兜底逻辑保留
- [电影判定依赖 movie: 前缀] 若未来电影键格式变化则误判 → 在工厂内集中判定，单一维护点
- [移除通知后用户感知] 下载过程不再有任何通知 → 属用户明确需求（只关心最终结果），在 proposal 中已作为 BREAKING 说明

## Migration Plan

- 代码一次性切换，无数据迁移（`notifications` 表结构不变，历史通知原样保留）
- 先改文案工厂 + 各调用点，再改 PushPlus 模板，最后改前端铃铛
- 回归重点：`notification_scan.py` 去重前缀不受影响（body 前缀逻辑独立于文案工厂，保持 `wr#`/`tq#` 前缀拼接于正文开头）

## Open Questions

无 — 文案格式、触发时机、前端呈现均已与用户确认。
