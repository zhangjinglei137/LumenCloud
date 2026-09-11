---
comet_change: fix-online-issues
role: technical-design
canonical_spec: openspec
---

# 深度技术设计：fix-online-issues

对应 OpenSpec change：`fix-online-issues`（7 项线上问题修复 + 安全审查）。本文件是对 open 阶段 `design.md` 高层决策（D1-D7）的深度技术细化。

## 1. 缺失集判定修正（D1）

### 现状与根因

`backend/app/routers/media.py` `_stats()`（约 L449-L468）：

```python
total = ep["total"]
if tmid and tmid in tmdb_totals:
    total = max(total, tmdb_totals[tmid])   # TMDB number_of_episodes，含未开播集
available = len(done_codes | ingested)      # 本系统完成 ∪ Emby 已入库（去重）
"missing": max(0, total - available)        # 未开播集被计入 missing
```

- `tmdb_totals` 来自 `TmdbCache.number_of_episodes`（仅 tv 有），是 TMDB 全集数，**含未开播集**。
- 用户反馈「未开播不应计入缺失」「已有计数出现重复」：缺失虚高；「已有」= done ∪ ingested 本身去重正确，疑似用户对数字与详情页其他统计口径不一致的观感（详情页按 TMDB 全集轴展示含未开播集）。

### 实现设计

在 `_stats()` 中新增「已开播集数」计算（仅 tv + tmdb_id 存在时）：

```
missing = max(0, aired_total - available)
其中 aired_total = episode_info_cache 中 air_date ≤ today 的集数
```

数据源与详情页「TMDB 全集」一致（`tmdb.get_episode_info(tmdb_id)` 返回 `[{season, episode, name, air_date}]`，源自 `episode_info_cache` 表，每日由 `episode_info_refresh` 刷新）：

- 列表聚合场景不允许逐影视逐季回源（会 N 倍放大 TMDB 请求）。判定链路：
  1. 对每个需要统计的 tv media，调用 `tmdb.get_episode_info(tmdb_id)`（只读 episode_info_cache）；
  2. 缓存非空 → `aired_total = count(air_date exists and air_date <= today)`。air_date 缺失/非法视为**已开播**（保守不误杀，宁可计为已开播）；air_date > today 计为未开播，从缺失基数扣除；
  3. 缓存为空（尚未构建/刷新失败）→ 回退现状口径 `aired_total = total`（与现状一致，不引入新错误）。
- `in_emby`/ingested 判定维持现状（`get_ingested_episode_codes` 已含 TTL 缓存 + 配置指纹）。
- `available` 计算不变（去重语义已正确）；`total` 字段仍返回 TMDB 全集数（前端展示参考）。

### 性能与并发

- 复用现状 `asyncio.gather` 模式扩展：`emby_targets` 判定改用 `aired_total`；episode_info_cache 为单表索引读（tmdb_id 唯一），命中零网络开销。
- episode_info_cache 缺失时**不得**触发批量回源（保持现状的整表无回源约束——现状列表接口也从不回源 TMDB season，只有详情页单影视回源）。

### 边界条件

| 场景 | missing 口径 |
|------|-------------|
| cache 全部有 air_date，部分未来 | 只扣未来集 |
| cache 存在但某集 air_date 为 null | 该集按已开播计入基数（保守） |
| cache 为空 | 回退 `total - available`（现状语义） |
| 外部服务故障（get_episode_info 抛异常） | try 包裹，回退 `total - available`，记 warning |
| movie / 无 tmdb_id | 不统计（现状行为） |

### 测试

- `test_media_list_emby_stats.py` 扩展：mock `get_episode_info` 返回含未来 air_date 的集 → 断言缺失不扣未来集；mock 返回 None（cache miss）→ 断言回退现状口径。
- 前端 `format.test.ts` `episodeSummaryText`：口径依赖后端，前端仅核对展示（已有用例覆盖）。

## 2. 影视详情状态收敛两值（D2）

### 现状与根因

- 后端模型 `Media.status` 注释含 tracking/downloading/paused/error，但 `create_media`（L546 `status="tracking"`）与 `patch_media`（L852 校验 `status 仅支持 tracking/paused`）已强制两值。**问题在历史遗留数据与前端字典**：
- 前端 `format.ts` `MEDIA_STATUS_MAP` 含 download 变体键，`mediaStatusLabel` 对未知键原样透传英文（如历史值 `download` → 显示英文）。

### 实现设计

1. **前端**：`MEDIA_STATUS_MAP` 增加归一兜底——`download` 归并到 `downloading: ['下载中', 'primary']`；`mediaStatusLabel/Type` 对未知值回退 `[原始值, 'info']` 改为统一回退中文「未知」+ info 灰（不原样透传英文）。
2. **后端**：确认无巡检/任务回写路径改写 `media.status`（scan.py 仅操作 episode_state/download_queue/task_queue，不碰 media.status）——代码核对即可，无逻辑改动。
3. **状态下拉**：详情页 `MediaDetailView.vue` 下拉已仅两选项（订阅中/已暂停），无需改。

### 测试

- 前端 `format.test.ts`：`mediaStatusLabel('download') === '下载中'`；未知值回退「未知」。
- 后端 `test_media_two_queue.py`/详情接口用例回归（status 两值写入校验已覆盖）。

## 3. Emby 封面代理路径修复（D3）

### 现状与根因（明确 bug）

`backend/app/services/emby.py` `_normalize_library_item`（L432）：

```python
poster_url = f"/api/poster?p=emby/{item_id}/Primary"   # 缺前导 /
```

`backend/app/services/poster.py` `_validate_poster_path`（L59-L69）：

```python
if p.startswith("/emby/"):
    parts = p.split("/")
    if len(parts) != 4 or parts[-1] != "Primary": return False
    return bool(_EMBY_ITEM_ID_RE.match(parts[2]))
```

FastAPI Query 解码后 `p="emby/{id}/Primary"` **不以 `/` 开头** → 不进 `/emby/` 分支，走 `posixpath.normpath` 后 `norm.startswith("/t/p/")` 也不满足 → **返回 False → 400**。全部 Emby 封面请求失败，图片全丢。

### 实现设计

```python
poster_url = f"/api/poster?p=/emby/{item_id}/Primary"   # 补前导 /
```

`poster.py` 校验逻辑不动（已正确：`/emby/<itemId>/Primary` 白名单 + 参数化回源）。同步核对 `_emby_image_url` 构造（已是 `{base}/Items/{item_id}/Images/Primary?api_key=...`，正确）。

### 测试

- `test_poster_proxy.py` 扩展：将生产构造路径（`/api/poster?p=/emby/<id>/Primary` 解码后的 p）直接喂 `_validate_poster_path` → True；非 `/emby/` 前缀 → 仍拒绝。
- 或在 `test_emby_library_all.py` 断言 `_normalize_library_item` 输出的 poster_url 中 `p=` 后为 `/emby/` 前缀。

## 4. 用户角色只读（D4）

### 现状

`UsersView.vue` 角色列（L114-L123）用 `el-select` 绑定 `row.role`，`onRoleChange` 调 `store.patchRole` → `PATCH /api/admin/users/{id}`。后端 `update_user_role`（admin-only + 自改/唯一管理员 409 保护）。

### 实现设计

- 前端角色列改为只读展示：保留 `roleTagType/roleLabel` 计算的 tag（或纯文本 + tag），移除 `el-select`、`onRoleChange`、`patchingRoleIds` 与 `patchRole` store 调用链。顶栏说明文案同步去掉「调整角色立即生效」字样。
- 后端接口保留（`test_admin_users.py` 回归不破坏）；`users store` 移除 `patchRole` action（无调用方）——仅删除前端调用，接口与 store 可保留 action 以最小 diff，但**无调用方 action 应删除**避免死代码。

### 测试

- 前端：构建通过；无角色编辑控件渲染（可加组件测试或人工核对）。
- 后端 `test_admin_users.py` 全量回归（PATCH 接口仍可用）。

## 5. Emby 订阅按钮按角色可见（D5）

### 现状

`EmbyLibraryView.vue` 订阅按钮（L495-L504）仅 `v-if="!m.in_media"` 无角色判断，guest 也能看到并使用 `POST /api/media`（后端 admin-only → guest 点击 403 拦截器报错）。

### 实现设计

- 单条订阅按钮、批量「全部加入订阅」入口（L377 附近）、TMDB 搜索对话框「加入订阅」按钮（L547），全部加 `v-if="auth.isAdmin"`；guest 浏览统一的「已订阅/未收录」无操作提示或隐藏操作行。
- `subscribe()`/`onSubscribeClick`/`markSubscribed` 逻辑保持（仅 admin 触达）。

### 测试

- 组件测试或构建核对：`auth.isAdmin === true` 渲染订阅按钮；false 不渲染。现有 `emby.test.ts`（API 层）不受影响。

## 6. 队列页访客权限语义（D6）

### 现状与根因

`QueueView.vue` onMounted（L245）：

```js
await Promise.all([store.fetchPage(), store.fetchCapacity(), store.fetchPauseState()])
```

- `fetchPage` → `GET /queue`：get_current_user（guest 可读）✓
- `fetchCapacity` → `GET /capacity`：get_current_user ✓
- `fetchPauseState` → `GET /queue/download/state`：**get_current_admin**（queue.py L287）→ guest 403 → 全局拦截器 toast「需要管理员权限」——用户反馈「可看但不该弹权限提示」。

### 实现设计

- `queue.py` `download_queue_state` 依赖从 `get_current_admin` 降为 `get_current_user`（暂停状态与在途数量非敏感，前端已按 `auth.isAdmin` 禁用暂停开关与控制按钮）。
- `QueueView.vue` 无需为 guest 分支（接口降权后自然可用）；控制按钮维持 `:disabled="!auth.isAdmin"` / `v-if="auth.isAdmin"`（现状已有）。
- 全局拦截器对 403 的 toast 语义不动（admin 路径真实 403 仍需提示）。

### 测试

- 后端 queue 测试：admin 与 guest 均 200 访问 `GET /queue/download/state`；写操作（pause/resume/cancel 等）仍 admin-only 403。
- 前端：构建通过。

## 7. 安全审查（D7）

### 审查范围与方法

逐模块核查以下清单，形成发现清单（严重级：Critical/High/Medium/Low），确认漏洞本 change 内修复：

| 领域 | 核查点 |
|------|--------|
| 认证 | JWT 验签（_JWT_SECRET 算法是否固定）、token 过期/注销、密码存储（哈希算法） |
| 授权 | 每个路由依赖（get_current_user/admin）是否与操作匹配；guest 可达写操作 |
| 注入 | SQL（SQLAlchemy 参数化）/ 命令注入 / 路径穿越 |
| SSRF | poster.py `_validate_poster_path`、emby/tmdb 回源 URL 拼接、alist/nastools 回源 |
| 敏感信息 | api_key/token/密码回显（settings GET 遮蔽是否完整）、日志泄露、响应脱敏（shared_code 等） |
| 前端 | XSS（v-html/插值）、token 存取（localStorage）、按钮级权限绕过、依赖漏洞（build 告警） |
| 配置 | 默认凭据、CORS、调试端点暴露 |

### 修复策略

- Critical/High：本 change 内修复 + 补测试。
- Medium：修复或记录为已知风险（决策点）。
- Low：记录到验证报告，不阻塞。

## 8. 变更文件清单

| 文件 | 变更 |
|------|------|
| `backend/app/routers/media.py` | `_stats()` aired_total 口径 |
| `backend/app/services/emby.py` | poster_url 补前导 `/` |
| `backend/app/routers/queue.py` | state 接口降权 |
| `frontend/src/utils/format.ts` | 状态字典兜底 |
| `frontend/src/views/UsersView.vue` | 角色只读 |
| `frontend/src/views/EmbyLibraryView.vue` | 订阅按钮按角色 |
| `frontend/src/stores/users.ts` | 移除 patchRole（可选，随 D4） |
| 测试文件 | 上述各模块对应用例 |

## 9. 风险与回退

- D1 依赖 episode_info_cache 完整性 → 回源/降级兜底（见 §1 边界条件表）。
- D4/D6 属权限/UI 语义调整，前端构建产物重建即可；git revert 可整体回退。
- D7 修复可能触及认证/授权核心 → 修复前置测试，逐项 commit，便于定位回退。

## 10. Open Questions

无（open 阶段已与用户确认范围；本阶段无改变 spec/方案/任务的未决项）。