// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { reactive } from 'vue'
import MediaDetailView from './MediaDetailView.vue'
import { formatTime } from '../utils/format'
import type { ActiveTask } from '../types'

// ElMessage 是 JS 服务（非组件），mock 掉以断言受理结果的提示分支
vi.mock('element-plus', () => ({
  ElMessage: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
  ElMessageBox: { confirm: vi.fn().mockResolvedValue('confirm') },
}))
import { ElMessage } from 'element-plus'

const { scanMock } = vi.hoisted(() => ({ scanMock: vi.fn() }))

// 可变 route mock：新增测试通过覆盖 mockRouteParams 模拟非法路由参数（C11 守卫）
let mockRouteParams: Record<string, unknown> = { id: '1' }
const pushMock = vi.fn()

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: mockRouteParams }),
  useRouter: () => ({ push: pushMock }),
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
  min_episode_size_gb?: number | null
  min_movie_size_gb?: number | null
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

const fetchDetailMock = vi.fn().mockResolvedValue(undefined)
const patchMock = vi.fn().mockResolvedValue(undefined)

vi.mock('../stores/media', () => ({
  useMediaStore: () => ({
    detail,
    loading: false,
    fetchDetail: fetchDetailMock,
    patch: patchMock,
    scan: scanMock,
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
  // name: 'ElSelect'：VTU 对自定义 stub 的拷贝不带 name，findComponent({ name }) 无法命中，显式命名以支持组件选择器匹配
  'el-select': { name: 'ElSelect', template: '<select><slot /></select>' },
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
          state: 'in_library',
          error: '历史失败原因不应展示',
          retry_count: 3,
          last_attempt_at: '2026-01-01T00:00:00Z',
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

describe('MediaDetailView 路由参数防御（fix-audit-issues C11 / 审查 A14）', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
    pushMock.mockClear()
    fetchDetailMock.mockClear()
  })

  afterEach(() => {
    mockRouteParams = { id: '1' }
  })

  it('非法 id（非有限数值）不发起 fetchDetail 请求，跳转列表页', async () => {
    mockRouteParams = { id: 'abc' }
    const wrapper = mountView()
    await flushPromises()
    // 不发 GET /api/media/NaN 类请求
    expect(fetchDetailMock).not.toHaveBeenCalled()
    // 守卫失败跳列表页（不展示死页）
    expect(pushMock).toHaveBeenCalledWith({ name: 'media-list' })
    wrapper.unmount()
  })

  it('合法 id 正常发起 fetchDetail 且不跳转', async () => {
    mockRouteParams = { id: '7' }
    const wrapper = mountView()
    await flushPromises()
    expect(fetchDetailMock).toHaveBeenCalledWith(7)
    expect(pushMock).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})

describe('状态下拉宽度（episode-status-and-detail-polish）', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })

  it('el-select 设置显式 min-width', async () => {
    const wrapper = mountView()
    await flushPromises()
    const select = wrapper.findComponent({ name: 'ElSelect' })
    expect(select.exists()).toBe(true)
    expect(select.attributes('style') ?? '').toContain('min-width')
  })
})

describe('MediaDetailView 大小下限设置（media-min-size-and-emby-redownload）', () => {
  type FormVm = {
    form: { min_episode_size_gb: number | null; min_movie_size_gb: number | null }
    saveSettings: () => Promise<void>
  }

  beforeEach(() => {
    document.body.innerHTML = ''
    patchMock.mockClear()
    detail.min_episode_size_gb = null
    detail.min_movie_size_gb = null
  })

  it('详情已有下限时回填到表单', async () => {
    detail.min_episode_size_gb = 0.2
    detail.min_movie_size_gb = 0.8
    const wrapper = mountView()
    await flushPromises()
    const vm = wrapper.vm as unknown as FormVm
    expect(vm.form.min_episode_size_gb).toBe(0.2)
    expect(vm.form.min_movie_size_gb).toBe(0.8)
    wrapper.unmount()
  })

  it('填写最小值后保存提交正确 payload', async () => {
    const wrapper = mountView()
    await flushPromises()
    const vm = wrapper.vm as unknown as FormVm
    expect('min_episode_size_gb' in vm.form).toBe(true)
    expect('min_movie_size_gb' in vm.form).toBe(true)
    vm.form.min_episode_size_gb = 0.3
    vm.form.min_movie_size_gb = 1.2
    await vm.saveSettings()
    expect(patchMock).toHaveBeenCalledWith(
      1,
      expect.objectContaining({ min_episode_size_gb: 0.3, min_movie_size_gb: 1.2 }),
    )
    wrapper.unmount()
  })

  it('留空时回填与提交均为 null（不限制）', async () => {
    const wrapper = mountView()
    await flushPromises()
    const vm = wrapper.vm as unknown as FormVm
    expect(vm.form.min_episode_size_gb).toBeNull()
    expect(vm.form.min_movie_size_gb).toBeNull()
    await vm.saveSettings()
    expect(patchMock).toHaveBeenCalledWith(
      1,
      expect.objectContaining({ min_episode_size_gb: null, min_movie_size_gb: null }),
    )
    wrapper.unmount()
  })
})

describe('MediaDetailView 巡检受理反馈（scan-outcome-attribution 4.4）', () => {
  type ScanVm = { onScan: () => Promise<void> }

  beforeEach(() => {
    document.body.innerHTML = ''
    scanMock.mockReset()
    vi.mocked(ElMessage.success).mockClear()
    vi.mocked(ElMessage.warning).mockClear()
  })

  it('受理成功 → 成功提示带 run id', async () => {
    scanMock.mockResolvedValue({ accepted: true, runId: 12 })
    const wrapper = mountView()
    await flushPromises()
    await (wrapper.vm as unknown as ScanVm).onScan()

    expect(ElMessage.success).toHaveBeenCalledWith('已触发巡检（任务 #12）')
    expect(ElMessage.warning).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('受理失败（accepted=false）→ 不出现成功提示，改为警告', async () => {
    scanMock.mockResolvedValue({ accepted: false, runId: null })
    const wrapper = mountView()
    await flushPromises()
    await (wrapper.vm as unknown as ScanVm).onScan()

    expect(ElMessage.success).not.toHaveBeenCalled()
    expect(ElMessage.warning).toHaveBeenCalled()
    wrapper.unmount()
  })
})
describe('MediaDetailView 更新诊断卡片（media-detail-update-diagnosis）', () => {
  const DIAG = {
    has_scan: true,
    scan_started_at: '2026-10-20T10:00:00Z',
    scan_finished_at: null,
    scan_status: 'success',
    skip_reason: null,
    skip_reason_text: '没有发现缺失集',
    missing_aired_total: 0,
    missing_episodes: [],
    missing_more: 0,
    gated_out_count: 0,
    baseline_suspect: false,
    baseline_source: null,
    baseline_degraded: false,
    baseline_detail: null,
    intervention: { level: 'intervention', label: '需要人工处理', reason: '连续无候选' },
    next_scan_at: null,
    not_scheduled_reason: 'paused',
    active_episode: null,
    active_stage: null,
  }

  beforeEach(() => {
    document.body.innerHTML = ''
  })

  afterEach(() => {
    delete (detail as Record<string, unknown>).diagnosis
  })

  it('有 diagnosis 时渲染卡片（位于 header 之下、集数表之上），无 diagnosis 不渲染', async () => {
    ;(detail as Record<string, unknown>).diagnosis = DIAG
    const wrapper = mountView()
    await flushPromises()
    const card = wrapper.find('.diagnosis-card')
    expect(card.exists()).toBe(true)
    expect(wrapper.text()).toContain('需要人工处理')
    // 位置：header 之后
    const header = wrapper.find('.detail-header')
    expect(header.exists()).toBe(true)
    wrapper.unmount()

    const wrapper2 = mountView()
    await flushPromises()
    // afterEach 未跑，这里先删
    delete (detail as Record<string, unknown>).diagnosis
    const wrapper3 = mountView()
    await flushPromises()
    expect(wrapper3.find('.diagnosis-card').exists()).toBe(false)
    wrapper2.unmount()
    wrapper3.unmount()
  })
})

describe('MediaDetailView 集数表尝试列（media-detail-update-diagnosis 3.x）', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })

  it('新增「最近尝试时间/尝试次数/失败原因」三列（stub 不渲染表头，断言列单元格行为）', async () => {
    const wrapper = mountView()
    await flushPromises()
    const text = wrapper.text()
    // 样例行无 last_attempt_at/retry_count → 时间列 —、次数列 —、失败原因列空态
    expect(text).toContain('尚未尝试下载')
    // in_library 样例行不渲染历史失败原因（D5）
    expect(text).not.toContain('历史失败原因不应展示')
    wrapper.unmount()
  })

  it('无尝试数据显示「尚未尝试下载」，不显示为失败', async () => {
    const wrapper = mountView()
    await flushPromises()
    // el-table-column stub 的样例行无 retry_count/last_attempt_at → 空态文案
    expect(wrapper.text()).toContain('尚未尝试下载')
    wrapper.unmount()
  })
})

// 5.1（comet task 0a356367）：三场景端到端渲染——真实 MediaDetailView + 真实诊断卡片，
// 仅 mock store/api。fixture 与后端 _build_diagnosis / scan._skip_message 的产物一致。
describe('MediaDetailView 诊断三场景（media-detail-update-diagnosis 5.1）', () => {
  type DiagFixture = Record<string, unknown>

  function baseDiag(): DiagFixture {
    return {
      has_scan: true,
      scan_started_at: '2026-10-20T10:00:00Z',
      scan_finished_at: '2026-10-20T10:02:00Z',
      scan_status: 'success',
      skip_reason: null,
      skip_reason_text: null,
      missing_aired_total: 0,
      missing_episodes: [],
      missing_more: 0,
      gated_out_count: 0,
      baseline_suspect: false,
      baseline_source: 'both',
      baseline_degraded: false,
      baseline_detail: null,
      intervention: { level: 'waiting', label: '等待下一轮巡检', reason: '等待下一轮巡检' },
      next_scan_at: '2026-10-21T10:00:00Z',
      not_scheduled_reason: null,
      active_episode: null,
      active_stage: null,
    }
  }

  beforeEach(() => {
    document.body.innerHTML = ''
  })

  afterEach(() => {
    delete (detail as Record<string, unknown>).diagnosis
  })

  it('有缺集无资源（no_candidates）→ intervention 卡片默认展开，含缺失集数与未获取到任何资源', async () => {
    const skipText = '测试剧存在已播缺失 4 集（S05E188、S05E189），本轮未获取到任何资源'
    ;(detail as Record<string, unknown>).diagnosis = {
      ...baseDiag(),
      scan_status: 'skipped',
      skip_reason: 'no_candidates',
      skip_reason_text: skipText,
      missing_aired_total: 4,
      missing_episodes: ['S05E188', 'S05E189'],
      missing_more: 2,
      intervention: {
        level: 'intervention',
        label: '已失败需干预',
        reason: '连续 3 轮巡检未获取到资源，资源可能尚未更新',
      },
    }
    const wrapper = mountView()
    await flushPromises()

    const card = wrapper.find('.diagnosis-card')
    expect(card.exists()).toBe(true)
    // intervention 默认展开 + 高亮
    expect(wrapper.find('.diagnosis-card--intervention').exists()).toBe(true)
    expect(wrapper.find('.diag-detail').exists()).toBe(true)

    const cardText = card.text()
    expect(cardText).toContain('已失败需干预')
    expect(cardText).toContain('缺失 4 集')
    expect(cardText).toContain('S05E188')
    expect(cardText).toContain('等 2 集')
    expect(cardText).toContain('未获取到任何资源')
    wrapper.unmount()
  })

  it('无缺失集（no_missing）→ waiting 卡片折叠为一行，不出现缺失集文案', async () => {
    const skipText = '测试剧无遗漏集（Emby 基线已覆盖），跳过巡检（本轮无需进一步动作）'
    ;(detail as Record<string, unknown>).diagnosis = {
      ...baseDiag(),
      scan_status: 'skipped',
      skip_reason: 'no_missing',
      skip_reason_text: skipText,
      intervention: { level: 'waiting', label: '等待下一轮巡检', reason: skipText },
    }
    const wrapper = mountView()
    await flushPromises()

    const card = wrapper.find('.diagnosis-card')
    expect(card.exists()).toBe(true)
    // waiting 折叠：详情区不渲染
    expect(wrapper.find('.diag-detail').exists()).toBe(false)
    expect(wrapper.find('.diagnosis-card--intervention').exists()).toBe(false)
    expect(card.text()).toContain('等待下一轮巡检')
    expect(card.text()).not.toContain('缺失')
    wrapper.unmount()
  })

  it('从未巡检（has_scan=false）→ waiting 卡片，展开后显示「尚未巡检」且不出现缺失/失败字样', async () => {
    ;(detail as Record<string, unknown>).diagnosis = {
      ...baseDiag(),
      has_scan: false,
      scan_started_at: null,
      scan_finished_at: null,
      scan_status: null,
      skip_reason: null,
      skip_reason_text: null,
      baseline_source: null,
      intervention: { level: 'waiting', label: '等待下一轮巡检', reason: '等待下一轮巡检' },
    }
    const wrapper = mountView()
    await flushPromises()

    const card = wrapper.find('.diagnosis-card')
    expect(card.exists()).toBe(true)
    // waiting 折叠：空态在详情区内，需展开查看（与 MediaDiagnosisCard.test.ts 一致）
    expect(wrapper.find('.diag-detail').exists()).toBe(false)
    await wrapper.find('.diag-summary').trigger('click')

    const cardText = card.text()
    expect(cardText).toContain('尚未巡检')
    expect(cardText).not.toContain('缺失')
    expect(cardText).not.toContain('失败')
    wrapper.unmount()
  })
})

// 5.1（comet task 0a356367）：集数表三列在三种行形态下的输出。
// 覆写 el-table-column stub，用测试指定的行渲染 scoped slot（默认 stub 的行固定）。
describe('MediaDetailView 集数表尝试三列（media-detail-update-diagnosis 5.1）', () => {
  type Row = Record<string, unknown>

  // el-table stub 未声明 props → 真实行数据在 $attrs.data；按它渲染 scoped slot，
  // 从而走真实的 episode_state → __tag → 三列格式化链路（默认 stub 的行是写死的）。
  function mountWithRow(row: Row) {
    detail.episode_state = [{ ...row } as unknown as TestEpisodeState]
    const stubs = {
      ...EP_STUBS,
      'el-table-column': {
        template: '<div><slot v-for="r in ($parent.$attrs.data || [])" :row="r" /></div>',
      },
    }
    return mount(MediaDetailView, {
      global: { stubs, directives: { loading: () => {} } },
      attachTo: document.body,
    })
  }

  beforeEach(() => {
    document.body.innerHTML = ''
    detail.episode_state = [{ id: 1, episode: 'S01E001', season: 1, episode_number: 1, name: '第一集' }]
  })

  afterEach(() => {
    detail.episode_state = [
      { id: 1, episode: 'S01E001', season: 1, episode_number: 1, name: '第一集' },
      { id: 2, episode: 'S01E002', season: 1, episode_number: 2 },
    ]
  })

  it('有尝试数据 → 最近尝试时间 + 尝试次数 + 失败原因', async () => {
    const attemptAt = '2026-10-20T10:00:00Z'
    const wrapper = mountWithRow({
      season: 1,
      episode_number: 1,
      episode: 'S01E001',
      name: '第一集',
      state: 'error',
      retry_count: 7,
      last_attempt_at: attemptAt,
      error: '源站无可用分享',
      size_gb: null,
      updated_at: null,
    })
    await flushPromises()
    const text = wrapper.text()
    expect(text).toContain(formatTime(attemptAt)) // 最近尝试时间（同一格式化工具）
    expect(text).toContain('7') // 尝试次数
    expect(text).toContain('源站无可用分享') // 失败原因
    expect(text).not.toContain('尚未尝试下载')
    wrapper.unmount()
  })

  it('无本地行 → 三列均为空态（尚未尝试下载 / —），不显示为失败', async () => {
    const wrapper = mountWithRow({
      season: 1,
      episode_number: 2,
      episode: 'S01E002',
      name: '第二集',
      state: 'not_aired',
      retry_count: null,
      last_attempt_at: null,
      error: null,
      size_gb: null,
      updated_at: null,
    })
    await flushPromises()
    const text = wrapper.text()
    expect(text).toContain('尚未尝试下载')
    expect(text).not.toContain('失败')
    // 时间列与次数列回退 —
    expect(text.match(/—/g)?.length ?? 0).toBeGreaterThanOrEqual(2)
    wrapper.unmount()
  })

  it('超时回退行（有 error / retry_count，无 last_attempt_at）→ 照常显示失败原因，不误判为尚未尝试', async () => {
    const wrapper = mountWithRow({
      season: 1,
      episode_number: 1,
      episode: 'S01E001',
      name: '第一集',
      state: 'pending',
      retry_count: 3,
      last_attempt_at: null,
      error: '超时回退（2h 无进展）',
      size_gb: null,
      updated_at: null,
    })
    await flushPromises()
    const text = wrapper.text()
    expect(text).toContain('超时回退（2h 无进展）')
    expect(text).not.toContain('尚未尝试下载')
    expect(text).toContain('3')
    wrapper.unmount()
  })

  it('in_library → 不渲染历史失败原因（仍显示尝试时间与次数）', async () => {
    const attemptAt = '2026-10-20T09:00:00Z'
    const wrapper = mountWithRow({
      season: 1,
      episode_number: 1,
      episode: 'S01E001',
      name: '第一集',
      state: 'in_library',
      retry_count: 3,
      last_attempt_at: attemptAt,
      error: '历史失败原因不应展示',
      size_gb: 1.5,
      updated_at: attemptAt,
    })
    await flushPromises()
    const text = wrapper.text()
    expect(text).toContain('已在库')
    expect(text).toContain(formatTime(attemptAt))
    expect(text).not.toContain('历史失败原因不应展示')
    wrapper.unmount()
  })
})
