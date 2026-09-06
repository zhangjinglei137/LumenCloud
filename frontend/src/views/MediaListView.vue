<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useMediaStore } from '../stores/media'
import { useAuthStore } from '../stores/auth'
import { TMDB_POSTER_BASE, type MediaItem } from '../types'
import {
  formatTime,
  mediaStatusLabel,
  mediaStatusType,
  mediaTypeLabel,
  taskStatusLabel,
  taskStatusType,
  timeAgo,
} from '../utils/format'

const router = useRouter()
const store = useMediaStore()
const auth = useAuthStore()

const viewMode = ref<'card' | 'table'>('card')
const scanningIds = ref<Set<number>>(new Set())

let timer: ReturnType<typeof setInterval> | undefined

onMounted(async () => {
  await store.fetchList()
  // 有运行中任务时每 20 秒自动刷新一次
  timer = setInterval(() => {
    if (store.items.some((m) => m.last_task_run?.status === 'running')) {
      store.fetchList()
    }
  }, 20000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})

const hasRunning = computed(() => store.items.some((m) => m.last_task_run?.status === 'running'))

function episodeText(m: MediaItem): string {
  const s = m.episode_stats
  if (!s) return '—'
  const avail = s.available ?? s.downloaded
  const total = s.total
  if (avail !== undefined && total !== undefined) return `${avail} / ${total} 集`
  if (total !== undefined) return `共 ${total} 集`
  if (avail !== undefined) return `已有 ${avail} 集`
  return '—'
}

async function onScan(m: MediaItem, e: Event) {
  e.stopPropagation()
  scanningIds.value.add(m.id)
  try {
    const taskRunId = await store.scan(m.id)
    ElMessage.success(`已触发巡检（任务 #${taskRunId}）`)
    await store.fetchList()
  } catch {
    // 拦截器已提示
  } finally {
    scanningIds.value.delete(m.id)
  }
}

async function onDelete(m: MediaItem, e: Event) {
  e.stopPropagation()
  await ElMessageBox.confirm(`确定删除《${m.title}》吗？相关巡检与队列记录将一并清理。`, '删除影视', {
    confirmButtonText: '删除',
    cancelButtonText: '取消',
    type: 'warning',
  })
  await store.remove(m.id)
  ElMessage.success('已删除')
  await store.fetchList()
}
</script>

<template>
  <div class="lc-page">
    <div class="lc-toolbar">
      <div class="left">
        <span class="lc-muted">共 {{ store.items.length }} 部</span>
        <el-tag v-if="hasRunning" type="primary" effect="plain" size="small">有巡检正在进行</el-tag>
      </div>
      <div class="right">
        <el-radio-group v-model="viewMode" size="small">
          <el-radio-button value="card">
            <el-icon style="vertical-align: -2px"><Grid /></el-icon>&nbsp;卡片
          </el-radio-button>
          <el-radio-button value="table">
            <el-icon style="vertical-align: -2px"><Tickets /></el-icon>&nbsp;表格
          </el-radio-button>
        </el-radio-group>
        <el-button :icon="'Refresh'" :loading="store.loading" @click="store.fetchList()">
          刷新
        </el-button>
        <el-button v-if="auth.isAdmin" type="primary" @click="router.push('/media/add')">
          <el-icon style="vertical-align: -2px"><Plus /></el-icon>&nbsp;添加影视
        </el-button>
      </div>
    </div>

    <div v-loading="store.loading && store.items.length === 0" style="min-height: 200px">
      <el-empty v-if="!store.loading && store.items.length === 0" description="还没有影视，点击右上角添加">
        <el-button v-if="auth.isAdmin" type="primary" @click="router.push('/media/add')">
          添加第一部影视
        </el-button>
      </el-empty>

      <!-- 卡片视图 -->
      <div v-if="viewMode === 'card' && store.items.length > 0" class="lc-media-grid">
        <div
          v-for="m in store.items"
          :key="m.id"
          class="lc-media-card"
          @click="router.push(`/media/${m.id}`)"
        >
          <div class="lc-poster">
            <img
              v-if="m.poster_path"
              :src="`${TMDB_POSTER_BASE}${m.poster_path}`"
              :alt="m.title"
              loading="lazy"
            />
            <div v-else class="lc-poster-fallback">{{ m.title }}</div>
            <el-tag class="lc-poster-type" size="small" effect="dark" :type="m.media_type === 'movie' ? 'warning' : 'primary'">
              {{ mediaTypeLabel(m.media_type) }}
            </el-tag>
          </div>
          <div class="lc-media-card-body">
            <h3 class="lc-media-card-title" :title="m.title">{{ m.title }}</h3>
            <div class="lc-media-card-meta">
              <div class="row">
                <span>
                  <el-tag size="small" :type="mediaStatusType(m.status)" effect="plain">
                    {{ mediaStatusLabel(m.status) }}
                  </el-tag>
                </span>
                <span>已有 {{ episodeText(m) }}</span>
              </div>
              <div class="row">
                <span v-if="m.in_emby" class="lc-muted">已在 Emby</span>
                <span v-else class="lc-muted">未入库 Emby</span>
                <span>{{ timeAgo(m.last_scan_at) }}</span>
              </div>
              <div v-if="m.last_task_run" class="row">
                <span class="lc-muted">最近任务</span>
                <el-tag size="small" :type="taskStatusType(m.last_task_run.status)" effect="plain">
                  {{ taskStatusLabel(m.last_task_run.status) }}
                </el-tag>
              </div>
              <div v-if="auth.isAdmin" class="row" style="margin-top: 6px">
                <el-button
                  size="small"
                  :loading="scanningIds.has(m.id)"
                  @click="onScan(m, $event)"
                >
                  <el-icon style="vertical-align: -2px"><RefreshRight /></el-icon>&nbsp;触发巡检
                </el-button>
                <el-button size="small" type="danger" plain @click="onDelete(m, $event)">删除</el-button>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 表格视图 -->
      <div v-if="viewMode === 'table' && store.items.length > 0" class="lc-panel" style="padding: 8px 16px">
        <el-table :data="store.items" style="width: 100%" @row-click="(row: MediaItem) => router.push(`/media/${row.id}`)">
          <el-table-column label="标题" min-width="200">
            <template #default="{ row }">
              <div class="title-cell">
                <img
                  v-if="row.poster_path"
                  :src="`${TMDB_POSTER_BASE}${row.poster_path}`"
                  :alt="row.title"
                  loading="lazy"
                  class="title-poster"
                />
                <div v-else class="title-poster title-poster-fallback">
                  {{ row.title.slice(0, 1) }}
                </div>
                <span style="font-weight: 600">{{ row.title }}</span>
                <el-tag size="small" effect="plain" style="margin-left: 8px">
                  {{ mediaTypeLabel(row.media_type) }}
                </el-tag>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="110">
            <template #default="{ row }">
              <el-tag size="small" :type="mediaStatusType(row.status)" effect="plain">
                {{ mediaStatusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="已有 / 总集数" width="130" align="center">
            <template #default="{ row }">{{ episodeText(row) }}</template>
          </el-table-column>
          <el-table-column label="Emby" width="90" align="center">
            <template #default="{ row }">
              <el-tag v-if="row.in_emby" size="small" type="success" effect="plain">已入库</el-tag>
              <span v-else class="lc-muted">未入库</span>
            </template>
          </el-table-column>
          <el-table-column label="最近扫描" width="150">
            <template #default="{ row }">{{ formatTime(row.last_scan_at) }}</template>
          </el-table-column>
          <el-table-column label="最近任务" width="110">
            <template #default="{ row }">
              <el-tag v-if="row.last_task_run" size="small" :type="taskStatusType(row.last_task_run.status)" effect="plain">
                {{ taskStatusLabel(row.last_task_run.status) }}
              </el-tag>
              <span v-else class="lc-muted">—</span>
            </template>
          </el-table-column>
          <el-table-column v-if="auth.isAdmin" label="操作" width="200" align="right">
            <template #default="{ row }">
              <el-button size="small" link type="primary" :loading="scanningIds.has(row.id)" @click.stop="onScan(row, $event)">
                触发巡检
              </el-button>
              <el-button size="small" link type="danger" @click.stop="onDelete(row, $event)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </div>
  </div>
</template>

<style scoped>
.title-cell {
  display: flex;
  align-items: center;
  gap: 10px;
}

.title-poster {
  width: 32px;
  height: 46px;
  border-radius: 4px;
  object-fit: cover;
  flex-shrink: 0;
}

.title-poster-fallback {
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(160deg, #1d232d 0%, #12161d 100%);
  color: var(--lc-text-secondary);
  font-size: 13px;
}
</style>
