import { defineStore } from 'pinia'
import { listLogsApi } from '../api'
import type { LogItem } from '../types'

export interface LogFilter {
  task_type?: string
  status?: string
  media_id?: number
  tmdb_id?: number
  title?: string
}

export const useLogsStore = defineStore('logs', {
  state: () => ({
    items: [] as LogItem[],
    loading: false,
    page: 1,
    pageSize: 30,
    hasMore: false,
  }),
  actions: {
    async fetchPage(filter: LogFilter, append = false): Promise<void> {
      this.loading = true
      try {
        const offset = append ? this.items.length : 0
        const data = await listLogsApi({ ...filter, limit: this.pageSize, offset })
        this.items = append ? [...this.items, ...data] : data
        this.hasMore = data.length >= this.pageSize
      } finally {
        this.loading = false
      }
    },
  },
})
