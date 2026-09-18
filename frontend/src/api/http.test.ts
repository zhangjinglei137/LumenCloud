// @vitest-environment jsdom
// http 拦截器与登录跳转单测（fix-audit-issues 9.7）：
// - 401 拦截器触发 toLogin：清 token + 整页跳登录（携带 redirect）
// - 重定向循环防护：已在 /login 页面时 401 不再跳转（避免 401→/login→401 死循环）
// - 非 401：统一错误提示（detail 字符串/msg 对象）+ 继续 reject
// - isLoginPathname：循环防护判定纯函数（为可测性从 toLogin 抽取，行为不变）
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { AxiosError } from 'axios'
import http, { getToken, isLoginPathname, setToken } from './http'

vi.mock('element-plus', () => ({
  ElMessage: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}))
import { ElMessage } from 'element-plus'

/** 取响应拦截器的 rejected 处理器（运行时 axios 实例结构） */
function getOnRejected(): (err: AxiosError) => Promise<never> {
  const handlers = (
    http.interceptors.response as unknown as {
      handlers: Array<{ fulfilled?: unknown; rejected?: (e: AxiosError) => Promise<never> }>
    }
  ).handlers
  const rejected = handlers[0]?.rejected
  if (!rejected) throw new Error('响应拦截器未注册 rejected 处理器')
  return rejected
}

/** 构造最小 AxiosError（带 status/data 的 response） */
function makeError(status: number, detail?: string): AxiosError {
  const response = {
    data: detail !== undefined ? { detail } : {},
    status,
    statusText: String(status),
    headers: {},
    config: {} as never,
  }
  return new AxiosError('Request failed', String(status), undefined, undefined, response)
}

// 可控 location mock：断言 toLogin 的整页跳转副作用（jsdom 中 href 赋值不可直接导航）
const locationMock = { pathname: '/queue', search: '', href: '' }

const onRejected = getOnRejected()

beforeEach(() => {
  Object.defineProperty(window, 'location', { value: locationMock, writable: true })
  locationMock.pathname = '/queue'
  locationMock.search = ''
  locationMock.href = ''
  localStorage.clear()
  vi.clearAllMocks()
})

describe('http 401 拦截器与登录跳转（fix-audit-issues 9.7）', () => {
  it('401 响应：清 token 并整页跳转登录（携带 redirect 回跳）', async () => {
    setToken('tok')
    const err = makeError(401)
    await expect(onRejected(err)).rejects.toBe(err) // 仍以 reject 透传
    expect(getToken()).toBeNull()
    expect(locationMock.href).toBe('/login?redirect=' + encodeURIComponent('/queue'))
  })

  it('已在登录页时 401 不再跳转（重定向循环防护），但仍清 token', async () => {
    locationMock.pathname = '/login'
    setToken('tok')
    const err = makeError(401)
    await expect(onRejected(err)).rejects.toBe(err)
    expect(locationMock.href).toBe('') // 未再次跳转
    expect(getToken()).toBeNull()
  })

  it('非 401：detail 字符串 → 统一错误提示并继续 reject', async () => {
    const err = makeError(503, '上游故障')
    await expect(onRejected(err)).rejects.toBe(err)
    expect(ElMessage.error).toHaveBeenCalledWith('上游故障')
    expect(locationMock.href).toBe('') // 不跳登录
  })

  it('isLoginPathname：/login 前缀判定（重定向循环防护纯函数）', () => {
    expect(isLoginPathname('/login')).toBe(true)
    expect(isLoginPathname('/login?redirect=%2Fqueue')).toBe(true)
    expect(isLoginPathname('/queue')).toBe(false)
    expect(isLoginPathname('')).toBe(false)
  })
})
