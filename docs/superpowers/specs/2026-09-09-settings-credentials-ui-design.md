---
comet_change: settings-credentials-ui
role: technical-design
canonical_spec: openspec
---

# Design Doc: settings-credentials-ui 服务凭据表单交互与废弃配置项清理

## 背景与目标

设置页「服务凭据配置」交互与信息密度不佳（清除按钮远离输入框、必填提示混入长文案、废弃配置项残留）。本设计落地三项变更：清除按钮与输入框同行、必填信息只表达「必填/可选」、移除 `scan_interval_minutes` 废弃配置项。

## 改动清单

| # | 文件 | 改动 |
|---|------|------|
| 1 | `frontend/src/views/SettingsView.vue` | `.cred-field` 结构调整（清除按钮移入输入框行）；CSS flex 布局 |
| 2 | `frontend/src/config/settingsMeta.ts` | default 文案精简；删除 `scan_interval_minutes` 条目 |
| 3 | `backend/app/routers/settings.py` | `_WHITELIST_EXACT` 移除 `scan_interval_minutes`；`_RETIRED_EXACT` 加入该键 |
| 4 | `backend/tests/test_settings_retired.py` | 扩展废弃键不透传用例（scan_interval_minutes） |
| 5 | 前端测试（新增） | 必填标签、清除按钮结构、废弃项不渲染用例 |

## 详细设计

### D1 清除按钮移入输入框行

**现状结构**（`SettingsView.vue` 548-608）：
```
.cred-field (flex column)
├── .cred-field-main → .cred-label-row (label + ?icon + 必填tag + 已修改tag)
├── el-input.cred-input
└── .cred-actions (清除 + 验证folderId)   ← 独立行
```

**目标结构**：
```
.cred-field (flex column)
├── .cred-field-main → .cred-label-row (label + ?icon + 必填tag + 已修改tag)
└── .cred-input-row (flex row, gap 8px, flex-wrap)          ← 新增
    ├── el-input (flex: 1; min-width: 0)
    ├── 清除按钮 (link danger, 现有 clearCred 逻辑不变)
    └── 验证 folderId (仅 quark_default_folder, 现有 runQuarkVerify 逻辑不变)
```

**实现要点**：
- `.cred-field` 保持 `flex-direction: column`；新增 `.cred-input-row { display:flex; align-items:center; gap:8px; flex-wrap:wrap }`
- `.cred-input` 由 `flex: none` 改为 `flex: 1; min-width: 0`（操作行内主占位）
- 删除 `.cred-actions` 独立行的 HTML 与 CSS（1019-1023）；操作按钮移入 `.cred-input-row`
- 窄屏（`.cred-list` 已回退单列）+ 字段过窄时 `flex-wrap` 允许按钮换行不强制同行，防挤压
- `clearCred` / `runQuarkVerify` / `savingCredKeys` 等脚本逻辑零改动

### D2 必填标签精简

**规则**：`settingsMeta.ts` 的 `default` 字段仅对凭据类键精简为「必填」/「可选」；「自动生成，无需修改」（internal 键）与业务参数「默认 XX」标签保留。

**具体替换**：

| 键 | 现值 | 新值 |
|----|------|------|
| alist_base_url | 必填（否则转存与直链不可用） | 必填 |
| cloudsaver_base_url | 必填（否则搜索与转存不可用） | 必填 |
| aria2_rpc_url | 必填（否则无法下载） | 必填 |
| nastools_base_url | 必填（否则不会自动入库） | 必填 |
| emby_base_url | 必填（否则无法判定入库状态） | 必填 |
| tmdb_api_key | 必填（否则无法搜索影视信息） | 必填 |
| tmdb_proxy | 可选，留空 = 官方地址 | 可选 |
| tmdb_http_proxy | 可选，留空 = 直连 | 可选 |
| pushplus_token | 可选，留空 = 只用站内通知 | 可选 |
| quark_default_folder | 可选，留空 = 不指定 | 可选 |
| emby_series_library_ids | 可选，不选 = 不过滤 | 可选 |

保留：internal_*（自动生成，无需修改）、业务参数「默认 XX」/「默认开启」/「默认关闭」等。

**说明**：长描述全部保留在 `desc`（tooltip 问号入口），信息不丢失；`required` 语义由 default 标签表达，不改后端。

### D3 移除废弃配置项 scan_interval_minutes

**现状引用分布**：
- `frontend/src/config/settingsMeta.ts:197-201` — 条目（必删）
- `backend/app/routers/settings.py:36` — `_WHITELIST_EXACT`（必删，editable_keys 自动不含）
- **保留**（per-media 详情页字段，另一语义）：`backend/app/models/__init__.py:69`（模型列）、`backend/app/routers/media.py:113,293`、`frontend/src/views/MediaDetailView.vue:38,49,342`、`frontend/src/types/index.ts:52,104`
- `backend/app/tasks/scan.py:1135,1167` — 注释提及，保留

**关键深化：加入 `_RETIRED_EXACT`**

open 阶段 design.md 的 Risk 项称「GET 返回但前端无 meta 时不渲染（现有 getSettingMeta 回退机制）」——**此判断有误**。实际链路：
1. `GET /api/settings`（settings.py:137-145）遍历 system_config 全量行，仅在 `_RETIRED_EXACT` 或敏感键时排除
2. 前端 `configEntries`（SettingsView.vue:57-65）`!(editableKeys.includes(k) && !getSettingMeta(k).selectOptions)`——键不在 editable_keys 时**保留**进入 configEntries
3. `businessEntries` 按 `getSettingMeta(k).selectOptions` 过滤，scan_interval_minutes 的 meta 删除后 fallback 为 `{ label: key, desc: '' }`（**英文键名**），无 selectOptions → 进入「业务参数」Tab 渲染

结论：**仅移除白名单时，存量 system_config 中的 scan_interval_minutes 会在「业务参数」Tab 以英文键名回退展示**，违反 spec「设置页不再展示该键」。

**修复**：与既有 `download_queue_max_concurrent` 相同的模式（settings.py:73-75 `_RETIRED_EXACT` + `test_settings_retired.py`）——将 `scan_interval_minutes` 加入 `_RETIRED_EXACT`，GET 响应层一律不透传该键。存量数据保守保留不删除。

### 前端渲染细节（getSettingMeta）

- 删除 meta 条目后，必须确认没有其他调用方依赖 `getSettingMeta('scan_interval_minutes')` 的 label——`SettingsView` 凭据表单与业务参数 Tab 均由 editable_keys / configEntries 驱动，删除后该键不再出现于任一 Tab，无调用残留风险
- `CRED_GROUP_ORDER` / `CRED_GROUP_LABELS` 中的 `'scan'` 分组保留（scan_baseline_required 等仍有键）

## 测试策略

### 后端 pytest（扩展 `backend/tests/test_settings_retired.py`）
- `editable_keys` 响应不含 `scan_interval_minutes`
- 注入存量 system_config 行后，GET 的 `system_config` / `config` 均不含该键
- PATCH `{scan_interval_minutes: "60"}` → 422（不在白名单）

### 前端 vitest（新增 `frontend/src/views/SettingsView.test.ts`）
- 必填标签渲染：含「必填」标签且不含「否则…」长文案
- 清除按钮结构：操作行包含清除按钮且与输入框同级（DOM 断言）
- 废弃项不渲染：system_config 含 scan_interval_minutes 时业务参数区不出现该键

> 注：元素-plus 组件在 vitest 中的渲染需 mock 局部组件或使用 mount（项目现有测试为纯函数测试 `utils/format.test.ts`，首次引入 SFC 测试需确认测试环境配置，必要时退化为组件结构静态断言或 store 级测试）。

### 手动验证
- `npm run build`（vue-tsc + vite）通过；后端启动无报错
- 设置页：清除按钮与输入框同行、窄屏不异常挤压、必填标签简洁、无「扫描间隔（分钟）（已废弃）」项

## 边界条件

- **存量数据**：system_config 中已存在的 scan_interval_minutes 不清除，仅 GET 不透传
- **per-media 字段**：媒体详情页的 per-media scan_interval_minutes 完全不受影响（media.py/model/MediaDetailView/types）
- **PATCH 行为**：移除白名单后该键 PATCH → 422（现有逻辑自动生效，无需改动）
- **scheduler 键**：`scheduler.*` 动态前缀不受影响（`_is_allowed_key` 前缀放行独立于精确白名单）
- **quark 验证 folderId**：随清除按钮一起移入操作行，验证逻辑/工具提示不变

## 回滚方案

- 还原 settingsMeta.ts、SettingsView.vue、settings.py 三处改动即可；无数据库变更，无迁移

## Open Questions

无（设计经用户确认）。