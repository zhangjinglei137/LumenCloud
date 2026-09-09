---
change: settings-credentials-ui
design-doc: docs/superpowers/specs/2026-09-09-settings-credentials-ui-design.md
base-ref: 4c0b5c369efb96fb92e82bb4682a854f5c63c04c
---

# settings-credentials-ui 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 设置页服务凭据表单清除按钮与输入框同行、必填信息只表达「必填/可选」、移除 scan_interval_minutes 废弃配置项（含存量数据不透传）。

**Architecture:** 纯前端模板/CSS 结构调整 + 静态 meta 数据文案精简 + 后端白名单/废弃键集合微调。无数据库变更、无 API 契约变更。

**Tech Stack:** Vue 3 + Element Plus（前端）、FastAPI + SQLAlchemy（后端）、vitest（前端测试）、pytest（后端测试）。

**Spec:** `docs/openspec/changes/settings-credentials-ui/specs/settings-credentials-ui/spec.md`
**Design Doc:** `docs/superpowers/specs/2026-09-09-settings-credentials-ui-design.md`

## Global Constraints

- 产物语言：zh-CN（文案、commit message、测试名均为中文描述）
- per-media `scan_interval_minutes`（媒体详情页字段）**不得改动**：`backend/app/models/__init__.py:69`、`backend/app/routers/media.py:113,293`、`frontend/src/views/MediaDetailView.vue`、`frontend/src/types/index.ts:52,104`
- 存量 system_config 数据不清除，仅 GET 响应层不透传
- 长描述保留在 `desc`（tooltip），不得丢失
- 前端测试命令：`npm run test`（vitest run）；前端构建：`npm run build`（vue-tsc + vite）；后端测试：`pytest`（backend 目录）
- 现有 vitest 配置 `environment: 'node'`，SFC 组件测试用文件级 `// @vitest-environment jsdom` 覆盖，**不得**全局修改 environment

---

### Task 1: settingsMeta.ts 必填文案精简 + 删除废弃项（含纯函数测试）

**Files:**
- Modify: `frontend/src/config/settingsMeta.ts:17-256`（SETTING_FIELD_META 条目）
- Test: `frontend/src/config/settingsMeta.test.ts`（新建）

**Interfaces:**
- Consumes: `SettingFieldMeta`（`frontend/src/types/index.ts:253`，含 `default` 字段）
- Produces: 精简后的 `SETTING_FIELD_META`（凭据键 default 为「必填/可选」、无 `scan_interval_minutes` 键）；`getSettingMeta('scan_interval_minutes')` 走 fallback 返回 `{ label: 'scan_interval_minutes', desc: '' }`

- [x] **Step 1: 写失败测试** `frontend/src/config/settingsMeta.test.ts`

```ts
import { describe, expect, it } from 'vitest'
import { SETTING_FIELD_META, getSettingMeta } from './settingsMeta'

// 必填 6 键：default 统一为「必填」，不含长文案
const REQUIRED_KEYS = [
  'alist_base_url',
  'cloudsaver_base_url',
  'aria2_rpc_url',
  'nastools_base_url',
  'emby_base_url',
  'tmdb_api_key',
]

// 可选 5 键：default 统一为「可选」
const OPTIONAL_KEYS = [
  'tmdb_proxy',
  'tmdb_http_proxy',
  'pushplus_token',
  'quark_default_folder',
  'emby_series_library_ids',
]

describe('settingsMeta 必填文案精简', () => {
  it.each(REQUIRED_KEYS)('%s 的 default 为「必填」', (key) => {
    expect(SETTING_FIELD_META[key]?.default).toBe('必填')
  })

  it.each(REQUIRED_KEYS)('%s 的 default 不含长文案', (key) => {
    expect(SETTING_FIELD_META[key]?.default).not.toMatch(/否则|无法|不可用/)
  })

  it.each(OPTIONAL_KEYS)('%s 的 default 为「可选」', (key) => {
    expect(SETTING_FIELD_META[key]?.default).toBe('可选')
  })
})

describe('settingsMeta 废弃项移除', () => {
  it('SETTING_FIELD_META 不含 scan_interval_minutes', () => {
    expect('scan_interval_minutes' in SETTING_FIELD_META).toBe(false)
  })

  it('getSettingMeta(scan_interval_minutes) 回退为英文键名（非「已废弃」文案）', () => {
    const meta = getSettingMeta('scan_interval_minutes')
    expect(meta.label).toBe('scan_interval_minutes')
    expect(meta.desc).toBe('')
  })

  it('internal 键「自动生成，无需修改」保留', () => {
    expect(SETTING_FIELD_META.internal_aria2_webhook_secret?.default).toBe('自动生成，无需修改')
  })

  it('业务参数「默认 XX」标签保留', () => {
    expect(SETTING_FIELD_META.quark_quota_gb?.default).toMatch(/^默认/)
  })
})
```

- [x] **Step 2: 运行测试确认失败**

Run: `cd frontend && npx vitest run src/config/settingsMeta.test.ts`
Expected: FAIL（6 个必填键 default 仍为长文案、scan_interval_minutes 仍存在）

- [x] **Step 3: 实现——替换 default 文案**

`frontend/src/config/settingsMeta.ts` 中：

| 行 | 键 | 现值 | 新值 |
|----|----|------|------|
| 23 | alist_base_url | `必填（否则转存与直链不可用）` | `必填` |
| 38 | cloudsaver_base_url | `必填（否则搜索与转存不可用）` | `必填` |
| 59 | aria2_rpc_url | `必填（否则无法下载）` | `必填` |
| 74 | nastools_base_url | `必填（否则不会自动入库）` | `必填` |
| 95 | emby_base_url | `必填（否则无法判定入库状态）` | `必填` |
| 110 | tmdb_api_key | `必填（否则无法搜索影视信息）` | `必填` |
| 117 | tmdb_proxy | `可选，留空 = 官方地址` | `可选` |
| 127 | tmdb_http_proxy | `可选，留空 = 直连` | `可选` |
| 151 | pushplus_token | `可选，留空 = 只用站内通知` | `可选` |
| 160 | quark_default_folder | `可选，留空 = 不指定` | `可选` |
| 168 | emby_series_library_ids | `可选，不选 = 不过滤` | `可选` |

- [x] **Step 4: 实现——删除废弃条目**

删除 `frontend/src/config/settingsMeta.ts:197-201` 的 `scan_interval_minutes` 整个条目（含 label/desc/default 三字段）。`CRED_GROUP_ORDER`/`CRED_GROUP_LABELS` 中的 `'scan'` 分组保留（scan_baseline_required 仍存在）。

- [x] **Step 5: 运行测试确认通过**

Run: `cd frontend && npx vitest run src/config/settingsMeta.test.ts`
Expected: PASS（10 个文案断言 + 4 个废弃项断言全绿）

- [x] **Step 6: Commit**

```bash
git add frontend/src/config/settingsMeta.ts frontend/src/config/settingsMeta.test.ts
git commit -m "feat(settings): 精简凭据必填文案并移除废弃配置项定义"
```

---

### Task 2: SettingsView.vue 清除按钮移入输入框行

**Files:**
- Modify: `frontend/src/views/SettingsView.vue:554-608`（模板 .cred-field 结构）
- Modify: `frontend/src/views/SettingsView.vue:947-1023`（CSS .cred-field/.cred-input/.cred-actions）

**Interfaces:**
- Consumes: `clearCred(key)`、`runQuarkVerify()`、`savingCredKeys`、`quarkVerifyLoading`（均已有，脚本零改动）
- Produces: `.cred-input-row` 操作行结构；`.cred-actions` 类不再存在于模板与 CSS

- [x] **Step 1: 结构调整——模板**

`frontend/src/views/SettingsView.vue:577-608`，将：

```html
<el-input
  v-model="credValues[key]"
  type="text"
  autocomplete="off"
  :placeholder="getSettingMeta(key).placeholder ?? ''"
  class="cred-input"
  @keyup.enter="saveAll"
/>
<div class="cred-actions">
  <el-button size="small" link type="danger" :disabled="savingCredKeys.has(key)" @click="clearCred(key)">
    清除
  </el-button>
  <el-tooltip v-if="key === 'quark_default_folder'" content="验证的是已保存的配置；刚修改过请先点「保存全部」" placement="top">
    <el-button size="small" :loading="quarkVerifyLoading" @click="runQuarkVerify">
      验证 folderId
    </el-button>
  </el-tooltip>
</div>
```

替换为（操作按钮移入 `el-input` 同一 `.cred-input-row` 行）：

```html
<div class="cred-input-row">
  <el-input
    v-model="credValues[key]"
    type="text"
    autocomplete="off"
    :placeholder="getSettingMeta(key).placeholder ?? ''"
    class="cred-input"
    @keyup.enter="saveAll"
  />
  <el-button size="small" link type="danger" :disabled="savingCredKeys.has(key)" @click="clearCred(key)">
    清除
  </el-button>
  <el-tooltip v-if="key === 'quark_default_folder'" content="验证的是已保存的配置；刚修改过请先点「保存全部」" placement="top">
    <el-button size="small" :loading="quarkVerifyLoading" @click="runQuarkVerify">
      验证 folderId
    </el-button>
  </el-tooltip>
</div>
```

- [x] **Step 2: 结构调整——CSS**

`frontend/src/views/SettingsView.vue:1013-1023`，将：

```css
.cred-input {
  max-width: none;
  flex: none;
  margin-top: 0;
}

.cred-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
```

替换为：

```css
.cred-input-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.cred-input {
  max-width: none;
  flex: 1;
  min-width: 0;
}
```

（`.cred-actions` 类整体删除；窄屏时 `flex-wrap` 允许按钮换行不挤压）

- [x] **Step 3: 确认无残留引用**

Run: `cd frontend && grep -rn "cred-actions" src/`
Expected: 无输出（模板与 CSS 均已移除）

- [x] **Step 4: 构建验证**

Run: `cd frontend && npm run build`
Expected: vue-tsc 类型检查通过、vite build 成功（产物输出 backend/static）

- [x] **Step 5: Commit**

```bash
git add frontend/src/views/SettingsView.vue
git commit -m "feat(settings): 清除按钮移入输入框同行布局"
```

---

### Task 3: 后端移除白名单 + 加入 _RETIRED_EXACT（含测试）

**Files:**
- Modify: `backend/app/routers/settings.py:36`（`_WHITELIST_EXACT` 移除键）
- Modify: `backend/app/routers/settings.py:73-75`（`_RETIRED_EXACT` 加入键）
- Test: `backend/tests/test_settings_retired.py`（扩展）

**Interfaces:**
- Consumes: 既有 `_RETIRED_EXACT` 透传排除逻辑（settings.py:140-141）
- Produces: `editable_keys` 响应不含 `scan_interval_minutes`；GET `system_config`/`config` 不透传存量该键；PATCH 该键返回 422

- [x] **Step 1: 写失败测试——扩展 `backend/tests/test_settings_retired.py`**

在文件末尾追加：

```python
async def test_scan_interval_minutes_retired_and_not_editable():
    """scan_interval_minutes 从可编辑白名单移除，且存量数据 GET 不透传（remove-deprecated-settings）。"""
    from datetime import datetime, timezone

    from fastapi.testclient import TestClient

    from app.database import async_session
    from app.main import app
    from app.models import SystemConfig

    # 注入存量数据
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with async_session() as session:
        await session.merge(SystemConfig(key="scan_interval_minutes", value="60", updated_at=now))
        await session.commit()

    client = TestClient(app)
    login = client.post("/api/auth/login", json={"username": "admin", "password": _ADMIN_PASSWORD})
    assert login.status_code == 200
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    res = client.get("/api/settings", headers=headers)
    assert res.status_code == 200
    body = res.json()
    # editable_keys 不含该键
    assert "scan_interval_minutes" not in body["editable_keys"]
    # GET 不透传存量值（system_config 与 config 均不含）
    assert "scan_interval_minutes" not in body["system_config"]
    assert "scan_interval_minutes" not in body["config"]

    # PATCH 该键 → 422
    patch = client.patch("/api/settings", headers=headers, json={"scan_interval_minutes": "60"})
    assert patch.status_code == 422
```

> 注意：先阅读 `test_settings_retired.py` 现有 `_ADMIN_PASSWORD` / 登录辅助变量名，若命名不同则沿用现有约定（本文件已含 `_recreate_admin` 与登录流程）。

- [x] **Step 2: 运行测试确认失败**

Run: `cd backend && pytest tests/test_settings_retired.py -x -q`
Expected: FAIL（`editable_keys` 仍含该键、GET 仍透传）

- [x] **Step 3: 实现——settings.py 两处修改**

1. `backend/app/routers/settings.py:36`：从 `_WHITELIST_EXACT` 删除 `"scan_interval_minutes",` 行
2. `backend/app/routers/settings.py:75`：将

```python
_RETIRED_EXACT = frozenset({"download_queue_max_concurrent"})
```

改为：

```python
_RETIRED_EXACT = frozenset({"download_queue_max_concurrent", "scan_interval_minutes"})
```

（`_EDITABLE_KEYS = _WHITELIST_EXACT - {emby_series_library_ids}` 自动不再包含该键，无需额外改动）

- [x] **Step 4: 运行测试确认通过**

Run: `cd backend && pytest tests/test_settings_retired.py -q`
Expected: PASS（既有废弃键用例 + 新用例全绿）

- [x] **Step 5: Commit**

```bash
git add backend/app/routers/settings.py backend/tests/test_settings_retired.py
git commit -m "feat(settings): 移除扫描间隔废弃配置项白名单并加入透传排除"
```

---

### Task 4: SettingsView 组件测试（首建 SFC 测试基建）

**Files:**
- Create: `frontend/src/views/SettingsView.test.ts`
- Modify: `frontend/package.json`（新增 devDependencies）
- Test: `frontend/vite.config.ts` 不改（用文件级 `@vitest-environment jsdom`）

**Interfaces:**
- Consumes: `SettingsView.vue` 渲染结果；mock 的 `useSettingsStore`/`useAuthStore`
- Produces: 组件结构回归断言（必填标签、清除按钮同行、废弃项不渲染）

- [x] **Step 1: 安装组件测试依赖**

Run: `cd frontend && npm install -D @vue/test-utils jsdom`
Expected: package.json 新增 `@vue/test-utils`、`jsdom` devDependencies，node_modules 安装成功

- [x] **Step 2: 写测试 `frontend/src/views/SettingsView.test.ts`**

```ts
// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import SettingsView from './SettingsView.vue'

// mock stores：SettingsView 依赖 settings/auth store
vi.mock('../stores/settings', () => ({
  useSettingsStore: () => ({
    settings: {
      editable_keys: ['alist_base_url', 'alist_token', 'quark_default_folder'],
      // 真实 GET 响应：scan_interval_minutes 已被后端 _RETIRED_EXACT 过滤，故 mock 不含该键
      system_config: {
        alist_base_url: 'http://192.168.1.10:5244',
        alist_token: '',
        quark_default_folder: '',
      },
      services: { alist: true, emby: false },
    },
    loading: false,
    fetchSettings: vi.fn().mockResolvedValue(undefined),
    fetchInvites: vi.fn().mockResolvedValue(undefined),
    patchConfig: vi.fn().mockResolvedValue(undefined),
    createInvites: vi.fn().mockResolvedValue([]),
    deleteInvite: vi.fn().mockResolvedValue(undefined),
  }),
}))

vi.mock('../stores/auth', () => ({
  useAuthStore: () => ({
    changePassword: vi.fn().mockResolvedValue(undefined),
  }),
}))

// Element Plus 组件 stub：jsdom 下无需真实渲染
const EP_STUBS = {
  'el-tabs': { template: '<div><slot /></div>' },
  'el-tab-pane': { template: '<div><slot /></div>' },
  'el-button': { template: '<button><slot /></button>' },
  'el-input': { template: '<input :value="modelValue" />', props: ['modelValue'] },
  'el-tooltip': { template: '<span><slot /></span>' },
  'el-tag': { template: '<span><slot /></span>' },
  'el-divider': { template: '<div><slot /></div>' },
  'el-icon': { template: '<span><slot /></span>' },
  'el-alert': { template: '<div><slot /></div>' },
  'el-empty': { template: '<div><slot /></div>' },
  'el-link': { template: '<a><slot /></a>' },
  'el-select': { template: '<select><slot /></select>' },
  'el-option': { template: '<option />' },
  'el-input-number': { template: '<input />' },
  'el-form': { template: '<form><slot /></form>' },
  'el-form-item': { template: '<div><slot /></div>' },
  'el-message-box': true,
}

function mountView() {
  return mount(SettingsView, {
    global: { stubs: EP_STUBS },
    attachTo: document.body,
  })
}

describe('SettingsView 服务凭据表单', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })

  it('必填标签渲染为「必填」且不含长文案', () => {
    const wrapper = mountView()
    const html = wrapper.html()
    expect(html).toContain('必填')
    expect(html).not.toContain('否则转存与直链不可用')
    expect(html).not.toContain('必填（')
  })

  it('清除按钮与输入框在同一 .cred-input-row 行内', () => {
    const wrapper = mountView()
    const row = wrapper.find('.cred-input-row')
    expect(row.exists()).toBe(true)
    // 同一行内既有输入框又有清除按钮
    expect(row.find('input').exists()).toBe(true)
    expect(row.text()).toContain('清除')
    // 独立操作行已移除
    expect(wrapper.find('.cred-actions').exists()).toBe(false)
  })

  it('凭据表单不渲染 scan_interval_minutes（editable_keys 不含 + 后端不透传）', () => {
    const wrapper = mountView()
    const html = wrapper.html()
    // 该键不在 editable_keys → 凭据表单无输入行；真实 GET 已被 _RETIRED_EXACT 过滤 → 业务参数区无英文键名
    expect(html).not.toContain('扫描间隔')
    expect(html).not.toContain('scan_interval_minutes')
  })
})
```

> 注意：若 `getSettingMeta` 之外的 store 字段（如 `settings.config`）被 onMounted 使用导致 mount 报错，按实际报错补齐 mock 字段（`system_config` 与 `config` 双键，见 SettingsView.vue:47-49）。

- [x] **Step 3: 运行测试确认通过**

Run: `cd frontend && npx vitest run src/views/SettingsView.test.ts`
Expected: PASS（3 个用例：必填标签、同行结构、废弃项不渲染）。若失败为 mock 缺失，补齐 store stub 后重跑。

- [x] **Step 4: 全量前端测试确认无回归**

Run: `cd frontend && npm run test`
Expected: PASS（现有 utils 测试 + 新增 settingsMeta/SettingsView 测试全绿）

- [x] **Step 5: Commit**

```bash
git add frontend/package.json frontend/package-lock.json frontend/src/views/SettingsView.test.ts
git commit -m "test(settings): 新增设置页组件测试与必填文案测试"
```

---

### Task 5: 文案残留检查 + 全量构建验证

**Files:**
- 无代码改动（检查 + 验证）

**Interfaces:**
- Consumes: Task 1-4 的产物
- Produces: 残留检查结论、构建/测试证据

- [x] **Step 1: grep「已废弃，不再生效」无残留**

Run: `cd frontend && grep -rn "已废弃，不再生效" src/`
Expected: 无输出

- [x] **Step 2: grep「扫描间隔（分钟）（已废弃）」无残留**

Run: `cd frontend && grep -rn "扫描间隔" src/`
Expected: 无输出（settingsMeta 条目已删除；MediaDetailView 不含此文案）

- [x] **Step 3: 确认 per-media 字段保留**

Run: `grep -rn "scan_interval_minutes" backend/app/models/__init__.py backend/app/routers/media.py frontend/src/views/MediaDetailView.vue frontend/src/types/index.ts`
Expected: 全部命中（模型列、media.py:113/293、MediaDetailView:38/49/342、types:52/104 保留，未被动过）

- [x] **Step 4: 全量验证**

Run: `cd frontend && npm run build`
Expected: vue-tsc + vite build 通过

Run: `cd backend && pytest -q`
Expected: 全部测试通过（含扩展后的 test_settings_retired.py）

- [x] **Step 5: 汇总验证证据**

记录到 `.comet.yaml`（若上一步 pytest 为全量）：

```bash
comet state record-check settings-credentials-ui build --command "cd frontend && npm run build && cd ../backend && pytest -q" --exit-code 0
```

> 若 Build 阶段守卫已自动记录构建，此处跳过；以实际执行结果为准，未执行不得声称通过。

---

### Task 6: 手动验证（设置页交互）

**Files:**
- 无代码改动

**Interfaces:**
- Consumes: Task 1-5 产物
- Produces: 手动验证记录（存证于验证报告）

- [x] **Step 1: 启动前端 + 后端（用户侧操作）**

按项目方式启动前后端服务，打开设置页「服务凭据」Tab。

- [x] **Step 2: 逐项核对**

1. 清除按钮与输入框同行（alist_base_url 等字段），窄屏窗口按钮可换行不挤压
2. 必填字段旁只显示「必填」标签，无「否则…」长文案；可选字段显示「可选」
3. 设置页无「扫描间隔（分钟）（已废弃）」项；业务参数 Tab 无 `scan_interval_minutes` 英文键名
4. 点击「清除」二次确认后清除生效、其余字段不变；填写字段点「保存全部」正常
5. quark_default_folder 的「验证 folderId」按钮正常显示在同行

- [x] **Step 3: 记录结果**

手动验证结果记入 Verify 阶段报告（本任务仅执行与记录，不单独提交）。

---

## Self-Review

**Spec 覆盖对照：**
- 「清除按钮位于输入框后」→ Task 2（结构）+ Task 4（测试）
- 「必填信息只展示是否必填」→ Task 1（文案）+ Task 4（渲染断言）
- 「移除废弃配置项」→ Task 1（meta 删除）+ Task 3（白名单/透传）+ Task 5（残留检查）
- 「存量数据不回退展示」（Spec Patch 新增场景）→ Task 3（_RETIRED_EXACT）+ Task 4（渲染断言）+ Task 5（grep）

**Placeholder 扫描：** 全部步骤含具体代码/命令，无 TBD/TODO。

**类型一致性：** `.cred-input-row`（Task 2 生产，Task 4 断言）、`getSettingMeta('scan_interval_minutes')` fallback（Task 1 测试，Task 4 渲染路径）在任务间一致。

**任务边界检查：** 6 个任务均独立可测、可独立审查；Task 1/3/4 携带 TDD 测试循环。
