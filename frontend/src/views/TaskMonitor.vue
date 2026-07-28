<template>
  <div class="page">
    <NavBar active="/admin/tasks" />

    <div class="content">
      <h2 class="section-title">任务监控</h2>
      <el-table :data="tasks" v-loading="loading" stripe>
        <el-table-column label="影视名称">
          <template #default="{ row }">
            <span>{{ row.media?.title || row.media?.name || row.title || row.media_name || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="episode_range" label="集数范围" width="180" />
        <el-table-column label="状态" width="160">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)">{{ statusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间" />
      </el-table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { taskAPI } from '../api'
import { useNotificationStore } from '../stores/notification'
import NavBar from '../components/NavBar.vue'

const notificationStore = useNotificationStore()
const tasks = ref<any[]>([])
const loading = ref(false)

onMounted(() => {
  notificationStore.fetchUnreadCount()
  loadTasks()
})

async function loadTasks() {
  loading.value = true
  try {
    const { data } = await taskAPI.list()
    tasks.value = data || []
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '加载任务列表失败')
  } finally {
    loading.value = false
  }
}

function statusType(status?: string) {
  switch (status) {
    case 'completed': return 'success'
    case 'running': return 'primary'
    case 'failed': return 'danger'
    case 'pending': return 'warning'
    default: return 'info'
  }
}

function statusText(status?: string) {
  switch (status) {
    case 'completed': return '已完成'
    case 'running': return '进行中'
    case 'failed': return '失败'
    case 'pending': return '等待中'
    default: return status || '未知'
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
.section-title {
  color: #fff;
  font-size: 24px;
  margin-bottom: 24px;
}
</style>
