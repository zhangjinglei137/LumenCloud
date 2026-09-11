// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import EmbyLibraryView from './EmbyLibraryView.vue'
import { useEmbyStore } from '../stores/emby'
import { listEmbyLibrariesApi, listEmbyLibraryApi, listAllEmbyLibraryApi } from '../api'
import type { EmbyLibraryFolder, EmbyLibraryItem } from '../types'

vi.mock('../api', () => ({
  listEmbyLibrariesApi: vi.fn(),
  listEmbyLibraryApi: vi.fn(),
  listAllEmbyLibraryApi: vi.fn(),
  createMediaApi: vi.fn(),
  scanMediaApi: vi.fn(),
}))

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

vi.mock('../stores/auth', () => ({
  useAuthStore: () => ({ isAdmin: true }),
}))

const mockedList = vi.mocked(listEmbyLibrariesApi)
const mockedListLibrary = vi.mocked(listEmbyLibraryApi)
const mockedListAll = vi.mocked(listAllEmbyLibraryApi)

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
  it('全部 Tab 聚合态：单次调用 listAllEmbyLibraryApi（不逐库请求）', async () => {
    const libs = [makeLib('m1', 'movies'), makeLib('t1', 'tvshows'), makeLib('x1', 'mixed')]
    mockedList.mockResolvedValue({ libraries: libs, total: 3 })

    const items = Array.from({ length: 120 }, (_, i) => makeItem(`a-${i}`, `片 A${i}`))
    mockedListAll.mockResolvedValue({ items, total: items.length, item_type: null })

    wrapper = mountView()
    await flushPromises()

    expect(mockedListAll).toHaveBeenCalledTimes(1)
    expect(mockedListAll).toHaveBeenCalledWith({ status: undefined })
    expect(mockedListLibrary).not.toHaveBeenCalled() // 全部 Tab 不再逐库
    const store = useEmbyStore()
    expect(store.items).toHaveLength(120)
    expect(store.error).toBeNull()
    expect(wrapper.findAll('.lc-media-card')).toHaveLength(120)
  })

  it('全部 Tab 聚合失败：置错误态', async () => {
    const libs = [makeLib('m1', 'movies')]
    mockedList.mockResolvedValue({ libraries: libs, total: 1 })
    mockedListAll.mockRejectedValue({
      response: { data: { detail: { code: 'emby_unreachable' } } },
    })

    wrapper = mountView()
    await flushPromises()

    expect(useEmbyStore().error).toBe('unavailable')
  })

  it('单库下钻：走 store.fetchLibrary，条目全量呈现', async () => {
    // 单个「全部」库 → 无下钻下拉（categoryLibraries.length === 1）；选中后走单库查询
    const libs = [makeLib('x1', null)]
    mockedList.mockResolvedValue({ libraries: libs, total: 1 })
    const items = Array.from({ length: 30 }, (_, i) => makeItem(`c-${i}`, `片 C${i}`))
    mockedListLibrary.mockResolvedValue({ items, total: items.length, item_type: null })

    wrapper = mountView()
    await flushPromises()

    // 下钻：选中具体库 → fetchSingle → store.fetchLibrary（listEmbyLibraryApi），不经聚合端点
    ;(wrapper.vm as unknown as { onLibraryChange: (v: string) => void }).onLibraryChange('x1')
    await flushPromises()

    expect(mockedListLibrary).toHaveBeenCalledTimes(1)
    expect(useEmbyStore().items).toHaveLength(30)
    expect(wrapper.findAll('.lc-media-card')).toHaveLength(30)
  })
})
