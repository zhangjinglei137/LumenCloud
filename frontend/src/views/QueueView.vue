<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { AxiosError } from 'axios'
import { useQueueStore } from '../stores/queue'
import { useAuthStore } from '../stores/auth'
import type { DownloadQueueItem, QueueTaskItem } from '../types'
import {
  DOWNLOAD_ACTIVE_STATUSES,
  downloadQueueStatusColor,
  downloadQueueStatusLabel,
  downloadQueueStatusType,
  formatBytes,
  formatFileSize,
  formatGb,
  formatSpeed,
  formatTime,
  taskQueueStatusColor,
  taskQueueStatusLabel,
  taskQueueStatusType,
  timeAgo,
} from '../utils/format'

const store = useQueueStore()
const auth = useAuthStore()

const retryingIds = ref<Set<number>>(new Set())
/** 控制面操作进行中的任务 id 集合（按钮 loading 用） */
const busyIds = ref<Set<number>>(new Set())
const refreshingCapacity = ref(false)

/** 当前激活 Tab：task=任务队列（扁平列表）/ download=下载队列（扁平列表） */
const activeTab = ref<'task' | 'download'>('task')
/** 下载队列「仅看活跃」开关（§8.1） */
const onlyActive = ref(false)
/** 下载 Tab 是否已加载过（切 Tab 时懒加载一次） */
const downloadLoaded = ref(false)

// ---------- 通用辅助 ----------

/**
 * 控制面操作统一执行器：
 * - 成功 → 提示 okMsg 并返回 true（由调用方决定是否刷新列表）
 * - 失败 → http 拦截器已按后端 detail 提示，这里不再重复，返回 false
 */
async function runOp(id: number | null, op: () => Promise<unknown>, okMsg: string): Promise<boolean> {
  if (id != null) {
    const next = new Set(busyIds.value)
    next.add(id)
    busyIds.value = next
  }
  try {
    await op()
    ElMessage.success(okMsg)
    return true
  } catch {
    return false
  } finally {
    if (id != null) {
      const done = new Set(busyIds.value)
      done.delete(id)
      busyIds.value = done
    }
  }
}

/** 操作后按当前 Tab 刷新对应列表 */
async function refreshCurrent() {
  if (activeTab.value === 'download') {
    await Promise.all([store.fetchDownloadPage(), store.fetchCapacity()])
  } else {
    await store.fetchPage()
  }
}

/** 自定义状态色（format.ts 第三元素）→ el-tag 柔和底色样式 */
function tagStyle(color: string | null): Record<string, string> | undefined {
  if (!color) return undefined
  return { color, background: `${color}1a`, borderColor: `${color}55` }
}

// ---------- 任务队列 Tab：扁平列表 ----------

/** 探测视图行：node 为 null（执行视图 node 与 status 同值） */
function isTaskQueue(row: QueueTaskItem): boolean {
  return row.node == null
}

/** 状态标签：探测视图用 TaskQueue 文案，执行视图用 DownloadQueue 文案 */
function flatStatusLabel(row: QueueTaskItem): string {
  if (isTaskQueue(row)) return taskQueueStatusLabel(row.status)
  return downloadQueueStatusLabel(row.status)
}

function flatStatusType(row: QueueTaskItem): string {
  if (isTaskQueue(row)) return taskQueueStatusType(row.status)
  return downloadQueueStatusType(row.status)
}

function flatStatusColor(row: QueueTaskItem): string | null {
  if (isTaskQueue(row)) return taskQueueStatusColor(row.status)
  return downloadQueueStatusColor(row.status)
}

/** 行展示标签：「影视名 - SxxExx」 */
function rowLabel(row: QueueTaskItem): string {
  return `${row.title || '该任务'} - ${row.episode || '—'}`
}

// ---------- 下载队列 Tab（扁平列表） ----------

/** 「仅看活跃」过滤后的下载列表（按准入顺序展示，后端返回序即准入序） */
const downloadRows = computed<DownloadQueueItem[]>(() => {
  if (!onlyActive.value) return store.downloadItems
  return store.downloadItems.filter((d) =>
    DOWNLOAD_ACTIVE_STATUSES.includes(d.status ?? ''),
  )
})

/** pending 子集的 id 顺序（上移/下移按钮的边界判定用） */
const pendingIds = computed<number[]>(() =>
  store.downloadItems.filter((d) => d.status === 'pending').map((d) => d.id),
)

function isFirstPending(id: number): boolean {
  return pendingIds.value[0] === id
}

function isLastPending(id: number): boolean {
  const ids = pendingIds.value
  return ids[ids.length - 1] === id
}

/** quota_wait 用户化排队原因：优先后端 quota_hint，否则按「文件大小 - 可用容量」推算还差多少 */
function quotaWaitText(d: DownloadQueueItem): string {
  if (d.quota_hint) return `等待容量：${d.quota_hint}`
  const availBytes = store.availableGb * 1024 ** 3
  const need = (d.file_size ?? 0) - availBytes
  return need > 0 ? `等待容量：还差 ${formatBytes(need)}` : '等待容量释放'
}

/** 下载行状态文案：quota_wait 用可读排队原因替代状态码 */
function downloadStatusText(d: DownloadQueueItem): string {
  if (d.status === 'quota_wait') return quotaWaitText(d)
  return downloadQueueStatusLabel(d.status)
}

/** downloading 行实时进度（0~100）；无数据返回 null（进度条降级为动画等待态） */
function downloadProgress(d: DownloadQueueItem): number | null {
  const p = d.id != null ? store.progressMap[d.id]?.progress : null
  if (typeof p !== 'number' || Number.isNaN(p)) return null
  return Math.max(0, Math.min(100, Math.round(p)))
}

function downloadSpeed(d: DownloadQueueItem): string | null {
  const s = d.id != null ? store.progressMap[d.id]?.speed : null
  return typeof s === 'number' ? formatSpeed(s) : null
}

// ---------- 操作 ----------

/** 取消单任务：二次确认（不可逆：删在途下载 + 清理夸克残留 + 释放预留容量） */
async function onCancel(id: number, label: string) {
  try {
    await ElMessageBox.confirm(
      `确定取消「${label}」吗？将删除在途下载、清理夸克残留并释放预留容量，此操作不可逆。`,
      '取消任务',
      { type: 'warning', confirmButtonText: '确认取消', cancelButtonText: '再想想' },
    )
  } catch {
    return
  }
  const ok = await runOp(id, () => store.cancel(id), '已取消任务')
  if (ok) await refreshCurrent()
}

/** 下载行的取消入口：拼装「影视 集号」标签 */
function onCancelDownload(d: DownloadQueueItem) {
  return onCancel(d.id, `${d.media_title || '该任务'} ${d.episode || ''}`.trim())
}

async function onPrioritize(id: number) {
  const ok = await runOp(id, () => store.prioritize(id), '已置顶，将优先准入')
  if (ok) await refreshCurrent()
}

async function onSort(id: number, direction: 'up' | 'down') {
  const ok = await runOp(id, () => store.sort(id, direction), '已调整顺序')
  if (ok) await store.fetchDownloadPage()
}

async function onSkip(id: number) {
  const ok = await runOp(id, () => store.skip(id), '已跳过该集（后续巡检不会重新入队）')
  if (ok) await refreshCurrent()
}

/** ready → 手动入队（promote 进下载队列；后端已实现 POST /api/queue/{id}/promote） */
async function onPromote(id: number) {
  const ok = await runOp(id, () => store.promote(id), '已加入下载队列')
  if (ok) await store.fetchPage()
}

async function onRetry(id: number) {
  const next = new Set(retryingIds.value)
  next.add(id)
  retryingIds.value = next
  try {
    await store.retry(id)
    ElMessage.success('已重新加入队列')
    await refreshCurrent()
  } catch (e) {
    // 统一拦截器已按后端 detail 提示；此处按状态码补充「同步列表」语义
    const status = (e as AxiosError)?.response?.status
    if (status === 404) {
      // 任务不存在或已被消费/状态已变更 → 刷新列表消除陈旧行
      ElMessage.warning('任务不存在或状态已变更，已为你刷新列表')
      await refreshCurrent()
    } else if (status === 409) {
      // episode_state 双表状态不一致（拦截器已提示「请稍后重试」）→ 同步列表
      await refreshCurrent()
    }
  } finally {
    const done = new Set(retryingIds.value)
    done.delete(id)
    retryingIds.value = done
  }
}

// ---------- 生命周期与轮询（§8.1：任务列表 15s 慢刷，downloading 行 2.5s 局部轮询） ----------

let slowTimer: ReturnType<typeof setInterval> | undefined
let progressTimer: ReturnType<typeof setInterval> | undefined

/** 切换到下载 Tab 时懒加载一次（首页从第 1 页开始） */
function onTabChange(name: string | number) {
  if (name === 'download' && !downloadLoaded.value) {
    downloadLoaded.value = true
    store.fetchDownloadPage(1)
    store.fetchCapacity()
    store.fetchPauseState()
  }
}

onMounted(async () => {
  await Promise.all([store.fetchPage(), store.fetchCapacity(), store.fetchPauseState()])
  // 列表/容量/暂停状态 15 秒慢刷（保持当前页，不跳回第 1 页）
  slowTimer = setInterval(() => {
    store.fetchPage(store.page)
    store.fetchCapacity()
    store.fetchPauseState()
    if (activeTab.value === 'download') store.fetchDownloadPage(store.downloadPage)
  }, 15000)
  // downloading 行 2.5 秒局部轮询实时进度（仅在下载 Tab 且有下载中行时发起）
  progressTimer = setInterval(() => {
    if (activeTab.value === 'download' && store.downloadingItems.length > 0) {
      store.fetchProgress()
    }
  }, 2500)
})

onUnmounted(() => {
  if (slowTimer) clearInterval(slowTimer)
  if (progressTimer) clearInterval(progressTimer)
})

/** 手动刷新容量：带 force 语义（后端暂忽略，拿到的是最近一次统计），按钮 loading + 诚实提示缓存语义 */
async function onRefreshCapacity() {
  refreshingCapacity.value = true
  try {
    await store.fetchCapacity(true)
    ElMessage.info(
      `已刷新为最近一次统计结果（检测时间：${formatTime(store.capacity?.checked_at)}；容量统计约有 30 秒缓存）`,
    )
  } finally {
    refreshingCapacity.value = false
  }
}

// ---------- 全局暂停开关（§8.1 高调呈现：顶栏 Switch + 暂停横幅） ----------

const pauseToggling = ref(false)

/** Switch 用 :model-value + @change（失败不乐观翻转，由接口结果驱动真实状态） */
async function onPauseToggle(next: boolean) {
  pauseToggling.value = true
  const ok = await runOp(
    null,
    () => (next ? store.pause() : store.resume()),
    next ? '队列已暂停：不再准入新任务，在途任务继续完成' : '队列已恢复',
  )
  if (ok) await store.fetchPauseState()
  pauseToggling.value = false
}
</script>

<template>
  <div class="lc-page">
    <!-- 顶栏：全局暂停开关高调呈现（§8.1：暂停=不取新+在途继续） -->
    <div class="lc-panel qv-topbar">
      <div class="qv-topbar-row">
        <h3 class="lc-panel-title" style="margin: 0">巡检队列</h3>
        <div class="qv-pause-switch">
          <el-switch
            :model-value="store.pauseState.paused"
            :loading="pauseToggling"
            :disabled="!auth.isAdmin"
            active-text="已暂停"
            inactive-text="运行中"
            inline-prompt
            style="--el-switch-on-color: var(--el-color-warning)"
            @change="onPauseToggle"
          />
          <span class="lc-muted" style="font-size: 12px">下载队列总开关</span>
        </div>
      </div>
      <el-alert
        v-if="store.pauseState.paused"
        type="warning"
        :closable="false"
        show-icon
        class="qv-pause-banner"
        :title="
          store.pauseState.in_flight != null
            ? `队列已暂停：不再准入新任务，在途 ${store.pauseState.in_flight} 个任务继续完成`
            : '队列已暂停：不再准入新任务，在途任务继续完成'
        "
      />
    </div>

    <el-tabs v-model="activeTab" class="qv-tabs" @tab-change="onTabChange">
      <!-- ============ 巡检队列 Tab：扁平列表 ============ -->
      <el-tab-pane label="巡检队列" name="task">
        <div class="lc-panel">
          <div class="lc-toolbar" style="margin-bottom: 14px">
            <span class="lc-muted" style="font-size: 12px">按创建时间升序的活跃任务</span>
            <div style="display: flex; gap: 8px">
              <el-button size="small" :loading="store.loading" @click="store.fetchPage()">
                <el-icon style="vertical-align: -2px"><Refresh /></el-icon>&nbsp;刷新
              </el-button>
            </div>
          </div>

          <el-empty v-if="!store.loading && store.items.length === 0" description="队列为空" :image-size="100" />
          <template v-else>
            <el-table
              v-loading="store.loading && store.items.length === 0"
              :data="store.items"
              row-key="id"
              style="width: 100%"
            >
              <el-table-column label="任务" min-width="250">
                <template #default="{ row }: { row: QueueTaskItem }">
                  <div style="display: flex; align-items: center; gap: 8px; min-width: 0">
                    <span class="qv-ep-badge">{{ row.episode || '—' }}</span>
                    <span style="font-weight: 600; font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap">
                      {{ row.title || '—' }}
                    </span>
                  </div>
                  <div class="lc-muted" style="font-size: 12px; margin-top: 4px; word-break: break-all">
                    {{ row.file_name || '—' }}
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="状态" width="130">
                <template #default="{ row }: { row: QueueTaskItem }">
                  <el-tag
                    size="small"
                    :type="flatStatusType(row)"
                    effect="plain"
                    :style="tagStyle(flatStatusColor(row))"
                  >
                    {{ flatStatusLabel(row) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="大小" width="160">
                <template #default="{ row }: { row: QueueTaskItem }">
                  <span style="font-size: 13px">{{ formatFileSize(row.file_size, row.size_estimated) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="更新时间" width="150">
                <template #default="{ row }: { row: QueueTaskItem }">
                  {{ formatTime(row.updated_at) }}
                </template>
              </el-table-column>
              <!-- 分享码列：明文分享码仅 admin 可见（guest 后端返回 null，列隐藏） -->
              <el-table-column v-if="auth.isAdmin" prop="share_code" label="分享码" width="140">
                <template #default="{ row }: { row: QueueTaskItem }">
                  <span class="qv-share-code" style="font-size: 13px">{{ row.share_code || '—' }}</span>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="230" align="right" fixed="right">
                <template #default="{ row }: { row: QueueTaskItem }">
                  <template v-if="auth.isAdmin">
                    <!-- pending / quota_wait：置顶 + 取消 -->
                    <template v-if="row.status === 'pending' || row.status === 'quota_wait'">
                      <el-button
                        link
                        type="primary"
                        size="small"
                        :loading="busyIds.has(row.id)"
                        @click="onPrioritize(row.id)"
                      >
                        置顶
                      </el-button>
                      <el-button
                        link
                        type="danger"
                        size="small"
                        :loading="busyIds.has(row.id)"
                        @click="onCancel(row.id, rowLabel(row))"
                      >
                        取消
                      </el-button>
                    </template>
                    <!-- 执行视图在途：取消 -->
                    <template v-else-if="['transferring', 'downloading', 'scrape', 'library'].includes(row.status)">
                      <el-button
                        link
                        type="danger"
                        size="small"
                        :loading="busyIds.has(row.id)"
                        @click="onCancel(row.id, rowLabel(row))"
                      >
                        取消
                      </el-button>
                    </template>
                    <!-- 探测视图 ready：手动入队 + 跳过 -->
                    <template v-else-if="isTaskQueue(row) && row.status === 'ready'">
                      <el-button
                        link
                        type="primary"
                        size="small"
                        :loading="busyIds.has(row.id)"
                        @click="onPromote(row.id)"
                      >
                        手动入队
                      </el-button>
                      <el-button
                        link
                        type="warning"
                        size="small"
                        :loading="busyIds.has(row.id)"
                        @click="onSkip(row.id)"
                      >
                        跳过
                      </el-button>
                    </template>
                    <!-- 探测视图 unmatched：跳过 -->
                    <el-button
                      v-else-if="isTaskQueue(row) && row.status === 'unmatched'"
                      link
                      type="warning"
                      size="small"
                      :loading="busyIds.has(row.id)"
                      @click="onSkip(row.id)"
                    >
                      跳过
                    </el-button>
                    <!-- 失败/异常：重试 -->
                    <el-button
                      v-else-if="row.status === 'failed' || row.status === 'error'"
                      link
                      type="primary"
                      size="small"
                      :loading="retryingIds.has(row.id)"
                      @click="onRetry(row.id)"
                    >
                      重试
                    </el-button>
                    <span v-else class="lc-muted" style="font-size: 12px">—</span>
                  </template>
                  <span v-else class="lc-muted" style="font-size: 12px">—</span>
                </template>
              </el-table-column>
            </el-table>

            <el-pagination
              v-if="store.total > store.pageSize"
              :current-page="store.page"
              :page-size="store.pageSize"
              :total="store.total"
              layout="prev, pager, next"
              style="justify-content: center; margin-top: 16px"
              @current-change="(p: number) => store.fetchPage(p)"
            />
          </template>
        </div>
      </el-tab-pane>

      <!-- ============ 下载队列 Tab：扁平列表（顶部容量条为首要信息） ============ -->
      <el-tab-pane label="下载队列" name="download">
        <!-- 容量条：已用 / 预留中 / 可用 三段堆叠 + 数值（§8.1 首要信息） -->
        <div v-if="store.capacity" class="lc-panel">
          <div class="lc-toolbar" style="margin-bottom: 14px">
            <h3 class="lc-panel-title" style="margin: 0">夸克网盘容量</h3>
            <el-tooltip
              content="容量统计由后端每 30 秒缓存一次，刷新展示的是最近一次统计结果"
              placement="left"
            >
              <el-button size="small" :loading="refreshingCapacity" @click="onRefreshCapacity">
                <el-icon v-if="!refreshingCapacity" style="vertical-align: -2px"><Refresh /></el-icon>
                &nbsp;刷新
              </el-button>
            </el-tooltip>
          </div>
          <div class="qv-capacity-bar" role="img" aria-label="容量三段条：已用 / 预留中 / 可用">
            <div
              class="seg used"
              :style="{ width: `${store.capacity.total_gb ? (store.usedGb / store.capacity.total_gb) * 100 : 0}%` }"
            />
            <div
              class="seg reserved"
              :style="{ width: `${store.capacity.total_gb ? (store.reservedGb / store.capacity.total_gb) * 100 : 0}%` }"
            />
            <div class="seg free" />
          </div>
          <div class="qv-capacity-legend">
            <span class="item"><i class="dot used" />已用 {{ formatGb(store.usedGb) }}</span>
            <span class="item"><i class="dot reserved" />预留中 {{ formatGb(store.reservedGb) }}</span>
            <span class="item"><i class="dot free" />可用 {{ formatGb(store.availableGb) }}</span>
          </div>
          <div class="lc-stat-row" style="margin-top: 14px">
            <div class="lc-stat">
              <span class="label">总量</span>
              <span class="value">{{ formatGb(store.capacity.total_gb) }}</span>
            </div>
            <div class="lc-stat">
              <span class="label">使用率</span>
              <span class="value">{{ store.usagePercent }}%</span>
            </div>
            <div class="lc-stat">
              <span class="label">数据来源</span>
              <span class="value" style="font-size: 14px">{{ store.capacity.source }}</span>
            </div>
            <div class="lc-stat">
              <span class="label">检测时间</span>
              <span class="value" style="font-size: 14px">{{ formatTime(store.capacity.checked_at) }}</span>
            </div>
          </div>
        </div>

        <!-- 扁平列表：按准入顺序，支持「仅看活跃」 -->
        <div class="lc-panel">
          <div class="lc-toolbar" style="margin-bottom: 14px">
            <span class="lc-muted" style="font-size: 12px">按准入顺序排列的执行队列</span>
            <div style="display: flex; align-items: center; gap: 14px">
              <!-- 静态标签常驻：inline-prompt 的 active-text 在未激活态不显示，用户无法辨识开关用途 -->
              <span
                class="lc-muted"
                style="font-size: 12px; cursor: pointer; user-select: none"
                @click="onlyActive = !onlyActive"
              >
                仅看活跃
              </span>
              <el-switch
                v-model="onlyActive"
                :aria-label="onlyActive ? '仅看活跃（已开启）' : '仅看活跃（已关闭，显示全部）'"
              />
              <el-button size="small" :loading="store.downloadLoading" @click="store.fetchDownloadPage()">
                <el-icon style="vertical-align: -2px"><Refresh /></el-icon>&nbsp;刷新
              </el-button>
            </div>
          </div>

          <el-empty
            v-if="!store.downloadLoading && downloadRows.length === 0"
            :description="onlyActive ? '当前没有活跃任务' : '下载队列为空'"
            :image-size="100"
          />
          <template v-else>
            <el-table
              v-loading="store.downloadLoading && store.downloadItems.length === 0"
              :data="downloadRows"
              style="width: 100%"
            >
              <el-table-column label="影视 / 分集" min-width="220">
                <template #default="{ row }">
                  <div style="display: flex; align-items: center; gap: 8px; min-width: 0">
                    <span class="qv-ep-badge">{{ row.episode || '—' }}</span>
                    <span style="font-weight: 600; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap">
                      {{ row.media_title || (row.media_id ? `影视 #${row.media_id}` : '—') }}
                    </span>
                  </div>
                  <div class="lc-muted" style="font-size: 12px; margin-top: 4px; word-break: break-all">
                    {{ row.file_name || '—' }}
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="状态" min-width="160">
                <template #default="{ row }">
                  <el-tag
                    size="small"
                    :type="downloadQueueStatusType(row.status)"
                    effect="plain"
                    :style="tagStyle(downloadQueueStatusColor(row.status))"
                  >
                    {{ downloadStatusText(row) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="进度 / 大小" width="210">
                <template #default="{ row }">
                  <!-- downloading：2.5s 局部轮询的实时进度条 + 速度 -->
                  <div v-if="row.status === 'downloading'">
                    <template v-if="downloadProgress(row) != null">
                      <el-progress :percentage="downloadProgress(row)!" :stroke-width="8" />
                      <span v-if="downloadSpeed(row)" class="lc-muted" style="font-size: 12px">
                        {{ downloadSpeed(row) }}
                      </span>
                    </template>
                    <el-progress v-else :percentage="100" :stroke-width="8" striped striped-flow :show-text="false" status="warning" />
                  </div>
                  <span v-else style="font-size: 13px">{{ formatBytes(row.file_size) }}</span>
                </template>
              </el-table-column>
              <el-table-column v-if="auth.isAdmin" prop="share_code" label="分享码" width="150">
                <template #default="{ row }">
                  <a
                    v-if="row.share_url && row.share_code"
                    :href="row.share_url"
                    target="_blank"
                    rel="noopener"
                    class="qv-share-link"
                  >{{ row.share_code }}</a>
                  <span v-else-if="row.share_code">{{ row.share_code }}</span>
                  <span v-else>—</span>
                </template>
              </el-table-column>
              <el-table-column label="备注" min-width="170">
                <template #default="{ row }">
                  <el-tooltip v-if="row.node_error" :content="row.node_error" placement="top" effect="dark">
                    <span style="color: var(--el-color-danger); font-size: 12px">
                      {{ row.node_error.slice(0, 40) }}{{ row.node_error.length > 40 ? '…' : '' }}
                    </span>
                  </el-tooltip>
                  <span v-else-if="row.retry_count" class="lc-muted" style="font-size: 12px">
                    已重试 {{ row.retry_count }} 次
                  </span>
                  <span v-else class="lc-muted">—</span>
                </template>
              </el-table-column>
              <el-table-column label="入队时间" width="120">
                <template #default="{ row }">
                  <span style="font-size: 12px">{{ timeAgo(row.enqueued_at) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="230" align="right" fixed="right">
                <template #default="{ row }">
                  <template v-if="auth.isAdmin">
                    <!-- pending：置顶 / 上移 / 下移 / 取消（排序按钮为拖拽的降级形态） -->
                    <template v-if="row.status === 'pending'">
                      <el-button link type="primary" size="small" :loading="busyIds.has(row.id)" @click="onPrioritize(row.id)">
                        置顶
                      </el-button>
                      <el-button link size="small" :disabled="isFirstPending(row.id)" :loading="busyIds.has(row.id)" @click="onSort(row.id, 'up')">
                        上移
                      </el-button>
                      <el-button link size="small" :disabled="isLastPending(row.id)" :loading="busyIds.has(row.id)" @click="onSort(row.id, 'down')">
                        下移
                      </el-button>
                      <el-button link type="danger" size="small" :loading="busyIds.has(row.id)" @click="onCancelDownload(row)">
                        取消
                      </el-button>
                    </template>
                    <!-- quota_wait：置顶 / 取消 -->
                    <template v-else-if="row.status === 'quota_wait'">
                      <el-button link type="primary" size="small" :loading="busyIds.has(row.id)" @click="onPrioritize(row.id)">
                        置顶
                      </el-button>
                      <el-button link type="danger" size="small" :loading="busyIds.has(row.id)" @click="onCancelDownload(row)">
                        取消
                      </el-button>
                    </template>
                    <!-- 在途：取消 -->
                    <template v-else-if="['transferring', 'downloading', 'scrape', 'library'].includes(row.status)">
                      <el-button link type="danger" size="small" :loading="busyIds.has(row.id)" @click="onCancelDownload(row)">
                        取消
                      </el-button>
                    </template>
                    <!-- failed：重试 -->
                    <el-button
                      v-else-if="row.status === 'failed'"
                      link
                      type="primary"
                      size="small"
                      :loading="retryingIds.has(row.id)"
                      @click="onRetry(row.id)"
                    >
                      重试
                    </el-button>
                    <span v-else class="lc-muted" style="font-size: 12px">—</span>
                  </template>
                  <span v-else class="lc-muted" style="font-size: 12px">—</span>
                </template>
              </el-table-column>
            </el-table>

            <el-pagination
              v-if="store.downloadTotal > store.pageSize"
              :current-page="store.downloadPage"
              :page-size="store.pageSize"
              :total="store.downloadTotal"
              layout="prev, pager, next"
              style="justify-content: center; margin-top: 16px"
              @current-change="(p: number) => store.fetchDownloadPage(p)"
            />
          </template>
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
/* ---------- 顶栏与暂停横幅 ---------- */
.qv-topbar {
  margin-bottom: 0;
  border-bottom-left-radius: 0;
  border-bottom-right-radius: 0;
}

.qv-topbar-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.qv-pause-switch {
  display: flex;
  align-items: center;
  gap: 10px;
}

.qv-pause-banner {
  margin-top: 12px;
}

.qv-tabs {
  margin-top: 16px;
}

/* ---------- 集号徽标 / 分享码 / 静默倒计时 ---------- */
.qv-ep-badge {
  display: inline-block;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  font-weight: 600;
  padding: 1px 8px;
  border-radius: 6px;
  background: var(--lc-accent-soft, var(--el-color-primary-light-9));
  color: var(--el-color-primary);
}

.qv-share-code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  margin-top: 2px;
}

.qv-share-link {
  color: var(--el-color-primary);
  text-decoration: underline;
  word-break: break-all;
}

/* ---------- 容量三段堆叠条（已用/预留中/可用） ---------- */
.qv-capacity-bar {
  display: flex;
  height: 14px;
  border-radius: 7px;
  overflow: hidden;
  background: var(--el-fill-color-light);
}

.qv-capacity-bar .seg {
  height: 100%;
  transition: width 0.4s ease;
}

.qv-capacity-bar .seg.used {
  background: var(--el-color-primary);
}

.qv-capacity-bar .seg.reserved {
  background: repeating-linear-gradient(
    45deg,
    #d9822b,
    #d9822b 6px,
    #e6a23c 6px,
    #e6a23c 12px
  );
}

.qv-capacity-bar .seg.free {
  flex: 1;
}

.qv-capacity-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 20px;
  margin-top: 10px;
  font-size: 12px;
}

.qv-capacity-legend .item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--lc-text-secondary, var(--el-text-color-secondary));
}

.qv-capacity-legend .dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.qv-capacity-legend .dot.used {
  background: var(--el-color-primary);
}

.qv-capacity-legend .dot.reserved {
  background: #d9822b;
}

.qv-capacity-legend .dot.free {
  background: var(--el-fill-color-darker, var(--el-border-color));
}
</style>
