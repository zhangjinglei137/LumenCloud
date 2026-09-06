import { defineStore } from 'pinia'
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
        this.logout()
        return false
      }
    },
    logout(): void {
      setToken(null)
      this.token = null
      this.user = null
    },
  },
})
