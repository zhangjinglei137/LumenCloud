import { defineStore } from 'pinia'
import {
  createInvitesApi,
  deleteInviteApi,
  getSettingsApi,
  listInvitesApi,
  patchSettingsApi,
} from '../api'
import type { InviteCode, SettingsResponse } from '../types'

export const useSettingsStore = defineStore('settings', {
  state: () => ({
    settings: null as SettingsResponse | null,
    invites: [] as InviteCode[],
    loading: false,
  }),
  actions: {
    async fetchSettings(): Promise<void> {
      this.loading = true
      try {
        this.settings = await getSettingsApi()
      } finally {
        this.loading = false
      }
    },
    async patchConfig(patch: Record<string, unknown>): Promise<void> {
      // 后端 PATCH 只返回 { ok: true }，不能赋给 settings（会清掉配置数据）；
      // 最新配置由调用方按需 fetchSettings() 重新拉取。
      await patchSettingsApi(patch)
    },
    async fetchInvites(): Promise<void> {
      this.invites = await listInvitesApi()
    },
    async createInvites(count = 1): Promise<string[]> {
      const r = await createInvitesApi(count)
      await this.fetchInvites()
      return r.codes
    },
    async deleteInvite(code: string): Promise<void> {
      await deleteInviteApi(code)
      this.invites = this.invites.filter((i) => i.code !== code)
    },
  },
})
