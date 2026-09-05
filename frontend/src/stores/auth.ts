import { defineStore } from 'pinia'
import { fetchMeApi, loginApi, registerApi } from '../api'
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
