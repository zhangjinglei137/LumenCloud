import { defineStore } from 'pinia'
import {
  listNotificationsApi,
  markAllNotificationsReadApi,
  markNotificationReadApi,
} from '../api'
import type { NotificationItem } from '../types'

export const useNotificationsStore = defineStore('notifications', {
  state: () => ({
    items: [] as NotificationItem[],
    unreadCount: 0,
  }),
  actions: {
    /**
     * 拉取站内通知列表（分页契约：GET /api/notifications?limit&offset → {items, total, unread_count}，
     * 与 /api/logs 同形状）。铃铛只展示最新通知，走后端缺省 limit=50——列表最多 50 条，
     * unreadCount 由后端全量独立统计，分页不影响未读计数。
     */
    async fetchList(): Promise<void> {
      try {
        const data = await listNotificationsApi()
        this.items = data.items
        this.unreadCount = data.unread_count
      } catch {
        // 通知失败静默，不打扰主流程
      }
    },
    async markRead(id: number): Promise<void> {
      await markNotificationReadApi(id)
      const item = this.items.find((i) => i.id === id)
      if (item) item.read = true
      this.unreadCount = Math.max(0, this.unreadCount - 1)
    },
    async markAllRead(): Promise<void> {
      await markAllNotificationsReadApi()
      for (const i of this.items) i.read = true
      this.unreadCount = 0
    },
  },
})
