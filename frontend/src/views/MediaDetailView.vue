<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useMediaStore } from '../stores/media'
import { useAuthStore } from '../stores/auth'
import { TMDB_POSTER_BASE } from '../types'
import {
  formatBytes,
  formatGb,
  formatTime,
  mediaStatusLabel,
  mediaStatusType,
  mediaTypeLabel,
  queueStatusLabel,
  queueStatusType,
  taskStatusLabel,
  taskStatusType,
} from '../utils/format'

const route = useRoute()
const router = useRouter()
const store = useMediaStore()
const auth = useAuthStore()

const mediaId = Number(route.params.id)
const saving = ref(false)
const scanning = ref(false)

const form = ref({
  max_episode_size_gb: null as number | null,
  max_movie_size_gb: null as number | null,
  scan_interval_minutes: null as number | null,
  status: 'tracking',
})

onMounted(async () => {
  await store.fetchDetail(mediaId)
  const d = store.detail
  if (d) {
    form.value = {
      max_episode_size_gb: d.max_episode_size_gb,
      max_movie_size_gb: d.max_movie_size_gb,
      scan_interval_minutes: d.scan_interval_minutes ?? null,
      status: d.status,
    }
  }
})

const detail = computed(() => store.detail)
const episodes = computed(() => detail.value?.episode_state ?? [])

function episodeLabel(ep: Record<string, unknown>): string {
  const season = ep.season ?? ep.season_number
  const episodeNumber = ep.episode_number
  if (season !== undefined && season !== null && episodeNumber !== undefined && episodeNumber !== null) {
    return `S${String(season).padStart(2, '0')}E${String(episodeNumber).padStart(2, '0')}`
  }
  // 无 season/episode_number（全量模式 movie：episode=文件名）→ 直接显示原始 episode
  if (ep.episode !== undefined && ep.episode !== null) return String(ep.episode)
  return '—'
}

async function saveSettings() {
  saving.value = true
  try {
    await store.patch(mediaId, { ...form.value })
    ElMessage.success('设置已保存')
    await store.fetchDetail(mediaId)
  } catch {
    // 拦截器已提示
  } finally {
    saving.value = false
  }
}

async function onScan() {
  scanning.value = true
  try {
    const taskRunId = await store.scan(mediaId)
    ElMessage.success(`已触发巡检（任务 #${taskRunId}）`)
  } finally {
    scanning.value = false
  }
}

async function onDelete() {
  if (!detail.value) return
  await ElMessageBox.confirm(
    `确定删除《${detail.value.title}》吗？相关巡检与队列记录将一并清理。`,
    '删除影视',
    { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
  )
  await store.remove(mediaId)
  ElMessage.success('已删除')
  router.push('/')
}
</script>

<template>
  <div v-loading="store.loading && !detail" class="lc-page">
    <template v-if="detail">
      <!-- 头部 -->
      <div class="lc-panel detail-header">
        <div class="poster">
          <div class="lc-poster" style="width: 120px; border-radius: 10px">
            <img
              v-if="detail.poster_path"
              :src="`${TMDB_POSTER_BASE}${detail.poster_path}`"
              :alt="detail.title"
            />
            <span v-else class="lc-poster-fallback" style="font-size: 14px">{{ detail.title }}</span>
          </div>
        </div>
        <div class="info">
          <div class="title-row">
            <h2 style="margin: 0">{{ detail.title }}</h2>
            <el-tag :type="mediaStatusType(detail.status)" effect="plain">
              {{ mediaStatusLabel(detail.status) }}
            </el-tag>
            <el-tag effect="plain">{{ mediaTypeLabel(detail.media_type) }}</el-tag>
            <el-tag v-if="detail.in_emby" type="success" effect="plain">已在 Emby</el-tag>
          </div>
          <div class="lc-muted" style="margin-top: 8px">
            TMDB ID：{{ detail.tmdb_id }} · 最近扫描：{{ formatTime(detail.last_scan_at) }}
          </div>
          <div v-if="detail.last_task_run" style="margin-top: 10px">
            <el-tag size="small" :type="taskStatusType(detail.last_task_run.status)" effect="plain">
              最近任务：{{ taskStatusLabel(detail.last_task_run.status) }}
            </el-tag>
            <span v-if="detail.last_task_run.message" class="lc-muted" style="margin-left: 8px; font-size: 12px">
              {{ detail.last_task_run.message }}
            </span>
          </div>
          <div class="actions">
            <el-button v-if="auth.isAdmin" type="primary" :loading="scanning" @click="onScan">
              <el-icon style="vertical-align: -2px"><RefreshRight /></el-icon>&nbsp;触发巡检
            </el-button>
            <el-button v-if="auth.isAdmin" type="danger" plain @click="onDelete">删除影视</el-button>
          </div>
        </div>
      </div>

      <el-row :gutter="16">
        <!-- 遗漏集 -->
        <el-col :xs="24" :md="14">
          <div class="lc-panel">
            <h3 class="lc-panel-title">集数状态（{{ episodes.length }}）</h3>
            <el-empty v-if="episodes.length === 0" description="暂无集数记录，触发一次巡检后会建立基线" :image-size="80" />
            <el-table v-else :data="episodes" size="small" max-height="480">
              <el-table-column label="集" width="110">
                <template #default="{ row }">{{ episodeLabel(row as Record<string, unknown>) }}</template>
              </el-table-column>
              <el-table-column label="状态" width="110">
                <template #default="{ row }">
                  <el-tag size="small" effect="plain">{{ (row as Record<string, unknown>).status ?? '—' }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="大小" width="100" align="right">
                <template #default="{ row }">
                  {{ formatGb((row as Record<string, unknown>).size_gb as number | null | undefined) }}
                </template>
              </el-table-column>
              <el-table-column label="更新时间">
                <template #default="{ row }">
                  {{ formatTime((row as Record<string, unknown>).updated_at as string | undefined) }}
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-col>

        <el-col :xs="24" :md="10">
          <!-- 转存队列摘要 -->
          <div class="lc-panel">
            <h3 class="lc-panel-title">转存队列</h3>
            <el-empty
              v-if="!detail.transfer_queue || detail.transfer_queue.length === 0"
              description="暂无队列任务"
              :image-size="80"
            />
            <div v-else class="queue-list">
              <div v-for="(q, i) in detail.transfer_queue" :key="q.id ?? i" class="queue-item">
                <div class="name" :title="q.file_name">{{ q.file_name ?? '—' }}</div>
                <div class="meta">
                  <el-tag size="small" :type="queueStatusType(q.status)" effect="plain">
                    {{ queueStatusLabel(q.status) }}
                  </el-tag>
                  <span class="lc-muted">{{ formatBytes(q.file_size) }}</span>
                  <span class="lc-muted">{{ formatTime(q.updated_at) }}</span>
                </div>
              </div>
            </div>
          </div>

          <!-- 大小覆盖设置 -->
          <div class="lc-panel">
            <h3 class="lc-panel-title">大小与巡检设置</h3>
            <el-alert
              v-if="!auth.isAdmin"
              type="info"
              :closable="false"
              show-icon
              title="仅管理员可修改"
              style="margin-bottom: 12px"
            />
            <el-form label-position="top" :disabled="!auth.isAdmin">
              <el-form-item label="单集大小上限（GB，留空用全局默认）">
                <el-input-number v-model="form.max_episode_size_gb" :min="0" :precision="1" :step="0.5" style="width: 100%" />
              </el-form-item>
              <el-form-item v-if="detail.media_type === 'movie'" label="电影大小上限（GB，留空用全局默认）">
                <el-input-number v-model="form.max_movie_size_gb" :min="0" :precision="1" :step="1" style="width: 100%" />
              </el-form-item>
              <el-form-item label="巡检间隔（分钟）">
                <el-input-number v-model="form.scan_interval_minutes" :min="5" :step="5" style="width: 100%" />
              </el-form-item>
              <el-form-item label="状态">
                <el-select v-model="form.status" style="width: 100%">
                  <el-option label="订阅中" value="tracking" />
                  <el-option label="已暂停" value="paused" />
                </el-select>
              </el-form-item>
              <el-button v-if="auth.isAdmin" type="primary" :loading="saving" style="width: 100%" @click="saveSettings">
                保存设置
              </el-button>
            </el-form>
          </div>
        </el-col>
      </el-row>
    </template>
  </div>
</template>

<style scoped>
.detail-header {
  display: flex;
  gap: 20px;
}

.info {
  flex: 1;
  display: flex;
  flex-direction: column;
}

.title-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.actions {
  margin-top: auto;
  padding-top: 16px;
  display: flex;
  gap: 10px;
}

.queue-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.queue-item {
  border: 1px solid var(--lc-border);
  border-radius: 10px;
  padding: 10px 12px;
}

.queue-item .name {
  font-size: 13px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  margin-bottom: 6px;
}

.queue-item .meta {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
}
</style>
