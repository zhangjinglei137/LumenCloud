import { defineStore } from 'pinia'
import { approveApi, createApprovalApi, listApprovalsApi, rejectApi } from '../api'
import type { ApprovalItem } from '../types'

export const useApprovalsStore = defineStore('approvals', {
  state: () => ({
    items: [] as ApprovalItem[],
    loading: false,
  }),
  getters: {
    pending: (s) => s.items.filter((i) => i.status === 'pending'),
    approved: (s) => s.items.filter((i) => i.status === 'approved'),
    rejected: (s) => s.items.filter((i) => i.status === 'rejected'),
  },
  actions: {
    async fetchList(): Promise<void> {
      this.loading = true
      try {
        this.items = await listApprovalsApi()
      } finally {
        this.loading = false
      }
    },
    async create(data: {
      title: string
      tmdb_id: number
      media_type: string
      poster_path?: string | null
    }): Promise<void> {
      await createApprovalApi(data)
      await this.fetchList()
    },
    async approve(id: number): Promise<void> {
      await approveApi(id)
      await this.fetchList()
    },
    async reject(id: number, reason: string): Promise<void> {
      await rejectApi(id, reason)
      await this.fetchList()
    },
  },
})
