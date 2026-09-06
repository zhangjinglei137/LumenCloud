<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useEmbyStore } from '../stores/emby'
import { useAuthStore } from '../stores/auth'
import type { EmbyItemType, EmbyLibraryItem } from '../types'

const router = useRouter()
const store = useEmbyStore()
const auth = useAuthStore()

/** 类型筛选：全部 / 电影 / 剧集 */
const itemType = ref<EmbyItemType | ''>('')

/** 标题关键字（客户端即时过滤，不额外请求） */
const keyword = ref('')

onMounted(() => {
  store.fetchLibrary()
})

const filteredItems = computed<EmbyLibraryItem[]>(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return store.items
  return store.items.filter((it) => it.title.toLowerCase().includes(kw))
})

function onTypeChange(val: EmbyItemType | '') {
  store.fetchLibrary(val || undefined)
}

function typeLabel(type: EmbyItemType): string {
  return type === 'movie' ? '电影' : '剧集'
}

function openInEmby(item: EmbyLibraryItem) {
  window.open(item.emby_web_url, '_blank', 'noopener')
}
</script>

<template>
  <div class="lc-page">
    <div class="lc-toolbar">
      <div class="left">
        <el-radio-group :model-value="itemType" size="small" @change="onTypeChange">
          <el-radio-button value="">全部</el-radio-button>
          <el-radio-button value="movie">电影</el-radio-button>
          <el-radio-button value="series">剧集</el-radio-button>
        </el-radio-group>
        <span class="lc-muted">共 {{ store.items.length }} 部</span>
      </div>
      <div class="right">
        <el-input
          v-model="keyword"
          placeholder="搜索标题"
          clearable
          size="small"
          style="width: 200px"
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>
        <el-button :icon="'Refresh'" :loading="store.loading" @click="onTypeChange(itemType)">
          刷新
        </el-button>
      </div>
    </div>

    <div v-loading="store.loading && !store.error" style="min-height: 200px">
      <!-- Emby 未配置：引导去设置页 -->
      <el-empty v-if="!store.loading && store.error === 'not_configured'" description="Emby 尚未配置">
        <template #description>
          <p class="lc-muted" style="margin: 0 0 8px">
            Emby 尚未配置，暂无法浏览媒体库
          </p>
        </template>
        <el-button v-if="auth.isAdmin" type="primary" @click="router.push('/settings')">
          去设置页配置 Emby
        </el-button>
        <p v-else class="lc-muted" style="margin: 0; font-size: 12px">
          请联系管理员在设置页配置 Emby 地址与 API Key
        </p>
      </el-empty>

      <!-- Emby 不可达：提示检查凭据，可重试 -->
      <el-empty v-else-if="!store.loading && store.error === 'unavailable'" description="Emby 服务不可达">
        <template #description>
          <p class="lc-muted" style="margin: 0 0 8px">
            无法连接 Emby，请检查设置页中的 Emby 地址与 API Key 是否正确
          </p>
        </template>
        <el-button type="primary" @click="onTypeChange(itemType)">重试</el-button>
      </el-empty>

      <!-- 已配置但库为空 -->
      <el-empty v-else-if="!store.loading && store.items.length === 0" description="Emby 库中暂无内容" />

      <!-- 卡片墙：复用影视库的 lc-media-grid / lc-poster 视觉 -->
      <div v-else class="lc-media-grid">
        <div
          v-for="m in filteredItems"
          :key="m.emby_id"
          class="lc-media-card"
          @click="openInEmby(m)"
        >
          <div class="lc-poster">
            <img v-if="m.poster_url" :src="m.poster_url" :alt="m.title" loading="lazy" />
            <div v-else class="lc-poster-fallback">{{ m.title }}</div>
            <el-tag
              class="lc-poster-type"
              size="small"
              effect="dark"
              :type="m.type === 'movie' ? 'warning' : 'primary'"
            >
              {{ typeLabel(m.type) }}
            </el-tag>
          </div>
          <div class="lc-media-card-body">
            <h3 class="lc-media-card-title" :title="m.title">{{ m.title }}</h3>
            <div class="lc-media-card-meta">
              <div class="row">
                <span v-if="m.year">{{ m.year }}</span>
                <span v-if="m.community_rating !== null" class="lc-muted">
                  评分 {{ m.community_rating.toFixed(1) }}
                </span>
              </div>
              <div class="row">
                <span class="lc-muted open-hint">
                  <el-icon style="vertical-align: -2px"><Monitor /></el-icon>
                  在 Emby 中打开
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 有结果但关键字过滤后为空 -->
      <el-empty v-if="!store.loading && store.items.length > 0 && filteredItems.length === 0" description="没有匹配的标题" />
    </div>
  </div>
</template>

<style scoped>
.open-hint {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  opacity: 0.75;
}
</style>
