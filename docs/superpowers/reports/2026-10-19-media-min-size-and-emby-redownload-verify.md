# 验证报告：media-min-size-and-emby-redownload

- 阶段：Verify（`verify_mode: full`）
- 验证日期：2026-10-19
- 基线提交：`7892dc5077b894374b4b04e652e00da706ea6a3c`
- 产物：`docs/openspec/changes/media-min-size-and-emby-redownload/`（proposal / 3 个 delta spec / design / tasks）

## Summary

| Dimension    | Status                                                     |
| ------------ | ---------------------------------------------------------- |
| Completeness | 11/11 任务完成；4 个 ADDED requirements（3 个 capability）   |
| Correctness  | 4/4 requirements 有实现证据；17/17 scenarios 有实现与测试    |
| Coherence    | D1–D5 全部遵循；无 delta spec ↔ design 矛盾；模式一致        |

集成审查（`review_mode: standard`，独立于实现者）：**可接受，0 CRITICAL / 0 IMPORTANT**，4 条 SUGGESTION 均非阻塞。

## 验证证据

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量测试 | `backend/.venv/bin/pytest backend/tests -q`（cwd=项目根） | 862 passed（Verify 阶段 `comet check run verify --local` 返回 `reused=true`，复用 Build 证据） |
| 前端构建 | `npm --prefix frontend run build` | exit=0，`✓ built in 6.62s`（仅既有 chunk >500 kB 警告） |
| 前端测试 | `npm --prefix frontend test` | 16 files / 135 tests passed |
| 迁移链 | 干净临时库 `alembic upgrade head` / `downgrade -1` / `upgrade head` | 三步成功，`current = 0019_media_min_size` |
| 任务完成度 | `comet state tasks --json` | 11/11，revision `dd1864d4…` |
| 规模评估 | `comet state scale` | recommendation=full（11 任务 / 3 delta spec） |

## 需求实现映射（Completeness / Correctness）

### media-size-filter（2 requirements / 9 scenarios）

| Requirement / Scenario | 实现 | 测试 |
| --- | --- | --- |
| 影视大小限制配置口径（全局默认/覆盖/留空或 0/分类型） | `config.py`（`DEFAULT_MIN_*`）、`models.Media.min_*`、`routers/settings.py` 白名单、`routers/media.py`（`MediaPatch`/`_media_dto`）、`scan.py::_size_limits` | `test_min_size_config.py`、`test_scan_size_bounds.py`（4 例优先级/覆盖/0 不限制）、前端 `MediaDetailView.test.ts` |
| 巡检入队前按大小过滤（过小/过大/边界/未知/不中断） | `scan.py::_size_out_of_bounds` + `_size_bounds_gb`，主循环与纯数字兜底共用 | `test_scan_size_bounds.py`（过小/过大/边界/未知/覆盖/不中断/纯数字兜底）、`test_scan_min_size_redownload_integration.py::test_scan_min_size_filtered_others_enqueued` |

### media-pipeline（1 requirement / 5 scenarios）

| Scenario | 实现 | 测试 |
| --- | --- | --- |
| 用户删除后自动重新下载 | `scan.py::_resolve_done_states` 自动重下分支 + `_scan_one` 触发转存消费 | `test_scan_redownload.py::test_grace_exhausted_resets_pending_and_counts`、集成用例场景 2 |
| 宽限期内不重复下载 | 宽限分支（`retry_count < 3`） | `test_grace_period_does_not_redownload`、`test_failed_rule_row_advances_grace_count` |
| 超过自动重下次数上限 | 终态分支（`_DONE_LIMIT_ERROR`，保留人工 retry） | `test_auto_redownload_exhausted_marks_failed_terminal`、`test_pending_row_not_reselected_on_next_round`、集成用例场景 2 |
| 入库确认后解除防重保留 | `delete(WHERE status=读取时状态)` | `test_confirmed_done_row_deleted`、`test_confirmed_failed_rule_row_deleted` |
| 失败记录不再永久阻挡重新下载 | 接管范围含本规则 failed 行（`_DONE_RULE_ERRORS`） | `test_confirmed_failed_rule_row_deleted`、`test_other_failure_type_not_touched`、`queue.retry_task` 归零用例 |

### media-detail-ui（1 requirement / 3 scenarios）

| Scenario | 实现 | 测试 |
| --- | --- | --- |
| 配置最小大小并保存生效 | `MediaDetailView.vue` 下限输入 + form/payload | `MediaDetailView.test.ts`（payload 断言） |
| 留空表示不限制 | `?? null` 回填与提交 | `MediaDetailView.test.ts`（null 断言） |
| 拒绝负值输入 | `el-input-number :min="0"` 控件约束 | 控件层，无自动化用例（见 SUGGESTION 2） |

## Coherence

- **Design 遵循**：D1 上下限统一为 `(min_gb, max_gb)`（边界放行、`None`/0 不限制）；D2 配置三层同构；D3 接管范围 + 宽限 3 轮 + 自动重下上限 3 次 + 终态保留人工入口；D4 复用既有 `download_queue` 行重置 `pending`，不新建任务队列行；D5 宽限沿用 `retry_count`、不新增计时字段；全部 CAS 条件更新保留。
- **delta spec ↔ design 矛盾**：无。design 与 delta spec 无冲突条款。
- **Design Doc 可定位**：`docs/openspec/changes/media-min-size-and-emby-redownload/design.md`（frontmatter `role: technical-design`、`canonical_spec: openspec`）。
- **代码模式一致性**：配置键/白名单、`mapped_column` 可空列、alembic「带 `server_default` 的 ADD COLUMN」、测试内 in-memory SQLite + mock 外部 IO 的写法均与既有模式一致。

## Issues

### CRITICAL（必须在归档前修复）

无。

### WARNING（应当修复）

无。

### SUGGESTION（可选改进，不阻塞归档）

1. `scan.py:547`：tv 全量模式（Emby 未收录软处理）下所有 done/本规则 failed 行都会被判缺失并推进宽限计数——沿用既有语义（全量模式无法按集判定），知悉即可。
2. `routers/media.py` 的 `min_*` / `max_*` 均无服务端 `ge=0` 校验（与既有 max 同构的既有缺口，非本次引入）；「拒绝负值」目前只由前端 `:min="0"` 兜住，无自动化用例。
3. `scan.py` 接管范围依赖 `_DONE_RULE_ERRORS` 常量文案匹配：未来若改动文案，存量行会脱离接管（已用常量收敛，风险低）。
4. 格式：`backend/tests/test_scan_redownload.py`、`frontend/src/views/MediaDetailView.test.ts` 文件末尾缺换行（纯格式，Verify 阶段不改实现，留待后续）。

### 已接受的偏差（记录原因与影响范围）

- `retry_count` 为 NULL 的极端行（列为 NOT NULL、默认 0，仅手工造数可得）：SQL 侧 `retry_count < / >=` 对 NULL 不命中，Python 侧 `or 0` 兜底；影响范围仅限手工异常数据，接受。
- 迁移 revision id 采用 `0019_media_min_size`（16 字符），而非任务书字面量（35 字符）：Postgres `alembic_version.version_num` 为 `VARCHAR(32)`，长 id 会导致升级失败；文件名仍保留完整语义，迁移内注释说明。
- 部署影响提示：0.1GB / 0.5GB 下限对存量媒体立即生效（design Risks 已声明）；若不希望拦截极小资源，部署时显式置 0。

## Final Assessment

**All checks passed. Ready for archive.**（4 条 SUGGESTION 可选改进；0 CRITICAL / 0 WARNING）

未在本次核实的范围：生产 PostgreSQL 上的迁移实测（仅在 SQLite 测试环境验证 upgrade/downgrade 循环），建议预发环境先跑一次迁移循环。
