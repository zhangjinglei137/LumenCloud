<template>
  <div class="page">
    <NavBar active="/" />

    <div class="content">
      <div class="search-bar">
        <el-input
          v-model="keyword"
          placeholder="搜索电影、剧集..."
          size="large"
          clearable
          @keyup.enter="search"
        >
          <template #append>
            <el-button type="primary" :icon="Search" :loading="loading" @click="search">搜索</el-button>
          </template>
        </el-input>
      </div>

      <el-empty v-if="!loading && results.length === 0" description="暂无内容，试试搜索" />

      <el-row :gutter="24" v-loading="loading">
        <el-col
          v-for="item in results"
          :key="item.id"
          :xs="12"
          :sm="8"
          :md="6"
          :lg="4"
          class="card-col"
        >
          <el-card class="media-card" shadow="hover" @click="goDetail(item.id)">
            <img
              v-if="item.poster_path"
              :src="`https://image.tmdb.org/t/p/w300${item.poster_path}`"
              :alt="item.title || item.name"
              class="poster"
              loading="lazy"
            />
            <div v-else class="poster placeholder">暂无海报</div>
            <div class="info">
              <h3 class="name">{{ item.title || item.name }}</h3>
              <div class="meta">
                <el-tag size="small" type="info">{{ item.media_type === 'tv' ? '剧集' : '电影' }}</el-tag>
                <span class="year">{{ formatYear(item.release_date || item.first_air_date) }}</span>
              </div>
              <div class="rating" v-if="item.vote_average">
                <el-icon color="#f7ba2a"><StarFilled /></el-icon>
                <span>{{ item.vote_average.toFixed(1) }}</span>
              </div>
            </div>
          </el-card>
        </el-col>
      </el-row>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Search, StarFilled } from '@element-plus/icons-vue'
import { mediaAPI } from '../api'
import { useNotificationStore } from '../stores/notification'
import NavBar from '../components/NavBar.vue'

const router = useRouter()
const notificationStore = useNotificationStore()

const keyword = ref('')
const results = ref<any[]>([])
const loading = ref(false)

onMounted(async () => {
  notificationStore.fetchUnreadCount()
  await loadMedia()
})

async function loadMedia() {
  loading.value = true
  try {
    const { data } = await mediaAPI.list({ page: 1 })
    results.value = data.results || data.items || data || []
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '加载影视列表失败')
  } finally {
    loading.value = false
  }
}

async function search() {
  if (!keyword.value.trim()) {
    await loadMedia()
    return
  }
  loading.value = true
  try {
    const { data } = await mediaAPI.search(keyword.value, 1)
    results.value = data.results || data.items || data || []
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '搜索失败')
  } finally {
    loading.value = false
  }
}

function goDetail(id: string | number) {
  router.push(`/media/${id}`)
}

function formatYear(dateStr?: string) {
  if (!dateStr) return '未知'
  return dateStr.split('-')[0] || dateStr
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
.search-bar {
  max-width: 600px;
  margin: 0 auto 32px;
}
.card-col {
  margin-bottom: 24px;
}
.media-card {
  cursor: pointer;
  border-radius: 12px;
  overflow: hidden;
  transition: transform 0.2s;
}
.media-card:hover {
  transform: translateY(-4px);
}
.poster {
  width: 100%;
  aspect-ratio: 2/3;
  object-fit: cover;
  display: block;
}
.placeholder {
  width: 100%;
  aspect-ratio: 2/3;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #2a2a3e;
  color: #999;
  font-size: 14px;
}
.info {
  padding: 12px;
}
.name {
  margin: 0 0 8px;
  font-size: 15px;
  color: #1a1a2e;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.year {
  color: #888;
  font-size: 13px;
}
.rating {
  display: flex;
  align-items: center;
  gap: 4px;
  color: #f7ba2a;
  font-size: 14px;
}
</style>
