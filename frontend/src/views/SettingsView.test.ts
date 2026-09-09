// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import SettingsView from './SettingsView.vue'

// mock stores：SettingsView 依赖 settings/auth store
vi.mock('../stores/settings', () => ({
  useSettingsStore: () => ({
    settings: {
      editable_keys: ['alist_base_url', 'alist_token', 'quark_default_folder'],
      // 真实 GET 响应：scan_interval_minutes 已被后端 _RETIRED_EXACT 过滤，故 mock 不含该键
      system_config: {
        alist_base_url: 'http://192.168.1.10:5244',
        alist_token: '',
        quark_default_folder: '',
      },
      services: { alist: true, emby: false },
    },
    // 邀请码 Tab 渲染时访问 store.invites.length（v-if 条件），空数组保证不渲染表格
    invites: [],
    loading: false,
    fetchSettings: vi.fn().mockResolvedValue(undefined),
    fetchInvites: vi.fn().mockResolvedValue(undefined),
    patchConfig: vi.fn().mockResolvedValue(undefined),
    createInvites: vi.fn().mockResolvedValue([]),
    deleteInvite: vi.fn().mockResolvedValue(undefined),
  }),
}))

vi.mock('../stores/auth', () => ({
  useAuthStore: () => ({
    changePassword: vi.fn().mockResolvedValue(undefined),
  }),
}))

// Element Plus 组件 stub：jsdom 下无需真实渲染
const EP_STUBS = {
  'el-tabs': { template: '<div><slot /></div>' },
  'el-tab-pane': { template: '<div><slot /></div>' },
  'el-button': { template: '<button><slot /></button>' },
  'el-input': { template: '<input :value="modelValue" />', props: ['modelValue'] },
  'el-tooltip': { template: '<span><slot /></span>' },
  'el-tag': { template: '<span><slot /></span>' },
  'el-divider': { template: '<div><slot /></div>' },
  'el-icon': { template: '<span><slot /></span>' },
  'el-alert': { template: '<div><slot /></div>' },
  'el-empty': { template: '<div><slot /></div>' },
  'el-link': { template: '<a><slot /></a>' },
  'el-select': { template: '<select><slot /></select>' },
  'el-option': { template: '<option />' },
  'el-input-number': { template: '<input />' },
  'el-form': { template: '<form><slot /></form>' },
  'el-form-item': { template: '<div><slot /></div>' },
  'el-message-box': true,
}

function mountView() {
  return mount(SettingsView, {
    global: { stubs: EP_STUBS },
    attachTo: document.body,
  })
}

describe('SettingsView 服务凭据表单', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })

  it('必填标签渲染为「必填」且不含长文案', () => {
    const wrapper = mountView()
    const html = wrapper.html()
    expect(html).toContain('必填')
    expect(html).not.toContain('否则转存与直链不可用')
    expect(html).not.toContain('必填（')
  })

  it('清除按钮与输入框在同一 .cred-input-row 行内', () => {
    const wrapper = mountView()
    const row = wrapper.find('.cred-input-row')
    expect(row.exists()).toBe(true)
    // 同一行内既有输入框又有清除按钮
    expect(row.find('input').exists()).toBe(true)
    expect(row.text()).toContain('清除')
    // 独立操作行已移除
    expect(wrapper.find('.cred-actions').exists()).toBe(false)
  })

  it('凭据表单不渲染 scan_interval_minutes（editable_keys 不含 + 后端不透传）', () => {
    const wrapper = mountView()
    const html = wrapper.html()
    // 该键不在 editable_keys → 凭据表单无输入行；真实 GET 已被 _RETIRED_EXACT 过滤 → 业务参数区无英文键名
    expect(html).not.toContain('扫描间隔')
    expect(html).not.toContain('scan_interval_minutes')
  })
})