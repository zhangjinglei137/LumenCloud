// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import LoginView from './LoginView.vue'

// 可变的 route query 与 router.push spy，供每个用例独立设定 redirect
const { pushSpy, routeQuery } = vi.hoisted(() => {
  return {
    pushSpy: vi.fn(),
    routeQuery: { redirect: undefined } as { redirect?: unknown },
  }
})

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: pushSpy }),
  useRoute: () => ({ query: routeQuery }),
}))

vi.mock('../stores/auth', () => ({
  useAuthStore: () => ({
    login: vi.fn().mockResolvedValue(undefined),
  }),
}))

vi.mock('element-plus', () => ({
  ElMessage: { success: vi.fn() },
}))

// Element Plus 组件 stub：jsdom 下无需真实渲染；el-form 提供 validate 供 submit 流程通过
const EP_STUBS = {
  'el-form': {
    template: '<form @submit.prevent><slot /></form>',
    methods: { validate: () => Promise.resolve(true) },
  },
  'el-form-item': { template: '<div><slot /></div>' },
  'el-input': { template: '<input />' },
  'el-button': { template: '<button @click="$emit(\'click\')"><slot /></button>' },
  'router-link': { template: '<a><slot /></a>' },
}

async function submitLogin() {
  const wrapper = mount(LoginView, { global: { stubs: EP_STUBS } })
  await wrapper.find('button').trigger('click')
  await flushPromises()
  return wrapper
}

describe('LoginView 登录跳转 redirect 白名单（fix-audit-issues / B5）', () => {
  beforeEach(() => {
    pushSpy.mockClear()
    routeQuery.redirect = undefined
    document.body.innerHTML = ''
  })

  it('redirect 为外部 URL 时回退默认首页', async () => {
    routeQuery.redirect = 'https://evil.com'
    await submitLogin()
    expect(pushSpy).toHaveBeenCalledWith('/')
  })

  it('redirect 为协议相对地址时回退默认首页', async () => {
    routeQuery.redirect = '//evil.com'
    await submitLogin()
    expect(pushSpy).toHaveBeenCalledWith('/')
  })

  it('redirect 为合法站内相对路径时保留', async () => {
    routeQuery.redirect = '/media/1'
    await submitLogin()
    expect(pushSpy).toHaveBeenCalledWith('/media/1')
  })

  it('redirect 为 javascript: 伪协议时回退默认首页', async () => {
    routeQuery.redirect = 'javascript:alert(1)'
    await submitLogin()
    expect(pushSpy).toHaveBeenCalledWith('/')
  })

  it('redirect 缺失或非字符串时回退默认首页', async () => {
    await submitLogin()
    expect(pushSpy).toHaveBeenCalledWith('/')

    routeQuery.redirect = 12345
    pushSpy.mockClear()
    await submitLogin()
    expect(pushSpy).toHaveBeenCalledWith('/')
  })
})
