# Tasks

## 1. 大小上下限的配置面与数据模型

- [x] 1.1 在 `backend/app/config.py` 增加默认常量 `DEFAULT_MIN_EPISODE_SIZE_GB = 0.1` / `DEFAULT_MIN_MOVIE_SIZE_GB = 0.5`，在 `backend/app/routers/settings.py` 的 `_WHITELIST_EXACT` 增加 `min_episode_size_gb` / `min_movie_size_gb`。验证：`backend/.venv/bin/pytest tests/test_settings_services.py tests/test_config_store.py` 全绿，且新增键可通过 `PATCH /api/settings` 写入后由 `GET /api/settings` 回读。 <!-- comet-task:ddd2773a-f8fa-4a4d-8ef2-301977d0816f -->
- [x] 1.2 在 `backend/app/models/__init__.py` 的 `Media` 增加可空列 `min_episode_size_gb` / `min_movie_size_gb`，在 `DownloadQueue` 增加 `auto_redownload_count`（Integer，`server_default=0`，非空）；新增 alembic 迁移 `0019`（`down_revision = "0018_user_token_version"`）加这三列，`downgrade` 对称删列。验证：`cd backend && .venv/bin/alembic upgrade head && .venv/bin/alembic downgrade -1 && .venv/bin/alembic upgrade head` 三步均成功；`backend/.venv/bin/pytest tests/test_api_smoke.py` 全绿。 <!-- comet-task:1cafaf40-070a-4cf3-8bd1-a2a61a4b0cf8 -->
- [x] 1.3 在 `backend/app/routers/media.py` 的 `MediaPatch` 增加两个最小大小字段，并在 `_media_dto` 透出；确认 `patch_media` 对负值/非法值按既有校验语义处理。验证：`backend/.venv/bin/pytest tests/test_api_smoke.py tests/test_media_episode_tags.py` 全绿，并新增/运行一条用例断言 patch 后 DTO 回显两个最小值。 <!-- comet-task:3b1c7142-818f-4e83-9f24-24e90f982b6d -->
- [x] 1.4 前端接线：`frontend/src/types/index.ts` 增加两个最小大小字段，`frontend/src/config/settingsMeta.ts` 增加 `min_episode_size_gb` / `min_movie_size_gb` 两条元数据（说明留空或 0 表示不限制），`frontend/src/views/MediaDetailView.vue` 在现有两个大小输入旁各加一个最小值输入（`:min="0"`，空值即不限制）并纳入 form 初始化与保存。验证：`cd frontend && npm test` 全绿，且 `MediaDetailView.test.ts` 新增用例覆盖「填写最小值保存后提交正确 payload」「留空提交为 null」。 <!-- comet-task:4db8c68d-554a-4c7e-8fe6-85e057980e23 -->

## 2. 巡检大小过滤扩展为上下限

- [x] 2.1 在 `backend/app/tasks/scan.py` 把 `_size_limits()` 扩展为返回 `(ep_min, ep_max, movie_min, movie_max)`（沿用「media 覆盖 > 全局配置 > settings 默认」），把 `_size_limit_gb()` 改为 `_size_bounds_gb()` 返回 `(min_gb, max_gb)`（`None`/`0` 视为不限制），并同步更新唯一调用点 `_read_size_limits()` 的返回类型。验证：`backend/.venv/bin/pytest tests/test_scan_async.py tests/test_scan_walk_size.py` 全绿。 <!-- comet-task:cf486e09-ce18-442c-ae8b-07c797c8ab7e -->
- [x] 2.2 在主循环与纯数字兜底分支应用同一判定 `min_bytes <= file_size <= max_bytes`：低于下限或高于上限的文件跳过并计入 `size_filtered`，日志区分两类原因；边界值放行、未知大小仍 fail-closed 跳过、被拦截不影响同轮其它文件入队。验证：新增用例覆盖「20MB 单集被下限拦截」「超上限仍被拦截」「等于下限/上限放行」「未知大小跳过」「单影视覆盖生效」「min 为 0/空时不限制」，运行 `backend/.venv/bin/pytest tests/test_scan_full_mode_filter.py tests/<新增用例文件>` 全绿。 <!-- comet-task:f8241357-ab4c-4f5b-8bd5-45c410112572 -->

## 3. Emby 侧删除后自动重新下载

- [x] 3.1 扩展 `backend/app/tasks/scan.py` 的 `_resolve_done_states()`：查询范围扩为 `status='done'` 或 `status='failed'` 且 `node_error` 属于本规则文案（`_DONE_FAIL_ERROR` / `_DONE_LIMIT_ERROR`）；仍缺失时按 `retry_count < _DONE_FAIL_RETRY_LIMIT` 累加宽限轮次，达到宽限上限且 `auto_redownload_count < _AUTO_REDOWNLOAD_LIMIT`（常量取 3）时按 `retry_task` 的重置语义把该行重置为 `pending`（清 `retry_count/node_attempt/save_task_id/save_attempt_at/error/node_*`）并 `auto_redownload_count + 1`，超出自动重下上限则写 `failed` 终态；Emby 已确认入库时删除该行（`done` 与上述 `failed` 行均适用）。所有更新/删除保持 `WHERE status=<读取时状态>` 条件以防并发。验证：新增用例覆盖「宽限期内不重下」「达到上限后重置为 pending 并计数 +1」「超出自动重下上限转 failed 终态」「Emby 已入库时删除记录（含失败行）」，运行 `backend/.venv/bin/pytest tests/test_scan_enqueue_dedup.py tests/<新增用例文件>` 全绿。 <!-- comet-task:4d1f0b3d-8140-418c-9d35-4f914a208d8f -->
- [x] 3.2 让 `backend/app/routers/queue.py` 的人工 `retry_task` 在重置 `DownloadQueue` 为 pending 时同步把 `auto_redownload_count` 清零（人工介入后重新给足自动重下预算），并确认该行重置后不会被巡检重复处理。验证：新增用例断言 `retry_task` 后 `auto_redownload_count == 0` 且 `status == 'pending'`，运行 `backend/.venv/bin/pytest tests/test_transfer_queue.py tests/<新增用例文件>` 全绿。 <!-- comet-task:84e55c4b-d832-47c0-9401-4340bd46eda0 -->

## 4. 集成验证

- [x] 4.1 跑通受影响的后端测试集合，确认无回归：`cd backend && .venv/bin/pytest tests/`（至少覆盖 `test_scan_*`、`test_transfer*`、`test_settings*`、`test_api_smoke.py`、`test_delete_media_fk.py`），记录实际通过结果。 <!-- comet-task:5c720ed4-ab85-41b6-8f64-1d3016560d17 -->
- [x] 4.2 端到端场景验证并记录实际输出：① 构造一条小于下限的单集资源，执行一次巡检，确认该文件被跳过、`size_filtered` 计数 +1、未写入任务队列；② 构造「已下载完成但 Emby 缺失」的集，连续执行巡检，确认经过宽限轮次后记录被重置为 `pending` 并触发重新下载，且 `auto_redownload_count` 按次递增；③ 手动在队列页重试后确认 `auto_redownload_count` 归零。 <!-- comet-task:45f19b61-7aa5-47f0-9d65-65a985a34502 -->
- [x] 4.3 前端构建与用例验证：`cd frontend && npm run build && npm test` 均成功，确认详情页最小值输入与设置页新配置项渲染与保存正常。 <!-- comet-task:29e22813-bb1a-4f2e-a37a-a63ce69945c8 -->

## Workflow follow-up

- 依 Comet 流程进入 Verify 阶段并完成最终集成审查后归档本 change。
- 归档后按既有交付方式（local/branch/push/PR）完成交付。
