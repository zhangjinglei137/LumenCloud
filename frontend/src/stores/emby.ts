import { defineStore } from 'pinia'
import { listEmbyLibrariesApi, listEmbyLibraryApi } from '../api'
import type { EmbyLibraryFolder, EmbyLibraryItem, EmbyLibraryQuery } from '../types'

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
    libraries: [] as EmbyLibraryFolder[],
    librariesLoading: false,
    librariesError: null as EmbyErrorCode,
  }),
  getters: {
    /** 分类 → 媒体库列表（电影/剧集/动漫/全部） */
    libraryGroups(state): Record<'movie' | 'series' | 'anime' | 'all', EmbyLibraryFolder[]> {
      const groups = { movie: [], series: [], anime: [], all: [] } as Record<string, EmbyLibraryFolder[]>
      for (const lib of state.libraries) {
        if (lib.collection_type === 'movies') groups.movie.push(lib)
        else if (lib.collection_type === 'tvshows' && lib.is_anime) groups.anime.push(lib)
        else if (lib.collection_type === 'tvshows') groups.series.push(lib)
        else groups.all.push(lib) // mixed / null
      }
      return groups as Record<'movie' | 'series' | 'anime' | 'all', EmbyLibraryFolder[]>
    },
  },
  actions: {
    async fetchLibraries(): Promise<void> {
      this.librariesLoading = true
      try {
        const res = await listEmbyLibrariesApi()
        this.libraries = res.libraries ?? []
        this.librariesError = null
      } catch (err) {
        this.libraries = []
        this.librariesError = parseEmbyErrorCode(err)
      } finally {
        this.librariesLoading = false
      }
    },
    async fetchLibrary(params: EmbyLibraryQuery): Promise<void> {
      this.loading = true
      try {
        const res = await listEmbyLibraryApi(params)
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
