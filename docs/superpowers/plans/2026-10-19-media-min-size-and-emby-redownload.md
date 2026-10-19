---
change: media-min-size-and-emby-redownload
design-doc: docs/openspec/changes/media-min-size-and-emby-redownload/design.md
base-ref: 7892dc5077b894374b4b04e652e00da706ea6a3c
archived-with: 2026-10-19-media-min-size-and-emby-redownload
---

<!-- comet-task-authority: docs/openspec/changes/media-min-size-and-emby-redownload/tasks.md -->

# 实施计划：最小影视大小限制 + Emby 删除后自动重下

需求以 `docs/openspec/changes/media-min-size-and-emby-redownload/specs/` 为准，技术决策以 `design.md`（D1–D5）为准。配置：`build_mode=autonomous`、`tdd_mode=tdd`、`review_mode=standard`。

## 执行车道（避免写冲突）

| 车道 | 任务 | 写入范围 | 依赖 |
| --- | --- | --- | --- |
| A | 1.1 / 1.2 / 1.3 | `backend/app/config.py`、`backend/app/routers/settings.py`、`backend/app/models/__init__.py`、`backend/alembic/versions/0019_*.py`、`backend/app/routers/media.py`、对应测试 | 无 |
| C | 1.4 | `frontend/src/types/index.ts`、`frontend/src/config/settingsMeta.ts`、`frontend/src/views/MediaDetailView.vue`、前端测试 | 无（前端测试以 mock 为主） |
| B | 2.1 / 2.2 / 3.1 / 3.2 | `backend/app/tasks/scan.py`、`backend/app/routers/queue.py`、对应测试 | A（需要 `Media.min_*` 列与 `DownloadQueue.auto_redownload_count`） |
| D | 4.2 | 新增集成测试文件 | A + B |
| 主会话 | 4.1 / 4.3、build 证据、独立审查 | — | A + B + C + D |

同一车道内的任务串行；A 与 C 可并行。

## 任务

### 1. 大小上下限的配置面与数据模型（车道 A / C）

<!-- comet-task-ref:ddd2773a-f8fa-4a4d-8ef2-301977d0816f -->

- **1.1** 范围：`backend/app/config.py` 新增 `DEFAULT_MIN_EPISODE_SIZE_GB = 0.1`、`DEFAULT_MIN_MOVIE_SIZE_GB = 0.5`；`backend/app/routers/settings.py` 白名单新增 `min_episode_size_gb` / `min_movie_size_gb`。约束：沿用既有「全局键 + 白名单」模式，不改 GET 契约形状。TDD：先加断言新键可 PATCH 后回读的失败用例（RED），再实现（GREEN）。验收：`backend/.venv/bin/pytest tests/test_settings_services.py tests/test_config_store.py` 全绿 + 新增用例通过。

<!-- comet-task-ref:1cafaf40-070a-4cf3-8bd1-a2a61a4b0cf8 -->

- **1.2** 范围：`Media` 增加可空列 `min_episode_size_gb` / `min_movie_size_gb`；`DownloadQueue` 增加 `auto_redownload_count`（Integer、非空、`server_default=0`）；alembic `0019`（`down_revision="0018_user_token_version"`）加三列，`downgrade` 对称。约束：与 0016/0018 同型（带 server_default 的 ADD COLUMN），revision id ≤32 字符。验收：`cd backend && .venv/bin/alembic upgrade head && .venv/bin/alembic downgrade -1 && .venv/bin/alembic upgrade head` 全成功；`pytest tests/test_api_smoke.py` 全绿。

<!-- comet-task-ref:3b1c7142-818f-4e83-9f24-24e90f982b6d -->

- **1.3** 范围：`backend/app/routers/media.py` 的 `MediaPatch` 增两字段、`_media_dto` 透出。约束：保持 `exclude_unset` 语义（未传字段不覆盖），负值按既有校验语义处理。TDD：先写 DTO 回显两个最小值的失败用例。验收：`pytest tests/test_api_smoke.py tests/test_media_episode_tags.py` 全绿 + 新增用例。

<!-- comet-task-ref:4db8c68d-554a-4c7e-8fe6-85e057980e23 -->

- **1.4**（车道 C）范围：`frontend/src/types/index.ts` 增字段；`settingsMeta.ts` 增两条元数据（含「留空或 0 表示不限制」）；`MediaDetailView.vue` 两个大小输入旁各加最小值输入（`:min="0"`，空 = null），纳入 form 初始化与保存 payload。约束：沿用元数据驱动与既有 `el-input-number` 样式，不加新组件。TDD：先写「填写最小值 → payload 正确」「留空 → null」的失败用例。验收：`cd frontend && npm test` 全绿 + 新增用例通过。

### 2. 巡检大小过滤扩展为上下限（车道 B）

<!-- comet-task-ref:cf486e09-ce18-442c-ae8b-07c797c8ab7e -->

- **2.1** 范围：`_size_limits()` 返回 `(ep_min, ep_max, movie_min, movie_max)`，`_size_limit_gb()` → `_size_bounds_gb()` 返回 `(min_gb, max_gb)`，更新 `_read_size_limits()` 与唯一调用点。约束：优先级仍为「media 覆盖 > 全局 > settings 默认」；`None`/`0` 不限制。验收：`pytest tests/test_scan_async.py tests/test_scan_walk_size.py` 全绿。

<!-- comet-task-ref:f8241357-ab4c-4f5b-8bd5-45c410112572 -->

- **2.2** 范围：主循环与纯数字兜底分支统一判定 `min_bytes <= file_size <= max_bytes`；越界跳过并计入 `size_filtered`，日志区分低于下限/高于上限。约束：边界值放行；未知大小维持 fail-closed 跳过；单文件跳过不中断同轮其它文件。TDD：先写「20MB 单集被下限拦截」「超上限拦截」「等于边界放行」「未知大小跳过」「覆盖生效」「min=0 不限制」失败用例。验收：`pytest tests/test_scan_full_mode_filter.py tests/test_scan_size_bounds.py` 全绿。

### 3. Emby 侧删除后自动重新下载（车道 B）

<!-- comet-task-ref:4d1f0b3d-8140-418c-9d35-4f914a208d8f -->

- **3.1** 范围：`_resolve_done_states()` 查询范围扩为 `done` 或 `failed`（`node_error IN (_DONE_FAIL_ERROR, _DONE_LIMIT_ERROR)`）；仍缺失时按宽限 3 轮累加 `retry_count`，宽限用尽且 `auto_redownload_count < 3` 时按 `retry_task` 语义重置为 `pending`（清 `retry_count/node_attempt/save_task_id/save_attempt_at/error/node_*`）并 `auto_redownload_count+1`；超上限写 `failed` 终态（`_DONE_LIMIT_ERROR` 文案）；Emby 已确认收录时删除该行（含上述 failed 行）。约束：全部更新/删除带 `WHERE status=<读取时状态>` 条件（CAS）；只接管本规则产生的失败；常量 `_AUTO_REDOWNLOAD_LIMIT = 3`。TDD：覆盖「宽限期内不重下」「达宽限上限重置为 pending 且计数 +1」「超自动重下上限转 failed 终态」「Emby 已入库时删除 done 行与失败行」。验收：`pytest tests/test_scan_enqueue_dedup.py tests/test_scan_redownload.py` 全绿。

<!-- comet-task-ref:84e55c4b-d832-47c0-9401-4340bd46eda0 -->

- **3.2** 范围：`backend/app/routers/queue.py` 的 `retry_task` 在 DownloadQueue failed/skipped → pending 时同步清零 `auto_redownload_count`。约束：保持既有条件更新与 409 语义不变。TDD：先写「retry 后 `auto_redownload_count == 0` 且 `status == 'pending'`」失败用例。验收：`pytest tests/test_transfer_queue.py tests/test_queue_retry_redownload.py` 全绿。

### 4. 集成验证（车道 D / 主会话）

<!-- comet-task-ref:5c720ed4-ab85-41b6-8f64-1d3016560d17 -->

- **4.1**（主会话）范围：`cd backend && .venv/bin/pytest tests/` 全量，记录实际通过结果。

<!-- comet-task-ref:45f19b61-7aa5-47f0-9d65-65a985a34502 -->

- **4.2**（车道 D）范围：新增集成用例（mock cloudSaver/Emby，风格对齐 `tests/test_scan_full_mode_filter.py`）：① 小于下限的单集被跳过 + `size_filtered=1` + 任务队列无新行；② 已 `done` 但 Emby 缺失的集，连续巡检后记录被重置为 `pending` 且 `auto_redownload_count` 递增；③ 模拟人工 retry 后计数归零。验收：该文件单独运行全绿，且输出可核对。

<!-- comet-task-ref:29e22813-bb1a-4f2e-a37a-a63ce69945c8 -->

- **4.3**（主会话）范围：`npm --prefix frontend run build` 与 `npm --prefix frontend test` 均成功。

## 验证与审查

- build 证据：`comet check run <change> build --local -- ...`（后端 pytest、前端 build/test），随后 `comet guard <change> build --apply`。
- 独立审查（standard）：对 `scan.py`（状态机与 CAS）与迁移触发一次独立审查（@oracle），CRITICAL/IMPORTANT 必须解决。

## 回退

- 代码回退：`git revert` 本次提交；数据库回退：`alembic downgrade -1`。
