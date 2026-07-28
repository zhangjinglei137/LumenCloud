<template>
  <div class="page">
    <NavBar active="/my-list" />

    <div class="content">
      <el-tabs v-model="activeTab" type="border-card">
        <el-tab-pane label="订阅列表" name="subscriptions">
          <el-table :data="subscriptions" v-loading="loading" stripe>
            <el-table-column label="影视名称">
              <template #default="{ row }">
                <span>{{ row.media?.title || row.media?.name || row.title || row.name }}</span>
              </template>
            </el-table-column>
            <el-table-column label="类型">
              <template #default="{ row }">
                <el-tag size="small">{{ (row.media?.media_type || row.media_type) === 'tv' ? '剧集' : '电影' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="240">
              <template #default="{ row }">
                <el-button size="small" type="danger" @click="unsubscribe(row.media_id || row.media?.id)">取消订阅</el-button>
                <el-button size="small" @click="markNotInterested(row.media_id || row.media?.id)">不想看</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="不想看" name="notInterested">
          <el-table :data="notInterested" v-loading="loading2" stripe>
            <el-table-column label="影视名称">
              <template #default="{ row }">
                <span>{{ row.media?.title || row.media?.name || row.title || row.name }}</span>
              </template>
            </el-table-column>
            <el-table-column label="类型">
              <template #default="{ row }">
                <el-tag size="small">{{ (row.media?.media_type || row.media_type) === 'tv' ? '剧集' : '电影' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="160">
              <template #default="{ row }">
                <el-button size="small" type="primary" @click="markInterested(row.media_id || row.media?.id)">恢复</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { subscriptionAPI, interactionAPI } from '../api'
import { useNotificationStore } from '../stores/notification'
import NavBar from '../components/NavBar.vue'

const notificationStore = useNotificationStore()
const activeTab = ref('subscriptions')
const subscriptions = ref<any[]>([])
const notInterested = ref<any[]>([])
const loading = ref(false)
const loading2 = ref(false)

onMounted(() => {
  notificationStore.fetchUnreadCount()
  loadSubscriptions()
})

watch(activeTab, (tab) => {
  if (tab === 'subscriptions') loadSubscriptions()
  else loadNotInterested()
})

async function loadSubscriptions() {
  loading.value = true
  try {
    const { data } = await subscriptionAPI.list()
    subscriptions.value = data || []
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '加载订阅列表失败')
  } finally {
    loading.value = false
  }
}

async function loadNotInterested() {
  loading2.value = true
  try {
    const { data } = await interactionAPI.listStatus('not_interested')
    notInterested.value = data || []
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '加载不想看列表失败')
  } finally {
    loading2.value = false
  }
}

async function unsubscribe(id: string | number) {
  try {
    await subscriptionAPI.unsubscribe(id)
    ElMessage.success('已取消订阅')
    await loadSubscriptions()
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '操作失败')
  }
}

async function markNotInterested(id: string | number) {
  try {
    await interactionAPI.setStatus(id, 'not_interested')
    ElMessage.success('已标记为不想看')
    await loadSubscriptions()
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '操作失败')
  }
}

async function markInterested(id: string | number) {
  try {
    await interactionAPI.setStatus(id, 'interested')
    ElMessage.success('已恢复')
    await loadNotInterested()
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '操作失败')
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
</style>
