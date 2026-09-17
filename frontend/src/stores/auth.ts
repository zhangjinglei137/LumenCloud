import { defineStore } from 'pinia'
import axios from 'axios'
import { fetchMeApi, loginApi, registerApi, changePasswordApi } from '../api'
import { getToken, setToken } from '../api/http'
import type { User } from '../types'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: getToken(),
    user: null as User | null,
  }),
  getters: {
    isAdmin: (s) => s.user?.role === 'admin',
    isGuest: (s) => s.user?.role === 'guest',
  },
  actions: {
    async login(username: string, password: string): Promise<void> {
      const data = await loginApi(username, password)
      setToken(data.access_token)
      this.token = data.access_token
      this.user = data.user
    },
    async register(username: string, password: string, inviteCode: string): Promise<void> {
      await registerApi(username, password, inviteCode)
    },
    /** 修改当前用户密码；错误（旧密码错 401 / 校验 422 等）原样抛出由调用方提示 */
    async changePassword(oldPassword: string, newPassword: string): Promise<void> {
      await changePasswordApi({ old_password: oldPassword, new_password: newPassword })
    },
    /** 拉取当前用户；失败（含 401）时清理登录态并返回 false */
    async fetchMe(): Promise<boolean> {
      if (!this.token) return false
      try {
        this.user = await fetchMeApi()
        return true
      } catch {
        await this.logout()
        return false
      }
    },
    /**
     * 登出（Task B4）：先通知后端清除服务端 httpOnly cookie 会话并吊销令牌，
     * 再清前端本地令牌（localStorage + state）。
     *
     * 用独立 axios 而非全局 http 实例：不触发 401 拦截器跳转/统一错误弹窗，
     * 登出接口失败（后端不可达/异常）仅 console.warn，不锁死本地登出——
     * 本地登录态照常清除，用户不会因后端故障被困在已登录状态。
     */
    async logout(): Promise<void> {
      try {
        await axios.post('/api/auth/logout', null, {
          timeout: 30000,
          headers: { Authorization: `Bearer ${getToken() ?? ''}` },
        })
      } catch (err) {
        // 后端故障不阻断本地登出：仅记录告警（cookie 可能残留，下次登录覆盖）
        console.warn('[auth] 登出接口调用失败，本地令牌仍将被清除:', err)
      }
      setToken(null)
      this.token = null
      this.user = null
    },
  },
})
