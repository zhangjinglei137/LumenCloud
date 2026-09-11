# Tasks: fix-online-issues

## 1. 缺失集判定修正（D1）

- [x] 1.1 后端 media.py `_stats()` 计算「已开播集数」：复用 tmdb.get_episode_info 的 air_date（≤ today）计数作为缺失基数，missing = 已开播集数 − available，total 仍返回 TMDB 全集数；外部服务故障降级为现状口径（total − available），验证 `pytest` 列表统计用例通过
- [x] 1.2 补充后端测试：未开播集不计入缺失（air_date 未来）、Emby 未配置回退、混合已开播/未开播场景，验证 `pytest` 新增用例通过
- [x] 1.3 前端核对 `episodeSummaryText`：缺失文案与后端新口径一致（无未开播集计入），验证 `vitest` format 用例通过

## 2. 影视详情状态收敛两值（D2）

- [x] 2.1 前端 format.ts `mediaStatusLabel/Type`：下载执行态键归并中文兜底（download/downloading → 下载中），未知值不再原样透传英文，验证 `vitest` 状态字典用例通过
- [x] 2.2 核对后端 create/patch 与巡检回写路径不产生 tracking/paused 之外的值，验证 pytest 详情/修改接口用例通过

## 3. Emby 封面代理修复（D3）

- [x] 3.1 后端 emby.py `_normalize_library_item`：poster_url 构造补前导 `/`（`/api/poster?p=/emby/{item_id}/Primary`），验证生成路径经 `_validate_poster_path` 校验通过
- [x] 3.2 后端补测试：构造的 Emby 代理路径通过校验、非法路径仍拒绝，验证 `pytest` poster 用例通过

## 4. 用户角色只读（D4）

- [x] 4.1 前端 UsersView.vue：角色列由 el-select 改为只读 tag（复用 roleLabel/roleTagType），移除角色修改入口与 patchRole 调用，验证前端构建通过且无角色编辑控件
- [x] 4.2 核对后端 PATCH /admin/users/{id} 保留但前端不再调用（admin-only + 既有 409 保护），验证 pytest admin 用户用例不受影响

## 5. Emby 订阅按钮按角色可见（D5）

- [x] 5.1 前端 EmbyLibraryView.vue：单条订阅按钮、批量订阅入口、TMDB 搜索对话框订阅入口加 `v-if="auth.isAdmin"`，guest 仅浏览，验证前端构建通过
- [x] 5.2 前端测试补充：admin 可见订阅入口、guest 不显示订阅按钮用例，验证 `vitest` 通过

## 6. 队列页访客权限语义（D6）

- [x] 6.1 后端 queue.py `GET /queue/download/state` 降为 get_current_user（登录即可读暂停状态），验证 pytest 队列状态接口用例（admin/guest 均可读）
- [x] 6.2 前端 QueueView.vue：onMounted 对 guest 不触发 403（状态接口登录可读后自然修复），控制按钮维持 auth.isAdmin 禁用/隐藏，验证前端构建通过且 guest 打开无权限弹窗

## 7. 安全审查（D7）

- [x] 7.1 后端安全审查：认证/授权（路由依赖覆盖）、SQL 注入（参数化）、SSRF（poster/emby 回源校验）、敏感信息泄露（api_key/token 回显），修复确认的高危/中危漏洞并补测试，验证 pytest 通过
- [x] 7.2 前端安全审查：XSS（v-html/插值）、权限绕过（按钮/路由级）、敏感信息暴露（localStorage/token），修复确认的漏洞，验证前端构建 + vitest 通过

## 8. 验证

- [x] 8.1 集成验证：后端 `pytest` 全量通过、前端 `vitest` 全量通过、`npm run build` 成功，验证 7 项问题对应行为（缺失集/状态/图片/角色/订阅/队列提示/安全修复）均符合 delta spec
