<template>
  <div class="page">
    <NavBar active="/notifications" />

    <div class="content">
      <div class="toolbar">
        <h2 class="section-title">通知中心</h2>
        <el-button type="primary" :loading="markLoading" @click="markAllRead">全部已读</el-button>
      </div>

      <el-timeline v-if="notifications.length > 0">
        <el-timeline-item
          v-for="n in notifications"
          :key="n.id"
          :type="n.is_read ? 'info' : 'primary'"
          :timestamp="n.created_at"
          :hollow="n.is_read"
        >
          <el-card :class="['notice-card', { unread: !n.is_read }]">
            <p>{{ n.message }}</p>
            <el-button v-if="!n.is_read" size="small" type="primary" text @click="markRead(n.id)">标为已读</el-button>
          </el-card>
        </el-timeline-item>
      </el-timeline>

      <el-empty v-else description="暂无通知" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { notificationAPI } from '../api'
import { useNotificationStore } from '../stores/notification'
import NavBar from '../components/NavBar.vue'

const notificationStore = useNotificationStore()
const notifications = ref<any[]>([])
const markLoading = ref(false)

onMounted(() => {
  loadNotifications()
})

async function loadNotifications() {
  try {
    const { data } = await notificationAPI.list()
    notifications.value = data || []
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '加载通知失败')
  }
}

async function markRead(id: string | number) {
  try {
    await notificationAPI.markRead(id)
    ElMessage.success('已标为已读')
    await loadNotifications()
    notificationStore.fetchUnreadCount()
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '操作失败')
  }
}

async function markAllRead() {
  markLoading.value = true
  try {
    await notificationAPI.markAllRead()
    ElMessage.success('全部已读')
    await loadNotifications()
    notificationStore.fetchUnreadCount()
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '操作失败')
  } finally {
    markLoading.value = false
  }
}
</script>

<style scoped>
.page {
  min-height: 100vh;
  background: #1a1a2e;
}
.content {
  max-width: 1400px;
  margin: 0 auto;
  padding: 32px 24px;
}
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 24px;
}
.section-title {
  color: #fff;
  font-size: 24px;
  margin: 0;
}
.notice-card {
  border-radius: 8px;
}
.notice-card.unread {
  background: #f0f7ff;
  border-left: 4px solid #409eff;
}
</style>
