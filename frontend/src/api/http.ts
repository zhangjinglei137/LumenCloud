import axios, { AxiosError } from 'axios'
import { ElMessage } from 'element-plus'

/** 统一 axios 实例：baseURL=/api，请求自动携带 Bearer token，401 跳登录 */
const http = axios.create({
  baseURL: '/api',
  timeout: 30000,
})

const TOKEN_KEY = 'lumencloud_token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string | null): void {
  if (token) {
    localStorage.setItem(TOKEN_KEY, token)
  } else {
    localStorage.removeItem(TOKEN_KEY)
  }
}

/**
 * 重定向循环防护判定：当前路径已是 /login（含带 query 的登录页）时不再跳转，
 * 避免 401 → /login →（登录页接口又 401）→ /login 死循环。
 * 纯函数抽取（9.7）：可单测，行为与内联判定一致。
 */
export function isLoginPathname(pathname: string): boolean {
  return pathname.startsWith('/login')
}

export function toLogin(): void {
  setToken(null)
  const current = window.location.pathname + window.location.search
  // 现状取舍：整页跳转（window.location.href）全量刷新，保留 SPA 外状态迁移的简单性；
  // 代价是会丢失当前路由/组件的内存状态。将来可改为 router.replace 保留 SPA 状态，
  // 届时需同步处理跳转后的 redirect 透传与登录态刷新逻辑。
  if (!isLoginPathname(window.location.pathname)) {
    window.location.href = `/login?redirect=${encodeURIComponent(current)}`
  }
}

http.interceptors.request.use((config) => {
  const token = getToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

http.interceptors.response.use(
  (response) => response,
  (error: AxiosError<{ detail?: string | { msg?: string } }>) => {
    if (error.response?.status === 401) {
      toLogin()
      return Promise.reject(error)
    }
    // 统一错误提示（调用方可通过静默方式自行处理）
    const detail = error.response?.data?.detail
    let msg = error.message || '请求失败'
    if (typeof detail === 'string') {
      msg = detail
    } else if (detail && typeof detail === 'object' && detail.msg) {
      msg = detail.msg
    }
    ElMessage.error(msg)
    return Promise.reject(error)
  },
)

export default http
