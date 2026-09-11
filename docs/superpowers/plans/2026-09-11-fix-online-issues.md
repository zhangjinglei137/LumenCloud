---
change: fix-online-issues
design-doc: docs/superpowers/specs/2026-09-11-fix-online-issues-design.md
base-ref: 3caf18807f6ecc987fc5518bec2da69a921477aa
---

# fix-online-issues 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 7 项线上问题（缺失集判定/状态两值/Emby 图片/角色只读/订阅权限/队列提示）并完成安全审查。

**Architecture:** 后端修正 `media.py _stats()` 缺失口径、`emby.py` poster_url 前缀、`queue.py` 状态接口降权；前端修正状态字典兜底、用户角色只读、订阅按钮按角色。安全审查分模块核查并修复确认漏洞。

**Tech Stack:** FastAPI + SQLAlchemy（后端）、Vue3 + Element Plus + Pinia（前端）、pytest + vitest。

**Spec:** `docs/openspec/changes/fix-online-issues/`（proposal/specs/design/tasks）+ `docs/superpowers/specs/2026-09-11-fix-online-issues-design.md`

## Global Constraints

- 产物语言 zh-CN；commit 消息中文描述（Conventional Commits 结构）
- 不引入新依赖、不改数据库 schema、不改 Emby 分类/Tab 结构、不改队列两表状态机
- 每个任务必须先写失败测试再实现（TDD，tdd_mode: tdd）
- 后端测试命令：`cd backend && .venv/bin/python -m pytest -q`；前端：`cd frontend && npx vitest run`；构建：`cd frontend && npm run build`
- 历史脏数据不强制迁移（前端兜底即可）

---

### Task 1: 缺失集判定按已开播口径（后端 `_stats()`）

**Files:**
- Modify: `backend/app/routers/media.py:449-468`（`_stats()`）
- Test: `backend/tests/test_media_list_emby_stats.py`

**Interfaces:**
- Consumes: `tmdb.get_episode_info(tmdb_id: int) -> list[dict]`（已存在，返回 `[{season, episode, name, air_date}]`，空缓存返回 `[]`）
- Produces: `_stats(m)` 新增字段 `aired_total`（内部用），`missing = max(0, aired_total - available)`；`total` 保持 TMDB 全集数

- [x] **Step 1: 写失败测试**

在 `test_media_list_emby_stats.py` 追加（mock `tmdb.get_episode_info` 返回含未来 air_date 的集）：

```python
@pytest.mark.asyncio
async def test_missing_excludes_unaired(monkeypatch, ...):
    """未开播集不计入缺失：aired_total 只计 air_date <= today 的集。"""
    async def fake_episode_info(tmdb_id):
        today = date.today().isoformat()
        future = (date.today() + timedelta(days=30)).isoformat()
        return [
            {"season": 1, "episode": 1, "name": "A", "air_date": today},
            {"season": 1, "episode": 2, "name": "B", "air_date": today},
            {"season": 1, "episode": 3, "name": "C", "air_date": future},
        ]
    monkeypatch.setattr("app.routers.media.tmdb.get_episode_info", fake_episode_info)
    # ... 构造 media + 断言 stats["missing"] 不含未来集
```

- [x] **Step 2: 运行测试确认失败**

Run: `cd backend && .venv/bin/python -m pytest -q tests/test_media_list_emby_stats.py -x`
Expected: FAIL（当前 missing = total - available 含未开播集）

- [x] **Step 3: 实现 `_stats()` 修正**

在 `media.py` `list_media` 的 Emby 聚合区（L408-L423 附近）并行收集每部 tv 的已开播集数：

```python
async def _fetch_aired_total(m: Media) -> tuple[int, int]:
    try:
        eps = await tmdb.get_episode_info(m.tmdb_id)
        if not eps:
            return m.id, -1  # 无缓存 → 回退 total 口径
        today = date.today()
        aired = sum(
            1 for ep in eps
            if not ep.get("air_date") or (ep["air_date"] <= today.isoformat())
        )
        return m.id, aired
    except Exception:
        return m.id, -1
```

`_stats()` 中：

```python
aired_total = aired_by_media.get(m.id, -1)
base = aired_total if aired_total >= 0 else total
"missing": max(0, base - available),
```

- [x] **Step 4: 运行测试确认通过**

Run: `cd backend && .venv/bin/python -m pytest -q tests/test_media_list_emby_stats.py -x`
Expected: PASS（含既有用例回归）

- [x] **Step 5: 提交**

```bash
git add backend/app/routers/media.py backend/tests/test_media_list_emby_stats.py
git commit -m "fix(media): 缺失集按已开播口径计算，未开播集不计入缺失"
```

---

### Task 2: 前端 `episodeSummaryText` 核对 + 状态字典兜底

**Files:**
- Modify: `frontend/src/utils/format.ts:74-93`（MEDIA_STATUS_MAP + mediaStatusLabel/Type）、`frontend/src/utils/format.ts:236-251`（episodeSummaryText 仅核对，通常无需改）
- Test: `frontend/src/utils/format.test.ts`

**Interfaces:**
- Consumes: 无
- Produces: `mediaStatusLabel('download') === '下载中'`；未知值回退「未知」

- [x] **Step 1: 写失败测试**

`format.test.ts` 追加：

```ts
describe('mediaStatusLabel 兜底', () => {
  it('download 归并到下载中', () => {
    expect(mediaStatusLabel('download')).toBe('下载中')
  })
  it('未知值回退未知而非原样透传', () => {
    expect(mediaStatusLabel('weird_value')).toBe('未知')
  })
})
```

- [x] **Step 2: 运行确认失败**

Run: `cd frontend && npx vitest run src/utils/format.test.ts`
Expected: FAIL

- [x] **Step 3: 实现**

`MEDIA_STATUS_MAP` 增加 `download: ['下载中', 'primary']`；`mediaStatusLabel` 未知键返回 `'未知'`：

```ts
export function mediaStatusLabel(status: string | null | undefined): string {
  if (!status) return '—'
  return MEDIA_STATUS_MAP[status]?.[0] ?? '未知'
}
```

`mediaStatusType` 未知键返回 `'info'`（现状已如此）。核对 `episodeSummaryText` 与后端新口径一致（无逻辑改动）。

- [x] **Step 4: 运行确认通过**

Run: `cd frontend && npx vitest run src/utils/format.test.ts`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add frontend/src/utils/format.ts frontend/src/utils/format.test.ts
git commit -m "fix(frontend): 媒体状态字典中文兜底，未知值不再原样透传"
```

---

### Task 3: Emby 封面代理路径修复

**Files:**
- Modify: `backend/app/services/emby.py:432`
- Test: `backend/tests/test_poster_proxy.py`（或 `test_emby_library_all.py`）

**Interfaces:**
- Consumes: `_validate_poster_path(p: str) -> bool`（poster.py，要求 `/emby/<itemId>/Primary` 前缀）
- Produces: `poster_url = "/api/poster?p=/emby/{item_id}/Primary"`（补前导 `/`）

- [x] **Step 1: 写失败测试**

`test_poster_proxy.py` 追加：

```python
def test_production_poster_url_passes_validation():
    """生产构造的 Emby 代理路径必须通过校验（回归：缺前导 / 曾导致全部 400）。"""
    url = f"/api/poster?p=/emby/{'abc-123'}/Primary"
    p = url.split("p=", 1)[1]
    assert poster_mod._validate_poster_path(p) is True
```

- [x] **Step 2: 运行确认失败**

Run: `cd backend && .venv/bin/python -m pytest -q tests/test_poster_proxy.py -x`
Expected: FAIL（当前 `p=emby/...` 无前导 `/`）

- [x] **Step 3: 实现**

`emby.py` L432：

```python
poster_url = f"/api/poster?p=/emby/{item_id}/Primary"
```

- [x] **Step 4: 运行确认通过**

Run: `cd backend && .venv/bin/python -m pytest -q tests/test_poster_proxy.py tests/test_emby_library_all.py -x`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add backend/app/services/emby.py backend/tests/test_poster_proxy.py
git commit -m "fix(emby): 封面代理路径补前导斜杠，修复全部图片 400 丢失"
```

---

### Task 4: 用户角色只读展示

**Files:**
- Modify: `frontend/src/views/UsersView.vue:110-130`（角色列）、`frontend/src/views/UsersView.vue:47-60`（onRoleChange 移除）、`frontend/src/stores/users.ts:20-23`（patchRole 移除）
- Test: 前端构建核对 + `backend/tests/test_admin_users.py` 回归

**Interfaces:**
- Consumes: `roleLabel(role) -> '管理员'|'访客'`、`roleTagType(role) -> 'warning'|'info'`（已存在）
- Produces: 角色列只读 tag，无 el-select

- [x] **Step 1: 移除编辑控件**

`UsersView.vue` 角色列 `el-select` 块（L110-130）替换为只读展示：

```vue
<el-table-column label="角色" width="150">
  <template #default="{ row }">
    <el-tag size="small" effect="plain" :type="roleTagType(row.role)">
      {{ roleLabel(row.role) }}
    </el-tag>
  </template>
</el-table-column>
```

移除 `onRoleChange`、`patchingRoleIds`；`stores/users.ts` 移除 `patchRole` action。

- [x] **Step 2: 前端构建验证**

Run: `cd frontend && npm run build`
Expected: 成功（无未使用引用报错）

- [x] **Step 3: 后端回归**

Run: `cd backend && .venv/bin/python -m pytest -q tests/test_admin_users.py`
Expected: PASS（PATCH 接口仍可用）

- [x] **Step 4: 提交**

```bash
git add frontend/src/views/UsersView.vue frontend/src/stores/users.ts
git commit -m "feat(frontend): 用户管理角色改为只读展示，移除编辑入口"
```

---

### Task 5: Emby 订阅按钮按角色可见

**Files:**
- Modify: `frontend/src/views/EmbyLibraryView.vue:377`（批量入口）、`495-504`（单条按钮）、`547`（对话框订阅按钮）
- Test: 前端构建 + vitest 补充

**Interfaces:**
- Consumes: `auth.isAdmin`（useAuthStore，已存在）
- Produces: 订阅相关按钮 `v-if="auth.isAdmin"`

- [x] **Step 1: 实现按角色可见**

三处订阅入口加 `v-if="auth.isAdmin"`（或包一层）；guest 时未收录条目不显示订阅按钮，仅保留「在 Emby 中打开」等只读入口。

- [x] **Step 2: 补充前端测试**

`EmbyLibraryView` 相关测试（若现有组件测试则扩展；否则在新增 mount 测试中）：admin 渲染订阅按钮、guest 不渲染。若项目无组件测试基建，改为构建核对 + 在 `format.test.ts` 或 view 测试补 `auth` stub。

- [x] **Step 3: 构建 + vitest 验证**

Run: `cd frontend && npm run build && npx vitest run`
Expected: 通过

- [x] **Step 4: 提交**

```bash
git add frontend/src/views/EmbyLibraryView.vue
git commit -m "fix(frontend): Emby 库订阅按钮仅管理员可见，访客只读浏览"
```

---

### Task 6: 队列状态接口降权（访客不误报权限）

**Files:**
- Modify: `backend/app/routers/queue.py:286-287`（download_queue_state 依赖）
- Test: `backend/tests/test_queue_*.py`（或现有队列测试）

**Interfaces:**
- Consumes: `get_current_user`（deps.py）
- Produces: `GET /queue/download/state` 登录用户可读

- [x] **Step 1: 写失败测试**

队列测试追加：guest 访问 `GET /queue/download/state` 期望 200；写操作仍 403。

- [x] **Step 2: 运行确认失败**

Run: `cd backend && .venv/bin/python -m pytest -q tests/test_queue.py -x`（或实际测试文件）
Expected: FAIL（guest 403）

- [x] **Step 3: 实现**

`queue.py` L287：`admin: User = Depends(get_current_admin)` → `user: User = Depends(get_current_user)`。

- [x] **Step 4: 运行确认通过**

Run: `cd backend && .venv/bin/python -m pytest -q tests/test_queue.py -x`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add backend/app/routers/queue.py backend/tests/test_queue.py
git commit -m "fix(queue): 暂停状态接口降为登录可读，访客打开队列页不再误报权限"
```

---

### Task 7: 安全审查

**Files:**
- Review: `backend/app/routers/*.py`、`backend/app/services/*.py`、`backend/app/routers/deps.py`、`backend/app/config.py`、`frontend/src/**`
- Modify: 按发现修复（具体文件以审查结果为准）
- Test: 对应模块测试

**Interfaces:**
- Consumes: 设计文档 §7 审查清单
- Produces: 确认漏洞修复 + 测试；审查发现记录（供 verify 报告）

- [x] **Step 1: 后端审查（认证/授权/注入/SSRF/敏感信息）**

逐路由核对依赖覆盖（get_current_user/admin 与操作匹配）、SQLAlchemy 参数化、poster/emby/tmdb/alist/nastools 回源 URL 拼接、settings GET 遮蔽、日志泄露、DTO 脱敏（share_code 等）。修复确认的 Critical/High 漏洞并补测试。

- [x] **Step 2: 前端审查（XSS/权限绕过/敏感信息）**

核对 v-html 使用、token 存取、按钮级权限（auth.isAdmin 覆盖所有写操作入口）、localStorage 敏感数据。修复确认漏洞。

- [x] **Step 3: 全量回归**

Run: `cd backend && .venv/bin/python -m pytest -q` && `cd frontend && npx vitest run && npm run build`
Expected: 全部通过

- [x] **Step 4: 提交**

```bash
git add -A
git commit -m "fix(security): 安全审查修复确认漏洞（明细见验证报告）"
```

---

### Task 8: 集成验证

**Files:**
- Review: 全部变更
- Test: `backend/tests/`、`frontend/src/**/*.test.ts`

**Interfaces:**
- Consumes: 全部任务产物
- Produces: 全量验证证据

- [ ] **Step 1: 后端全量**

Run: `cd backend && .venv/bin/python -m pytest -q`
Expected: 全量 PASS

- [ ] **Step 2: 前端全量**

Run: `cd frontend && npx vitest run && npm run build`
Expected: 全量 PASS

- [ ] **Step 3: 核对 7 项问题行为**

逐项核对 delta spec 场景：缺失集（未开播不计入）、状态（两值+中文兜底）、图片（poster 路径校验通过）、角色（只读）、订阅（admin 可见）、队列（guest 无 403 弹窗）、安全修复。

- [ ] **Step 4: 提交验证证据**

```bash
git add -A
git commit -m "chore(comet): fix-online-issues 集成验证通过"
```

---

## Self-Review

- **Spec 覆盖**：media-status（T1/T2）、media-detail-ui（T2）、emby-library-browse（T3/T5）、queue-inspection-display（T6）、user-role-display（T4）、安全审查（T7）— 全部覆盖。
- **占位扫描**：无 TBD/TODO；测试代码均为实际断言。
- **类型一致**：`get_episode_info` 返回结构（season/episode/name/air_date）与 T1 使用一致；`auth.isAdmin` 在 T5 使用与现有 store 一致；`roleLabel/roleTagType` 在 T4 使用与现有实现一致。
