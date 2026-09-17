# Proposal: fix-logs-and-ui-polish

## Why

用户反馈四类界面与配置问题，影响日常使用体验：
1. 运行日志页部分任务类型因前端映射与后端实际写入值不一致而显示英文（如 `sync_nastools`、`notify`、`prune_history`），且筛选列表混入已不再产生的历史别名类型。
2. 运行日志分页依赖「多取 1 条」探测估算总条数，后端不返回 total，导致分页总条数显示不准确；日志保留天数（`task_run_retention_days`）后端清理任务已支持但设置页无配置入口，无法动态调整。
3. 「邀请码管理」（分享码）位于设置页，与用户管理职能分离，操作路径不直观；应迁移到用户管理页统一管理。
4. 影视库卡片集数统计文案重复渲染「已有」前缀，展示为「已有 已有 15 缺失 0」。

## What Changes

- **运行日志任务类型映射修正**：前端 `TASK_TYPE_MAP` 与 `LogsView` 筛选列表对齐后端实际写入值（`sync_nastools` 而非 `nastools_sync`、`notify` 而非 `notification_scan`、补 `prune_history`），移除无关的历史别名类型（`media_scan`、`recovery`、`transfer_retry`、`download` 若不再产生）。
- **运行日志真实分页**：后端 `/api/logs` 返回 `{items, total}` 分页契约（对齐 queue），前端 log store 直接消费真实 total，移除「多取 1 条」估算逻辑。
- **日志保留天数动态配置**：将 `task_run_retention_days` 加入设置 PATCH 白名单与前端可配置键清单（`editable_keys`），在设置页业务参数区渲染输入项，保存即生效（`prune_history_job` 已读取该键）。
- **邀请码管理迁移到用户管理页**：将设置页「邀请码管理」tab（生成/复制/复制注册链接/删除）整体迁移至用户管理页（`UsersView`），设置页移除该 tab；后端 invite API 不变。
- **影视库卡片「已有」文案去重**：修复合集中「已有 {{ episodeText(m) }}」模板前缀与 `episodeSummaryText` 自身「已有 N 缺失 M」前缀的重复渲染，保持「已有 15 缺失 0」单前缀输出。

## Capabilities

### New Capabilities

- `run-logs`: 运行日志页面的任务类型中文映射、真实分页（total 契约）与日志保留天数动态配置。
- `user-invite-management`: 用户管理页承载邀请码（分享码）的生成、复制、复制注册链接与删除管理，作为用户管理的一部分。

### Modified Capabilities

- `media-status`: 影视列表集数统计文案修复——「已有 N 缺失 M」不得因模板与聚合函数双重前缀重复渲染为「已有 已有 N 缺失 M」。

## Impact

- **前端**：`frontend/src/utils/format.ts`（TASK_TYPE_MAP）、`frontend/src/views/LogsView.vue`（筛选列表）、`frontend/src/stores/logs.ts`（分页 total 消费）、`frontend/src/views/SettingsView.vue`（移除邀请码 tab、新增保留天数配置项）、`frontend/src/views/UsersView.vue`（新增邀请码管理）、`frontend/src/views/MediaListView.vue`（卡片文案去重）、`frontend/src/config/settingsMeta.ts`（新增 `task_run_retention_days` 元数据）、`frontend/src/types/index.ts`（日志分页响应类型）。
- **后端**：`backend/app/routers/logs.py`（返回 `{items, total}` 契约）、`backend/app/routers/settings.py`（PATCH 白名单 + editable_keys 增补 `task_run_retention_days`）。
- **测试**：`frontend/src/utils/format.test.ts`（任务类型映射、文案去重）、`backend/tests/test_task_cleanup.py` / `test_prune_history.py`（保留天数配置读取验证）、日志分页契约测试。
- **API 变更**：`GET /api/logs` 响应结构从数组改为 `{items, total}`（**BREAKING**，前端同步适配）。