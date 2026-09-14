# 修复通知模板（fix-notification-templates）

## Why

当前站内铃铛与 PushPlus 推送的通知文案存在多处不一致：标题风格混杂（有的用冒号、有的裸拼接）、英文 raw 事件名直接暴露给用户（如 `NaSTools transfer.fail`）、PushPlus 固定纯文本 txt 模板可读性差、前端铃铛逐条渲染没有类型区分。同时 `download_started`（下载开始/开始入库）与「下载完成」两条通知噪音偏大，用户只关心最终「入库完成」结果与媒体名称/集数，不需要看到内部 id 与文件名。

## What Changes

- **移除两条 `download_started` 通知**：`approvals.py` 的「开始入库」与 `transfer.py` 的「下载开始」（**BREAKING**：该事件不再产生任何站内/推送通知，EVENT_DOWNLOAD_STARTED 常量保留但不再被调用）。
- **移除 `transfer.py` 的「下载完成」通知**（**BREAKING**：downloading→scrape 阶段不再通知），`download_complete` 仅保留 `library_check.py` 的「入库完成」通知。
- **重写「入库完成」文案**：标题与正文统一为「媒体 {media_name} · {episode} 已入库完成。」，其中 `episode` 为 `SxxExx` 格式；电影（无集数）为「媒体 {title} 已入库完成。」。不再出现 media_id 与文件名。
- **统一全部通知文案风格**：审批待办、各类 flow_error（转存失败/容量告警/NasTools 同步失败/转存流程告警）的标题与正文格式规范化。
- **去除英文 raw 事件名**：`nastools_notify` 的 `transfer.fail` / `download.fail` 等在用户可见文案中替换为中文描述（仅内部 event_type 值保留英文）。
- **PushPlus 美化**：推送模板由固定 `txt` 改为 `html`，标题加粗、正文分段、媒体名/集数高亮。
- **前端铃铛增强**：按事件类型区分图标与颜色（成功=入库完成 绿色、待审批 蓝色、失败/告警 红色）。

## Capabilities

### New Capabilities

- `notifications`: 站内铃铛 + PushPlus 推送的通知模板与渲染行为。覆盖通知文案规范、事件类型的用户可见呈现（标题/正文/图标/颜色）、PushPlus HTML 模板格式，以及通知触发时机（仅入库完成、审批待办、错误告警）。

### Modified Capabilities

（无 — 现有 spec 中无 `notifications` capability，全部为新能力描述。）

## Impact

- **后端**：`backend/app/services/notifier.py`（PushPlus template 切换、NotifyEvent 用法）、`backend/app/services/pushplus.py`（html 模板支持）、`backend/app/tasks/library_check.py`（入库完成文案）、`backend/app/tasks/transfer.py`（移除 download_started / 下载完成通知）、`backend/app/routers/approvals.py`（移除开始入库通知）、`backend/app/tasks/notification_scan.py`（文案规范化）、`backend/app/services/capacity.py`（文案规范化）、`backend/app/tasks/nastools_sync.py`（文案规范化）、`backend/app/routers/nastools_notify.py`（去英文事件名）。
- **前端**：`frontend/src/layouts/MainLayout.vue`（铃铛渲染：图标/颜色分类）。
- **API 契约**：`GET /api/notifications` 返回字段不变（`event_type`/`title`/`body`/`read` 等），仅内容文本变化；无 schema 变更。
- **测试**：需更新受通知文案/触发行为影响的现有测试（如 `test_library_check.py` 中 download_complete 断言、`test_approval_dup.py` 审批通知断言，以及 Nastools 相关通知测试）。