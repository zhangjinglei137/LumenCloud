<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useEmbyStore } from '../stores/emby'
import { useAuthStore } from '../stores/auth'
import { createMediaApi, scanMediaApi } from '../api'
import type { EmbyItemType, EmbyLibraryItem } from '../types'

const router = useRouter()
const store = useEmbyStore()
const auth = useAuthStore()

/** 类型 Tab：全部 / 电影 / 剧集 / 动漫（动漫对应后端 anime=true，按 Name 匹配动漫库） */
type EmbyTab = '' | EmbyItemType | 'anime'
const tab = ref<EmbyTab>('')

/** 「仅在更」筛选（仅对剧集/动漫 Tab 显示；勾选时请求带 status=continuing） */
const onlyContinuing = ref(false)

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

/** 按当前 Tab / 仅在更状态发请求（刷新、Tab 切换、checkbox 共用） */
function fetchCurrent() {
  store.fetchLibrary({
    itemType: tab.value === 'anime' ? undefined : tab.value || undefined,
    anime: tab.value === 'anime',
    status: onlyContinuing.value ? 'continuing' : undefined,
  })
}

function onTabChange(val: EmbyTab) {
  // 回写选中 Tab（:model-value 单向绑定，须手动同步，否则点击不生效）
  tab.value = val
  // 仅在更仅对剧集/动漫有效；切到其他 Tab 时自动复位，避免残留 status 参数
  if (val !== 'series' && val !== 'anime') onlyContinuing.value = false
  fetchCurrent()
}

function typeLabel(type: EmbyItemType): string {
  return type === 'movie' ? '电影' : '剧集'
}

function openInEmby(item: EmbyLibraryItem) {
  // D-1：serverId 获取失败时后端返回 null，无详情页地址则不跳转
  if (!item.emby_web_url) return
  window.open(item.emby_web_url, '_blank', 'noopener')
}

/** Q12：正在「加入订阅」的 Emby 条目 id 集合，用于按钮 loading 与防重复提交 */
const subscribing = ref<Set<string>>(new Set())

/**
 * Q12：Emby 库一键订阅——复用 POST /api/media。
 * 注意：后端 media_type 仅接受 movie/tv，Emby 的 series 须映射为 tv；
 * Emby 海报是完整 URL，与 poster_path（TMDB 相对路径）语义不同，订阅时不传。
 */
async function subscribe(m: EmbyLibraryItem): Promise<void> {
  if (subscribing.value.has(m.emby_id)) return
  subscribing.value.add(m.emby_id)
  try {
    const created = await createMediaApi({
      title: m.title,
      tmdb_id: m.tmdb_id,
      media_type: m.type === 'movie' ? 'movie' : 'tv', // series → tv（后端契约）
    })
    ElMessage.success(`已加入订阅：${m.title}`)
    // 触发一次巡检，立即补齐集数状态（fire-and-forget，不阻塞；结果见运行日志）
    scanMediaApi(created.id).catch(() => {})
    await store.fetchLibrary() // 刷新 in_media 角标
  } catch {
    // 拦截器已提示（如 tmdb_id 已存在时 409「该影视已在影视库」）；刷新使角标与本地一致
    await store.fetchLibrary()
  } finally {
    subscribing.value.delete(m.emby_id)
  }
}
</script>

<template>
  <div class="lc-page">
    <div class="lc-toolbar">
      <div class="left">
        <el-radio-group :model-value="tab" size="small" @change="onTabChange">
          <el-radio-button value="">全部</el-radio-button>
          <el-radio-button value="movie">电影</el-radio-button>
          <el-radio-button value="series">剧集</el-radio-button>
          <el-radio-button value="anime">动漫</el-radio-button>
        </el-radio-group>
        <!-- 仅在更：仅对剧集/动漫 Tab 显示（后端 SeriesStatus=continuing） -->
        <el-checkbox
          v-if="tab === 'series' || tab === 'anime'"
          v-model="onlyContinuing"
          size="small"
          @change="fetchCurrent"
        >
          仅在更
        </el-checkbox>
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
        <el-button :icon="'Refresh'" :loading="store.loading" @click="fetchCurrent">
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
        <el-button type="primary" @click="fetchCurrent">重试</el-button>
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
            <!-- 已纳入管理角标：本地 Media 表已收录（in_media=true） -->
            <el-tag
              v-if="m.in_media"
              class="lc-poster-in-media"
              size="small"
              effect="dark"
              type="success"
            >
              已纳入管理
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
                <!-- 未收录条目一键订阅；@click.stop 阻止冒泡触发 openInEmby -->
                <el-button
                  v-if="!m.in_media"
                  size="small"
                  type="primary"
                  :loading="subscribing.has(m.emby_id)"
                  @click.stop="subscribe(m)"
                >
                  加入订阅
                </el-button>
                <!-- D-1：serverId 获取失败（emby_web_url 为 null）时隐藏入口 -->
                <span v-if="m.emby_web_url" class="lc-muted open-hint">
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

/* 「已纳入管理」角标：海报右上角，与左上角类型标签（.lc-poster-type）对称 */
.lc-poster-in-media {
  position: absolute;
  top: 10px;
  right: 10px;
}
</style>
