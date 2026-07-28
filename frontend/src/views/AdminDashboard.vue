<template>
  <div class="page">
    <NavBar active="/admin" />

    <div class="content">
      <h2 class="section-title">订阅审批</h2>
      <el-table :data="pendingList" v-loading="loading" stripe>
        <el-table-column label="影视名称">
          <template #default="{ row }">
            <span>{{ row.media?.title || row.media?.name || row.title || row.name }}</span>
          </template>
        </el-table-column>
        <el-table-column label="订阅者">
          <template #default="{ row }">
            <span>{{ row.subscriber?.username || row.user?.username || row.username || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="vote_count" label="票数" width="120" />
        <el-table-column label="操作" width="220">
          <template #default="{ row }">
            <el-button size="small" type="success" :loading="row.approving" @click="approve(row)">通过</el-button>
            <el-button size="small" type="danger" :loading="row.deleting" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { adminAPI } from '../api'
import { useNotificationStore } from '../stores/notification'
import NavBar from '../components/NavBar.vue'

const notificationStore = useNotificationStore()
const pendingList = ref<any[]>([])
const loading = ref(false)

onMounted(() => {
  notificationStore.fetchUnreadCount()
  loadPending()
})

async function loadPending() {
  loading.value = true
  try {
    const { data } = await adminAPI.subscriptions()
    pendingList.value = (data || []).map((r: any) => ({ ...r, approving: false, deleting: false }))
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '加载审批列表失败')
  } finally {
    loading.value = false
  }
}

async function approve(row: any) {
  row.approving = true
  try {
    await adminAPI.approve(row.media_id || row.media?.id)
    ElMessage.success('已通过')
    await loadPending()
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '操作失败')
  } finally {
    row.approving = false
  }
}

async function remove(row: any) {
  row.deleting = true
  try {
    await adminAPI.deleteMedia(row.media_id || row.media?.id)
    ElMessage.success('已删除')
    await loadPending()
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '操作失败')
  } finally {
    row.deleting = false
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
