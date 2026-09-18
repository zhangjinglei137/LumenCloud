// @vitest-environment jsdom
// 路由守卫单测（fix-audit-issues 9.7）：
// - token 缺失 → 跳登录并携带 redirect
// - token 存在但 fetchMe 失败 → 清 token（fetchMe 内部清理登录态）并跳登录
// - requiresAdmin 页：非 admin 拒绝回首页 + 警告提示；admin 放行
// 用真实 router 与真实 auth store（仅 mock 外部 api 与 axios 登出调用），
// 守卫行为与 fetchMe 清理逻辑均得到真实覆盖。
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useAuthStore } from '../stores/auth'
import { getToken, setToken } from '../api/http'
import router from './index'
import type { User } from '../types'

// axios 仅登出调用（auth store logout 的独立 axios.post）；create 供 http.ts 模块加载
const { mockHttp } = vi.hoisted(() => {
  const mockHttp = {
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  }
  return { mockHttp }
})

vi.mock('axios', () => ({
  default: {
    create: vi.fn(() => mockHttp),
    post: vi.fn().mockResolvedValue({ data: {} }),
  },
}))

vi.mock('../api', () => ({
  fetchMeApi: vi.fn(),
  loginApi: vi.fn(),
  registerApi: vi.fn(),
  changePasswordApi: vi.fn(),
}))

vi.mock('element-plus', () => ({
  ElMessage: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}))
import { ElMessage } from 'element-plus'
import { fetchMeApi } from '../api'

function makeUser(role: 'admin' | 'guest'): User {
  return { id: 1, username: 'u', role, created_at: '' } as User
}

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('路由守卫（fix-audit-issues 9.7）', () => {
  it('token 缺失：访问受保护页跳登录并携带 redirect 回跳', async () => {
    const auth = useAuthStore()
    expect(auth.token).toBeNull()
    await router.push('/queue')
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/queue')
  })

  it('token 存在但 fetchMe 失败：清 token 并跳登录（登录态不残留）', async () => {
    const auth = useAuthStore()
    setToken('expired-token')
    auth.token = 'expired-token'
    vi.mocked(fetchMeApi).mockRejectedValue(new Error('401'))

    await router.push('/queue')

    expect(fetchMeApi).toHaveBeenCalled()
    expect(auth.token).toBeNull() // fetchMe 失败已清理登录态
    expect(getToken()).toBeNull()
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/queue')
  })

  it('requiresAdmin 页：非 admin 拒绝并回首页 + 警告提示', async () => {
    const auth = useAuthStore()
    setToken('guest-token')
    auth.token = 'guest-token'
    auth.user = makeUser('guest')

    await router.push('/settings')

    expect(router.currentRoute.value.name).toBe('media-list')
    expect(ElMessage.warning).toHaveBeenCalledWith('该页面仅管理员可访问')
  })

  it('requiresAdmin 页：admin 角色放行', async () => {
    const auth = useAuthStore()
    setToken('admin-token')
    auth.token = 'admin-token'
    auth.user = makeUser('admin')

    await router.push('/settings')

    expect(router.currentRoute.value.name).toBe('settings')
  })
})
