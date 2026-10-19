# Brainstorm Summary

- Change: media-min-size-and-emby-redownload
- Date: 2026-10-19

## 已确认事实（代码核实）

- `library_check` 的入库轮询只在 **Emby 已确认收录**时才把 `download_queue` 置 `done`（`_finalize_done`），并同时删除夸克文件。因此一条 `done` 记录事后在 Emby 缺失，基本可判定为 Emby 侧被删除。
- 巡检 `_resolve_done_states()` 只查 `status='done'`；`done→failed`（`retry_count+1`）后该行再也不会被巡检选中，`_enqueue()` 又对同集任意 `download_queue` 状态判 `existing` → 永久阻挡，只能人工 `retry_task`。
- `retry_task` 已定义 failed → pending 的重置语义（清 `retry_count/node_attempt/save_task_id/save_attempt_at/error/node_*`），可直接复用。
- 转存取件只在同键 `download_queue` 不存在时从 `task_queue` 建行；重置既有行为 `pending` 即可直接重新下载，无需重建探测。
- 现有大小过滤点：`scan._size_limits()` / `_size_limit_gb()`，主循环与纯数字兜底分支各自判断上限；未知大小 fail-closed 跳过。

## 确认的技术方案

1. 大小上下限：`_size_limits()` 返回四值，`_size_bounds_gb()` 返回 `(min_gb, max_gb)`；判定 `min_bytes <= size <= max_bytes`；`None`/`0` 不限制。配置三层沿用现有上限结构（全局键 + `Media` 覆盖列 + 前端元数据/详情页）。
2. 防重解除：`_resolve_done_states()` 查询范围扩为 `done` 或「本规则产生的 failed」；**宽限轮次 = 3（用户确认，沿用 `_DONE_FAIL_RETRY_LIMIT`）**，宽限用尽后按 `retry_task` 语义重置为 `pending` 并递增 `auto_redownload_count`（上限 3），超上限转 `failed` 终态；Emby 已确认收录时删除记录（含上述 failed 行）。人工 `retry_task` 清零 `auto_redownload_count`。
3. 迁移：alembic `0019` 新增 `media.min_episode_size_gb` / `media.min_movie_size_gb`（可空）与 `download_queue.auto_redownload_count`（非空，默认 0）。

## 关键取舍与风险

- 自动重下依赖旧分享快照，分享失效时按现有转存失败路径处理（不新增失效检测）。
- 宽限 3 轮 + 自动重下上限 3 次：默认巡检间隔下约 3 小时开始重下，最多自动重下 3 次。
- 大小下限默认 0.1GB（单集）/ 0.5GB（电影），留空或 0 可关闭。

## 测试策略

- 后端：`backend/.venv/bin/pytest tests/`（重点 `test_scan_*`、`test_transfer*`、`test_settings*`、`test_api_smoke.py`）；新增用例覆盖大小上下限边界、未知大小、覆盖生效、宽限不重下、达上限重置为 pending、超上限终态、已入库清理、手动重试清零。
- 前端：`cd frontend && npm test && npm run build`，`MediaDetailView.test.ts` 覆盖最小值填写与留空。

## Spec Patch

- 无（delta spec 未量化轮次，宽限轮次属设计层参数，不需要回写需求）。
