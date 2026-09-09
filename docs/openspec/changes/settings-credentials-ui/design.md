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
