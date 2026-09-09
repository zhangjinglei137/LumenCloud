## Context

设置页「下载队列最大并发数（download_queue_max_concurrent）」已废弃：容量准入机制上线后该值不再生效，前端 settingsMeta.ts:240 仍有定义（标签含「已废弃」）。参见 proposal.md - Why。

## Goals / Non-Goals

**Goals**
- 从设置页移除该废弃项（前端元数据）
- 清理代码中对 `download_queue_max_concurrent` 的读写/透传引用
- 清理文档提及

**Non-Goals**
- 删除 system_config 中的存量键值（保留，兼容历史数据）
- 不改任何下载队列/容量准入逻辑
- 不引入设置项生命周期通用框架（仅处理本废弃项）

## Decisions

### D1: 前端移除

- `frontend/src/config/settingsMeta.ts`：删除 `download_queue_max_concurrent` 条目。
- 设置页读取后端返回的 fields 时，该键若仍被后端返回则按 `getSettingMeta` 未知名回退逻辑处理——因此**后端也必须停止返回/透传该键**，否则前端会按原始键名展示。

### D2: 后端清理

- 审计 `config_store.py`、`routers/settings.py`、`services/config_store.py` 中对该键的读写与字段白名单；确认容量准入逻辑无读取（transfer.py 使用 own 常量 `_PAUSE_CONFIG_KEY` 等，不依赖该键——验证后删除透传）。
- settings 接口的 fields 列表/校验白名单中移除该键，保证 GET settings 不再返回它。

### D3: 文档清理

- 检索 `docs/`、`README.md`、设计文档中「下载队列最大并发数 / download_queue_max_concurrent」提及并移除或标注历史。

### D4: 存量数据

- 不新增迁移；`system_config` 中已有该键保留（无破坏式删除）。后端读写路径不 touch 它即可。

## Risks / Trade-offs

- [后端仍透传该键 → 前端 fallback 展示英文键名] → 前后端同步移除，验证 GET /api/settings 响应不含该键。
- [有历史配置依赖代码读取该键] → grep 全仓验证无引用（测试也需同步）。

## Migration Plan

1. 前端删 settingsMeta 条目。
2. 后端删 settings 透传/白名单引用与文档提及。
3. grep 验证 `download_queue_max_concurrent` 仅剩存量数据说明（可无残留）。
4. 回滚：git revert 即可，无数据迁移。

## Open Questions

无。