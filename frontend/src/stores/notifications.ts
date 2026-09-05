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
