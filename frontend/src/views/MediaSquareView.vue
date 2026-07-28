<template>
  <div class="media-square">
    <NavBar active="/" />

    <main class="main">
      <header class="page-header">
        <h1 class="page-title">影视广场</h1>
        <p class="page-subtitle">探索 Emby 媒体库，发现下一部想看的好片</p>
      </header>

      <section class="controls">
        <div class="search-bar">
          <el-input
            v-model="keyword"
            placeholder="搜索影视..."
            clearable
            size="large"
            @keyup.enter="doSearch"
            @clear="clearSearch"
          >
            <template #append>
              <el-button :loading="loading && viewMode === 'search'" @click="doSearch">
                <el-icon><Search /></el-icon>
              </el-button>
            </template>
          </el-input>
        </div>

        <el-radio-group
          v-if="viewMode === 'library'"
          v-model="filterType"
          class="filter-bar"
          @change="onFilterChange"
        >
          <el-radio-button value="all">全部</el-radio-button>
          <el-radio-button value="movie">电影</el-radio-button>
          <el-radio-button value="tv">剧集</el-radio-button>
        </el-radio-group>
      </section>

      <div class="mode-hint">
        <span v-if="viewMode === 'search'" class="search-hint">
          <el-icon><Search /></el-icon>
          <span>“{{ lastKeyword }}” 的搜索结果</span>
          <el-button text type="primary" size="small" @click.stop="clearSearch">返回库</el-button>
        </span>
        <span v-else class="library-hint">
          {{ filterType === 'all' ? '全部影视' : filterType === 'movie' ? '电影库' : '剧集库' }}
          <em v-if="total > 0"> · {{ total }} 部</em>
        </span>
      </div>

      <el-skeleton
        v-if="loading && items.length === 0"
        :rows="8"
        animated
        class="skeleton"
      />

      <el-row v-else :gutter="20" class="card-grid">
        <el-col
          v-for="(item, index) in items"
          :key="item.emby_id || item.tmdb_id"
          :xs="12"
          :sm="8"
          :md="6"
          :lg="4"
          :xl="3"
          class="card-col"
          :style="{ '--i': index }"
        >
          <el-card shadow="never" class="media-card" @click="goDetail(item)">
            <div class="poster-wrap">
              <img
                v-if="getPosterUrl(item)"
                :src="getPosterUrl(item)"
                :alt="item.title"
                class="poster"
                loading="lazy"
              />
              <div v-else class="poster-placeholder">
                <el-icon :size="40"><VideoCamera /></el-icon>
              </div>
              <div class="poster-overlay">
                <el-icon class="play-icon"><VideoPlay /></el-icon>
              </div>
            </div>
            <div class="info">
              <h3 class="title" :title="item.title">{{ item.title }}</h3>
              <div class="meta">
                <el-tag
                  size="small"
                  :type="item.type === 'movie' ? 'primary' : 'success'"
                  effect="dark"
                >
                  {{ item.type === 'movie' ? '电影' : '剧集' }}
                </el-tag>
                <span v-if="item.year" class="year">{{ item.year }}</span>
              </div>
              <div class="stats">
                <span v-if="item.play_count" class="stat" title="播放次数">
                  <el-icon><View /></el-icon>
                  {{ item.play_count }}
                </span>
                <span v-if="item.subscription_count" class="stat" title="订阅人数">
                  <el-icon><User /></el-icon>
                  {{ item.subscription_count }}
                </span>
                <span v-if="item.community_rating" class="stat rating" title="评分">
                  <el-icon><StarFilled /></el-icon>
                  {{ item.community_rating.toFixed(1) }}
                </span>
              </div>
            </div>
          </el-card>
        </el-col>
      </el-row>

      <div v-if="viewMode === 'library' && total > 0" class="pagination">
        <el-pagination
          v-model:current-page="page"
          :page-size="24"
          :total="total"
          layout="prev, pager, next"
          @current-change="loadLibrary"
        />
      </div>

      <el-empty
        v-if="!loading && items.length === 0"
        :description="viewMode === 'search' ? '未搜索到相关影视' : '暂无影视内容'"
      />
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Search, VideoCamera, VideoPlay, View, User, StarFilled } from '@element-plus/icons-vue'
import api from '../api'
import NavBar from '../components/NavBar.vue'

const router = useRouter()

const keyword = ref('')
const lastKeyword = ref('')
const items = ref<any[]>([])
const loading = ref(false)
const page = ref(1)
const total = ref(0)
const filterType = ref<'all' | 'movie' | 'tv'>('all')
const viewMode = ref<'library' | 'search'>('library')

// ponytail: hardcode Emby base for now; replace with config endpoint when available
const EMBY_BASE_URL = 'http://thntime.fun:8096'

onMounted(() => {
  loadLibrary()
})

async function loadLibrary() {
  loading.value = true
  viewMode.value = 'library'
  try {
    const { data } = await api.get('/media/library', {
      params: { page: page.value, item_type: filterType.value },
    })
    items.value = data.items || []
    total.value = data.total || 0
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '加载影视库失败')
    items.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

async function doSearch() {
  const trimmed = keyword.value.trim()
  if (!trimmed) {
    clearSearch()
    return
  }
  loading.value = true
  viewMode.value = 'search'
  lastKeyword.value = trimmed
  try {
    const { data } = await api.get('/media/search', {
      params: { keyword: trimmed, page: 1 },
    })
    items.value = (data.results || []).map((r: any) => ({
      emby_id: null,
      tmdb_id: r.tmdb_id,
      title: r.title,
      type: r.media_type,
      year: r.release_date ? r.release_date.slice(0, 4) : null,
      poster_path: r.poster_path,
      play_count: 0,
      subscription_count: 0,
      community_rating: r.vote_average,
    }))
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '搜索失败')
    items.value = []
  } finally {
    loading.value = false
  }
}

function clearSearch() {
  keyword.value = ''
  lastKeyword.value = ''
  page.value = 1
  loadLibrary()
}

function onFilterChange() {
  page.value = 1
  loadLibrary()
}

function getPosterUrl(item: any): string {
  if (item.poster_path) {
    return `https://image.tmdb.org/t/p/w300${item.poster_path}`
  }
  if (item.emby_id && item.image_tag) {
    return `${EMBY_BASE_URL}/emby/Items/${item.emby_id}/Images/Primary?maxHeight=450&tag=${item.image_tag}&quality=90`
  }
  return ''
}

function goDetail(item: any) {
  const id = item.local_media_id || item.tmdb_id || item.emby_id
  if (id) {
    router.push(`/media/${id}`)
  }
}
</script>

<style scoped>
.media-square {
  min-height: 100vh;
  background:
    radial-gradient(ellipse at 20% 0%, rgba(46, 58, 110, 0.22) 0%, transparent 45%),
    radial-gradient(ellipse at 80% 100%, rgba(30, 40, 80, 0.18) 0%, transparent 40%),
    #0f0f1a;
  color: #e8e8f0;
}

.main {
  max-width: 1440px;
  margin: 0 auto;
  padding: 32px 24px 64px;
}

.page-header {
  text-align: center;
  margin-bottom: 32px;
}

.page-title {
  margin: 0 0 8px;
  font-size: 32px;
  font-weight: 700;
  letter-spacing: -0.02em;
  background: linear-gradient(90deg, #ffffff 0%, #b8c3e8 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
}

.page-subtitle {
  margin: 0;
  color: #9396b0;
  font-size: 15px;
}

.controls {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 18px;
  margin-bottom: 18px;
}

.search-bar {
  width: 100%;
  max-width: 620px;
}

.search-bar :deep(.el-input__wrapper) {
  background: rgba(255, 255, 255, 0.06);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.08), 0 4px 20px rgba(0, 0, 0, 0.18);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 12px 0 0 12px;
}

.search-bar :deep(.el-input__inner) {
  color: #f0f0f5;
}

.search-bar :deep(.el-input__inner::placeholder) {
  color: #6e7292;
}

.search-bar :deep(.el-input-group__append) {
  background: rgba(64, 158, 255, 0.16);
  border: 1px solid rgba(64, 158, 255, 0.28);
  border-left: none;
  border-radius: 0 12px 12px 0;
  color: #7ec1ff;
  padding: 0 16px;
}

.filter-bar :deep(.el-radio-button__inner) {
  background: rgba(255, 255, 255, 0.05);
  border-color: rgba(255, 255, 255, 0.08);
  color: #b8bcd3;
}

.filter-bar :deep(.el-radio-button__original-radio:checked + .el-radio-button__inner) {
  background: rgba(64, 158, 255, 0.18);
  border-color: rgba(64, 158, 255, 0.35);
  color: #7ec1ff;
  box-shadow: -1px 0 0 0 rgba(64, 158, 255, 0.35);
}

.mode-hint {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 28px;
  margin-bottom: 24px;
  font-size: 13px;
  color: #8e92ad;
}

.search-hint {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.library-hint em {
  font-style: normal;
  color: #6c7090;
}

.skeleton {
  padding: 12px 0;
}

.card-grid {
  --enter-delay: 40ms;
}

.card-col {
  margin-bottom: 24px;
  animation: card-enter 0.45s cubic-bezier(0.16, 1, 0.3, 1) both;
  animation-delay: calc(var(--i, 0) * var(--enter-delay));
}

@keyframes card-enter {
  from {
    opacity: 0;
    transform: translateY(16px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.media-card {
  cursor: pointer;
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 12px;
  overflow: hidden;
  transition: transform 0.25s cubic-bezier(0.16, 1, 0.3, 1),
    box-shadow 0.25s cubic-bezier(0.16, 1, 0.3, 1),
    border-color 0.25s ease;
}

.media-card:hover {
  transform: translateY(-6px) scale(1.01);
  box-shadow: 0 18px 40px rgba(0, 0, 0, 0.35), 0 0 0 1px rgba(64, 158, 255, 0.2);
  border-color: rgba(64, 158, 255, 0.25);
}

.media-card :deep(.el-card__body) {
  padding: 0;
}

.poster-wrap {
  position: relative;
  overflow: hidden;
  border-radius: 12px 12px 0 0;
}

.poster {
  width: 100%;
  aspect-ratio: 2 / 3;
  object-fit: cover;
  display: block;
  transition: transform 0.35s cubic-bezier(0.16, 1, 0.3, 1), filter 0.35s ease;
}

.media-card:hover .poster {
  transform: scale(1.05);
  filter: brightness(1.08);
}

.poster-placeholder {
  width: 100%;
  aspect-ratio: 2 / 3;
  background: linear-gradient(135deg, #1e2138 0%, #141628 100%);
  display: flex;
  align-items: center;
  justify-content: center;
  color: #4d5270;
}

.poster-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.32);
  opacity: 0;
  transition: opacity 0.3s ease;
}

.media-card:hover .poster-overlay {
  opacity: 1;
}

.play-icon {
  font-size: 44px;
  color: rgba(255, 255, 255, 0.92);
  transform: scale(0.85);
  transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1);
}

.media-card:hover .play-icon {
  transform: scale(1);
}

.info {
  padding: 14px;
}

.title {
  margin: 0 0 8px;
  font-size: 15px;
  font-weight: 600;
  color: #f2f3f8;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.year {
  color: #8c90ab;
  font-size: 13px;
}

.stats {
  display: flex;
  align-items: center;
  gap: 14px;
  font-size: 12px;
  color: #8c90ab;
}

.stat {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.stat .el-icon {
  font-size: 14px;
}

.stat.rating {
  color: #f7ba2a;
}

.pagination {
  display: flex;
  justify-content: center;
  margin-top: 36px;
}

:deep(.el-empty__description) {
  color: #6e7292;
}

@media (prefers-reduced-motion: reduce) {
  .card-col,
  .media-card,
  .poster,
  .poster-overlay,
  .play-icon {
    animation: none;
    transition: none;
  }
  .media-card:hover .poster {
    transform: none;
  }
}

@media (max-width: 768px) {
  .main {
    padding: 20px 16px 48px;
  }
  .page-title {
    font-size: 26px;
  }
  .info {
    padding: 10px;
  }
  .title {
    font-size: 14px;
  }
  .stats {
    gap: 10px;
  }
}
</style>
