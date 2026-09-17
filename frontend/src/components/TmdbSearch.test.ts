// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import TmdbSearch from './TmdbSearch.vue'
import type { TmdbSearchResult } from '../types'

// vi.hoisted：vi.mock 工厂被提升到文件顶部，顶层变量需经 hoisted 定义才能被引用
const { searchTmdbApiMock } = vi.hoisted(() => ({ searchTmdbApiMock: vi.fn() }))

vi.mock('../api', () => ({
  searchTmdbApi: searchTmdbApiMock,
}))

// Element Plus 组件 stub（沿用 QueueView/UsersView 先例；el-input 支持 v-model + append slot）
const EP_STUBS = {
  'el-input': {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template:
      '<div class="el-input"><input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" /><slot /></div>',
  },
  'el-button': { template: '<button type="button"><slot /></button>' },
  'el-empty': { template: '<div>{{ description }}</div>', props: ['description'] },
  'el-alert': { template: '<div class="el-alert">{{ title }}</div>', props: ['title', 'type'] },
  'el-tag': { template: '<span><slot /></span>' },
}

function makeResult(over: Partial<TmdbSearchResult> = {}): TmdbSearchResult {
  return {
    title: '测试电影',
    tmdb_id: 100,
    media_type: 'movie',
    poster_path: null,
    ...over,
  }
}

function mountView() {
  return mount(TmdbSearch, {
    global: { stubs: EP_STUBS, directives: { loading: () => {} } },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  document.body.innerHTML = ''
})

afterEach(() => {
  searchTmdbApiMock.mockReset()
})

describe('TmdbSearch 搜索错误态（fix-audit-issues C11 / 审查 A13）', () => {
  it('搜索失败：置错误态内联提示，且不残留上次搜索结果', async () => {
    // 先成功一次（产生结果），再失败 → 结果应被清空
    searchTmdbApiMock.mockResolvedValueOnce([makeResult({ title: '上次成功结果' })])
    const wrapper = mountView()
    const vm = wrapper.vm as unknown as { keyword: string; search: () => Promise<void> }

    vm.keyword = '测试'
    await vm.search()
    await flushPromises()
    expect(wrapper.text()).toContain('上次成功结果')

    // 第二次搜索失败（拦截器会统一 toast；组件内联错误态兜底）
    searchTmdbApiMock.mockRejectedValueOnce(new Error('boom'))
    await vm.search()
    await flushPromises()

    // 错误态内联提示出现
    expect(wrapper.text()).toContain('搜索失败')
    // 上次结果不残留
    expect(wrapper.text()).not.toContain('上次成功结果')
    expect(wrapper.findAll('.lc-tmdb-item')).toHaveLength(0)
  })

  it('搜索成功后不残留错误态', async () => {
    searchTmdbApiMock.mockResolvedValueOnce([makeResult({ title: '成功结果' })])
    const wrapper = mountView()
    const vm = wrapper.vm as unknown as { keyword: string; search: () => Promise<void> }

    vm.keyword = '测试'
    await vm.search()
    await flushPromises()

    expect(wrapper.text()).not.toContain('搜索失败')
    expect(wrapper.text()).toContain('成功结果')
  })
})
