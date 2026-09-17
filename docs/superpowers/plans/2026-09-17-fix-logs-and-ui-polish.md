---
change: fix-logs-and-ui-polish
design-doc: docs/superpowers/specs/2026-09-17-fix-logs-and-ui-polish-design.md
base-ref: 38fa9d14a22eba3fb789b31e0a6ac552a8294f24
---

# fix-logs-and-ui-polish 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: 使用 superpowers:executing-plans 或 subagent-driven-development 按计划逐任务实施。步骤使用 checkbox（`- [ ]`）语法跟踪。

**Goal:** 修复运行日志任务类型显示/分页/保留天数配置、邀请码管理迁移到用户管理页、影视库卡片「已有」文案去重。

**Architecture:** 前后端同仓（FastAPI + Vue3/Element Plus）。后端 `/api/logs` 改 `{items, total}` 契约、settings 白名单加保留天数键；前端对齐任务类型映射、消费真实 total、迁移邀请码区块、去重卡片文案。

**Tech Stack:** Python FastAPI + SQLAlchemy（async）；Vue3 + Element Plus + Pinia + Vitest。

**Spec:** `docs/superpowers/specs/2026-09-17-fix-logs-and-ui-polish-design.md`（技术设计，D-A1~D-A5）
**Change:** `docs/openspec/changes/fix-logs-and-ui-polish/`（proposal/specs/tasks）

---

## Global Constraints

- 产物语言：zh-CN（所有用户可见文案、commit 描述用中文）
- 后端任务类型**不得新增/重命名**写入值，仅前端映射展示层对齐
- `GET /api/logs` 契约变化为 **BREAKING**，前后端同仓同步适配，不得只改一端
- 不引入新依赖；不做数据库 schema 变更；不动 `cleanup.py` 清理逻辑
- 提交信息遵循 Conventional Commits（type+scope+中文摘要）
- 遵守 AGENTS.md：不得主动启动/停止服务，验证只运行测试/构建命令

---

### Task 1: 任务类型映射对齐（format.ts + LogsView.vue）

**Files:**
- Modify: `frontend/src/utils/format.ts:416-440`（TASK_TYPE_MAP）
- Modify: `frontend/src/views/LogsView.vue:17-18`（筛选列表）
- Test: `frontend/src/utils/format.test.ts`

**Interfaces:**
- Consumes: 现有 `taskTypeLabel(type)` / `taskTypeType(type)`（签名不变）
- Produces: 更新后的 `TASK_TYPE_MAP`（9 个当前有效键 + `media_scan`/`recovery` 历史兜底键）；`LogsView.vue` 的 `taskTypes` 常量 = 当前有效键子集

- [x] **Step 1: 更新 TASK_TYPE_MAP**

将 `frontend/src/utils/format.ts` 的 `TASK_TYPE_MAP`（当前 12 键：scan_media, media_scan, scan_all_media, transfer, transfer_retry, download, nastools_sync, cleanup, notification_scan, capacity_alert, recover, recovery）改为：

```ts
/** 任务类型 → 中文标签 + Element Plus tag type（未知类型回退原值 / info）。
 * 键集与 backend/app/tasks/*.py 的 record_task_run 写入值一一对应：
 * scan.py(scan_media/scan_all_media) transfer.py(transfer) cleanup.py(cleanup/prune_history)
 * capacity_alert.py(capacity_alert) recovery.py(recover) nastools_sync.py(sync_nastools)
 * notification_scan.py(notify)。media_scan/recovery 为历史别名兜底（存量数据可能残留）。 */
const TASK_TYPE_MAP: Record<string, [string, string]> = {
  scan_media: ['影视巡检', 'primary'],
  scan_all_media: ['定时巡检', 'primary'],
  transfer: ['转存', 'warning'],
  cleanup: ['空间清理', 'info'],
  prune_history: ['历史清理', 'info'],
  capacity_alert: ['容量告警', 'danger'],
  recover: ['超时恢复', 'danger'],
  sync_nastools: ['目录同步入库', 'success'],
  notify: ['通知', 'info'],
  // 历史别名兜底（已不再写入，仅存量数据展示）
  media_scan: ['影视巡检', 'primary'],
  recovery: ['超时恢复', 'danger'],
}
```

注意：移除 `transfer_retry`、`download`、`nastools_sync`、`notification_scan` 四个键（后端无任何写入点，grep 0 命中）。

- [x] **Step 2: 更新 LogsView 筛选列表**

将 `frontend/src/views/LogsView.vue:18`：

```ts
// 与后端任务类型取值对齐（backend/app/tasks/*）；不含 media_scan/recovery 历史别名
const taskTypes = ['scan_media', 'scan_all_media', 'transfer', 'cleanup', 'prune_history', 'capacity_alert', 'recover', 'sync_nastools', 'notify']
```

- [x] **Step 3: 补充/运行前端测试**

在 `frontend/src/utils/format.test.ts` 增加：

```ts
describe('taskTypeLabel / taskTypeType（任务类型映射）', () => {
  it('新键 sync_nastools / notify / prune_history 映射中文', () => {
    expect(taskTypeLabel('sync_nastools')).toBe('目录同步入库')
    expect(taskTypeLabel('notify')).toBe('通知')
    expect(taskTypeLabel('prune_history')).toBe('历史清理')
  })
  it('历史别名 media_scan / recovery 兜底不显示英文', () => {
    expect(taskTypeLabel('media_scan')).toBe('影视巡检')
    expect(taskTypeLabel('recovery')).toBe('超时恢复')
  })
  it('已移除键 transfer_retry / left未知 回退原值 + info', () => {
    expect(taskTypeLabel('transfer_retry')).toBe('transfer_retry') // 存量数据原值兜底
    expect(taskTypeType('whatever_unknown')).toBe('info')
  })
})
```

（`taskTypeLabel` 需从 `../utils/format` import，若测试文件未引入则补 import。）

运行: `cd frontend && npx vitest run src/utils/format.test.ts`
Expected: 全部通过

- [x] **Step 4: 交叉核对后端写入点**

验证：`grep -rhoP 'record_task_run\([^)]*?"[a-z_]+"' backend/app/tasks/*.py | grep -oP '"[a-z_]+"$' | sort -u` 与 map 当前有效键一致（差异仅允许 `media_scan`/`recovery` 兜底键多出）。

- [x] **Step 5: Commit**

```bash
git add frontend/src/utils/format.ts frontend/src/views/LogsView.vue frontend/src/utils/format.test.ts
git commit -m "fix(logs): 对齐运行日志任务类型映射并清理无关类型"
```

---

### Task 2: 后端 /api/logs 返回 {items, total} 契约

**Files:**
- Modify: `backend/app/routers/logs.py:34-91`（list_logs）
- Modify: `backend/tests/test_fix_online.py:189-240`（契约适配）
- Modify: `backend/tests/test_task_run_duration.py` / `test_scan_run_phases.py`（若引用返回数组则适配）

**Interfaces:**
- Consumes: 现有筛选参数（task_type/status/media_id/tmdb_id/title/limit/offset）
- Produces: `list_logs` 返回 `dict {"items": list[dict], "total": int}`

- [x] **Step 1: 修改 list_logs 返回分页对象**

`backend/app/routers/logs.py` `list_logs` 返回值改为：

```python
    stmt = (
        stmt.order_by(TaskRun.started_at.desc(), TaskRun.id.desc())
        .limit(limit)
        .offset(offset)
    )
    # 分页真实 total：基于同一筛选条件 count（复用 stmt 子查询，筛选单点维护）
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await session.execute(count_stmt)).scalar() or 0
    rows = (await session.execute(stmt)).all()
    return {
        "items": [
            {
                "id": r[0].id,
                "task_type": r[0].task_type,
                "media_id": r[0].media_id,
                "status": r[0].status,
                "message": r[0].message,
                "started_at": r[0].started_at,
                "finished_at": r[0].finished_at,
                "duration_seconds": r[0].duration_seconds,
                "media_title": r[1],
                "tmdb_id": r[2],
                "phases": _json_or_none(r[0].phases),
                "scan_detail": _json_or_none(r[0].scan_detail),
            }
            for r in rows
        ],
        "total": total,
    }
```

注意：`func` 已在 `from sqlalchemy import func, select` 导入。返回注解 `-> list[dict]` 改为 `-> dict`。

- [x] **Step 2: 适配既有测试**

`backend/tests/test_fix_online.py` `_call_list_logs` 调用方全部改访问 `.items`：
- `rows = await _call_list_logs(session, tmdb_id=42)` → `res = ...; rows = res["items"]`
- `len(rows) == 2` → `assert res["total"] == 2` 且 `len(rows) == 2`
- `none_case == []` → `assert res["total"] == 0 and res["items"] == []`

检查 `test_task_run_duration.py`、`test_scan_run_phases.py`、`test_recovery.py` 中是否有 `await list_logs(...)` 后遍历/索引数组的断言，做同样适配。用 `grep -rn "list_logs(" backend/tests/` 找出全部调用点。

- [x] **Step 3: 新增 total 断言**

在 `test_fix_online.py` 的 `test_logs_tmdb_filter_and_title` 中补充：
- 无过滤时 `res["total"] == 4`
- limit 分页时 `listLogs(limit=2)` → `len(items)==2 and total==4`

- [x] **Step 4: 运行后端测试**

Run（在 backend 目录，需后端 venv）: `python -m pytest tests/test_fix_online.py tests/test_task_run_duration.py tests/test_scan_run_phases.py -q`
Expected: 全部通过

若没有可用 venv，记录 `COMET_*` 构建证据时注明 Python 依赖缺失（现状 imports 无法解析），测试改在 CI/具备环境时运行，并将该限制记录在 verify 证据中。

- [x] **Step 5: Commit**

```bash
git add backend/app/routers/logs.py backend/tests/test_fix_online.py
git commit -m "feat(logs): /api/logs 返回 items+total 真实分页契约"
```

---

### Task 3: 前端消费真实 total（api/types/logs store）

**Files:**
- Modify: `frontend/src/types/index.ts`（LogListResponse）
- Modify: `frontend/src/api/index.ts:314-328`（listLogsApi）
- Modify: `frontend/src/stores/logs.ts:30-49`（fetchPage）
- Test: `frontend/src/stores/logs.ts`（如无 store 测试则不改测试，靠 build 验证）

**Interfaces:**
- Consumes: 后端 `{items, total}` 响应
- Produces: `listLogsApi` 返回 `Promise<LogListResponse>`；`fetchPage` 直接消费 `res.total`

- [ ] **Step 1: types 增补响应类型**

`frontend/src/types/index.ts` 增补：

```ts
export interface LogListResponse {
  items: LogItem[]
  total: number
}
```

（`LogItem` 已存在于该文件。）

- [ ] **Step 2: 改 api**

`frontend/src/api/index.ts` `listLogsApi`：

```ts
export function listLogsApi(params: {
  task_type?: string
  status?: string
  media_id?: number
  tmdb_id?: number
  title?: string
  limit?: number
  offset?: number
}) {
  const query = Object.fromEntries(
    Object.entries(params).filter(([, value]) => value !== undefined && value !== null && value !== ''),
  )
  return http.get<LogListResponse>('/logs', { params: query }).then((r) => r.data)
}
```

- [ ] **Step 3: 改 store**

`frontend/src/stores/logs.ts` `fetchPage` 移除「limit+1 探测」：

```ts
async fetchPage(filter: LogFilter = {}, page = 1, pageSize?: number): Promise<void> {
  this.loading = true
  try {
    const size = pageSize ?? this.pageSize
    const offset = (page - 1) * size
    const res = await listLogsApi({ ...filter, limit: size, offset })
    if (res.items.length === 0 && page > 1) {
      // 末页越界回退一页重取
      await this.fetchPage(filter, page - 1, size)
      return
    }
    this.items = res.items
    this.page = page
    this.pageSize = size
    this.total = res.total
  } finally {
    this.loading = false
  }
}
```

同步更新 state 中 `total` 的注释（删除「估算」描述，改为「后端真实 total」）。

- [ ] **Step 4: 验证前端构建**

Run: `cd frontend && npx vue-tsc --noEmit && npx vitest run`
Expected: 类型与单测通过（无 logs store 测试则至少 build 通过）

- [ ] **Step 5: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/api/index.ts frontend/src/stores/logs.ts
git commit -m "feat(logs): 前端消费运行日志真实 total 分页"
```

---

### Task 4: 设置页 log 保留天数配置（后端白名单 + 前端元数据）

**Files:**
- Modify: `backend/app/routers/settings.py:32-70`（_WHITELIST_EXACT / _EDITABLE_KEYS）
- Modify: `frontend/src/config/settingsMeta.ts`（task_run_retention_days 元数据）
- Modify: `frontend/src/views/SettingsView.test.ts`（若断言受限则适配）
- Test: `backend/tests/`（settings PATCH 白名单用例）

**Interfaces:**
- Consumes: 既有 settings GET/PATCH 机制、`saveKey` 渲染
- Produces: `task_run_retention_days` 可 PATCH 且落入业务参数 Tab

- [ ] **Step 1: 后端白名单**

`backend/app/routers/settings.py` `_WHITELIST_EXACT` 增加一行（放在 `capacity_alert_threshold` 附近）：

```python
    # D-A3：运行日志/容量快照保留天数（天）；cleanup.prune_history_job 读取，缺省 30
    "task_run_retention_days",
```

`_EDITABLE_KEYS` 改为：

```python
_EDITABLE_KEYS = frozenset(_WHITELIST_EXACT - {"emby_series_library_ids", "task_run_retention_days"})
```

（若不排除，该键会进入 configEntries 的排除分支——`editable_keys.includes(k) && !selectOptions` ——导致业务参数区不渲染。）

- [ ] **Step 2: settingsMeta 元数据**

`frontend/src/config/settingsMeta.ts` 增加：

```ts
  task_run_retention_days: {
    label: '运行日志保留天数',
    desc: 'task_run 运行日志与容量快照记录保留天数，超期由每日清理任务删除。缺省 30 天。',
    placeholder: '如 30',
    default: '30（默认）',
  },
```

（值为字符串输入即可：后端 `max(1, int(...))` 已防 0/非数字。设置页 GET 返回 system_config 值会落入 `businessEntries` → 文本输入渲染。）

- [ ] **Step 3: 后端测试**

新增/补充 settings PATCH 测试（可挂在既有 settings 测试文件或 `test_prune_history.py`）：
- PATCH `{"task_run_retention_days": "60"}` → 200 + `{"ok": true}`
- `prune_history_job` 使用配置值：`test_prune_history.py` 已有用例，补充「system_config 写入 60 后 cutoff 按 60 天」断言（如已有则该测试仍应通过）。

- [ ] **Step 4: 运行后端测试**

Run: `python -m pytest tests/test_prune_history.py tests/test_task_cleanup.py -q`（有 venv 时）
Expected: 通过

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/settings.py frontend/src/config/settingsMeta.ts backend/tests/
git commit -m "feat(settings): 运行日志保留天数支持动态配置"
```

---

### Task 5: 邀请码管理迁移到用户管理页

**Files:**
- Modify: `frontend/src/views/UsersView.vue`
- Modify: `frontend/src/views/SettingsView.vue:728-779`（移除 invites tab）
- Modify: `frontend/src/views/SettingsView.vue`（脚本：generate/removeInvite/copy*）
- Modify: `frontend/src/views/UsersView.test.ts`（邀请码区块）
- Test: `frontend/src/views/UsersView.test.ts`、`frontend/src/views/SettingsView.test.ts`

**Interfaces:**
- Consumes: `stores/settings.ts` invite actions（fetchInvites/createInvites/deleteInvite）；`InviteCode` 类型
- Produces: `UsersView` 内 invites 表格区块（生成/复制/复制注册链接/删除）

- [ ] **Step 1: UsersView 增加邀请码区块**

`frontend/src/views/UsersView.vue`：
- import 增加 `useSettingsStore`、`formatTime`（已有）、Element Plus 图标（Refresh 已有；复制不需图标）
- 模板在 `lc-panel` 内，用户列表 el-pagination 之后增加「邀请码」区块（用 `<el-divider>` 分隔 + 标题 + 生成按钮 + 表格），结构复制自 SettingsView 728-777：

```vue
      <el-divider content-position="left">邀请码管理</el-divider>
      <div class="lc-toolbar" style="margin-bottom: 12px">
        <div class="left">
          <el-input-number v-model="generateCount" :min="1" :max="20" size="small" style="width: 120px" />
          <el-button type="primary" size="small" :loading="settings.invites === undefined" @click="generate">生成邀请码</el-button>
        </div>
      </div>
      <el-empty v-if="settings.invites.length === 0" description="暂无邀请码" :image-size="60" />
      <el-table v-else :data="settings.invites" size="small">
        <el-table-column label="邀请码" min-width="160">
          <template #default="{ row }"><span style="font-family: monospace">{{ row.code }}</span></template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag v-if="row.used_by" type="info" size="small" effect="plain">已使用</el-tag>
            <el-tag v-else type="success" size="small" effect="plain">可用</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="使用者" width="140">
          <template #default="{ row }">{{ row.used_by_username ?? row.used_by ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="使用时间" width="160">
          <template #default="{ row }">{{ row.used_at ? formatTime(row.used_at) : '—' }}</template>
        </el-table-column>
        <el-table-column label="操作" min-width="200" align="right">
          <template #default="{ row }">
            <el-button size="small" link type="primary" @click="copyInviteCode(row.code)">复制</el-button>
            <el-button size="small" link type="primary" @click="copyRegisterLink(row.code)">复制注册链接</el-button>
            <el-button v-if="!row.used_by" size="small" link type="danger" @click="removeInvite(row.code)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
```

脚本增加（复制自 SettingsView 对应实现）：

```ts
const settings = useSettingsStore()
const generateCount = ref(1)

async function generate() {
  const codes = await settings.createInvites(generateCount.value)
  ElMessage.success(`已生成 ${codes.length} 个邀请码`)
}

async function removeInvite(code: string) {
  await ElMessageBox.confirm(`确定删除邀请码 ${code} 吗？`, '删除邀请码', {
    confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning',
  })
  await settings.deleteInvite(code)
  ElMessage.success('已删除')
}

async function copyText(text: string): Promise<boolean> {
  // Q6：非安全上下文回退隐藏 textarea + execCommand（复制 SettingsView 原实现）
}

async function copyInviteCode(code: string): Promise<void> { /* 复制 + ElMessage */ }
async function copyRegisterLink(code: string): Promise<void> {
  const link = `${location.origin}/register?code=${encodeURIComponent(code)}`
  if (await copyText(link)) ElMessage.success('已复制注册链接')
  else ElMessage.error('复制失败，请手动复制')
}
```

`onMounted` 改为：`Promise.all([store.fetchList(), settings.fetchInvites()])`。

- [ ] **Step 2: 移除 SettingsView invites tab**

`frontend/src/views/SettingsView.vue`：
- 删除模板 728-779「邀请码管理」el-tab-pane 整块
- 删除脚本中 `generateCount`、`generate`、`removeInvite`、`copyText`、`copyInviteCode`、`copyRegisterLink`（及不再使用的 import，如 `ElMessageBox` 若仅 invites 使用需检查其他用例）
- `onMounted`（397-401 行）：`await Promise.all([store.fetchSettings(), store.fetchInvites()])` → `await store.fetchSettings()`
- 检查 `store.invites` 引用是否全部移除

- [ ] **Step 3: 更新前端测试**

`SettingsView.test.ts`：
- mock 中 `invites: []`、`fetchInvites`、`createInvites`、`deleteInvite` 可保留（多余 mock 无害）或清理；重点确认移除 invites tab 后既有断言仍通过（`el-tab-pane` stub 已存在）

`UsersView.test.ts`：
- mock `../stores/settings`（`{ invites: [], fetchInvites: vi.fn(), createInvites: vi.fn().mockResolvedValue(['ABC']), deleteInvite: vi.fn() }`）
- 增加断言：渲染「邀请码管理」区块骨架（生成按钮存在），或邀请码列表渲染（mock 传入一条 invite 后表格展示 code）
- EP_STUBS 补充 `el-input-number`（若缺）、`el-divider`、`el-empty`（若缺）

- [ ] **Step 4: 验证前端**

Run: `cd frontend && npx vue-tsc --noEmit && npx vitest run src/views/UsersView.test.ts src/views/SettingsView.test.ts`
Expected: 通过

- [ ] **Step 5: Commit**

```bash
git add frontend/src/views/UsersView.vue frontend/src/views/SettingsView.vue frontend/src/views/UsersView.test.ts frontend/src/views/SettingsView.test.ts
git commit -m "feat(users): 邀请码管理从设置页迁移至用户管理页"
```

---

### Task 6: 影视库卡片「已有」文案去重

**Files:**
- Modify: `frontend/src/views/MediaListView.vue:165`
- Modify: `frontend/src/utils/format.test.ts`（missing=0 单前缀断言）

**Interfaces:**
- Consumes: `episodeText(m)`（内部调 `episodeSummaryText`，返回已含前缀的「已有 N 缺失 M」）
- Produces: 模板仅输出 `episodeText(m)`

- [ ] **Step 1: 修改模板**

`frontend/src/views/MediaListView.vue:165`：

```vue
<span v-else>{{ episodeText(m) }}</span>
```

（移除硬编码 `已有 ` 前缀；`episodeSummaryText` 本身返回「已有 N 缺失 M」。）

- [ ] **Step 2: 补充 format 断言**

`frontend/src/utils/format.test.ts` 的 `episodeSummaryText` describe 中补充：

```ts
it('missing=0 依然单前缀「已有 N 缺失 0」', () => {
  expect(episodeSummaryText({ available: 15, total: 15, missing: 0 }, 'tv', null)).toBe('已有 15 缺失 0')
})
```

- [ ] **Step 3: 验证**

Run: `cd frontend && npx vitest run src/utils/format.test.ts`
Expected: 通过

- [ ] **Step 4: Commit**

```bash
git add frontend/src/views/MediaListView.vue frontend/src/utils/format.test.ts
git commit -m "fix(media): 修复影视库卡片「已有」重复前缀"
```

---

### Task 7: 集成验证与守卫证据

**Files:**
- 全部本 change diff

**Interfaces:**
- Consumes: Task 1-6 产物
- Produces: verify 阶段证据（构建/测试输出、commit 列表、关键 diff）

- [ ] **Step 1: 前端全量构建与测试**

Run: `cd frontend && npm run build 2>&1 | tail -20 && npx vitest run 2>&1 | tail -30`
Expected: build 成功，全部测试通过

- [ ] **Step 2: 后端测试（有 venv 时）**

Run: `python -m pytest tests/test_fix_online.py tests/test_prune_history.py tests/test_task_cleanup.py tests/test_task_run_duration.py -q`
Expected: 通过（无环境则记录受限说明）

- [ ] **Step 3: 记录构建证据**

若项目无自动探测构建命令或需显式证据：

```bash
comet state record-check fix-logs-and-ui-polish build --command "cd frontend && npm run build" --exit-code 0
```

（仅在实际运行成功且 exit 0 时记录；后端 pytest 同理可记录。）

- [ ] **Step 4: 收尾审视**

- `git log --oneline -8` 覆盖 6 个功能 commit
- `git diff 38fa9d14a22eba3fb789b31e0a6ac552a8294f24 --stat` 确认改动文件都在计划内
- tasks.md 勾选全部任务（1.1-6.2）

- [ ] **Step 5: Commit 收尾（如有）**

若勾选 tasks.md 产生 diff：`git add docs/openspec/changes/fix-logs-and-ui-polish/tasks.md && git commit -m "chore(fix-logs-and-ui-polish): 勾选 build 阶段任务"`