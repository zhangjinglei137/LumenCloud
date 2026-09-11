# Task 8：集成验证报告（fix-online-issues）

- 日期：2026-09-12
- 基线 HEAD：ded8d9b
- 变更范围：fix-online-issues change 前 7 个任务产物
- 结论：**全部通过**

## 1. 后端全量 pytest

命令：`cd backend && .venv/bin/python -m pytest -q`

结果：

```
587 passed, 2 warnings in 177.96s (0:02:57)
```

- 587 用例全部通过（含前 7 个任务的回归/新增测试：test_media_list_emby_stats / test_queue_auth / test_queue_aria2_gid_redaction / test_static_path_traversal / test_poster_proxy 等）。
- 2 条 warning 均为第三方库 Deprecation（starlette/httpx、anyio BlockingPortal），非本项目代码，不影响结论。

## 2. 前端 vitest + build

命令：`cd frontend && npx vitest run && npm run build`

vitest 结果：

```
Test Files  10 passed (10)
     Tests  90 passed (90)
   Start at  01:11:06
   Duration  5.84s
```

- 10 个测试文件 / 90 个用例全部通过。

build 结果：

```
✓ built in 7.57s
```

- 构建成功，产物输出至 ../backend/static/。仅有 chunk 大小提示（element-plus 单包 1.08MB > 500kB 警告），属既有提示，非错误。

## 3. 7 项问题行为核对表

| # | 项 | 代码位置（file:line） | 测试覆盖 | 结论 |
|---|----|----------------------|----------|------|
| 1 | 缺失集：未开播集不计入缺失 | `backend/app/routers/media.py:476-499`（`_stats` 中 `aired_total` 已开播口径，`base = aired_total if aired_total >= 0 else total`，`missing = max(0, base - available)`） | `backend/tests/test_media_list_emby_stats.py:199-232`（test_stats_missing_excludes_unaired：缓存 4 集含 1 集 30 天后未开播，断言 total=30 / available=1 / missing=2，而非 total 口径 29） | 通过 |
| 2 | 状态：两值 + 中文兜底 | `frontend/src/utils/format.ts:86-89`（`mediaStatusLabel`：`MEDIA_STATUS_MAP[status]?.[0] ?? '未知'`；`download` 残留值归并「下载中」） | `frontend/src/utils/format.test.ts:149-159`（download→下载中、weird_value→未知、tag type primary/info） | 通过 |
| 3 | 图片：poster 路径校验通过 | `backend/app/services/emby.py:429-432`（`poster_url = f"/api/poster?p=/emby/{item_id}/Primary"`，带前导 `/`）+ `backend/app/services/poster.py:59`（`p.startswith("/emby/")` 校验放行） | `backend/tests/test_poster_proxy.py:278-303`（test_production_poster_url_passes_validation、test_normalized_poster_url_passes_validation：生产构造的 poster_url 的 p 参数通过 `_validate_poster_path`） | 通过 |
| 4 | 角色：只读展示 | `frontend/src/views/UsersView.vue:19-24`（roleTagType/roleLabel）+ `:95-97`（el-tag 只读展示角色文案；无 el-select / onRoleChange 角色编辑） | `frontend/src/views/UsersView.test.ts:95-106`（断言任意行 `.el-select` 数量为 0、html 不含 el-select、以 tag 文案展示） | 通过 |
| 5 | 订阅：admin 可见订阅按钮 | `frontend/src/views/EmbyLibraryView.vue:370`（批量订阅 `v-if="auth.isAdmin && pendingItems.length > 0"`）、`:496`（单条订阅 `v-if="auth.isAdmin && !m.in_media"`）、`:548`（TMDB 对话框提交 `v-if="auth.isAdmin"`） | `frontend/src/views/embyLibraryView.test.ts`（含于 10 文件 / 90 用例，全量通过） | 通过 |
| 6 | 队列：guest 打开队列页无 403 弹窗 | `backend/app/routers/queue.py:290-297`（GET `/queue/download/state` 由 admin-only 降为 `Depends(get_current_user)`；注释说明暂停开关与在途任务数为展示数据） | `backend/tests/test_queue_auth.py`（guest 读 state→200、admin 读→200、未登录→401、guest POST pause→403 保持 admin-only、admin pause→200） | 通过 |
| 7 | 安全：路径穿越拒绝 + aria2_gid 脱敏 | 路径穿越：`backend/app/main.py` serve_spa（SPA fallback 路径规范化，配合测试） | 路径穿越：`backend/tests/test_static_path_traversal.py:41-58`（4 种 `..%2f` 编码穿越 payload 均不泄露 static 根外机密文件，且正常静态文件/SPA fallback 语义保持）；aria2_gid：`backend/tests/test_queue_aria2_gid_redaction.py`（guest 下载列表 aria2_gid/share_code 为 None，admin 明文可见回归锁定） | 通过 |

## 4. 失败/异常处理

- 无失败。后端 2 条 Deprecation warning 与前端 chunk 大小提示均为既有第三方/构建提示，非本次变更引入，不影响通过结论。
- 本任务未修改任何生产代码（前 7 个任务产物经全量回归确认真实有效）。

## 5. 结论

- 后端 587 passed；前端 90 passed；build PASS。
- 7 项问题行为全部通过（代码位置 + 测试覆盖 + 运行结果三证一致）。
