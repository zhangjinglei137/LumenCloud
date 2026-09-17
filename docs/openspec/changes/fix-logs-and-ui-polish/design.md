## Context

现状约束（参见 proposal.md Why 与 specs 各能力）：

- 任务类型映射：`frontend/src/utils/format.ts:417` `TASK_TYPE_MAP` 登记 12 个类型，但后端实际写入值含 `sync_nastools`（nastools_sync.py，非 `nastools_sync`）、`notify`（notification_scan.py，非 `notification_scan`）、`prune_history`（cleanup.py）——三者未映射导致显示英文；`media_scan`/`recovery`/`transfer_retry`/`download` 为历史别名或不再产生。`LogsView.vue:18` 筛选列表与 map 同源。
- 日志分页：后端 `GET /api/logs`（`backend/app/routers/logs.py:35`）直接返回数组，不返回 total；前端 `stores/logs.ts` 以「limit+1 探测下一页」估算 total（`total` 注释标明是估算），分页总条数与末页体验不稳定。
- 保留天数：`backend/app/tasks/cleanup.py` `prune_history_job` 已读取 `system_config.task_run_retention_days`（缺省 `_RETENTION_DAYS=30`），但该键不在设置 PATCH 白名单（`backend/app/routers/settings.py:32` `_WHITELIST_EXACT`）和 `editable_keys` 中，设置页（`frontend/src/config/settingsMeta.ts`）无配置入口。
- 邀请码管理：设置在 `SettingsView.vue`（`el-tab-pane name="invites"`，约 728-779 行），依赖 `stores/settings.ts` 的 fetchInvites/createInvites/deleteInvite 与 `copyText/formatTime` 等本地函数；后端 invite API（`backend/app/routers/admin.py`）不变。`UsersView.vue` 为纯用户列表 + 前端本地分页。
- 卡片文案：`MediaListView.vue:165` 模板 `已有 {{ episodeText(m) }}`，而 `episodeSummaryText`（`format.ts:251`）自身返回「已有 N 缺失 M」（`已有 ${avail} 缺失 ${missing}`）——双重前缀渲染为「已有 已有 15 缺失 0」。`format.test.ts:129` 有既有测试断言单前缀。

## Goals / Non-Goals

**Goals:**
- 运行日志任务类型显示中文、筛选列表仅含有效类型
- 运行日志分页基于后端真实 total，移除「多取 1 条」估算
- `task_run_retention_days` 可在设置页业务参数区配置并立即生效
- 邀请码管理从设置页迁移到用户管理页，后端 API 与数据契约不变
- 影视库卡片集数统计文案单前缀「已有 N 缺失 M」

**Non-Goals:**
- 不重命名后端已产生的历史 task_run 数据（仅在展示层映射，数据保持原值）
- 不新增任务类型、不改任务调度与日志写入逻辑、不引入新依赖
- 不做邀请码后端逻辑改动（仅前端迁移）
- 不做数据库 schema 变更

## Decisions

### D1：任务类型映射收敛为「后端实际写入值 + 历史兼容别名」
`TASK_TYPE_MAP` 以当前后端实际写入的任务类型为准：`scan_media`、`scan_all_media`、`transfer`、`cleanup`、`capacity_alert`、`recover`、`sync_nastools`、`notify`、`prune_history`。历史已产生的旧值（如 `recovery`、`media_scan` 若存量存在）保留映射兜底展示，但筛选列表只列当前有效类型；确认不再产生的 `transfer_retry`/`download` 等从 map 与列表一并移除（若存量数据存在则仍以未知类型样式展示原值，不吞数据）。
- 备选：从数据库 `SELECT DISTINCT task_type` 动态生成列表——需新增接口且查询开销与管理复杂，对低频变动的类型集收益低；静态清单 + 注释对齐 `tasks/*.py` 写入点更简单。
- `notify`、`prune_history`、`sync_nastools` 等新类型以中文标签补齐（如「通知」「历史清理」「目录同步入库」）。

### D2：日志分页返回 `{items, total}` 契约
后端 `list_logs` 复用同一筛选条件额外执行 `count(*)`，返回 `{"items": [...], "total": n}`；`GET /api/logs/{id}` 单条接口不受影响。前端 `listLogsApi` 返回类型改为 `LogListResponse`，`stores/logs.ts` 删除「limit+1 探测」逻辑，直接使用 `res.total`，保留空页/末页越界保护（`data.items.length === 0 && page > 1` 回退一页）。
- 备选：维持数组 + 新增 `X-Total-Count` 头——契约不显式、前端取值不便；对象化与 queue 契约保持一致（`queue` 已用 `{items, total}`）。
- **BREAKING**：`GET /api/logs` 响应从数组改为对象，前端同步适配（同仓仅一处调用方）。

### D3：保留天数配置纳入设置白名单与业务参数区
- 后端：`settings.py` `_WHITELIST_EXACT` 增加 `task_run_retention_days`，由于它是「业务参数」而非服务凭据，需从 `_EDITABLE_KEYS` 语义对齐——参考 `emby_series_library_ids` 的处理：加入 PATCH 白名单，但排除在 `editable_keys` 凭据表单之外。设置页业务参数区按现有 pattern（`config/settingsMeta.ts` 加 `task_run_retention_days` 条目 + 数字输入/保存即 PATCH）渲染。
- `cleanup.py` 已读取该键，无需改动清理逻辑；配置保存经 `config_store.refresh()` 立即生效。
- 默认值展示 30 天，与 `_RETENTION_DAYS` 一致。

### D4：邀请码管理迁移到用户管理页
`UsersView.vue` 增加邀请码管理区块：复用 `stores/settings.ts` 既有 invite actions（fetchInvites/createInvites/deleteInvite）或迁入 `stores/users.ts`（择优：迁移时保证 `onMounted` 同时拉取用户列表与邀请码）；将 `SettingsView.vue` 邀请码相关脚本（`generateCount`、`generate`、`removeInvite`、`copyText`、`copyInviteCode`、`copyRegisterLink`）与模板整体搬移到 `UsersView.vue`，移除 `SettingsView.vue` 的 invites tab。后端 invite API 不动。
- 决策点：函数与 store 动作放 `UsersView` 内复用 `settings` store（改动最小，避免 store 搬迁重复）——按此执行。
- 设置页 `onMounted` 移除 `fetchInvites()`；页面不再依赖 invites 渲染。

### D5：卡片文案去重
`MediaListView.vue:165` 改为仅输出 `episodeText(m)`（其内部已含「已有 N 缺失 M」前缀），移除模板中硬编码的「已有 」；或反向让 `episodeSummaryText` 去前缀（不可行——`format.test.ts` 与表格视图可能复用）。选前者：模板 `{{ episodeText(m) }}`。更新/新增 `format.test.ts` 断言「已有 N 缺失 M」无重复前缀场景，`MediaListView.test`（若有）同步。

## Risks / Trade-offs

- [D1 静态清单与后端任务类型漂移] → map 注释标注各键对应的 `tasks/*.py` 写入点；新增任务类型时要求同步更新（在 tasks.md 记录检查点）
- [D2 count(*) 在超大数据量下开销] → task_run 行数受清理任务（30 天默认）约束，量级可控；沿用 queue 既有契约模式
- [D3 该键同时影响容量快照清理] → 与既有语义一致（`prune_history_job` 同键），设置文案注明「运行日志与容量快照保留天数」
- [D4 邀请码逻辑从设置页移出后，设置页 onMounted 少一次请求] → 无副作用；用户管理页多一次请求，量级小
- [D5 仅改模板，`episodeSummaryText` 保持不变] → 表格视图与其他调用方（若有）语义不变，最小回归面

## Migration Plan

- 前端与后端同步发布：接口契约变化（logs 分页）由同仓调用方同步适配，无跨版本兼容需求（前后端同仓同发）。
- 存量 task_run 中的历史任务类型（如已不再产生的 `recovery`/`media_scan`）无需迁移，展示层映射/未知类型兜底即可。
- 回滚：`GET /api/logs` 恢复数组契约需同时回滚前端 store；其余改动均可独立回滚。

## Open Questions

无（四个问题均有明确实现路径，未发现会改变 spec/方案/任务拆分的未知项）。