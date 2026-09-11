## Context

现状约束（参见 proposal.md Why）：

- 影视列表集数统计在 `backend/app/routers/media.py` `_stats()` 中计算：`total` 取自 TMDB 全集数（`tmdb_cache.number_of_episodes`，含未开播集），`available = 本系统完成集 ∪ Emby 已入库集`，`missing = total - available` —— 未开播集被计入缺失。前端 `frontend/src/utils/format.ts` `episodeSummaryText` 展示「已有 N 缺失 M」。
- media.status 模型注释含 tracking/downloading/paused/error 四值，但 PATCH 接口已限制仅 tracking/paused（`media.py:852`）；历史数据或旧路径可能残留 downloading 等值，前端 `mediaStatusLabel` 缺键时原样透传英文。
- Emby 封面代理：`backend/app/services/emby.py:432` 构造 `poster_url = "/api/poster?p=emby/{item_id}/Primary"`（缺前导 `/`），而 `backend/app/services/poster.py` `_validate_poster_path` 要求 `/emby/<itemId>/Primary` 前缀（`p.startswith("/")`），导致全部 Emby 封面请求 400 失败——问题 3 根因。
- 队列页：`QueueView.vue` onMounted 调用 `fetchPauseState()`（`GET /queue/download/state`，admin-only），guest 打开页面即 403 且全局拦截器 toast「需要管理员权限」；控制按钮已有 `auth.isAdmin` 条件，但暂停状态读取未按角色分支。

## Goals / Non-Goals

**Goals:**
- 修正缺失集判定：未开播集不计入缺失，「已有/缺失」口径与 TMDB 已开播集一致
- media.status 收敛为 tracking/paused 两值；前端状态展示对未知值做中文兜底，不原样透传英文
- 修复 Emby 封面代理路径校验失败，图片恢复显示
- 用户管理角色列只读展示，移除编辑控件
- Emby 库「加入订阅」按钮仅 admin 可见
- 队列页访客不再误弹权限提示，控制操作按角色禁用/隐藏
- 完成全代码安全审查并修复确认漏洞

**Non-Goals:**
- 不改 Emby 分类/Tab 结构、不改队列两表状态机、不引入新外部依赖
- 不对历史脏数据做强制迁移（前端兜底即可，数据库不改）

## Decisions

### D1：缺失集按「TMDB 已开播集」口径计算
`missing = (TMDB 已开播集数) - available`，其中 TMDB 已开播集数来自 `get_episode_info`（episode_info_cache，含 air_date）过滤 `air_date <= today` 计数；缓存未命中回源 TMDB。total 字段仍返回 TMDB 全集数（供「已有 N / 总集数」参考），但缺失只按已开播口径。
- 备选：在 `tmdb_totals` 处按季过滤未开播——数据源与详情页 air_date 逻辑重复，且 TMDB 缓存层已聚合全集信息，D1 复用现有 `get_episode_info` 更一致。
- 降级：外部服务故障时缺失回退 `total - available`（现状口径），不报错。

### D2：media.status 两值语义 + 前端兜底
- 后端：`create/patch` 已强制 tracking/paused（保持），追加校验历史读取路径不产生新值（巡检/任务回写不得改 media.status）。
- 前端：`mediaStatusLabel/Type` 未知值回退中文（`download/downloading → 下载中` 归并到 `downloading` 键），状态下拉仅两选项（现状已符合）。

### D3：Emby 封面代理路径修复
`emby.py` `_normalize_library_item` 的 `poster_url` 改为 `/api/poster?p=/emby/{item_id}/Primary`（补前导 `/`），与 `_validate_poster_path` 前缀一致；`poster.py` 校验逻辑不动。同步补后端测试断言该构造路径通过校验。

### D4：用户角色只读
`UsersView.vue` 角色列由 el-select 改为纯展示 tag（复用现有 `roleLabel/roleTagType`）；移除 `patchRole` 调用入口。后端 `PATCH /admin/users/{id}` 接口保留（admin-only，已有 409 保护），仅前端不再暴露编辑控件——最小改动、不影响既有安全边界。

### D5：Emby 订阅按钮按角色可见
`EmbyLibraryView.vue` 订阅按钮（含批量订阅与 TMDB 搜索对话框入口）外层加 `v-if="auth.isAdmin"`；guest 仅浏览。

### D6：队列页权限语义修正
`GET /queue/download/state` 从 admin-only 降为 `get_current_user`（暂停状态本身非敏感，前端已有角色分支控制操作）；`QueueView.vue` onMounted 对 guest 不再触发 403。控制按钮维持 `auth.isAdmin` 禁用/隐藏。
- 备选：仅前端 guest 不请求——后端语义仍不一致（该接口数据 admin 才可见），D6 更彻底。

### D7：安全审查
对后端认证/授权/注入/SSRF/敏感信息暴露与前端 XSS/权限绕过做一次系统审查；确认的漏洞在本 change 内修复并补测试，审查结果记录在 verify 报告。

## Risks / Trade-offs

- [D1 依赖 TMDB episode_info_cache 完整性] → 缓存未命中回源；服务故障降级为现状口径（不报错）
- [D4 保留后端角色接口，UI 移除后角色固定] → 与用户需求一致（仅展示）；如需彻底关闭后端能力可后续单独处理
- [D6 状态接口放宽到登录用户] → 数据非敏感；若未来暴露更细粒度内部信息需重评
- [D7 审查范围广] → 分模块核查，确认漏洞优先修高危/中危，低危记录到报告

## Migration Plan

- 纯行为修正，无数据库变更；前端构建产物由 `npm run build` 重建
- 回滚：涉及改动均为小范围行为修正，git revert 对应 commit 即可

## Open Questions

- 无（D1 降级口径、D4 后端接口去留已在决策中明确）
