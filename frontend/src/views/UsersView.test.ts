// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { computed, inject, provide, reactive } from 'vue'
import UsersView from './UsersView.vue'
import type { UserItem } from '../types'

// ---------- 可变 store mock（reactive：注入数据后模板可响应更新） ----------
const storeState = reactive({
  items: [] as UserItem[],
  loading: false,
  fetchList: vi.fn(),
  remove: vi.fn(),
})

vi.mock('../stores/users', () => ({
  useUsersStore: () => storeState,
}))

const authState = reactive({ user: null as { id: number } | null })

vi.mock('../stores/auth', () => ({
  useAuthStore: () => authState,
}))

function makeUser(over: Partial<UserItem> = {}): UserItem {
  return {
    id: 1,
    username: '测试用户',
    role: 'guest',
    invite_code: 'ABC123',
    created_at: '2026-01-01T00:00:00Z',
    ...over,
  }
}

// ---------- Element Plus 组件 stub（沿用 QueueView/embyLibraryView 先例） ----------
const TABLE_ROWS = Symbol('table-rows')

const EP_STUBS = {
  'el-button': { template: '<button type="button"><slot /></button>', props: ['loading', 'disabled', 'link', 'type'] },
  'el-tooltip': { template: '<span><slot /></span>', props: ['disabled', 'content'] },
  'el-tag': { template: '<span><slot /></span>', props: ['type'] },
  'el-icon': { template: '<span><slot /></span>' },
  'el-empty': { template: '<div><slot /></div>' },
  // el-select stub：渲染可识别的 <select class="el-select">，用于断言「角色列不再出现下拉框」
  'el-select': { template: '<select class="el-select"><slot /></select>', props: ['modelValue'] },
  'el-option': { template: '<option />', props: ['value', 'label'] },
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
    template: '<div class="el-pagination" :data-total="total"></div>',
  },
  Refresh: { template: '<span class="icon-refresh" />' },
}

let wrapper: ReturnType<typeof mount> | undefined

function mountView() {
  return mount(UsersView, {
    global: { stubs: EP_STUBS, directives: { loading: () => {} } },
    attachTo: document.body,
  })
}

beforeEach(() => {
  document.body.innerHTML = ''
  vi.clearAllMocks()
  storeState.items = []
  storeState.loading = false
  authState.user = null
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = undefined
})

describe('UsersView 角色只读展示（fix-online-issues）', () => {
  it('角色列只读展示：任意行均不渲染 el-select 下拉框，以 tag 文案展示角色', async () => {
    storeState.items = [
      makeUser({ id: 1, username: 'admin1', role: 'admin' }),
      makeUser({ id: 2, username: 'guest1', role: 'guest' }),
    ]
    wrapper = mountView()
    await flushPromises()

    // 角色编辑控件（el-select）不得出现
    expect(wrapper.findAll('.el-select')).toHaveLength(0)
    expect(wrapper.html()).not.toContain('el-select')
    // 只读 tag 展示角色文案（roleLabel 映射）
    const text = wrapper.text()
    expect(text).toContain('管理员')
    expect(text).toContain('访客')
  })

  it('顶部说明文案不再提示「调整角色」', async () => {
    storeState.items = [makeUser({ id: 1 })]
    wrapper = mountView()
    await flushPromises()
    expect(wrapper.text()).not.toContain('调整角色立即生效')
  })

  it('删除入口保留（未误删）', async () => {
    storeState.items = [makeUser({ id: 2, role: 'guest' })]
    wrapper = mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('删除')
  })
})
