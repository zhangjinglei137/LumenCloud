// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import EmbyLibraryView from './EmbyLibraryView.vue'
import { useEmbyStore } from '../stores/emby'
import { listEmbyLibrariesApi, listEmbyLibraryApi } from '../api'
import type { EmbyLibraryFolder, EmbyLibraryItem } from '../types'

vi.mock('../api', () => ({
  listEmbyLibrariesApi: vi.fn(),
  listEmbyLibraryApi: vi.fn(),
  createMediaApi: vi.fn(),
  scanMediaApi: vi.fn(),
}))

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

vi.mock('../stores/auth', () => ({
  useAuthStore: () => ({ isAdmin: true }),
}))

const mockedList = vi.mocked(listEmbyLibrariesApi)
const mockedListLibrary = vi.mocked(listEmbyLibraryApi)

function makeItem(emby_id: string, title: string): EmbyLibraryItem {
  return {
    emby_id,
    title,
    type: 'movie',
    year: 2026,
    poster_url: null,
    community_rating: null,
    tmdb_id: null,
    emby_web_url: null,
    in_media: false,
    media_id: null,
  }
}

function makeLib(id: string, collection_type: string | null): EmbyLibraryFolder {
  return { id, name: id, collection_type, is_anime: false }
}

// Element Plus 组件 stub：jsdom 下无需真实渲染（沿用 SettingsView/QueueView 先例）
const EP_STUBS = {
  'el-radio-group': { template: '<div class="el-radio-group"><slot /></div>', props: ['modelValue'] },
  'el-radio-button': { template: '<label class="el-radio-button"><slot /></label>', props: ['value'] },
  'el-select': { template: '<select class="el-select"><slot /></select>', props: ['modelValue'] },
  'el-option': { template: '<option />', props: ['value', 'label'] },
  'el-input': { template: '<input class="el-input" />', props: ['modelValue'], emits: ['update:modelValue'] },
  'el-button': { template: '<button type="button"><slot /></button>', props: ['loading', 'disabled'] },
  'el-empty': { template: '<div class="el-empty"><slot /></div>' },
  'el-tag': { template: '<span><slot /></span>', props: ['type'] },
  'el-icon': { template: '<span><slot /></span>' },
  'el-dialog': { template: '<div class="el-dialog"><slot /></div>', props: ['modelValue'] },
  TmdbSearch: { template: '<div class="tmdb-search" />', props: ['placeholder'] },
  Search: { template: '<span class="icon-search" />' },
  Refresh: { template: '<span class="icon-refresh" />' },
  CircleCheckFilled: { template: '<span class="icon-circle-check" />' },
  Monitor: { template: '<span class="icon-monitor" />' },
}

let wrapper: ReturnType<typeof mount> | undefined

function mountView() {
  return mount(EmbyLibraryView, {
    global: { stubs: EP_STUBS, directives: { loading: () => {} } },
    attachTo: document.body,
  })
}

beforeEach(() => {
  document.body.innerHTML = ''
  vi.clearAllMocks()
  setActivePinia(createPinia())
})

afterEach(() => {
  wrapper?.unmount()
})

describe('EmbyLibraryView 去分页数据流', () => {
  it('多库聚合：按 emby_id 去重合并，全量渲染不截断（无分页）', async () => {
    const libs = [makeLib('x1', 'mixed'), makeLib('x2', null)] // 均归入「全部」分类，走聚合路径
    mockedList.mockResolvedValue({ libraries: libs, total: 2 })

    const itemsA = Array.from({ length: 80 }, (_, i) => makeItem(`a-${i}`, `片 A${i}`))
    const itemsB = [
      makeItem('a-0', '片 A0'), // 与库 A 重复 → 聚合去重
      makeItem('a-10', '片 A10'), // 与库 A 重复 → 聚合去重
      ...Array.from({ length: 40 }, (_, i) => makeItem(`b-${i}`, `片 B${i}`)),
    ]
    mockedListLibrary.mockImplementation(async ({ library_id }) => {
      const items = library_id === 'x1' ? itemsA : itemsB
      return { items, total: items.length, item_type: null }
    })

    wrapper = mountView()
    await flushPromises()

    // 逐库各请求一次；聚合结果 = 80 + (2 重复 + 40) - 2 条重复 = 120
    expect(mockedListLibrary).toHaveBeenCalledTimes(2)
    const store = useEmbyStore()
    expect(store.items).toHaveLength(120)
    expect(store.items.map((i) => i.emby_id)).toContain('a-0')
    expect(store.items.map((i) => i.emby_id)).toContain('b-39')
    expect(store.error).toBeNull()

    // 视图全量渲染 store.items（filteredItems = 无筛选 → 全部），无分页截断
    expect(wrapper.findAll('.lc-media-card')).toHaveLength(120)
    expect(wrapper.find('.lc-media-grid').exists()).toBe(true)
  })

  it('单库下钻：走 store.fetchLibrary，条目全量呈现', async () => {
    // 单个「全部」库 → 无下钻下拉（categoryLibraries.length === 1），直接聚合该库
    const libs = [makeLib('x1', null)]
    mockedList.mockResolvedValue({ libraries: libs, total: 1 })
    const items = Array.from({ length: 30 }, (_, i) => makeItem(`c-${i}`, `片 C${i}`))
    mockedListLibrary.mockResolvedValue({ items, total: items.length, item_type: null })

    wrapper = mountView()
    await flushPromises()

    expect(mockedListLibrary).toHaveBeenCalledTimes(1)
    expect(useEmbyStore().items).toHaveLength(30)
    expect(wrapper.findAll('.lc-media-card')).toHaveLength(30)
  })
})
