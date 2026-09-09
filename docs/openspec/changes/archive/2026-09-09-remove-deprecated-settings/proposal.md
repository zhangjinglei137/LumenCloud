## Why

设置页存在已废弃配置项「下载队列最大并发数（`download_queue_max_concurrent`）」，容量准入调度机制上线后其语义早已失效，但前端仍展示、代码与文档仍有残留引用，干扰用户理解与维护。

## What Changes

- **移除前端设置项**：从 `settingsMeta.ts` 删除 `download_queue_max_concurrent` 的元数据定义，设置页不再展示该废弃项。
- **清理后端引用**：移除 `config_store` / 设置管理 / routers/settings 中对 `download_queue_max_concurrent` 的读取、白名单或透传引用（若存在）。
- **清理文档**：删除文档中对「下载队列最大并发数」的提及（设计文档、README 等）。
- **存量数据不误伤**：`system_config` 中已存在的该键值保留不删（避免破坏式迁移）；仅不再参与读写与展示。

## Capabilities

### New Capabilities
- `settings-lifecycle`: 设置项生命周期管理：废弃设置项的识别与移除规则、设置页仅展示生效项

### Modified Capabilities
<!-- 无既有能力被修改：项目 specs 目录尚为空（首次建立 specs）。 -->

## Impact

- **frontend**: `src/config/settingsMeta.ts`（删除条目）
- **backend**: `app/services/config_store.py`、`app/routers/settings.py`（如引用该键则清理）
- **docs**: 设计文档/README 中「下载队列最大并发数」提及
- **tests**: 如有对废弃键的测试/断言则更新或删除