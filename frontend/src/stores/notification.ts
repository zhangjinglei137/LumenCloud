import { defineStore } from 'pinia'
import { ref } from 'vue'
import { notificationAPI } from '../api'

export const useNotificationStore = defineStore('notification', () => {
  const unreadCount = ref(0)

  async function fetchUnreadCount() {
    try {
      const { data } = await notificationAPI.list(true)
      if (Array.isArray(data)) {
        unreadCount.value = data.length
      } else {
        unreadCount.value = data?.unread_count || 0
      }
    } catch {
      unreadCount.value = 0
    }
  }

  return { unreadCount, fetchUnreadCount }
})
