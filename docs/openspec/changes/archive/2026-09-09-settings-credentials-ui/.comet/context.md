# Comet Design Handoff

- Change: settings-credentials-ui
- Phase: design
- Mode: compact
- Context hash: 9442d40b6efe80eca965f57d8aebcdb09cfdc05c947397f812d725600e52d47a

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This handoff is a deterministic, source-traceable context pack, not an agent-authored summary.

## docs/openspec/changes/settings-credentials-ui/proposal.md

- Source: docs/openspec/changes/settings-credentials-ui/proposal.md
- Lines: 1-24
- SHA256: 52cf6546a065e300b3699773b03314038533e295d0ce5f5112eecdd873478801

```md
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

```

## docs/openspec/changes/settings-credentials-ui/design.md

- Source: docs/openspec/changes/settings-credentials-ui/design.md
- Lines: 1-65
- SHA256: 9e99ff28f199258d569fc5d766ae9af48549b7b9b0a7e21565e1c2a314511afb

```md
# Design: settings-credentials-ui

## Context

现状（参见 proposal.md - Why）：
- `SettingsView.vue` 服务凭据字段结构为 `.cred-field`：label 行 → el-input → `.cred-actions`（清除按钮独立行）
- `settingsMeta.ts` 每个字段 `default` 携带长文案（如「必填（否则转存与直链不可用）」）作为右侧 info 标签
- `scan_interval_minutes` 在 `settingsMeta.ts`（标记已废弃）、后端 `settings.py` editable_keys 白名单、media 模型/详情页均有出现 —— 注意：媒体详情页的 `scan_interval_minutes` 是 per-media 字段（详情页编辑），与设置页系统级废弃项是不同上下文

## Goals / Non-Goals

**Goals**
- 清除按钮与输入框同行（字段单元内紧跟）
- 必填信息精简为「必填 / 可选」
- 设置页移除 scan_interval_minutes 废弃项（settingsMeta + settings.py 白名单）

**Non-Goals**
- 不改后端 settings GET/PATCH 的凭据明文回显逻辑
- 不改媒体详情页 per-media scan_interval_minutes（属于 media-detail-ui / 既有行为，非系统设置废弃项）
- 不删除存量 system_config 数据

## Decisions

### D1：清除按钮移入输入框行

**决策**：`.cred-field` 改 flex 布局，`el-input` 后紧跟「清除」按钮（input + button 同一行），删除独立 `.cred-actions` 行；quark「验证 folderId」按钮保留在字段内（输入框后、清除旁）。

**理由**：操作与输入对象同视觉单元，符合「清除按钮需要在输入框的后面」的明确要求；flex 避免绝对定位。

**备选**：使用 el-input 的 append slot 放清除按钮 → 语义上是输入后缀而非操作按钮，且 clearable 只清输入框不改后端，否决。

### D2：必填文案精简

**决策**：`default` 字段语义调整为「必填/可选」二值标签。对现有含长文案的 default（「必填（否则…）」）统一改为「必填」；可选字段 default 改为「可选」（或「可选，留空 = …」保持简短）；长描述保留在 desc（tooltip 说明）。

**理由**：用户明确「只展示必填即可」；desc tooltip 已承载详细说明，不丢失信息。

**备选**：前端渲染时截断 default 文案 → 侵入且不可控，否决。

### D3：移除设置页废弃项

**决策**：删除 `settingsMeta.ts` 的 `scan_interval_minutes` 条目，并从 `backend/app/routers/settings.py` editable_keys 白名单移除该键（保留 media 模型的列与详情页 per-media 编辑，避免破坏既有功能）。

**理由**：设置页不再展示/编辑该废弃项；per-media 字段是另一语义（每影视扫描间隔，虽后端 scan 不再读取，但属于媒体详情上下文，不在本 change 范围）。

**备选**：同时清理 media 模型列 → 超范围（需迁移且影响详情页），否决。

## Risks / Trade-offs

- [移除白名单后存量 system_config 中该键仍在] → 不清数据，仅不可编辑；GET 返回但前端无 meta 时不渲染（现有 getSettingMeta 回退机制）
- [必填精简后用户不明具体后果（如「必填（否则转存不可用）」）] → desc tooltip 保留完整说明，默认标签只表达必填性
- [清除按钮与输入框同行在窄屏挤压] → flex 允许按钮换行（flex-wrap），不强制同行

## Migration Plan

纯前端 + 后端白名单微调：
1. settingsMeta.ts：精简 default 文案、删除 scan_interval_minutes
2. SettingsView.vue：清除按钮移入输入框行
3. settings.py：editable_keys 移除 scan_interval_minutes
4. 验证：设置页无废弃项、必填标签简洁、清除按钮跟随输入框、保存/清除正常
5. 回滚：还原上述文件；无数据库影响

## Open Questions

无（设计决策均已确定）。

```

## docs/openspec/changes/settings-credentials-ui/tasks.md

- Source: docs/openspec/changes/settings-credentials-ui/tasks.md
- Lines: 1-17
- SHA256: 13b6ff602f6e19e552d8f0c16a6c111a719e305aad357070916970fa8834c242

```md
# Tasks: settings-credentials-ui

## 1. 前端：清除按钮位置与必填文案

- [ ] 1.1 SettingsView.vue `.cred-field` 改 flex 布局，清除按钮移入输入框同一行（紧跟输入框后；quark「验证 folderId」保留同字段内），验证清除按钮与输入框同行且窄屏不挤压
- [ ] 1.2 settingsMeta.ts 精简 default 文案：含「必填（否则…）」的字段统一为「必填」、可选字段为「可选」短标签（长描述保留在 desc），验证设置页必填标签简洁
- [ ] 1.3 前端测试补充：必填标签渲染、清除按钮位置结构用例，验证 `vitest` 通过

## 2. 移除废弃配置项

- [ ] 2.1 删除 settingsMeta.ts 中 `scan_interval_minutes` 条目，验证设置页不再出现「扫描间隔（分钟）（已废弃）」
- [ ] 2.2 backend/app/routers/settings.py editable_keys 白名单移除 `scan_interval_minutes`，验证该键不再可编辑且其余键不受影响
- [ ] 2.3 全仓 grep 确认「已废弃，不再生效」展示文案无残留（保留 media 模型列与详情页 per-media 字段），验证 grep 干净

## 3. 验证

- [ ] 3.1 手动验证设置页：无废弃项、必填标签简洁、清除按钮跟随输入框、保存/清除凭据功能正常，验证 `npm run build` 与后端启动无报错

```

## docs/openspec/changes/settings-credentials-ui/specs/settings-credentials-ui/spec.md

- Source: docs/openspec/changes/settings-credentials-ui/specs/settings-credentials-ui/spec.md
- Lines: 1-45
- SHA256: c394bdf4d4f5d9c9cd274a4edacfbfe7eb4efcf4a6934de7d2413ec51bce0ee9

```md
## Purpose

为设置页「服务凭据配置」表单提供清晰的交互与信息密度：清除按钮与输入框同行，必填信息只表达「是否必填」，废弃配置项不再出现在设置页。

## ADDED Requirements

### Requirement: 清除按钮位于输入框后

服务凭据配置中每个字段的「清除」操作 SHALL 紧跟该字段输入框之后（同一行/同一字段单元内），不得置于独立的远离操作区。

#### Scenario: 清除按钮跟随输入框
- **WHEN** 用户查看某个服务凭据字段
- **THEN** 该字段输入框后方显示「清除」按钮，操作路径清晰

#### Scenario: 清除凭据
- **WHEN** 用户点击某字段的「清除」
- **THEN** 二次确认后清除该凭据，保留其余字段不变

### Requirement: 必填信息只展示是否必填

服务凭据字段的必填提示 SHALL 只表达「必填」与「可选」两类信息，不混入长描述文案（如「否则转存与直链不可用」）；必填字段标注「必填」，可选字段不强制标注。

#### Scenario: 必填字段简洁标注
- **WHEN** 用户查看必填服务凭据字段
- **THEN** 字段旁只显示「必填」标签，不含长文案

#### Scenario: 可选字段
- **WHEN** 用户查看可选服务凭据字段
- **THEN** 不显示必填标签（或不显示误导性说明），字段说明保留在原有说明入口

### Requirement: 移除废弃配置项

设置页 SHALL 不再展示已标注「已废弃，不再生效」的配置项（scan_interval_minutes）；该键 SHALL 从设置页可编辑白名单中移除，代码中不再存在「已废弃，不再生效」文案展示。

#### Scenario: 设置页无废弃项
- **WHEN** 用户打开设置页
- **THEN** 不再看到「扫描间隔（分钟）（已废弃）」或任何「已废弃，不再生效」配置项

#### Scenario: 废弃键不可编辑
- **WHEN** 系统处理设置项可编辑集合
- **THEN** scan_interval_minutes 不在可编辑白名单中，页面无法修改该键

#### Scenario: 存量数据不回退展示
- **WHEN** system_config 中存在存量的 scan_interval_minutes 数据且用户打开设置页
- **THEN** 设置页任意 Tab 均不展示该键（含英文键名回退展示），存量数据仅保留不清除

```
