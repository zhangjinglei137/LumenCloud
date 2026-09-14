# Brainstorm Summary

- Change: fix-notification-templates
- Date: 2026-09-14

## 确认的技术方案

1. **文案工厂**：新增 `backend/app/services/notify_templates.py`，统一所有通知生成，返回 `(title, body)`。函数按事件类型划分：`download_complete(media_name, episode, is_movie)`、`approval_pending(title)`、`flow_error_transfer(...)`、`flow_error_capacity(...)`、`flow_error_nastools_event(中文名, title)` 等。去重前缀（`wr#`/`tq#`）由调用方在正文首部拼接，工厂不负责；电影判定集中 `media_type == "movie"`。
2. **入库完成媒体名**：`library_check.py` 主循环已有 `media` 对象 → `media.title` 传入 `_finalize_done`，通知改用工厂，不再出现 media_id/file_name。
3. **移除 3 处噪音通知**：approvals.py「开始入库」、transfer.py「下载开始」、transfer.py「下载完成」的 notify 调用删除；保留周边逻辑（刮削触发 `_spawn(scrape_runner)`）；`EVENT_DOWNLOAD_STARTED` 常量保留。
4. **英文事件名映射**：nastools_notify.py 内部判定用英文常量，展示层映射 `transfer.finished→转存完成`、`transfer.fail→转存失败`、`download.fail→下载失败`。
5. **PushPlus HTML**：`PushPlusNotifier.notify` 内纯文本→HTML 转换（标题加粗、换行分段、「媒体 X · SxxExx」高亮），`template="html"` 发送；站内 body 存纯文本。
6. **前端铃铛映射**：`format.ts` 新增 `event_type → {label, icon, color}`：download_complete=绿+CircleCheckFilled、approval_pending=蓝+BellFilled、flow_error=红+WarningFilled、未知=灰默认；MainLayout.vue 标题行前置图标 + 着色 dot。

## 关键取舍与风险

- 移除通知破坏现有测试断言（test_transfer.py:281/531、test_library_check.py、test_capacity_alert.py:96、test_oracle_fixes.py wr/tq 断言）→ 同步更新
- notification_scan 去重前缀不可破坏 → 前缀由调用方拼接，逻辑不变
- PushPlus HTML 只用简单标签（b/p/span），不引复杂样式

## 测试策略

- 新增 `test_notify_templates.py`（工厂单测，含剧集/电影/集号边界）
- 更新受影响断言（test_transfer / test_library_check / test_capacity_alert / test_oracle_fixes）
- PushPlus HTML 转换单测
- 前端 `npm run build` 验证

## Spec Patch

无（现有 delta spec 已覆盖全部确认决策）