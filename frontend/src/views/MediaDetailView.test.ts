// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { reactive } from 'vue'
import MediaDetailView from './MediaDetailView.vue'
import type { ActiveTask } from '../types'

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: '1' } }),
  useRouter: () => ({ push: vi.fn() }),
}))

// reactive 化：测试可注入 episode_state 触发组件 computed 重新求值（分组过滤断言依赖）
// 显式声明宽类型：episode_state 允许 season/episode_number 为 null（非标准集号场景）
interface TestEpisodeState {
  id: number
  episode: string
  season: number | null
  episode_number: number | null
  name?: string
}

const detail = reactive<{
  id: number
  title: string
  media_type: string
  status: string
  episode_state: TestEpisodeState[]
  tmdb_episodes: Array<{ season: number; episode: number; name?: string }>
  active_tasks: ActiveTask[]
}>({
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
  active_tasks: [],
})

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
  // 订阅状态选择（Task 3 修复轮 1）：label 文案渲染在 select 容器内
  'el-select': { template: '<select><slot /></select>' },
  'el-option': { template: '<option />' },
  // 分组导航（Task 3）：label 内容渲染在 ep-group-nav 容器内
  'el-radio-group': { template: '<div class="ep-group-nav"><slot /></div>' },
  'el-radio-button': { template: '<label><slot /></label>' },
  'el-table': { template: '<div class="el-table"><slot /></div>' },
  // el-table-column stub 无法从表格上下文注入 row，补充安全样例行，
  // 补齐模板列访问的字段（__tag/size_gb/updated_at 等），确保 scoped slot 冒烟可渲染
  'el-table-column': {
    template: '<div><slot v-for="r in sampleRows" :row="r" /></div>',
    data: () => ({
      // 两行：一行有名称（渲染「第一集」），一行无名称（名称列回退 S01E02）
      // episode/status/source 字段为「当前进行中任务」表格冒烟渲染补齐
      // （该表格列访问 row.episode / row.source / row.status，按 source 复用对应状态字典）
      sampleRows: [
        {
          season: 1,
          episode_number: 1,
          name: '第一集',
          episode: 'S01E001',
          status: 'downloading',
          source: 'dq',
          __tag: { label: '已在库', type: 'success', reason: '冒烟样例行' },
          size_gb: 1.5,
          updated_at: '2026-01-01T00:00:00Z',
        },
        {
          season: 1,
          episode_number: 2,
          name: '',
          episode: 'S01E002',
          status: 'probing',
          source: 'task',
          __tag: { label: '已开播', type: 'warning', reason: '冒烟样例行' },
          size_gb: null,
          updated_at: null,
        },
      ],
    }),
  },
}

function mountView() {
  return mount(MediaDetailView, {
    global: { stubs: EP_STUBS, directives: { loading: () => {} } },
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

describe('MediaDetailView 布局（media-detail-ui）', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })

  it('剧集 + 有分组数据 → header 紧凑组/分组导航渲染，转存队列区块不存在', async () => {
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.find('.ep-group-nav').exists()).toBe(true) // 有分组数据
    expect(wrapper.find('.header-settings').exists()).toBe(true) // admin 紧凑组
    expect(wrapper.find('.header-settings select').exists()).toBe(true) // 订阅状态编辑入口（tracking/paused）
    expect(wrapper.find('.queue-list').exists()).toBe(false) // 原转存队列区块已移除
    expect(wrapper.html()).not.toContain('转存队列') // 详情页无转存队列
    const html = wrapper.html()
    expect(html).toContain('第一集') // 集数名称展示
    expect(html).toContain('S01E02') // 无名称行回退集号
  })
})

describe('MediaDetailView 分组过滤（media-detail-ui）', () => {
  // 默认数据（其余 describe 依赖）
  const DEFAULT_EPISODE_STATE = [
    { id: 1, episode: 'S01E001', season: 1, episode_number: 1, name: '第一集' },
    { id: 2, episode: 'S01E002', season: 1, episode_number: 2 },
  ]

  // 数据集：episode_number 为 1 / 101 / null（非标准集号，后端 fallback DTO 返回 null）
  // episodeTotal = max(1, 101, 0) = 101 → 分组 [{1-100},{101-101}]
  const GROUPED_EPISODE_STATE = [
    { id: 1, episode: 'S01E001', season: 1, episode_number: 1, name: '第一集' },
    { id: 2, episode: 'S01E101', season: 1, episode_number: 101, name: '第101集' },
    { id: 3, episode: 'SP01', season: null, episode_number: null, name: '特典' },
  ]

  beforeEach(() => {
    document.body.innerHTML = ''
    detail.episode_state = GROUPED_EPISODE_STATE.map((e) => ({ ...e }))
  })

  afterEach(() => {
    detail.episode_state = DEFAULT_EPISODE_STATE.map((e) => ({ ...e }))
  })

  it('未选分组 → 全部行（含无集号行）', async () => {
    const wrapper = mountView()
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      activeGroup: string | null
      filteredRows: Array<Record<string, unknown>>
    }
    expect(vm.activeGroup).toBeNull()
    expect(vm.filteredRows).toHaveLength(3)
  })

  it('选中分组 → 按区间过滤，episode_number 为 null 的行始终保留（回归 W1）', async () => {
    const wrapper = mountView()
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      activeGroup: string | null
      filteredRows: Array<Record<string, unknown>>
    }
    const episodeNumbers = () => vm.filteredRows.map((r) => r.episode_number)

    // 选「1-100」：1 与 null 保留，101 被过滤
    vm.activeGroup = '1-100'
    await flushPromises()
    expect(episodeNumbers()).toEqual([1, null])

    // 选「101-101」：101 与 null 保留，1 被过滤
    vm.activeGroup = '101-101'
    await flushPromises()
    expect(episodeNumbers()).toEqual([101, null])
  })
})

describe('MediaDetailView 当前进行中任务区块（episode-status-and-detail-polish）', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
    detail.active_tasks = []
  })

  afterEach(() => {
    detail.active_tasks = []
  })

  it('有任务时渲染区块：集号 + 集名 + 按 source 的状态标签', async () => {
    detail.active_tasks = [
      { season: 1, episode: 'S01E001', status: 'downloading', source: 'dq', air_date: '2026-01-01' },
      { season: 1, episode: 'S01E002', status: 'probing', source: 'task', air_date: null },
    ]
    const wrapper = mountView()
    await flushPromises()
    const text = wrapper.text()
    expect(text).toContain('当前进行中任务')
    expect(text).toContain('S01E001')
    expect(text).toContain('下载中') // dq 字典
    expect(text).toContain('S01E002')
    expect(text).toContain('探测中') // task 字典
  })

  it('无任务时隐藏区块', async () => {
    detail.active_tasks = []
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.text()).not.toContain('当前进行中任务')
  })
})