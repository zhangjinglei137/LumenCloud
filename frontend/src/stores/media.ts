import { defineStore } from 'pinia'
import {
  createMediaApi,
  deleteMediaApi,
  getMediaApi,
  listMediaApi,
  patchMediaApi,
  scanMediaApi,
} from '../api'
import type { MediaDetail, MediaItem, MediaPatch } from '../types'

export const useMediaStore = defineStore('media', {
  state: () => ({
    items: [] as MediaItem[],
    detail: null as MediaDetail | null,
    loading: false,
  }),
  actions: {
    async fetchList(): Promise<void> {
      this.loading = true
      try {
        this.items = await listMediaApi()
      } finally {
        this.loading = false
      }
    },
    async fetchDetail(id: number): Promise<void> {
      this.loading = true
      try {
        this.detail = await getMediaApi(id)
      } finally {
        this.loading = false
      }
    },
    async create(data: {
      title: string
      tmdb_id: number
      media_type: string
      poster_path?: string | null
    }): Promise<MediaItem> {
      return createMediaApi(data)
    },
    async patch(id: number, patch: MediaPatch): Promise<void> {
      await patchMediaApi(id, patch)
    },
    async remove(id: number): Promise<void> {
      await deleteMediaApi(id)
    },
    // E-1：巡检 fire-and-forget，task_run_id 可能为 null（结果轮询运行日志页）
    // accepted 是受理结果：false 表示未建巡检记录（如 media 已删除），调用方不得提示成功
    async scan(id: number): Promise<{ accepted: boolean; runId: number | null }> {
      const r = await scanMediaApi(id)
      return { accepted: r.accepted, runId: r.run_id }
    },
  },
})
