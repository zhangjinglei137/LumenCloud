# Brainstorm Summary

- Change: settings-credentials-ui
- Date: 2026-09-09

## 确认的技术方案

### D1 清除按钮移入输入框行
- `.cred-field` 结构改为：label 行 + 操作行（el-input 与「清除」「验证 folderId」按钮同一行，flex 布局，input `flex:1; min-width:0`）
- 窄屏用 `flex-wrap` 兜底换行，避免挤压；删除独立 `.cred-actions` 行

### D2 必填标签精简
- 「必填（否则…）」6 个键 → 统一「必填」（alist_base_url / cloudsaver_base_url / aria2_rpc_url / nastools_base_url / emby_base_url / tmdb_api_key）
- 「可选，留空 = …」→「可选」（tmdb_proxy / tmdb_http_proxy / pushplus_token / quark_default_folder / emby_series_library_ids）
- internal 键「自动生成，无需修改」、业务参数「默认 XX」标签保留（非必填语义，spec 只约束服务凭据字段）

### D3 移除废弃配置项
- `settingsMeta.ts` 删除 scan_interval_minutes 条目
- `settings.py` `_WHITELIST_EXACT` 移除 `scan_interval_minutes`（editable_keys 自动不含）
- **深化**：`scan_interval_minutes` 加入 `_RETIRED_EXACT`（GET 不透传存量，避免前端英文键名回退展示）；沿用 download_queue_max_concurrent 既有模式
- 保留 per-media scan_interval_minutes（模型列、media.py、MediaDetailView、types）

## 关键取舍与风险

| 取舍 | 风险 |
|------|------|
| `_RETIRED_EXACT` 后端不透传（而非前端 HIDDEN_KEYS） | 最小侵入、复用既有模式；存量数据不清除仅不展示 |
| 必填标签只动凭据类，业务参数原样 | 改动面最小，spec 仅约束服务凭据字段 |
| 清除按钮同行靠 flex-wrap 兜底 | 窄屏按钮可能换行到第二行（可接受，不挤压） |

## 测试策略

- 后端 pytest：扩展 `test_settings_retired.py` —— scan_interval_minutes 不在 editable_keys、GET 不透传存量值、PATCH 该键返回 422
- 前端 vitest：新增 SettingsView 用例 —— 必填标签简洁（无长文案）、清除按钮与输入框同行结构、废弃项不渲染
- 手动验证：`npm run build` + 设置页保存/清除交互正常

## Spec Patch

- delta spec 补充验收场景：「存量 system_config 含 scan_interval_minutes 时，设置页任意 Tab 均不展示该键（含英文键名回退）」