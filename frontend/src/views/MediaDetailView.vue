<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useMediaStore } from '../stores/media'
import { useAuthStore } from '../stores/auth'
import {
  buildEpisodeGroups,
  downloadQueueStatusColor,
  downloadQueueStatusLabel,
  downloadQueueStatusType,
  episodeDisplayName,
  episodeStateTag,
  episodeStateTooltip,
  formatGb,
  formatTime,
  mediaStatusLabel,
  mediaStatusType,
  mediaTypeLabel,
  seriesStatusLabel,
  seriesStatusType,
  taskQueueStatusColor,
  taskQueueStatusLabel,
  taskQueueStatusType,
  taskStatusLabel,
  taskStatusType,
} from '../utils/format'
import { posterUrl } from '../utils/poster'

const route = useRoute()
const router = useRouter()
const store = useMediaStore()
const auth = useAuthStore()

const mediaId = Number(route.params.id)
const saving = ref(false)
const scanning = ref(false)
const posterBroken = ref(false)

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

/** 进行中任务行（active_tasks 契约见 types.ActiveTask；source 决定状态字典） */
const activeTasks = computed(() => detail.value?.active_tasks ?? [])

/** 集数行 + 预计算的 4 色分类 tag（模板直接使用，免去逐行重复判定；字段契约见 episodeStateTag） */
const episodeRows = computed(() =>
  episodes.value.map((ep) => ({ ...ep, __tag: episodeStateTag({ ...ep }) })),
)

/** TMDB 全集列表（按 season/episode 升序兜底） */
const tmdbEpisodes = computed(() => {
  const list = detail.value?.tmdb_episodes ?? []
  return [...list].sort((a, b) => {
    const seasonDiff = (a.season ?? 0) - (b.season ?? 0)
    if (seasonDiff !== 0) return seasonDiff
    return (a.episode ?? 0) - (b.episode ?? 0)
  })
})

/** 组总数（分组依据）：episode_state 最大集号；无 episode_state 时用 TMDB 全长 */
const episodeTotal = computed<number>(() => {
  const nums = episodes.value.map((e) => Number(e.episode_number)).filter((n) => Number.isFinite(n))
  if (nums.length > 0) return Math.max(...nums)
  return tmdbEpisodes.value.length
})

/** 每 100 集分组 tag（电影/无数据返回空数组 → 不渲染导航） */
const groups = computed(() => buildEpisodeGroups(episodeTotal.value))

/** 当前选中分组的 label；null = 全部 */
const activeGroup = ref<string | null>(null)

/** 分组过滤后的表格行；无集号行始终显示 */
const filteredRows = computed(() => {
  const active = activeGroup.value
  const g = groups.value.find((grp) => grp.label === active)
  if (!g) return episodeRows.value
  return episodeRows.value.filter((row) => {
    // EpisodeState 类型未声明 episode_number（spread 后索引签名丢失），按 Record 访问
    // 无集号行始终显示（null/undefined 不参与分组过滤；注意 Number(null) === 0，必须先判空再数值化）
    const raw = (row as Record<string, unknown>).episode_number
    if (raw === null || raw === undefined) return true
    const n = Number(raw)
    if (!Number.isFinite(n)) return true
    return n >= g.start && n <= g.end
  })
})

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

/** 名称列展示（原 episodeName 函数替换为 format 层纯函数） */
function episodeName(ep: Record<string, unknown>): string {
  return episodeDisplayName(ep)
}

/** 格式化 TMDB 集数为 S01E04 */
function tmdbEpisodeLabel(item: { season?: number | null; episode?: number | null }): string {
  const season = item.season ?? 0
  const episodeNumber = item.episode ?? 0
  return `S${String(season).padStart(2, '0')}E${String(episodeNumber).padStart(2, '0')}`
}

/** 查找 TMDB 集数对应的 episode_state 分类色；未匹配返回 default（灰色） */
function tmdbEpisodeStateType(
  item: { season?: number | null; episode?: number | null; in_emby?: boolean },
): 'success' | 'info' | 'warning' | 'danger' | 'default' {
  if (item.in_emby === true) return 'success'
  const label = tmdbEpisodeLabel(item)
  const matched = episodes.value.find((ep) => {
    if (
      ep.season != null &&
      ep.episode != null &&
      item.season != null &&
      item.episode != null
    ) {
      return ep.season === item.season && ep.episode === item.episode
    }
    const epStr = ep.episode as unknown
    if (typeof epStr === 'string' && epStr.toUpperCase().includes(label)) {
      return true
    }
    return false
  })
  if (matched) {
    return episodeStateTag({ ...matched }).type
  }
  return 'default'
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
    // E-1：巡检 fire-and-forget，task_run_id 不再同步返回；结果轮询运行日志页
    ElMessage.success(taskRunId != null ? `已触发巡检（任务 #${taskRunId}）` : '已触发巡检，结果见运行日志')
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
              v-if="detail.poster_path && !posterBroken"
              :src="posterUrl(detail.poster_path)!"
              :alt="detail.title"
              @error="posterBroken = true"
            />
            <span v-else-if="!detail.poster_path" class="lc-poster-fallback" style="font-size: 14px">{{ detail.title }}</span>
            <span v-else class="lc-poster-fallback" style="font-size: 30px; padding: 0">{{ detail.title.slice(0, 1) }}</span>
          </div>
        </div>
        <div class="info">
          <div class="title-row">
            <h2 style="margin: 0">{{ detail.title }}</h2>
            <el-tag :type="mediaStatusType(detail.status)" effect="plain">
              {{ mediaStatusLabel(detail.status) }}
            </el-tag>
            <el-tag effect="plain">{{ mediaTypeLabel(detail.media_type) }}</el-tag>
            <el-tag
              v-if="detail.series_status"
              :type="seriesStatusType(detail.series_status, detail.media_type)"
              effect="plain"
            >
              {{ seriesStatusLabel(detail.series_status, detail.media_type) }}
            </el-tag>
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
        <!-- 大小与巡检设置（仅管理员，窄屏换行） -->
        <div v-if="auth.isAdmin" class="header-settings">
          <div class="hs-item" v-if="detail.media_type !== 'movie'">
            <label class="hs-label">单集大小上限 (GB)</label>
            <el-input-number v-model="form.max_episode_size_gb" :min="0" :precision="1" :step="0.5" size="small" />
          </div>
          <div class="hs-item" v-if="detail.media_type === 'movie'">
            <label class="hs-label">电影大小上限 (GB)</label>
            <el-input-number v-model="form.max_movie_size_gb" :min="0" :precision="1" :step="1" size="small" />
          </div>
          <div class="hs-item">
            <label class="hs-label">巡检间隔 (分钟)</label>
            <el-input-number v-model="form.scan_interval_minutes" :min="5" :step="5" size="small" />
          </div>
          <div class="hs-item">
            <label class="hs-label">状态</label>
            <el-select v-model="form.status" size="small">
              <el-option label="订阅中" value="tracking" />
              <el-option label="已暂停" value="paused" />
            </el-select>
          </div>
          <el-button type="primary" :loading="saving" size="small" @click="saveSettings">保存</el-button>
        </div>
      </div>

      <!-- 影视状态（电影：无集数概念，单列全宽） -->
      <div v-if="detail.media_type === 'movie'" class="lc-panel">
        <h3 class="lc-panel-title">影视状态</h3>
        <el-tag :type="seriesStatusType(detail.series_status, 'movie')" effect="plain">
          {{ seriesStatusLabel(detail.series_status, 'movie') }}
        </el-tag>
        <p class="lc-muted" style="margin-top: 8px; font-size: 12px">电影无集数概念，状态以 TMDB 为准</p>
      </div>

      <!-- 当前进行中任务（episode-status-and-detail-polish：仅 tv + 有任务时展示） -->
      <div v-if="detail.media_type !== 'movie' && activeTasks.length > 0" class="lc-panel">
        <h3 class="lc-panel-title">当前进行中任务</h3>
        <el-table :data="activeTasks" size="small" style="width: 100%">
          <el-table-column label="集" width="110">
            <template #default="{ row }">
              <span>{{ row.episode }}</span>
            </template>
          </el-table-column>
          <el-table-column label="名称" min-width="140">
            <template #default="{ row }">
              <span>{{ episodeDisplayName(row) || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="120" align="center">
            <template #default="{ row }">
              <el-tag
                :type="row.source === 'task' ? taskQueueStatusType(row.status) : downloadQueueStatusType(row.status)"
                :color="(row.source === 'task' ? taskQueueStatusColor(row.status) : downloadQueueStatusColor(row.status)) ?? undefined"
                effect="plain"
              >
                {{ row.source === 'task' ? taskQueueStatusLabel(row.status) : downloadQueueStatusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <!-- 集数状态（剧集：单列全宽） -->
      <div v-if="detail.media_type !== 'movie'" class="lc-panel">
        <h3 class="lc-panel-title">集数状态（{{ episodes.length }}）</h3>
        <!-- 四色图例：tag 分类说明，悬停标签可查看原始执行状态等详情 -->
        <p class="ep-legend lc-muted">
          <span class="ep-legend-item"><i class="ep-dot success" />已在库</span>
          <span class="ep-legend-item"><i class="ep-dot info" />未开播</span>
          <span class="ep-legend-item"><i class="ep-dot warning" />已开播</span>
          <span class="ep-legend-item"><i class="ep-dot danger" />异常</span>
          <span class="ep-legend-hint">悬停状态标签查看详情</span>
        </p>

        <!-- 分组导航（有分组数据且非电影才渲染） -->
        <div v-if="groups.length > 0" class="ep-group-nav">
          <el-radio-group v-model="activeGroup" size="small">
            <el-radio-button :value="null">全部</el-radio-button>
            <el-radio-button v-for="g in groups" :key="g.label" :value="g.label">{{ g.label }}</el-radio-button>
          </el-radio-group>
        </div>

        <el-empty
          v-if="episodes.length === 0"
          :description="
            tmdbEpisodes.length > 0 ? '暂无任务记录，TMDB 全集见下方列表' : '暂无集数记录，触发一次巡检后会建立基线'
          "
          :image-size="80"
        />
        <el-table v-else :data="filteredRows" size="small" max-height="600">
          <el-table-column label="集" width="110">
            <template #default="{ row }">{{ episodeLabel(row as Record<string, unknown>) }}</template>
          </el-table-column>
          <el-table-column label="名称" min-width="140">
            <template #default="{ row }">
              <span :title="episodeName(row as Record<string, unknown>)">
                {{ episodeName(row as Record<string, unknown>) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="96" align="center">
            <template #default="{ row }">
              <el-tooltip placement="top" :show-after="200">
                <template #content>
                  <div v-for="(line, i) in episodeStateTooltip(row)" :key="i">{{ line }}</div>
                </template>
                <el-tag size="small" effect="plain" :type="row.__tag.type">
                  {{ row.__tag.label }}
                </el-tag>
              </el-tooltip>
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

        <!-- TMDB 全集（保留完整展示，移到表格下方） -->
        <div v-if="tmdbEpisodes.length > 0" class="tmdb-episodes">
          <div class="tmdb-episodes-title">TMDB 全集（{{ tmdbEpisodes.length }} 集）</div>
          <div class="tmdb-episodes-grid">
            <div
              v-for="(ep, idx) in tmdbEpisodes"
              :key="idx"
              class="tmdb-episodes-item"
              :title="ep.name ?? undefined"
            >
              <div class="tmdb-episodes-label">
                <i class="ep-dot" :class="tmdbEpisodeStateType(ep)" />
                <span>{{ tmdbEpisodeLabel(ep) }}</span>
              </div>
              <div class="tmdb-episodes-date lc-muted">{{ ep.air_date ?? '—' }}</div>
            </div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.detail-header {
  display: flex;
  gap: 20px;
  flex-wrap: wrap; /* 窄屏紧凑组换行 */
  align-items: flex-start;
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

/* 头部紧凑设置组（仅管理员可见） */
.header-settings {
  display: flex;
  align-items: flex-end;
  gap: 12px;
  margin-left: auto; /* 靠最右 */
  flex-wrap: wrap;
}

.hs-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.hs-label {
  font-size: 12px;
  color: var(--lc-text-secondary, #909399);
}

/* 集数分组导航 */
.ep-group-nav {
  margin: 0 0 12px;
}

/* 集数状态四色图例（与 ep-dot 对应 el-tag type：success/info/warning/danger） */
.ep-legend {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  margin: -6px 0 10px;
  font-size: 12px;
}

.ep-legend-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.ep-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.ep-dot.success {
  background: var(--el-color-success);
}

.ep-dot.info {
  background: var(--el-color-info);
}

.ep-dot.warning {
  background: var(--el-color-warning);
}

.ep-dot.danger {
  background: var(--el-color-danger);
}

.ep-legend-hint {
  opacity: 0.65;
}

/* TMDB 全集网格 */
.tmdb-episodes {
  margin-bottom: 14px;
}

.tmdb-episodes-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 8px;
}

.tmdb-episodes-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(72px, 1fr));
  gap: 8px;
}

.tmdb-episodes-item {
  border: 1px solid var(--lc-border);
  border-radius: 8px;
  padding: 6px 4px;
  text-align: center;
  transition: background 0.2s;
}

.tmdb-episodes-item:hover {
  background: var(--el-fill-color-light);
}

.tmdb-episodes-label {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  font-size: 13px;
  font-weight: 600;
}

.tmdb-episodes-date {
  margin-top: 2px;
  font-size: 11px;
  line-height: 1.2;
}

.ep-dot.default {
  background: var(--el-text-color-placeholder);
}

@media (max-width: 768px) {
  .header-settings {
    flex-basis: 100%;
    margin-left: 0;
  }
}
</style>
