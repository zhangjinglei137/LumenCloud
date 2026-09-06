import { defineStore } from 'pinia'
import { getCapacityApi, listQueueApi, retryQueueItemApi } from '../api'
import type { Capacity, QueueItem } from '../types'

export const useQueueStore = defineStore('queue', {
  state: () => ({
    items: [] as QueueItem[],
    capacity: null as Capacity | null,
    loading: false,
    page: 1,
    pageSize: 20,
    hasMore: false,
  }),
  getters: {
    usagePercent: (s) => {
      if (!s.capacity || !s.capacity.total_gb) return 0
      return Math.min(100, Math.round((s.capacity.used_gb / s.capacity.total_gb) * 100))
    },
  },
  actions: {
    async fetchPage(append = false): Promise<void> {
      this.loading = true
      try {
        const offset = append ? this.items.length : 0
        const data = await listQueueApi(this.pageSize, offset)
        this.items = append ? [...this.items, ...data] : data
        this.hasMore = data.length >= this.pageSize
      } finally {
        this.loading = false
      }
    },
    async fetchCapacity(force = false): Promise<void> {
      try {
        this.capacity = await getCapacityApi(force)
      } catch {
        // 容量接口失败不阻塞队列展示；拦截器已提示
      }
    },
    async retry(id: number): Promise<void> {
      await retryQueueItemApi(id)
    },
  },
})
