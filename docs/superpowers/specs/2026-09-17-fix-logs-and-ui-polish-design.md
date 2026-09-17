---
comet_change: fix-logs-and-ui-polish
role: technical-design
canonical_spec: openspec
archived-with: 2026-09-17-fix-logs-and-ui-polish
status: final
---

# 运行日志与界面修复 — 深度技术设计

## Context

参见 `docs/openspec/changes/fix-logs-and-ui-polish/proposal.md` 的动机与 `design.md` 的高层决策（D1-D5）。本文件是 D1-D5 的深度技术细化，聚焦实现级细节、边界条件与测试策略。

现状代码约束：

- 前端任务类型映射：`frontend/src/utils/format.ts:417` `TASK_TYPE_MAP`（Record<string, [中文标签, tag type]>），`taskTypeLabel`/`taskTypeType` 未知键回退原值 / `info`。筛选列表 `LogsView.vue:18` 为硬编码数组，与 map 键集不完全一致。
- 后端任务类型写入点（已逐文件核对 `record_task_run` 调用，含 `scan.py:1517/1583`、`transfer.py` 多处、`cleanup.py:189/197`、`nastools_sync.py:94/114/174`、`notification_scan.py:101`、`capacity_alert.py`、`recovery.py:276`）：
  - `scan_media`（单影视巡检）、`scan_all_media`（定时巡检）
  - `transfer`、`cleanup`
  - `capacity_alert`、`recover`
  - `sync_nastools`（非 `nastools_sync` ！）、`notify`（非 `notification_scan` ！）、`prune_history`
- 日志分页：`backend/app/routers/logs.py:35` `list_logs` 返回 `list[dict]`；前端 `stores/logs.ts` 以 `limit+1` 探测估算 total。
- 保留天数：`backend/app/tasks/cleanup.py:156` `_RETENTION_CONFIG_KEY = "task_run_retention_days"`，`prune_history_job` 读取 system_config（缺省 `_RETENTION_DAYS=30`）。
- 设置白名单：`backend/app/routers/settings.py:32` `_WHITELIST_EXACT` + `:70` `_EDITABLE_KEYS = frozenset(_WHITELIST_EXACT - {"emby_series_library_ids"})`——**新业务键若不从 `_EDITABLE_KEYS` 排除，会被前端 settings 页当作服务凭据文本框渲染**。
- 邀请码：设置页 `SettingsView.vue:728-779`（invites tab），逻辑在组件内（`generateCount`/`generate`/`removeInvite`/`copyText`/`copyInviteCode`/`copyRegisterLink`），store 动作在 `stores/settings.ts`（fetchInvites/createInvites/deleteInvite）；后端 `admin.py` invites 三接口。`UsersView.vue` 为纯用户列表 + 前端本地分页。
- 卡片文案：`MediaListView.vue:165` 模板 `已有 {{ episodeText(m) }}`；`episodeSummaryText`（`format.ts:251`）已返回「已有 N 缺失 M」（`已有 ${avail} 缺失 ${missing}`）→ 双重前缀。`format.test.ts:129-137` 有既有断言（`episodeSummaryText` 输出单前缀），表格视图复用该函数。

## Goals / Non-Goals

**Goals：**
- 任务类型映射与后端实际写入值 100% 对齐，未知值优雅兜底
- 日志分页 total 真实（后端 count），前端移除探测估算
- `task_run_retention_days` 可在设置页配置并即时生效
- 邀请码管理逻辑完整迁移至用户管理页，后端 API 零改动
- 卡片文案单前缀「已有 N 缺失 M」

**Non-Goals：**
- 不重命名/迁移历史 task_run 数据；不新增任务类型
- 不改清理任务调度逻辑与 task_run 写入
- 不引入新依赖；不做 posting schema 变更

## Decisions

### D-A1：`TASK_TYPE_MAP` 精确键集

最终键集（与后端实际写入一一对应）：

| 键 | 中文标签 | tag | 来源文件 |
|---|---|---|---|
| `scan_media` | 影视巡检 | primary | scan.py |
| `scan_all_media` | 定时巡检 | primary | scan.py |
| `transfer` | 转存 | warning | transfer.py |
| `cleanup` | 空间清理 | info | cleanup.py |
| `prune_history` | 历史清理 | info | cleanup.py |
| `capacity_alert` | 容量告警 | danger | capacity_alert.py |
| `recover` | 超时恢复 | danger | recovery.py |
| `sync_nastools` | 目录同步入库 | success | nastools_sync.py |
| `notify` | 通知 | info | notification_scan.py |

历史别名决策：`media_scan`、`recovery` 曾在早期版本产生（`LogsView.vue:17` 注释「media_scan/recovery 为历史别名」）。存量数据可能含历史值 → **保留这两条兜底映射**（中文标签展示），但筛选列表不列出（历史类型用户无需主动筛选）。`transfer_retry`、`download`、`nastools_sync`、`notification_scan` 无任何写入点（grep 0 命中）→ **从 map 与列表一并移除**；若存量数据残留此类值，走「未知类型回退原值 + info」兜底，不吞数据。

`LogsView.vue` 筛选列表 = map 中「当前有效键」子集（排除 `media_scan`/`recovery` 兜底别名），与 D1 一致。

### D-A2：日志分页 `{items, total}` 契约

后端 `list_logs`：
1. 保持现有 filter/join/order 逻辑构造 `stmt`
2. 基于同一筛选条件构造 `count_stmt = select(func.count()).select_from(stmt.subquery())`（简单可靠；或对无 join 搜索场景用 `select(func.count(TaskRun.id)).where(同条件)`——为降低实现偏差，采用 **subquery count**，筛选条件单点维护）
3. 返回 `{"items": [原 dict 列表], "total": n}`

前端：
- `types/index.ts` 增 `LogListResponse { items: LogItem[]; total: number }`
- `api/index.ts` `listLogsApi` 返回 `LogListResponse`
- `stores/logs.ts` `fetchPage`：`const res = await listLogsApi({...filter, limit: size, offset})`；`this.items = res.items`；`this.total = res.total`；保留 `res.items.length === 0 && page > 1` 回退一页保护（末页越界兜底）；删除 hasMore/slice 估算逻辑
- `LogsView.vue` 模板无需改动（已绑定 `store.total`/`store.page`/`store.pageSize`）

### D-A3：保留天数配置接入

后端 `settings.py`：
- `_WHITELIST_EXACT` 增加 `"task_run_retention_days"`
- `_EDITABLE_KEYS` 排除它：`frozenset(_WHITELIST_EXACT - {"emby_series_library_ids", "task_run_retention_days"})`——保证不落入凭据文本框表单（`SettingsView` editable_keys 渲染区），而落入「业务参数」区（该区按白名单+元数据渲染）

前端：
- `settingsMeta.ts` 增加元数据：label「运行日志保留天数」、desc 注明「同时影响容量快照清理（task_run 与 quark_capacity_log）」、placeholder「如 30」、default「30（默认）」
- `SettingsView.vue` 业务参数区新增渲染项（数字输入 + 保存按钮，复用既有 `saveKey` 模式；若无匹配现有渲染分支则按 `emby_series_library_ids`/`scan_baseline_required` 模式补一个 number 渲染分支）
- `cleanup.py` 零改动（已读取该键；`max(1, int(...))` 已防 0/负值）

### D-A4：邀请码管理迁移

原则：**逻辑与模板整体迁出，store 动作复用 `stores/settings.ts`**（避免把 invite API 逻辑复制进 users store —— 后端接口归属 admin 域，settings store 已有封装；`UsersView` 引入 `useSettingsStore` 无架构副作用）。

`UsersView.vue` 变更：
- import `useSettingsStore`；`const settings = useSettingsStore()`
- 组件脚本复制：`generateCount` ref、`generate`、`removeInvite`、`copyText`、`copyInviteCode`、`copyRegisterLink`（`copyText` 是通用剪贴板函数，设置页原样复制；如 `UsersView` 已无此类函数则原样迁入）
- 模板：`el-tabs` 里加一个「邀请码管理」tab pane，内容 = 原 invites 表格（生成按钮/空态/表格/复制/复制链接/删除），`onMounted` 中 `Promise.all([store.fetchList(), settings.fetchInvites()])`
- 保留用户列表既有内容为第一个 tab（`el-tabs` 包裹；若破坏既有测试则调整为同页分区块而非 tab——实现时优先不改变用户列表 DOM 结构，用 tab 包裹最外层让既有 `pagedItems`/分页不变）

`SettingsView.vue` 变更：
- 删除 invites `el-tab-pane` 块（728-779）
- 删除 `generateCount`/`generate`/`removeInvite`/`copyInviteCode`/`copyRegisterLink` 及相关 import（`copyText` 若仅 invites 使用则一并移除）
- `onMounted` 移除 `store.fetchInvites()`（保留 `store.fetchSettings()`）
- store mock 测试 `SettingsView.test.ts` 中 invites 相关 stub 可保留（多余 mock 无害）或清理

### D-A5：卡片文案去重

`MediaListView.vue:165`：`<span v-else>已有 {{ episodeText(m) }}</span>` → `<span v-else>{{ episodeText(m) }}</span>`。

`episodeSummaryText` 零改动（format.test.ts:129-137 既有断言保护；表格视图 `media-status` spec 场景依赖「已有 N 缺失 M」完整串）。补充测试：format.test.ts 增加 `已有 15 缺失 0`/`已有 5 缺失 15` 断言（现已有 missing>0 场景，补齐 missing=0 场景防回归）。

## Risks / Trade-offs

| 风险 | 缓解 |
|---|---|
| `/api/logs` 契约 BREAKING | 同仓同发布；前端唯一调用方同步适配；后端测试同步更新 |
| `task_run_retention_days` 误入凭据表单 | `_EDITABLE_KEYS` 显式排除 + settingsMeta 注册；settings 测试覆盖 |
| `UsersView` 引入 settings store 增加耦合 | 仅复用 invite 三动作，无跨域共享状态；如后续 users store 复杂化可再抽离 |
| 迁移邀请码时遗漏 `copyText` 依赖 | 迁移脚本核对 import 清单；`npm run build`/vitest 捕获未定义引用 |
| map 与后端未来新增类型漂移 | map 注释标注来源文件名；新增任务类型时同步检查（tasks.md 已含审计任务） |

## Migration Plan

- 前后端同仓同版发布；无数据库迁移（系统配置为 key-value）
- 存量 task_run 历史类型：展示层兜底（未知类型原值 + info），数据不动
- 回滚：`/api/logs` 契约需前后端同步回滚；其余单项可独立回滚

## Testing Strategy

- 前端 vitest：`format.test.ts`（任务类型新键映射 + missing=0 单前缀 + 未知回退）、`UsersView.test.ts`（邀请码区块渲染与操作）、`SettingsView.test.ts`（移除 invites 后通过，无引用残留）、`npm run build`
- 后端 pytest：`test_prune_history.py`/`test_task_cleanup.py`（保留天数配置键读取）、引用 `/logs` 契约的用例（`test_fix_online.py` 等）适配 `{items, total}`、settings PATCH 白名单新键用例
- 手工/集成：设置页保存保留天数后检查 task_run 清理按新值执行（如有运行环境）

## Open Questions

无（四问题的实现路径、契约变更与测试矩阵均已明确，未发现会改变 spec/方案/任务的未知项）。
