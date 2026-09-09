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
      sampleRows: [
        {
          season: 1,
          episode_number: 1,
          name: '第一集',
          __tag: { label: '已在库', type: 'success', reason: '冒烟样例行' },
          size_gb: 1.5,
          updated_at: '2026-01-01T00:00:00Z',
        },
        {
          season: 1,
          episode_number: 2,
          name: '',
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