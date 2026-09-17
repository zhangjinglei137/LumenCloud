# Brainstorm Summary

- Change: fix-logs-and-ui-polish
- Date: 2026-09-17

## 确认的技术方案

### 问题 1：运行日志任务类型显示英文
后端实际写入任务类型全集（已核对 `backend/app/tasks/*.py`）：`scan_media`、`scan_all_media`、`transfer`、`cleanup`、`capacity_alert`、`recover`、`sync_nastools`、`notify`、`prune_history`。
前端 `TASK_TYPE_MAP` 需：
- 补齐缺失键：`sync_nastools`（目录同步入库）、`notify`（通知）、`prune_history`（历史清理）
- 移除不产生类型：`transfer_retry`、`download`；历史别名 `nastools_sync`→`sync_nastools`、`notification_scan`→`notify`（`media_scan`/`recovery` 视存量保留兜底映射或移除，筛选列表只列有效类型）
- `LogsView.vue` 筛选列表与 map 同步对齐
- 未知类型回退原值 + info 样式（既有行为保留）

### 问题 2：运行日志分页 + 保留天数
- 后端 `list_logs` 增加 count，返回 `{items, total}`（对齐 queue 契约）；前端 store 移除「limit+1 探测」估算，直接消费 total，保留空页回退保护
- `task_run_retention_days` 加入 settings PATCH 白名单（`_WHITELIST_EXACT`），但需从 `_EDITABLE_KEYS` 排除（否则被当作服务凭据渲染文本框）——参照 `emby_series_library_ids` 先例；settingsMeta.ts 增加该键元数据，设置页业务参数区渲染数字输入，保存即生效（cleanup.py 已读取该键，无需改清理逻辑）

### 问题 3：邀请码管理迁移到用户管理页
- `UsersView.vue` 增加邀请码管理区块：复用 `stores/settings.ts` invite actions，迁移 `SettingsView.vue` 的 generate/copy/remove/copyRegisterLink 逻辑与 invites 表格模板；`onMounted` 同时拉取用户列表与邀请码
- 移除 `SettingsView.vue` 的「邀请码管理」tab 及 `fetchInvites()` 调用；后端 invite API 契约不变

### 问题 4：影视库卡片「已有」重复
根因：`MediaListView.vue:165` 模板硬编码 `已有 {{ episodeText(m) }}`，而 `episodeSummaryText`（format.ts:251）已返回「已有 N 缺失 M」→ 双重前缀。
修复：模板仅输出 `episodeText(m)`（内部已含前缀）；`episodeSummaryText` 保持不变（format.test.ts 既有断言与表格视图依赖）。卡片显示「已有 15 缺失 0」。

## 关键取舍与风险

- 静态类型清单 vs 动态查库：静态 map + 注释标注来源文件，低频类型集收益更高
- 分页 count(*) 开销：task_run 受清理任务约束（默认 30 天），量级可控
- **BREAKING**：`GET /api/logs` 响应数组→对象，同仓前端同步适配，无跨版本兼容需求
- 历史 task_run 存量类型：展示层映射/未知类型兜底，不改数据

## 测试策略

- 前端：`npm run test`（format.test.ts 单前缀断言、UsersView.test.ts 邀请码区块、SettingsView.test.ts 移除 invites 后通过）、`npm run build`
- 后端：引用 `/logs` 的既有测试适配新契约；`test_prune_history.py`/`test_task_cleanup.py` 保留天数配置读取
- 截图问题 4 以 format 单测 + 模板修复验证

## Spec Patch

- `specs/run-logs/spec.md` 保持现有 4 条 ADDED（映射/筛选/分页/保留天数）——已覆盖，无回写
- `specs/user-invite-management/spec.md` 已覆盖迁移与操作——无回写
- `specs/media-status/spec.md` 已覆盖单前缀——无回写
