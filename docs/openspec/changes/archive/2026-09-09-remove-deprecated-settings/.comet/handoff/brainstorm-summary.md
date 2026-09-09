# Brainstorm Summary

- Change: remove-deprecated-settings
- Date: 2026-09-09

## 确认的技术方案

**关键事实（探查确认）**
- 前端 `frontend/src/config/settingsMeta.ts:240-244` 有 `download_queue_max_concurrent` 条目（标签含「已废弃」）。
- 后端 **无任何代码引用**（grep `*.py` 零匹配）：`config_store.py`、`routers/settings.py` 均无读写/白名单/透传该键；`_WHITELIST_EXACT` 不含它。
- **存量数据**：数据库 `system_config` 存在 `download_queue_max_concurrent = "50"`（2026-09-08 写入）。
- **GET /api/settings 返回 system_config 全量行（无字段过滤）** → 存量键值目前仍会出现在响应中，前端经 `getSettingMeta` 未知名回退会按英文键名展示。因此任务 2.1「GET 不再包含该键」需要真实的后端响应层改动。

**方案（用户已确认）**
- 后端 `routers/settings.py`：新增已废弃键常量 `_RETIRED_EXACT`，`get_settings` 构造 config 时跳过该键；DB 存量保留不删（无迁移）。
- 前端 `settingsMeta.ts`：删除 `download_queue_max_concurrent` 条目。
- 文档：`docs/影视下载两队列重设计.md:293` 历史设计文档提及**保留原样**（spec 允许文档说明性提及）；README 无提及。
- 测试：后端新增 GET /api/settings 响应不含该键的断言（注入存量键场景）；前端构建 + 全量 pytest。

## 关键取舍与风险

- 取舍：响应层排除 vs 删除存量行——前者保留历史数据（spec D4 要求），后者破坏式迁移被否。
- 风险：`GET /api/settings` 与 `media-poster-proxy` 窗口同时修改 `backend/app/routers/settings.py`（不同区域：本 change 改 GET 响应构造 + 顶部常量；poster 改 `_WHITELIST_EXACT`）。需精确 add 文件、避免 `git add -A` 相互污染。

## 测试策略

- 后端：pytest 全量 + 新增断言（DB 有存量键时 GET /api/settings 响应不含该键）。
- 前端：`npm run build`（vue-tsc + vite build）。

## Spec Patch

无（现有 delta spec 已覆盖：废弃键移除、不再读写透传、存量保留、设置页不含废弃项）。