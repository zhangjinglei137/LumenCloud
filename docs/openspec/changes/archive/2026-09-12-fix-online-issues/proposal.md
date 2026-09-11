## Why

线上反馈共 7 个问题需要处理：①影视库缺失集判定错误（未开播集被计入缺失，且「已有」计数出现重复）；②影视详情状态字段混入非两值状态（如 download）且未转义中文、状态下拉选项不全；③Emby 影视库全部影视图片丢失；④用户管理角色不应可编辑（仅展示）；⑤Emby 影视库「加入订阅」字样应仅管理员可见；⑥任务队列对访客用户误弹「需要管理员权限」提示（可见但不可操作是预期，提示文案不应出现）；⑦全代码安全漏洞审查。

## What Changes

- **缺失集判定修正**（问题 1）：影视列表集数统计中，未开播（air_date 晚于今天）的集不纳入缺失计数；「已有 N 缺失 M」的 N（已有）与 M（缺失）计数口径核对修正，消除重复计数
- **影视详情状态收敛为两值**（问题 2）：media.status 仅保留 tracking（订阅中）/ paused（已暂停）两个合法值；下载等执行态不得写入该字段；状态展示缺键时回退中文而非原样透传，状态下拉与字典保持一致
- **Emby 影视库图片修复**（问题 3）：修复 Emby 封面代理路径构造（poster_url 前缀缺失导致 `_validate_poster_path` 校验失败返回 400），使 Emby 封面经代理正常加载
- **用户管理角色只读**（问题 4）：用户管理列表角色列仅展示（tag/文本），移除角色编辑下拉
- **Emby 库订阅按钮按角色可见**（问题 5）：「加入订阅」按钮仅 admin 角色显示，guest 不展示订阅入口
- **任务队列访客权限提示修正**（问题 6）：访客进入任务队列页不再误弹「需要管理员权限」；列表可见、操作按钮禁用/隐藏，权限语义与提示一致
- **全代码安全审查**（问题 7）：对后端（认证/授权/注入/SSRF/敏感信息）与前端（XSS/权限绕过）进行安全审查，修复确认的漏洞

## Capabilities

### New Capabilities
- `user-role-display`: 用户管理列表角色只读展示（此前无用户管理 spec，本次新增角色展示约束）

### Modified Capabilities
- `media-status`: 影视列表集数统计修正——未开播集不计入缺失，「已有/缺失」计数口径核对
- `media-detail-ui`: 影视详情状态收敛为 tracking/paused 两值，状态字典与下拉对齐
- `emby-library-browse`: Emby 封面代理路径修复 + 「加入订阅」按钮仅管理员可见
- `queue-inspection-display`: 访客访问任务队列不误报权限提示，操作入口按角色禁用/隐藏

## Impact

- 后端：`backend/app/routers/media.py`（缺失集统计、media.status 校验）、`backend/app/services/emby.py`（poster_url 构造）、`backend/app/services/poster.py`（路径校验核对）、`backend/app/routers/queue.py`（download/state 权限语义）、`backend/app/routers/admin.py`（用户角色接口，若只读化则调整）、`backend/app/routers/deps.py`（权限提示语义）
- 前端：`frontend/src/views/MediaListView.vue`（集数文案）、`frontend/src/views/MediaDetailView.vue`（状态下拉/字典）、`frontend/src/views/EmbyLibraryView.vue`（图片/订阅按钮权限）、`frontend/src/views/UsersView.vue`（角色只读）、`frontend/src/views/QueueView.vue`（访客权限展示）、`frontend/src/utils/format.ts`（状态字典回退）
- 测试：后端 pytest（缺失集统计、poster 路径、队列权限）、前端 vitest（状态字典、订阅按钮权限）
- 依赖：无新增；数据库无变更（仅行为修正）
