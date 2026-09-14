# Tasks: 修复通知模板

## 1. 后端通知文案工厂

- [ ] 1.1 新建 `backend/app/services/notify_templates.py`：实现 `approval_pending(title) -> (title, body)`、`download_complete(media_name, episode, is_movie) -> (title, body)`、`flow_error_transfer_failed(...)`、`flow_error_capacity(...)`、`flow_error_nastools_sync(...)`、`flow_error_nastools_event(...)`、`flow_error_generic(...)` 等文案工厂函数，并验证 `pytest backend/tests/test_notify_templates.py` 通过（电影/剧集/含集号/超界集号入参用例）
- [ ] 1.2 工厂函数保证用户可见文案不含媒体 id 与文件名、不使用英文 raw 事件名；正文首行保留 `wr#<id>` / `tq#<id>` 前缀拼接能力（前缀逻辑由调用方传入，工厂不负责去重判定），验证单测断言输出格式

## 2. 移除噪音通知

- [ ] 2.1 删除 `backend/app/routers/approvals.py` 中「开始入库」notifier.notify 调用（原 211-220 行），验证 `pytest backend/tests/test_approval_dup.py`、`test_p1_fixes.py` 相关用例更新后通过
- [ ] 2.2 删除 `backend/app/tasks/transfer.py` 中「下载开始」与「下载完成」两处 notifier.notify 调用（保留刮削触发逻辑），验证 `pytest backend/tests/test_library_check.py` 相关断言更新后通过
- [ ] 2.3 确认 `EVENT_DOWNLOAD_STARTED` 常量保留但无调用点，`download_complete` 仅剩「入库完成」一处调用，验证 grep 结果与 `pytest backend/tests/test_oracle_fixes.py` 通过

## 3. 入库完成文案改造

- [ ] 3.1 `backend/app/tasks/library_check.py` 入库完成通知改为调用 `notify_templates.download_complete(...)`：在现有 session 中查询 `Media.title` 取得媒体名，电影（`movie:` 前缀）与剧集分支分别生成文案，验证通知文案为「媒体 {title} · SxxExx 已入库完成。」/「媒体 {title} 已入库完成。」且断言不含 media_id/文件名
- [ ] 3.2 确认删除转移队列/下载完成阶段的其它 download_complete 通知后无遗漏调用，验证 grep `EVENT_DOWNLOAD_COMPLETE` 仅 library_check 一处

## 4. 其余通知点接入文案工厂

- [ ] 4.1 `notification_scan.py` 审批待办与转存失败两条通知改用工厂（保留 body 前缀），验证 `pytest backend/tests/test_oracle_fixes.py` 通过且去重前缀未被破坏
- [ ] 4.2 `capacity.py` 容量告警、`nastools_sync.py` 同步失败、`transfer.py` 转存失败/流程告警改用工厂文案，验证 `pytest backend/tests/test_capacity_alert.py`、`test_library_check.py`、`backend/tests/test_nastools_notify.py` 通过
- [ ] 4.3 `nastools_notify.py` 失败事件中文映射（transfer.finished→转存完成 / transfer.fail→转存失败 / download.fail→下载失败），验证 `backend/tests/test_nastools_notify.py` 断言更新后通过

## 5. PushPlus HTML 模板

- [ ] 5.1 `backend/app/services/pushplus.py`：`send()` 透传 template 参数，`PushPlusNotifier.notify` 将纯文本 body 转换为 HTML（标题加粗、按换行分段、媒体名与集数高亮）后以 `template="html"` 发送，验证 `pytest backend/tests/` 相关 PushPlus 用例（含新建的转换单测）通过
- [ ] 5.2 验证未配置 token 时 HTML 路径仍整体跳过、站内通知不受影响

## 6. 前端铃铛类型呈现

- [ ] 6.1 `frontend/src/utils/format.ts` 或 `MainLayout.vue` 新增 `event_type → {label, icon, color}` 映射（download_complete 绿/成功图标、approval_pending 蓝/待审批图标、flow_error 红/告警图标、未知回退默认），验证 `npm run build`（或前端 lint/typecheck）通过
- [ ] 6.2 铃铛条目标题前渲染类型图标与强调色 dot，保留 unread 高亮与全部已读功能，验证前端构建通过且人工查看铃铛面板三类通知样式区分可见

## 7. 集成验证

- [ ] 7.1 后端全量测试通过：`cd backend && python -m pytest`（或项目约定测试命令）无失败
- [ ] 7.2 前端构建通过（`npm run build` 或项目约定命令）
- [ ] 7.3 核对 `docs/openspec/changes/fix-notification-templates/specs/notifications/spec.md` 全部 Requirement/Scenario 有对应实现与测试覆盖，验证清单无遗漏