import { defineStore } from 'pinia'
import { listEmbyLibraryApi } from '../api'
import type { EmbyItemType, EmbyLibraryItem } from '../types'

/** Emby 库拉取失败分类：与后端 503 detail.code 对应 */
export type EmbyErrorCode = 'not_configured' | 'unavailable' | null

interface EmbyErrorShape {
  response?: { data?: { detail?: { code?: string } | string } }
}

/** 从 axios 错误中解析后端约定的 detail.code */
function parseEmbyErrorCode(err: unknown): EmbyErrorCode {
  const detail = (err as EmbyErrorShape)?.response?.data?.detail
  if (detail && typeof detail === 'object') {
    if (detail.code === 'emby_not_configured') return 'not_configured'
    if (detail.code === 'emby_unreachable') return 'unavailable'
  }
  return 'unavailable'
}

export const useEmbyStore = defineStore('emby', {
  state: () => ({
    items: [] as EmbyLibraryItem[],
    loading: false,
    error: null as EmbyErrorCode,
  }),
  actions: {
    async fetchLibrary(itemType?: EmbyItemType): Promise<void> {
      this.loading = true
      try {
        const res = await listEmbyLibraryApi(itemType)
        this.items = res.items
        this.error = null
      } catch (err) {
        this.items = []
        this.error = parseEmbyErrorCode(err)
        // 全局拦截器已弹出错误提示；视图据 error 呈现对应空态/错误态
      } finally {
        this.loading = false
      }
    },
  },
})
