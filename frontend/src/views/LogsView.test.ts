// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import LogsView from './LogsView.vue'
import { listLogsApi } from '../api'
import http from '../api/http'

vi.mock('../api', () => ({
  listLogsApi: vi.fn(),
}))

vi.mock('../api/http', () => ({
  default: { get: vi.fn() },
}))

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

const mockedListLogs = vi.mocked(listLogsApi)
const mockedHttpGet = vi.mocked(http.get)

// Element Plus 组件 stub：jsdom 下无需真实渲染（沿用 SettingsView/QueueView/EmbyLibraryView 先例）。
// el-option stub 输出 data-value 便于断言后端下发的筛选项取值。
const EP_STUBS = {
  'el-select': { template: '<select class="el-select"><slot /></select>', props: ['modelValue'] },
  'el-option': { template: '<option :data-value="value" />', props: ['value', 'label'] },
  'el-input-number': { template: '<input class="el-input-number" />' },
  'el-input': { template: '<input class="el-input" />', props: ['modelValue'], emits: ['update:modelValue'] },
  'el-button': { template: '<button type="button"><slot /></button>', props: ['loading', 'disabled'] },
  'el-icon': { template: '<span><slot /></span>' },
  Search: { template: '<span class="icon-search" />' },
  'el-empty': { template: '<div class="el-empty"><slot /></div>' },
  'el-table': { template: '<div class="el-table"><slot /></div>' },
  'el-table-column': { template: '<div class="lc-col" />' },
  'el-tooltip': { template: '<span><slot /></span>' },
  'el-tag': { template: '<span><slot /></span>', props: ['type'] },
  'el-pagination': { template: '<div class="el-pagination" />' },
  'router-link': { template: '<a><slot /></a>' },
}

let wrapper: ReturnType<typeof mount> | undefined

function mountView() {
  return mount(LogsView, {
    global: { stubs: EP_STUBS, directives: { loading: () => {} } },
    attachTo: document.body,
  })
}

beforeEach(() => {
  document.body.innerHTML = ''
  vi.clearAllMocks()
  setActivePinia(createPinia())
  // 日志列表默认返回空（本组用例聚焦筛选项来源，不关心表格体）
  mockedListLogs.mockResolvedValue({ items: [], total: 0 })
})

afterEach(() => {
  wrapper?.unmount()
})

describe('LogsView 任务类型筛选项后端下发（Design D11）', () => {
  /** 第一个 el-select 是任务类型下拉（模板顺序），状态下拉在其后 */
  function taskTypeSelectOptions(w: ReturnType<typeof mount>) {
    const select = w.findAll('select.el-select')[0]
    return select.findAll('option').map((o) => o.attributes('data-value'))
  }

  it('筛选项由后端 /logs/task-types 返回的类型列表渲染（不再硬编码）', async () => {
    mockedHttpGet.mockResolvedValue({ data: { types: ['scan_media', 'transfer'] } })

    wrapper = mountView()
    await flushPromises()

    expect(mockedHttpGet).toHaveBeenCalledWith('/logs/task-types')
    expect(taskTypeSelectOptions(wrapper)).toEqual(['scan_media', 'transfer'])
  })

  it('task-types 拉取失败回退空筛选项，不阻断日志列表主功能', async () => {
    mockedHttpGet.mockRejectedValue(new Error('network down'))

    wrapper = mountView()
    await flushPromises()

    expect(taskTypeSelectOptions(wrapper)).toHaveLength(0) // 任务类型筛选项为空
    expect(mockedListLogs).toHaveBeenCalledTimes(1) // 日志列表照常拉取
  })
})
