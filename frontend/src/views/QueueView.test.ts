// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { computed, inject, provide, reactive } from 'vue'
import QueueView from './QueueView.vue'
import type { DownloadQueueItem, QueueTaskItem } from '../types'

// ---------- 可变 store mock（reactive：注入数据 / 切换角色后模板可响应更新） ----------
const storeState = reactive({
  items: [] as QueueTaskItem[],
  capacity: null as Record<string, unknown> | null,
  loading: false,
  page: 1,
  pageSize: 50,
  total: 0,
  downloadItems: [] as DownloadQueueItem[],
  downloadLoading: false,
  downloadPage: 1,
  downloadTotal: 0,
  pauseState: { paused: false, in_flight: null },
  progressMap: {} as Record<number, unknown>,
  downloadingItems: [] as DownloadQueueItem[],
  availableGb: 0,
  usedGb: 0,
  reservedGb: 0,
  usagePercent: 0,
  // 组件 script/template 调用的动作（onMounted / 分页 / 按钮）
  fetchPage: vi.fn(),
  fetchDownloadPage: vi.fn(),
  fetchCapacity: vi.fn(),
  fetchPauseState: vi.fn(),
  fetchProgress: vi.fn(),
  cancel: vi.fn(),
  prioritize: vi.fn(),
  sort: vi.fn(),
  skip: vi.fn(),
  promote: vi.fn(),
  retry: vi.fn(),
  pause: vi.fn(),
  resume: vi.fn(),
})

vi.mock('../stores/queue', () => ({
  useQueueStore: () => storeState,
}))

const authState = reactive({ isAdmin: true })

vi.mock('../stores/auth', () => ({
  useAuthStore: () => authState,
}))

function makeTask(over: Partial<QueueTaskItem> = {}): QueueTaskItem {
  return {
    id: 1,
    media_id: 1,
    title: '测试剧',
    episode: 'S01E01',
    status: 'pending',
    node: null,
    file_name: '测试剧.S01E01.mp4',
    file_size: 1024 ** 3,
    updated_at: '2026-01-01T00:00:00Z',
    enqueued_at: '2026-01-01T00:00:00Z',
    ...over,
  }
}

function makeDownload(over: Partial<DownloadQueueItem> = {}): DownloadQueueItem {
  return {
    id: 1,
    media_id: 1,
    media_title: '测试剧',
    episode: 'S01E01',
    file_name: '测试剧.S01E01.mp4',
    file_size: null,
    share_code: null,
    status: 'done',
    node_error: null,
    retry_count: null,
    enqueued_at: null,
    updated_at: null,
    ...over,
  }
}

// ---------- Element Plus 组件 stub ----------
// el-table stub 通过 provide 暴露行数据 getter；el-table-column stub 注入它，
// 使不同表格的 scoped slot 各渲染对应 store 数据（任务表 → items / 下载表 → downloadItems）
const TABLE_ROWS = Symbol('table-rows')

const EP_STUBS = {
  'el-button': { template: '<button type="button"><slot /></button>' },
  'el-tooltip': { template: '<span><slot /></span>' },
  'el-tag': { template: '<span><slot /></span>' },
  'el-icon': { template: '<span><slot /></span>' },
  'el-empty': { template: '<div><slot /></div>' },
  'el-switch': { template: '<span><slot /></span>' },
  'el-alert': { template: '<div><slot /></div>' },
  'el-progress': { template: '<span><slot /></span>' },
  'el-tabs': { template: '<div class="el-tabs"><slot /></div>' },
  'el-tab-pane': {
    props: ['label', 'name'],
    template: '<div class="el-tab-pane"><span class="pane-label">{{ label }}</span><slot /></div>',
  },
  'el-table': {
    props: ['data'],
    template: '<div class="el-table"><slot /></div>',
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    setup(props: any) {
      provide(TABLE_ROWS, () => props.data ?? [])
    },
  },
  'el-table-column': {
    template: '<div><slot v-for="r in rows" :row="r" /></div>',
    setup() {
      const getRows = inject<(() => unknown[]) | null>(TABLE_ROWS, null)
      const rows = computed<unknown[]>(() => (getRows ? getRows() : []))
      return { rows }
    },
  },
  'el-pagination': {
    props: ['currentPage', 'pageSize', 'total'],
    emits: ['current-change'],
    template:
      '<div class="el-pagination" :data-total="total" :data-current="currentPage" :data-page-size="pageSize">' +
      '<button class="page-prev" @click="$emit(\'current-change\', currentPage - 1)">prev</button>' +
      '<button class="page-next" @click="$emit(\'current-change\', currentPage + 1)">next</button>' +
      '</div>',
  },
  Refresh: { template: '<span class="icon-refresh" />' },
}

let wrapper: ReturnType<typeof mount> | undefined

function mountView() {
  return mount(QueueView, {
    global: { stubs: EP_STUBS, directives: { loading: () => {} } },
    attachTo: document.body,
  })
}

beforeEach(() => {
  document.body.innerHTML = ''
  vi.clearAllMocks()
  storeState.items = []
  storeState.downloadItems = []
  storeState.total = 0
  storeState.downloadTotal = 0
  storeState.page = 1
  storeState.downloadPage = 1
  storeState.pageSize = 50
  storeState.loading = false
  storeState.downloadLoading = false
  storeState.pauseState = { paused: false, in_flight: null }
  authState.isAdmin = true
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = undefined
})

describe('QueueView 巡检队列 Tab（queue-inspection-rework）', () => {
  it('任务 Tab 更名「巡检队列」，原「任务队列」不再出现', async () => {
    wrapper = mountView()
    await flushPromises()
    const labels = wrapper.findAll('.pane-label').map((w) => w.text())
    expect(labels).toContain('巡检队列')
    expect(labels).not.toContain('任务队列')
    expect(wrapper.html()).not.toContain('任务队列')
  })

  it('巡检队列行渲染标题、集号与「约 」标注大小（size_estimated=true）', async () => {
    storeState.items = [
      makeTask({ title: '测试剧', episode: 'S01E01', file_size: 1024 ** 3, size_estimated: true }),
    ]
    storeState.total = 1
    wrapper = mountView()
    await flushPromises()
    const html = wrapper.html()
    expect(html).toContain('测试剧')
    expect(html).toContain('S01E01')
    expect(html).toContain('约 1.00 GB')
  })

  it('巡检队列大小列：file_size=0 显示「—」，精确值无「约」', async () => {
    storeState.items = [makeTask({ file_size: 0, size_estimated: false })]
    wrapper = mountView()
    await flushPromises()
    // 大小单元格只渲染一次且为「—」（0 值语义，Task 3 的 formatFileSize）
    const text = wrapper.text()
    expect(text).toContain('—')
    expect(text).not.toContain('0 B')
    expect(text).not.toContain('约')
  })

  it('任务 Tab 分享码列：admin 可见明文，guest 隐藏', async () => {
    storeState.items = [{ ...makeTask({ id: 1 }), share_code: 'TASKCODE' }]
    storeState.total = 1

    authState.isAdmin = true
    wrapper = mountView()
    await flushPromises()
    expect(wrapper.html()).toContain('TASKCODE')
    wrapper.unmount()

    authState.isAdmin = false
    wrapper = mountView()
    await flushPromises()
    expect(wrapper.html()).not.toContain('TASKCODE')
  })
})

describe('QueueView 下载队列分享码链接（queue-inspection-rework）', () => {
  it('有 share_url+share_code → <a> 可点击新窗口；仅 share_code → 纯文本不可点击', async () => {
    storeState.downloadItems = [
      makeDownload({
        id: 1,
        media_title: '剧A',
        share_code: 'ABCD1234',
        share_url: 'https://pan.quark.cn/s/abcd1234',
      }),
      makeDownload({ id: 2, media_title: '剧B', share_code: 'WXYZ5678', share_url: null }),
    ]
    storeState.downloadTotal = 2
    wrapper = mountView()
    await flushPromises()

    const links = wrapper.findAll('a.qv-share-link')
    expect(links).toHaveLength(1)
    expect(links[0].attributes('href')).toBe('https://pan.quark.cn/s/abcd1234')
    expect(links[0].attributes('target')).toBe('_blank')
    expect(links[0].attributes('rel')).toBe('noopener')
    expect(links[0].text()).toBe('ABCD1234')
    // 无 share_url 的行以纯文本呈现（html 中无第二个 <a>）
    expect(wrapper.html()).toContain('WXYZ5678')
  })

  it('两个 Tab 的 el-pagination 依 total 渲染，翻页回调分别调用 fetchPage / fetchDownloadPage', async () => {
    storeState.items = [makeTask({ id: 1 }), makeTask({ id: 2 })]
    storeState.total = 120
    storeState.downloadItems = [makeDownload({ id: 1 })]
    storeState.downloadTotal = 120
    wrapper = mountView()
    await flushPromises()

    const pagers = wrapper.findAll('.el-pagination')
    expect(pagers).toHaveLength(2)
    expect(pagers[0].attributes('data-total')).toBe('120')
    expect(pagers[1].attributes('data-total')).toBe('120')

    await pagers[0].find('.page-next').trigger('click')
    await pagers[1].find('.page-next').trigger('click')
    expect(storeState.fetchPage).toHaveBeenCalledWith(2)
    expect(storeState.fetchDownloadPage).toHaveBeenCalledWith(2)
  })
})
