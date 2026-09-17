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
     * 后端返回的真实 total（GET /logs 契约 {items, total}），供 el-pagination 页码展示。
     */
    total: 0,
  }),
  actions: {
    /**
     * 标准分页拉取（替换旧的 loadMore 无限滚动）。
     * 直接消费后端 total：limit 按 pageSize 精确传参，不再 +1 探测下一页。
     */
    async fetchPage(filter: LogFilter = {}, page = 1, pageSize?: number): Promise<void> {
      this.loading = true
      try {
        const size = pageSize ?? this.pageSize
        const offset = (page - 1) * size
        const res = await listLogsApi({ ...filter, limit: size, offset })
        if (res.items.length === 0 && page > 1) {
          // 末页越界（如筛选后数据变少）：回退一页重取
          await this.fetchPage(filter, page - 1, size)
          return
        }
        this.items = res.items
        this.page = page
        this.pageSize = size
        this.total = res.total
      } finally {
        this.loading = false
      }
    },
  },
})
