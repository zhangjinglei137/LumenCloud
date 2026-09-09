# Comet Design Handoff

- Change: remove-deprecated-settings
- Phase: design
- Mode: compact
- Context hash: e7346d70fabc855302eef151e1b78d3764b45da1f189321da29d9296c15d68fc

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This handoff is a deterministic, source-traceable context pack, not an agent-authored summary.

## docs/openspec/changes/remove-deprecated-settings/proposal.md

- Source: docs/openspec/changes/remove-deprecated-settings/proposal.md
- Lines: 1-24
- SHA256: 69ae340aa0e51ae44c2937cf4234f58f2a4d7a1490bf5a7f320befb020e129b5

```md
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
```

## docs/openspec/changes/remove-deprecated-settings/design.md

- Source: docs/openspec/changes/remove-deprecated-settings/design.md
- Lines: 1-50
- SHA256: b20cc2566cc571175af0940fc9e44d0d21db35172a998c7f06b64d92994cc7fc

```md
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
```

## docs/openspec/changes/remove-deprecated-settings/tasks.md

- Source: docs/openspec/changes/remove-deprecated-settings/tasks.md
- Lines: 1-16
- SHA256: 27ccbd611a53bcf6cbee5e35d54eb2b7aea823c4624b7f6e7bc6d7bd721c67e1

```md
## 1. 前端移除废弃项

- [ ] 1.1 从 settingsMeta.ts 删除 download_queue_max_concurrent 条目；验证前端构建通过且设置页不再展示该条目

## 2. 后端清理引用

- [ ] 2.1 审计并移除 config_store 与 settings router 中对 download_queue_max_concurrent 的读取/校验/透传；验证 GET /api/settings 响应不再包含该键
- [ ] 2.2 grep 全仓验证 download_queue_max_concurrent 无残留代码引用（测试同步清理）；验证 pytest 通过

## 3. 文档清理

- [ ] 3.1 检索并清理 docs/ 与 README 中「下载队列最大并发数 / download_queue_max_concurrent」提及；验证文档检索无残留

## 4. 存量数据与收尾

- [ ] 4.1 确认 system_config 存量键值保留不被删除（无迁移）；验证存储中键值不受影响
- [ ] 4.2 全量后端测试 + 前端构建通过
```

## docs/openspec/changes/remove-deprecated-settings/specs/settings-lifecycle/spec.md

- Source: docs/openspec/changes/remove-deprecated-settings/specs/settings-lifecycle/spec.md
- Lines: 1-28
- SHA256: edbb600192dcc25f9171e7c4e2f2e418788ffb06923e93e93fc0a86fd3bbb4cc

```md
## Purpose

为系统设置提供设置项生命周期管理能力：识别并移除已废弃配置项（如下载队列最大并发数），保证设置页仅展示当前生效的设置项，避免残留引用干扰使用与维护。

## ADDED Requirements

### Requirement: 废弃设置项从设置页移除

系统 SHALL 不在设置页展示已废弃且失效的设置项（下载队列最大并发数 `download_queue_max_concurrent`）；前端设置元数据中 SHALL 不再包含该废弃项定义。

#### Scenario: 设置页不含废弃项
- **WHEN** 用户打开设置 → 业务参数
- **THEN** 页面不展示「下载队列最大并发数」相关条目

### Requirement: 废弃键不再参与读写与透传

系统 SHALL 不再读取、写入或透传已废弃配置键（download_queue_max_concurrent）；代码中不得残留对该键的引用。

#### Scenario: 废弃键无代码引用
- **WHEN** 检索代码中 download_queue_max_concurrent 引用
- **THEN** 除存量数据兼容与文档说明外无引用

### Requirement: 存量数据不误伤

已经从 system_config 写入过废弃键值的存量数据 SHALL 被保留不受影响，不做破坏式删除。

#### Scenario: 存量键值保留
- **WHEN** 系统升级后旧 system_config 中已有该键值
- **THEN** 该键值保留在存储中，不影响其他设置读写
```
