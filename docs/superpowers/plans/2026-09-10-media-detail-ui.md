---
change: media-detail-ui
design-doc: docs/superpowers/specs/2026-09-10-media-detail-ui-design.md
base-ref: c94c3a6252adf9711d89e14c815edd947f9187bd
---

# media-detail-ui 实施计划（影视详情页信息层级与集数浏览）

> **给智能体执行者：** 必需子技能：`subagent-driven-development`（推荐）或 `executing-plans` 逐任务实施本计划。步骤使用复选框（`- [ ]`）跟踪。

**目标：** 重设计影视详情页：移除转存队列、header 右侧紧凑设置控件组、集数状态最大显示 + 每 100 集分组导航 + TMDB 集名称展示。

**架构：** 纯前端改动。`format.ts` 新增两个纯函数（`buildEpisodeGroups` 分组生成、`episodeDisplayName` 集名称/集号回退），`MediaDetailView.vue` 重构布局（header 紧凑组 + 单列全宽 + 分组过滤 + 名称列回退）。后端契约不改。

**技术栈：** Vue 3（`<script setup>`）、Element Plus、Pinia、TypeScript、Vitest。

**Spec（事实源）：** `docs/openspec/changes/media-detail-ui/specs/media-detail-ui/spec.md`；设计细化见 `docs/superpowers/specs/2026-09-10-media-detail-ui-design.md`。

## Global Constraints

- 产物语言：中文（UI 文案、commit message 均用中文）
- 不改后端：`backend/` 一律不碰；`episode_state`/`tmdb_episodes`/`transfer_queue` 契约照常返回，前端忽略
- 前端构建验证：`cd frontend && npm run test`（vitest）与 `npm run build`（vue-tsc + vite）必须通过
- commit type 用 Conventional Commits，描述用中文（如 `feat(media-detail)：...`）
- 每任务完成后 git commit；不合并其他 change 的改动到本 change 提交
- guest 语义保持：非 admin 不显示修改控件（沿用现有「仅管理员可修改」处理）

---

## Task 1: format.ts 新增分组生成与集名称回退纯函数（TDD）

**Files:**
- Modify: `frontend/src/utils/format.ts`（在文件末尾追加导出）
- Test: `frontend/src/utils/format.test.ts`

**Interfaces:**
- Consumes: 无（独立纯函数）
- Produces:
  - `interface EpisodeGroup { label: string; start: number; end: number }`
  - `buildEpisodeGroups(total: number, pageSize?: number): EpisodeGroup[]`
  - `episodeDisplayName(row: Record<string, unknown>): string`（`name` 优先；回退 `season`+`episode_number` → `SxxExx`；再回退 `—`）

- [x] **Step 1: 写失败测试**

在 `frontend/src/utils/format.test.ts` 末尾追加（注意该文件顶部已有一段 `import {}`，新测试在自己的 `describe` 内引用新函数；为避免重复 import 命名冲突，添加到文件尾部并展开 import）：

```ts
import { buildEpisodeGroups, episodeDisplayName } from './format'

describe('集数分组生成（media-detail-ui）', () => {
  it('350 集 → 1-100/101-200/201-300/301-350', () => {
    expect(buildEpisodeGroups(350)).toEqual([
      { label: '1-100', start: 1, end: 100 },
      { label: '101-200', start: 101, end: 200 },
      { label: '201-300', start: 201, end: 300 },
      { label: '301-350', start: 301, end: 350 },
    ])
  })
  it('恰好 100 集 → 单组 1-100', () => {
    expect(buildEpisodeGroups(100)).toEqual([{ label: '1-100', start: 1, end: 100 }])
  })
  it('99 集 → 末组收缩到 1-99', () => {
    expect(buildEpisodeGroups(99)).toEqual([{ label: '1-99', start: 1, end: 99 }])
  })
  it('101 集 → 两组 [1-100, 101-101]', () => {
    expect(buildEpisodeGroups(101)).toEqual([
      { label: '1-100', start: 1, end: 100 },
      { label: '101-101', start: 101, end: 101 },
    ])
  })
  it('0 / 负数 → 空数组', () => {
    expect(buildEpisodeGroups(0)).toEqual([])
    expect(buildEpisodeGroups(-5)).toEqual([])
  })
})

describe('集数名称回退（media-detail-ui）', () => {
  it('有 name 返回 name', () => {
    expect(episodeDisplayName({ name: '第一集' })).toBe('第一集')
  })
  it('无 name 但有 season/episode_number → SxxExx', () => {
    expect(episodeDisplayName({ season: 1, episode_number: 4 })).toBe('S01E04')
  })
  it('无 name 且无集号 → —', () => {
    expect(episodeDisplayName({})).toBe('—')
  })
})
```

- [x] **Step 2: 运行测试验证失败**

Run: `cd frontend && npx vitest run src/utils/format.test.ts`
Expected: FAIL —— `buildEpisodeGroups is not a function` / `episodeDisplayName is not a function`。

- [x] **Step 3: 实现两个纯函数**

在 `frontend/src/utils/format.ts` 末尾追加：

```ts
/** 集数分组（media-detail-ui）：每 pageSize 集一组；末组收缩到 total；total<=0 → [] */
export interface EpisodeGroup {
  label: string
  start: number
  end: number
}

export function buildEpisodeGroups(total: number, pageSize = 100): EpisodeGroup[] {
  if (!Number.isFinite(total) || total <= 0 || pageSize <= 0) return []
  const groups: EpisodeGroup[] = []
  const count = Math.ceil(total / pageSize)
  for (let i = 0; i < count; i += 1) {
    const start = i * pageSize + 1
    const end = Math.min((i + 1) * pageSize, total)
    groups.push({ label: `${start}-${end}`, start, end })
  }
  return groups
}

/** 集数名称展示（media-detail-ui）：name → SxxExx → —，永不裸空 */
export function episodeDisplayName(row: Record<string, unknown>): string {
  const name = row.name
  if (typeof name === 'string' && name !== '') return name
  const season = row.season ?? row.season_number
  const episodeNumber = row.episode_number
  if (season !== undefined && season !== null && episodeNumber !== undefined && episodeNumber !== null) {
    return `S${String(season).padStart(2, '0')}E${String(episodeNumber).padStart(2, '0')}`
  }
  return '—'
}
```

- [x] **Step 4: 运行测试验证通过**

Run: `cd frontend && npx vitest run src/utils/format.test.ts`
Expected: PASS（新增 describe 全绿，旧用例不受影响）。

- [x] **Step 5: 提交**

```bash
git add frontend/src/utils/format.ts frontend/src/utils/format.test.ts
git commit -m "test(media-detail)：新增集数分组与名称回退纯函数"
```

---

## Task 2: MediaDetailView script —— 分组状态、过滤与 import 整理

**Files:**
- Modify: `frontend/src/views/MediaDetailView.vue`（`<script setup>` 段）

**Interfaces:**
- Consumes: `buildEpisodeGroups`、`episodeDisplayName`（Task 1 产出）；现有 `episodeRows` computed（保留）
- Produces:
  - `groups = computed(() => EpisodeGroup[])`（空数组则不渲染分组导航）
  - `activeGroup = ref<EpisodeGroup | null>(null)`（null = 全部）
  - `filteredRows = computed(() => EpisodeRow[])`（模板表格改用；无集号行不受过滤）
  - `episodeTotal = computed(() => number)`（episode_state 最大 episode_number，tmdbEpisodes.length 兜底）

- [x] **Step 1: 写失败测试（组件渲染级验证）**

沿用仓库先例 `SettingsView.test.ts` 的模式（`@vitest-environment jsdom` + `vi.mock` store/route + Element Plus stub），不引入 `@pinia/testing`。分组/回退逻辑由 Task 1 纯函数单测覆盖；**本 Task 组件测试只做挂载冒烟**（防 script 改动引入运行错误与类型破坏）——模板元素的详细断言（`.ep-group-nav`、`.header-settings`、转存队列移除）归入 Task 3 Step 1。

新建 `frontend/src/views/MediaDetailView.test.ts`：

```ts
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import MediaDetailView from './MediaDetailView.vue'

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: '1' } }),
  useRouter: () => ({ push: vi.fn() }),
}))

const detail = {
  id: 1,
  title: '测试剧',
  media_type: 'tv',
  status: 'tracking',
  episode_state: [
    { id: 1, episode: 'S01E001', season: 1, episode_number: 1, name: '第一集' },
    { id: 2, episode: 'S01E002', season: 1, episode_number: 2 },
  ],
  tmdb_episodes: [
    { season: 1, episode: 1, name: '第一集' },
    { season: 1, episode: 2 },
  ],
}

vi.mock('../stores/media', () => ({
  useMediaStore: () => ({
    detail,
    loading: false,
    fetchDetail: vi.fn().mockResolvedValue(undefined),
    patch: vi.fn().mockResolvedValue(undefined),
    scan: vi.fn().mockResolvedValue(null),
    remove: vi.fn().mockResolvedValue(undefined),
  }),
}))

vi.mock('../stores/auth', () => ({
  useAuthStore: () => ({ isAdmin: true }),
}))

// Element Plus 组件 stub（同 SettingsView.test.ts 风格）
const EP_STUBS = {
  'el-button': { template: '<button><slot /></button>' },
  'el-tooltip': { template: '<span><slot /></span>' },
  'el-tag': { template: '<span><slot /></span>' },
  'el-icon': { template: '<span><slot /></span>' },
  'el-empty': { template: '<div><slot /></div>' },
  'el-input-number': { template: '<input />' },
  'el-table': { template: '<div class="el-table"><slot /></div>' },
  'el-table-column': { template: '<div><slot /></div>' },
}

function mountView() {
  return mount(MediaDetailView, {
    global: { stubs: EP_STUBS },
    attachTo: document.body,
  })
}

describe('MediaDetailView script 冒烟（media-detail-ui）', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })

  it('剧集详情可挂载，集数名称数据正常流入模板', async () => {
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.exists()).toBe(true)
    const html = wrapper.html()
    expect(html).toContain('第一集') // 集数名称数据可见（script 未破坏数据流）
  })
})
```

> 注：若挂载受 Element Plus 未注册指令（`v-loading`）等副作用影响超预期，在 stubs/global config 中补充（如 `global: { directives: { loading: () => {} } }`）；仍无法稳定挂载时允许降级为「构建通过」并把冒烟验证并入 Task 3 手动清单，commit message 注明。

- [x] **Step 2: 运行测试验证失败**

Run: `cd frontend && npx vitest run src/views/MediaDetailView.test.ts`
Expected: 测试文件或用例失败/挂载报错（尚未改动视图，组测为占位）。

- [x] **Step 3: script 段实现**

在 `frontend/src/views/MediaDetailView.vue` 的 `<script setup>` 中：

```ts
// 现有 script setup 中已定义 episodes / episodeRows / tmdbEpisodes computed；
// 新增以下逻辑并同步 import（见下方 import 变更）

/** 组总数（分组依据）：episode_state 最大集号；无 episode_state 时用 TMDB 全长 */
const episodeTotal = computed<number>(() => {
  const nums = episodes.value.map((e) => Number(e.episode_number)).filter((n) => Number.isFinite(n))
  if (nums.length > 0) return Math.max(...nums)
  return tmdbEpisodes.value.length
})

/** 每 100 集分组 tag（电影/无数据返回空数组 → 不渲染导航） */
const groups = computed(() => buildEpisodeGroups(episodeTotal.value))

/** 当前选中分组的 label；null = 全部 */
const activeGroup = ref<string | null>(null)

/** 分组过滤后的表格行；无集号行始终显示 */
const filteredRows = computed(() => {
  const active = activeGroup.value
  const g = groups.value.find((grp) => grp.label === active)
  if (!g) return episodeRows.value
  return episodeRows.value.filter((row) => {
    const n = Number(row.episode_number)
    if (!Number.isFinite(n)) return true
    return n >= g.start && n <= g.end
  })
})

/** 名称列展示（原 episodeName 函数替换为 format 层纯函数） */
function episodeName(ep: Record<string, unknown>): string {
  return episodeDisplayName(ep)
}
```

同步 import 变更（**仅新增**，不移除任何既有 import——模板区块仍引用 `formatBytes/queueStatusLabel/queueStatusType`，移除工作归 Task 3）：
- 在 `../utils/format` 值导入块内**新增** `buildEpisodeGroups`、`episodeDisplayName`（`EpisodeGroup` 类型由 `groups` computed 的返回类型推导，无需显式 import）

注意：`ref`/`computed` 已在顶部 import（保留）；`buildEpisodeGroups`/`episodeDisplayName` 是新增 import；`episodeTotal` 依赖 `tmdbEpisodes` computed（已在 script 定义）。不要显式 import 任何类型（`EpisodeGroup` 由 `groups` computed 推导）。

- [x] **Step 4: 运行测试验证通过**

Run: `cd frontend && npx vitest run src/views/MediaDetailView.test.ts && npm run build`
Expected: 冒烟测试通过（或按注记降级后仅 build 通过）；`vue-tsc` 无类型错误。

- [x] **Step 5: 提交**

```bash
git add frontend/src/views/MediaDetailView.vue frontend/src/views/MediaDetailView.test.ts
git commit -m "feat(media-detail)：新增集数分组状态与过滤逻辑"
```

---

## Task 3: MediaDetailView 模板重构 —— 紧凑 header、单列全宽、分组导航、移除转存队列

**Files:**
- Modify: `frontend/src/views/MediaDetailView.vue`（`<template>` 与 `<style scoped>` 段）

**Interfaces:**
- Consumes: `groups` / `activeGroup` / `filteredRows` / `episodeName` / `form` / `saveSettings` / `auth.isAdmin`（Task 2 与现有 script）
- Produces: 修改后的模板布局（无新导出）

- [x] **Step 1: 写失败测试（模板断言）**

更新 `frontend/src/views/MediaDetailView.test.ts`（Task 2 已建冒烟测试文件），追加模板元素断言（stub 需新增 `el-radio-group`/`el-radio-button` 与 `v-loading` 指令处理）：

```ts
// 在 Task 2 已有测试文件基础上追加 stub 与用例
const EP_STUBS = {
  // ...Task 2 已有 stubs...，
  'el-radio-group': { template: '<div class="ep-group-nav"><slot /></div>' },
  'el-radio-button': { template: '<label><slot /></label>' },
}

describe('MediaDetailView 布局（media-detail-ui）', () => {
  it('剧集 + 有分组数据 → header 紧凑组/分组导航渲染，转存队列区块不存在', async () => {
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.find('.ep-group-nav').exists()).toBe(true) // 有分组数据
    expect(wrapper.find('.header-settings').exists()).toBe(true) // admin 紧凑组
    expect(wrapper.find('.transfer-queue-block').exists()).toBe(false) // 转存队列已移除
    const html = wrapper.html()
    expect(html).toContain('第一集') // 集数名称展示
    expect(html).toContain('S01E02') // 无名称行回退集号
  })
})
```

> 挂载 media-detail-view 需要正确处理 `v-loading` 自定义指令；若 Element Plus 组件树引入过量副作用，允许把 `EP_STUBS` 换成全局 stub（`global: { stubs: [...EP_STUBS 键] }`）并局部 stub `el-table` 等，只断言模板结构与 `.transfer-queue-block` 缺席。

- [x] **Step 2: 实现模板**

`<template>` 结构调整为：

1. **header 紧凑组**：在 `.info` 块之后、`.detail-header` 之内追加：

```vue
<!-- 大小与巡检设置（仅管理员，窄屏换行） -->
<div v-if="auth.isAdmin" class="header-settings">
  <div class="hs-item" v-if="detail.media_type !== 'movie'">
    <label class="hs-label">单集大小上限 (GB)</label>
    <el-input-number v-model="form.max_episode_size_gb" :min="0" :precision="1" :step="0.5" size="small" />
  </div>
  <div class="hs-item" v-if="detail.media_type === 'movie'">
    <label class="hs-label">电影大小上限 (GB)</label>
    <el-input-number v-model="form.max_movie_size_gb" :min="0" :precision="1" :step="1" size="small" />
  </div>
  <div class="hs-item">
    <label class="hs-label">巡检间隔 (分钟)</label>
    <el-input-number v-model="form.scan_interval_minutes" :min="5" :step="5" size="small" />
  </div>
  <el-button type="primary" :loading="saving" size="small" @click="saveSettings">保存</el-button>
</div>
```

2. **移除转存队列**：删除原右列中「转存队列」lc-panel（`v-if="!detail.transfer_queue ..."` 所在整块）。

3. **单列全宽**：删除 `el-row :gutter="16"` 与两个 `el-col` 包裹，改为直接展开主体；`media_type === 'movie'` 保留「影视状态」面板；tv 分支的集数状态 lc-panel 全宽。

4. **集数状态区**（tv 分支）：

```vue
<div v-if="detail.media_type !== 'movie'" class="lc-panel">
  <h3 class="lc-panel-title">集数状态（{{ episodes.length }}）</h3>
  <p class="ep-legend lc-muted"> ...（图例保留原样）... </p>

  <!-- 分组导航（有分组数据且非电影才渲染） -->
  <div v-if="groups.length > 0" class="ep-group-nav">
    <el-radio-group v-model="activeGroup" size="small">
      <el-radio-button :value="null">全部</el-radio-button>
      <el-radio-button v-for="g in groups" :key="g.label" :value="g.label">{{ g.label }}</el-radio-button>
    </el-radio-group>
  </div>

  <el-empty v-if="episodes.length === 0" ... />   <!-- 保留 -->
  <el-table v-else :data="filteredRows" size="small" max-height="600">  <!-- max-height 480 → 600 -->
    <el-table-column label="集" width="110">
      <template #default="{ row }">{{ episodeLabel(row as Record<string, unknown>) }}</template>
    </el-table-column>
    <el-table-column label="名称" min-width="140">
      <template #default="{ row }">
        <span :title="episodeName(row)">{{ episodeName(row) }}</span>
      </template>
    </el-table-column>
    ...（状态/大小/更新时间列保留原样，数据源 row 不变）...
  </el-table>

  <!-- TMDB 全集网格（保留完整展示，移到表格下方） -->
  <div v-if="tmdbEpisodes.length > 0" class="tmdb-episodes"> ...（原样保留）... </div>
</div>
```

> `el-radio-button :value="null"` 需要 Element Plus 版本支持 null 值绑定（2.9 支持）；若类型报错改用 `el-tag` 点击方案（activeGroup 切换 + 样式 class）。

5. **删除旧「大小与巡检设置」lc-panel**（原 `:324-354` 整块），字段并入 header 紧凑组。

`<style scoped>` 追加：

```css
.detail-header {
  display: flex;
  gap: 20px;
  flex-wrap: wrap;               /* 窄屏紧凑组换行 */
  align-items: flex-start;
}

.header-settings {
  display: flex;
  align-items: flex-end;
  gap: 12px;
  margin-left: auto;             /* 靠最右 */
  flex-wrap: wrap;
}

.hs-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.hs-label {
  font-size: 12px;
  color: var(--lc-text-secondary, #909399);
}

.ep-group-nav {
  margin: 0 0 12px;
}

@media (max-width: 768px) {
  .header-settings {
    flex-basis: 100%;
    margin-left: 0;
  }
}
```

删除 `.queue-list` / `.queue-item` 相关样式。

- [x] **Step 3: 运行测试验证通过**

Run: `cd frontend && npx vitest run src/views/MediaDetailView.test.ts && npm run build`
Expected: 组件测试 pass、`vue-tsc` + `vite build` 成功、无 unused import 告警。

- [x] **Step 4: 提交**

```bash
git add frontend/src/views/MediaDetailView.vue frontend/src/views/MediaDetailView.test.ts
git commit -m "feat(media-detail)：详情页布局重构与集数分组导航"
```

---

## Task 4: 全量验证与收尾

**Files:**（验证，一般不改文件；若有问题回到对应 Task 修复）
- Run: `frontend/` 全量测试与构建

- [x] **Step 1: 全量单元测试**

Run: `cd frontend && npm run test`
Expected: 全部 PASS（新增 + 既有用例）。

- [x] **Step 2: 生产构建**

Run: `cd frontend && npm run build`
Expected: `vue-tsc --noEmit` 无错误，`vite build` 成功产出 `dist/`。

- [x] **Step 3: 手动验证清单**（需注解结果，作为验证证据）

1. 打开某 350 集剧集详情：分组 tag 出现 1-100/101-200/201-300/301-350
2. 点击「101-200」→ 表格仅 101-200 共 100 行；点「全部」恢复全量
3. 集数行：有 TMDB 名称的显示名称，无名称显示 SxxExx（无裸 —）
4. 电影详情：无分组导航、无 TMDB 网格；header 显示「电影大小上限 + 巡检间隔 + 保存」
5. header 右侧紧凑组：admin 可见；guest 登录不可见且不报错
6. 转存队列区块彻底消失
7. 窄屏（<768px）：header 紧凑组换行到整行，标题不被挤压，无横向溢出

- [x] **Step 4: 提交（如有遗留修改）**

```bash
git add -A frontend/src
git commit -m "fix(media-detail)：验证收尾修正"
```
（若 Step 1-3 无改动，跳过本步。）