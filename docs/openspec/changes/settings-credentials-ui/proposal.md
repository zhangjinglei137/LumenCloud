## Why

设置页「服务凭据配置」的交互与信息密度不佳：清除按钮位于输入框下方的独立操作区，操作路径割裂；字段右侧的说明标签混入长文案（如「必填（否则转存与直链不可用）」），用户只需知道是否必填；同时存在已明确标注「已废弃，不再生效」的配置项（扫描间隔）仍留在页面，造成困惑。

## What Changes

- **清除按钮移到输入框后面**：服务凭据输入框后紧跟「清除」操作，与输入框同一行
- **必填说明精简**：「必填（否则转存与直链不可用）」类长文案简化为「必填」标签，只展示必填与否；可选项不再强标
- **移除废弃配置项**：删除「扫描间隔（分钟）（已废弃）」项（settingsMeta 定义 + 后端 editable_keys/白名单引用 + 文档提及），不再展示「已废弃，不再生效」

## Capabilities

### New Capabilities
- `settings-credentials-ui`: 设置页服务凭据配置表单的交互布局（清除按钮位置）与必填信息展示能力

### Modified Capabilities
- `settings-lifecycle`: 移除已废弃的 `scan_interval_minutes` 配置项（不再展示、不再可编辑）

## Impact

- 前端：`frontend/src/views/SettingsView.vue`（清除按钮位置、必填标签渲染）、`frontend/src/config/settingsMeta.ts`（scan_interval_minutes 移除、default 文案精简）
- 后端：`backend/app/routers/settings.py`（editable_keys 白名单若含 scan_interval_minutes 则移除）、`backend/app/config.py`（如引用）
- 数据：存量 system_config 中 scan_interval_minutes 数据不删除（仅不再展示/编辑），避免误伤
- 无数据库迁移
