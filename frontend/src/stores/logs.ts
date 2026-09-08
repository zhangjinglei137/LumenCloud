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
    /**
     * 估算总条数：后端 /logs 不返回 total，按「多取 1 条探测是否还有下一页」推算，
     * 仅供 el-pagination 页码展示（保证当前页之后可继续后翻；翻到底自动回退修正）。
     */
    total: 0,
  }),
  actions: {
    /**
     * 标准分页拉取（替换旧的 loadMore 无限滚动）。
     * limit 多取 1 条：仅作「下一页是否存在」探测，展示时截掉，避免依赖后端 total。
     */
    async fetchPage(filter: LogFilter = {}, page = 1, pageSize?: number): Promise<void> {
      this.loading = true
      try {
        const size = pageSize ?? this.pageSize
        const offset = (page - 1) * size
        const data = await listLogsApi({ ...filter, limit: size + 1, offset })
        if (data.length === 0 && page > 1) {
          // 翻到空页（恰好到底、估算 total 多报一页）：回退一页重取
          await this.fetchPage(filter, page - 1, size)
          return
        }
        const hasMore = data.length > size
        this.items = hasMore ? data.slice(0, size) : data
        this.page = page
        this.pageSize = size
        this.total = hasMore ? offset + size + 1 : offset + this.items.length
      } finally {
        this.loading = false
      }
    },
  },
})
