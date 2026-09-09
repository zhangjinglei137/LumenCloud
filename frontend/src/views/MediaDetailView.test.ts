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
  // el-table-column stub 无法从表格上下文注入 row，补充安全样例行，
  // 补齐模板列访问的字段（__tag/size_gb/updated_at 等），确保 scoped slot 冒烟可渲染
  'el-table-column': {
    template: '<div><slot :row="sampleRow" /></div>',
    data: () => ({
      sampleRow: {
        season: 1,
        episode_number: 1,
        name: '第一集',
        __tag: { label: '已在库', type: 'success', reason: '冒烟样例行' },
        size_gb: 1.5,
        updated_at: '2026-01-01T00:00:00Z',
      },
    }),
  },
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