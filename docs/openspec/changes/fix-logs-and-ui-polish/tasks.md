## 1. 运行日志任务类型映射修正（specs/run-logs ①）

- [x] 1.1 修正 `frontend/src/utils/format.ts` `TASK_TYPE_MAP`：对齐后端实际写入值（补 `sync_nastools`、`notify`、`prune_history` 中文标签；确认移除不再产生的 `transfer_retry`/`download`；保留历史别名如 `media_scan`/`recovery` 兜底映射），并同步更新 `LogsView.vue:18` 筛选列表仅含有效类型；验证 `format.test.ts` 与 `frontend` `npm run test`（如有任务类型用例则补充断言）
- [x] 1.2 核对 `backend/app/tasks/*.py` 每个 `record_task_run` 写入的任务类型与 map 键一一对应（审计确认无遗漏、无引入新别名），在 map 注释标注各键来源文件；验证 grep 结果与 map 键集一致

## 2. 运行日志真实分页（specs/run-logs ③）

- [x] 2.1 后端 `backend/app/routers/logs.py` `list_logs` 复用筛选条件执行 count 统计，返回 `{"items": [...], "total": n}`；验证新增/既有后端测试（`test_fix_online.py`、`test_task_run_duration.py` 等引用 `/logs` 的用例）通过
- [x] 2.2 前端 `frontend/src/api/index.ts` `listLogsApi` 返回类型改为 `LogListResponse`（`types/index.ts` 增补），`stores/logs.ts` 移除「limit+1 探测」逻辑直接消费 `res.total`，保留 `items.length===0 && page>1` 回退保护；验证 `npm run build` 通过且运行日志页分页总条数与后端一致

## 3. 日志保留天数动态配置（specs/run-logs ④）

- [x] 3.1 后端 `backend/app/routers/settings.py`：`task_run_retention_days` 加入 `_WHITELIST_EXACT`，并从 `_EDITABLE_KEYS`（服务凭据表单）排除，确保业务参数可 PATCH 且不被当作凭据字段渲染；验证 PATCH 白名单单元测试/接口用例
- [x] 3.2 前端 `frontend/src/config/settingsMeta.ts` 增加 `task_run_retention_days` 元数据（中文标签「运行日志保留天数」、说明、默认 30），`SettingsView.vue` 业务参数区按既有数字/保存 pattern 渲染；验证设置页可见该配置并可保存、`npm run test` 通过
- [x] 3.3 验证 `prune_history_job`（`backend/tests/test_prune_history.py`、`test_task_cleanup.py`）在配置键存在/缺省两种情况下按预期清理（确认无需改动 cleanup.py 逻辑）

## 4. 邀请码管理迁移到用户管理页（specs/user-invite-management）

- [x] 4.1 `UsersView.vue` 增加邀请码管理区块：复用 `stores/settings.ts` invite actions，迁移 `SettingsView.vue` 的 `generateCount`/`generate`/`removeInvite`/`copyText`/`copyInviteCode`/`copyRegisterLink` 与 invites 表格模板；`onMounted` 同时拉取用户列表与邀请码；验证用户管理页可完成生成/复制/复制链接/删除，`npm run test`（含 `UsersView.test.ts`）通过
- [x] 4.2 移除 `SettingsView.vue` 的「邀请码管理」tab（脚本与模板），`onMounted` 去掉 `fetchInvites()`；验证设置页不再展示该 tab 且 `SettingsView.test.ts` 通过
- [x] 4.3 核对邀请码后端 API（`backend/app/routers/admin.py` invites 三接口）契约未变，既有后端测试通过

## 5. 影视库卡片集数统计文案去重（specs/media-status）

- [x] 5.1 `MediaListView.vue:165` 移除模板硬编码「已有 」前缀，仅输出 `episodeText(m)`（其内部已含「已有 N 缺失 M」）；验证卡片显示「已有 15 缺失 0」而非「已有 已有 15 缺失 0」，`format.test.ts` 既有断言保持通过
- [x] 5.2 `format.test.ts` 补充/确认「已有 N 缺失 M」单前缀场景断言（覆盖 missing=0 与 missing>0），`npm run test` 通过

## 6. 集成验证与收尾

- [x] 6.1 前端构建 + 全部测试通过（`npm run build`、`npm run test`）；后端相关测试通过（`pytest` 对应文件）；diff 审视确认四问题修复无回归
- [x] 6.2 汇总验证证据（测试输出、构建结果、关键 diff）供 verify 阶段使用