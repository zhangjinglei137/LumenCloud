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

export function toLogin(): void {
  setToken(null)
  const current = window.location.pathname + window.location.search
  if (!window.location.pathname.startsWith('/login')) {
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
