<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { AxiosError } from 'axios'
import { getScanTaskDetailApi, scanMediaApi } from '../api'
import { useQueueStore } from '../stores/queue'
import { useAuthStore } from '../stores/auth'
import type {
  DownloadQueueItem,
  QueueChildTask,
  QueueMediaTask,
  ScanTaskDetail,
  ScanTaskSummary,
} from '../types'
import {
  DOWNLOAD_ACTIVE_STATUSES,
  QUEUE_FLOW_NODES,
  SCAN_PHASE_NODES,
  downloadQueueStatusColor,
  downloadQueueStatusLabel,
  downloadQueueStatusType,
  formatBytes,
  formatGb,
  formatSpeed,
  formatTime,
  mediaTypeLabel,
  queueAggregateLabel,
  queueAggregateType,
  queueNodeLabel,
  queueNodeType,
  scanPhaseLabel,
  scanResultLabel,
  scanResultType,
  shareCodeShort,
  taskQueueStatusColor,
  taskQueueStatusLabel,
  taskQueueStatusType,
  taskStatusLabel,
  taskStatusType,
  timeAgo,
  timeUntil,
} from '../utils/format'

const router = useRouter()
const store = useQueueStore()
const auth = useAuthStore()

const retryingIds = ref<Set<number>>(new Set())
/** 控制面操作进行中的任务 id 集合（按钮 loading 用） */
const busyIds = ref<Set<number>>(new Set())
const refreshingCapacity = ref(false)

/** 当前激活 Tab：task=任务队列（影视分组树）/ download=下载队列（扁平列表） */
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

// ---------- 任务队列 Tab：影视分组树 ----------

/** 巡检记录伪子行（挂影视父级 children 尾部，与分集子任务并列展示） */
interface ScanRow {
  __scan: true
  __key: string
  task: ScanTaskSummary
  parent: QueueMediaTask
}

function isParent(row: unknown): row is QueueMediaTask {
  return Array.isArray((row as QueueMediaTask).children)
}

function isScanRow(row: unknown): row is ScanRow {
  return (row as ScanRow).__scan === true
}

/** 表格数据：影视父级 children 尾部注入巡检记录行（仅展示层，store 数据不变） */
const tableData = computed<QueueMediaTask[]>(() =>
  store.items.map((p) => {
    const scans = p.scan_tasks ?? []
    if (!scans.length) return p
    const scanRows = scans.map(
      (t, i): ScanRow => ({ __scan: true, __key: `s-${t.id ?? i}`, task: t, parent: p }),
    )
    return { ...p, children: [...(p.children ?? []), ...(scanRows as unknown as QueueChildTask[])] }
  }),
)

/** 子任务节点值；缺失兜底 idle，保证标签恒有合理展示 */
function childNodeOf(c: QueueChildTask): string {
  return c.node || 'idle'
}

/** 子任务展示状态：新契约 tq_status 优先，否则回退旧 node 值 */
function childStatusOf(c: QueueChildTask): { kind: 'tq' | 'node'; value: string } {
  return c.tq_status ? { kind: 'tq', value: c.tq_status } : { kind: 'node', value: childNodeOf(c) }
}

/** 失败诊断文案：后端 _child_dto 只返回 node_error */
function childError(c: QueueChildTask): string | null {
  return c.node_error || c.error || null
}

/** 子任务重试次数展示值（新契约 retry 语义沿用 node_attempt） */
function childAttempts(c: QueueChildTask): number {
  return c.node_attempt ?? 0
}

/** unmatched 静默倒计时文案（「2 天后重试」），未到期/无数据返回 null */
function silentCountdown(c: QueueChildTask): string | null {
  if (c.tq_status !== 'unmatched') return null
  const until = timeUntil(c.silent_until)
  return until ? `${until}重试` : null
}

function parentPercent(p: QueueMediaTask): number {
  const total = p.total_count ?? p.children?.length ?? 0
  const done = p.done_count ?? 0
  return total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0
}

function parentCounts(p: QueueMediaTask): string {
  const total = p.total_count ?? p.children?.length ?? 0
  return `${p.done_count ?? 0}/${total} 集`
}

function failedCount(p: QueueMediaTask): number {
  return (
    p.children?.filter(
      (c) => !isScanRow(c) && (c.tq_status === 'error' || childNodeOf(c) === 'failed'),
    ).length ?? 0
  )
}

/**
 * 父级探测聚合计数（待探 n · 就绪 n · 静默 n）：
 * 优先按 children.tq_status 统计；无新契约字段时回退后端 probe_counts；都没有返回 null 不显示
 */
function probeCountLine(p: QueueMediaTask): string | null {
  const counts = { pending: 0, ready: 0, unmatched: 0 }
  let any = false
  for (const c of p.children ?? []) {
    if (isScanRow(c)) continue
    if (c.tq_status === 'pending' || c.tq_status === 'probing') counts.pending += 1
    else if (c.tq_status === 'ready') counts.ready += 1
    else if (c.tq_status === 'unmatched') counts.unmatched += 1
    else continue
    any = true
  }
  if (!any && p.probe_counts) {
    counts.pending = p.probe_counts.pending ?? 0
    counts.ready = p.probe_counts.ready ?? 0
    counts.unmatched = p.probe_counts.unmatched ?? 0
    any = true
  }
  if (!any) return null
  const parts: string[] = []
  if (counts.pending) parts.push(`待探 ${counts.pending}`)
  if (counts.ready) parts.push(`就绪 ${counts.ready}`)
  if (counts.unmatched) parts.push(`静默 ${counts.unmatched}`)
  return parts.length ? parts.join(' · ') : null
}

/** 父级更新时间取子任务最新一次（巡检行不参与） */
function latestUpdate(p: QueueMediaTask): string {
  const times = (p.children ?? [])
    .filter((c) => !isScanRow(c))
    .map((c) => c.updated_at)
    .filter((t): t is string => !!t)
  return formatTime(times.length ? times.reduce((a, b) => (a > b ? a : b)) : null)
}

/** 父级行巡检摘要：运行中→「巡检中…」，否则「上次巡检：xx 前 · message」；无记录返回 null 不显示 */
function scanSummary(p: QueueMediaTask): string | null {
  const t = p.scan_tasks?.[0]
  if (!t) return null
  if (t.status === 'running') return '巡检中…'
  const base = `上次巡检：${timeAgo(t.started_at)}`
  return t.message ? `${base} · ${t.message}` : base
}

// ---------- 任务详情 Drawer ----------

const detailVisible = ref(false)
/** 当前查看详情的影视任务（打开时的快照；列表 15s 自动刷新不影响已打开内容） */
const detailMedia = ref<QueueMediaTask | null>(null)
/** 当前选中查看流程链的子任务行 key */
const detailChildKey = ref<string | null>(null)

const detailChildren = computed<QueueChildTask[]>(() => detailMedia.value?.children ?? [])

const detailChild = computed<QueueChildTask | null>(() => {
  const children = detailChildren.value
  return children.find((c) => c.__key === detailChildKey.value) ?? null
})

/** 打开时默认选中失败子任务（无则第一个），让用户优先看到需要处理的集 */
function pickDefaultChild(parent: QueueMediaTask): string | null {
  const children = parent.children ?? []
  const failed = children.find((c) => childNodeOf(c) === 'failed' || c.tq_status === 'error')
  return (failed ?? children[0])?.__key ?? null
}

function openDetail(parent: QueueMediaTask, child?: QueueChildTask) {
  detailMedia.value = parent
  detailChildKey.value = child?.__key ?? pickDefaultChild(parent)
  detailVisible.value = true
}

/** 子任务行打开详情：先找回所属影视父级 */
function openChildDetail(row: QueueChildTask) {
  const parent = store.items.find((p) => p.children?.some((c) => c.__key === row.__key))
  if (parent) openDetail(parent, row)
}

function selectChild(c: QueueChildTask) {
  detailChildKey.value = c.__key ?? null
}

/**
 * el-steps 的 active 下标：
 * - 流程节点：当前节点 process、之前 finish、之后 wait
 * - done：active 超出末位 → 全部对勾
 * - idle / failed：-1 → 全部待办；failed 另由红色错误提示呈现诊断文案
 */
const childStepActive = computed(() => {
  const node = detailChild.value?.node
  if (!node || node === 'idle' || node === 'failed') return -1
  const idx = QUEUE_FLOW_NODES.indexOf(node)
  if (idx < 0) return -1
  return node === 'done' ? QUEUE_FLOW_NODES.length : idx
})

// ---------- 巡检详情 Drawer（与分集详情 drawer 状态独立，互不干扰） ----------

const scanVisible = ref(false)
const scanLoading = ref(false)
const rescanning = ref(false)
/** 巡检详情（打开时先用摘要兜底渲染，接口返回后替换为完整详情） */
const scanDetail = ref<ScanTaskDetail | null>(null)
/** 所属影视标题 / media_id（摘要 ScanTaskSummary 不含 media_id，由父级带入） */
const scanParentTitle = ref('')
const scanMediaId = ref<number | null>(null)

async function openScanDetail(parent: QueueMediaTask, task: ScanTaskSummary) {
  scanParentTitle.value = parent.title || '—'
  scanMediaId.value = parent.media_id ?? null
  scanDetail.value = { ...task }
  scanVisible.value = true
  scanLoading.value = true
  try {
    const detail = await getScanTaskDetailApi(task.id)
    // 媒体名以后端返回为准（media_title 为空时保留父级标题）
    if (detail.media_title) scanParentTitle.value = detail.media_title
    if (detail.media_id) scanMediaId.value = detail.media_id
    scanDetail.value = detail
  } catch {
    // 详情接口失败（如后端未实现 /api/logs/{id}）：保留摘要展示，拦截器已提示
  } finally {
    scanLoading.value = false
  }
}

/** 单个阶段的 el-step 状态：done→finish、process→process、error→error、skipped/wait→wait（跳过黄色由 is-skipped 类呈现） */
function scanStepStatus(node: string): 'wait' | 'process' | 'finish' | 'error' {
  const d = scanDetail.value
  let st = d?.phases?.[node]?.status
  // 阶段状态缺失时用 failed_phase 兜底定位异常节点
  if (!st && d?.scan_detail?.failed_phase === node) st = 'error'
  switch (st) {
    case 'done':
      return 'finish'
    case 'process':
      return 'process'
    case 'error':
      return 'error'
    default:
      return 'wait'
  }
}

function scanStepSkipped(node: string): boolean {
  return scanDetail.value?.phases?.[node]?.status === 'skipped'
}

/** el-steps active：首个未完成阶段的下标；全部完成 → 超末位（全对勾）；无 phases → 按整体状态兜底 */
const scanStepActive = computed(() => {
  const d = scanDetail.value
  const phases = d?.phases
  if (!phases) return d?.status === 'success' ? SCAN_PHASE_NODES.length : 0
  const idx = SCAN_PHASE_NODES.findIndex((n) => phases[n]?.status !== 'done')
  return idx < 0 ? SCAN_PHASE_NODES.length : idx
})

/** 结果摘要统计行（只显示有值的项；缺 N 集 / 已入队恒显示，来自 scan_detail） */
const scanStats = computed(() => {
  const d = scanDetail.value?.scan_detail
  if (!d) return [] as { label: string; value: number }[]
  const items: { label: string; value: number }[] = [
    { label: '缺失集', value: d.missing_total ?? 0 },
    { label: '已入队', value: d.enqueued ?? 0 },
  ]
  if (d.existing_skipped) items.push({ label: '已收录跳过', value: d.existing_skipped })
  if (d.unmatched) items.push({ label: '未匹配文件', value: d.unmatched })
  if (d.size_filtered) items.push({ label: '大小过滤', value: d.size_filtered })
  if (d.non_video) items.push({ label: '非视频', value: d.non_video })
  return items
})

const scanMissingItems = computed(() => scanDetail.value?.scan_detail?.missing_items ?? [])

/** 重新巡检：复用 POST /media/{id}/scan，成功后刷新队列 */
async function onRescan() {
  if (!scanMediaId.value) return
  rescanning.value = true
  try {
    await scanMediaApi(scanMediaId.value)
    ElMessage.success('已触发重新巡检，结果稍后刷新可见')
    scanVisible.value = false
    await store.fetchPage()
  } finally {
    rescanning.value = false
  }
}

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

/** 手动触发单影视探测（按影视补集） */
async function onProbe(p: QueueMediaTask) {
  if (!p.media_id) return
  const ok = await runOp(p.media_id, () => store.probe(p.media_id!), `已触发「${p.title || ''}」探测`)
  if (ok) await store.fetchPage()
}

// ---------- 手动加集 Dialog（表单 SxxExx） ----------

const addVisible = ref(false)
const addSubmitting = ref(false)
const addForm = reactive<{ media_id: number | null; episode: string }>({
  media_id: null,
  episode: '',
})

/** 可选影视：当前队列树中有 media_id 的父级 */
const mediaOptions = computed(() =>
  store.items
    .filter((p) => typeof p.media_id === 'number')
    .map((p) => ({ value: p.media_id as number, label: p.title || `影视 #${p.media_id}` })),
)

function openAddDialog(parent?: QueueMediaTask) {
  addForm.media_id = parent?.media_id ?? null
  addForm.episode = ''
  addVisible.value = true
}

/** SxxExx（1-2 位季 + 1-3 位集）或电影归一化键 movie:<title> */
function validEpisode(v: string): boolean {
  return /^S\d{1,2}E\d{1,3}$/i.test(v.trim()) || v.trim().toLowerCase().startsWith('movie:')
}

async function onAddSubmit() {
  if (!addForm.media_id) {
    ElMessage.warning('请选择影视')
    return
  }
  const episode = addForm.episode.trim()
  if (!validEpisode(episode)) {
    ElMessage.warning('集号格式不正确，请输入如 S01E05（电影填 movie:片名）')
    return
  }
  addSubmitting.value = true
  const ok = await runOp(
    null,
    () => store.addTask({ media_id: addForm.media_id!, episode }),
    `已加入探测队列：${episode}`,
  )
  addSubmitting.value = false
  if (ok) {
    addVisible.value = false
    await store.fetchPage()
  }
}

// ---------- 生命周期与轮询（§8.1：整树 15s 慢刷，downloading 行 2.5s 局部轮询） ----------

let slowTimer: ReturnType<typeof setInterval> | undefined
let progressTimer: ReturnType<typeof setInterval> | undefined

/** 切换到下载 Tab 时懒加载一次 */
function onTabChange(name: string | number) {
  if (name === 'download' && !downloadLoaded.value) {
    downloadLoaded.value = true
    store.fetchDownloadPage()
    store.fetchCapacity()
    store.fetchPauseState()
  }
}

onMounted(async () => {
  await Promise.all([store.fetchPage(), store.fetchCapacity(), store.fetchPauseState()])
  // 整树/容量/暂停状态 15 秒慢刷（避免高频刷新导致展开状态丢失与闪烁）
  slowTimer = setInterval(() => {
    store.fetchPage()
    store.fetchCapacity()
    store.fetchPauseState()
    if (activeTab.value === 'download') store.fetchDownloadPage()
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

async function loadMore() {
  if (activeTab.value === 'download') await store.fetchDownloadPage(true)
  else await store.fetchPage(true)
}
</script>

<template>
  <div class="lc-page">
    <!-- 顶栏：全局暂停开关高调呈现（§8.1：暂停=不取新+在途继续） -->
    <div class="lc-panel qv-topbar">
      <div class="qv-topbar-row">
        <h3 class="lc-panel-title" style="margin: 0">任务队列</h3>
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
      <!-- ============ 任务队列 Tab：影视分组树 ============ -->
      <el-tab-pane label="任务队列" name="task">
        <div class="lc-panel">
          <div class="lc-toolbar" style="margin-bottom: 14px">
            <span class="lc-muted" style="font-size: 12px">按影视分组的探测与补集队列</span>
            <div style="display: flex; gap: 8px">
              <el-button v-if="auth.isAdmin" size="small" @click="openAddDialog()">
                <el-icon style="vertical-align: -2px"><Plus /></el-icon>&nbsp;手动加集
              </el-button>
              <el-button size="small" :loading="store.loading" @click="store.fetchPage()">
                <el-icon style="vertical-align: -2px"><Refresh /></el-icon>&nbsp;刷新
              </el-button>
            </div>
          </div>

          <el-empty v-if="!store.loading && store.items.length === 0" description="队列为空" :image-size="100" />
          <template v-else>
            <el-table
              v-loading="store.loading && store.items.length === 0"
              :data="tableData"
              row-key="__key"
              :tree-props="{ children: 'children' }"
              style="width: 100%"
            >
              <el-table-column label="影视 / 分集" min-width="250">
                <template #default="{ row }">
                  <template v-if="isParent(row)">
                    <div style="font-weight: 600">{{ row.title || '—' }}</div>
                    <div class="lc-muted" style="font-size: 12px; margin-top: 2px">
                      {{ mediaTypeLabel(row.media_type) }}
                      <template v-if="probeCountLine(row)"> · {{ probeCountLine(row) }}</template>
                    </div>
                    <div
                      v-if="scanSummary(row)"
                      class="lc-muted"
                      style="font-size: 12px; margin-top: 2px"
                    >
                      {{ scanSummary(row) }}
                    </div>
                  </template>
                  <template v-else-if="isScanRow(row)">
                    <div style="font-weight: 600; font-size: 13px">巡检</div>
                    <div
                      class="lc-muted"
                      style="font-size: 12px; margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap"
                    >
                      {{ row.task.message || '—' }}
                    </div>
                  </template>
                  <template v-else>
                    <span class="qv-ep-badge">{{ row.episode || '—' }}</span>
                    <div class="lc-muted" style="font-size: 12px; margin-top: 4px; word-break: break-all">
                      {{ row.file_name || '—' }}
                    </div>
                    <div
                      v-if="shareCodeShort(row.share_code ?? row.share_code_tail)"
                      class="lc-muted qv-share-code"
                    >
                      分享码 {{ shareCodeShort(row.share_code ?? row.share_code_tail) }}
                    </div>
                  </template>
                </template>
              </el-table-column>
              <el-table-column label="状态" width="130">
                <template #default="{ row }">
                  <el-tag
                    v-if="isParent(row)"
                    size="small"
                    :type="queueAggregateType(row.aggregate_status)"
                    effect="plain"
                  >
                    {{ queueAggregateLabel(row.aggregate_status) }}
                  </el-tag>
                  <el-tag
                    v-else-if="isScanRow(row)"
                    size="small"
                    :type="taskStatusType(row.task.status)"
                    effect="plain"
                  >
                    {{ taskStatusLabel(row.task.status) }}
                  </el-tag>
                  <!-- 子行：新契约 tq_status 优先（含状态色约定），否则回退旧 node 映射 -->
                  <template v-else>
                    <el-tag
                      v-if="childStatusOf(row).kind === 'tq'"
                      size="small"
                      :type="taskQueueStatusType(childStatusOf(row).value)"
                      effect="plain"
                      :style="tagStyle(taskQueueStatusColor(childStatusOf(row).value))"
                      :class="{ 'qv-pulse': childStatusOf(row).value === 'probing' }"
                    >
                      {{ taskQueueStatusLabel(childStatusOf(row).value) }}
                    </el-tag>
                    <el-tag v-else size="small" :type="queueNodeType(childNodeOf(row))" effect="plain">
                      {{ queueNodeLabel(childNodeOf(row)) }}
                    </el-tag>
                    <el-tooltip
                      v-if="silentCountdown(row)"
                      content="确认无资源，静默中；到期后自动重新探测"
                      placement="top"
                      effect="dark"
                    >
                      <div class="qv-silent-countdown">{{ silentCountdown(row) }}</div>
                    </el-tooltip>
                  </template>
                </template>
              </el-table-column>
              <el-table-column label="进度" width="200">
                <template #default="{ row }">
                  <div v-if="isParent(row)" style="display: flex; align-items: center; gap: 8px">
                    <el-progress :percentage="parentPercent(row)" :stroke-width="8" style="flex: 1" />
                    <span style="font-size: 12px">{{ parentCounts(row) }}</span>
                  </div>
                  <span v-else-if="isScanRow(row)" class="lc-muted" style="font-size: 12px">
                    {{
                      row.task.duration_seconds != null
                        ? `耗时 ${Math.round(row.task.duration_seconds)} 秒`
                        : '—'
                    }}
                  </span>
                  <span v-else style="font-size: 13px">{{ formatBytes(row.file_size) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="备注" min-width="180">
                <template #default="{ row }">
                  <template v-if="isParent(row)">
                    <span v-if="failedCount(row) > 0" style="color: var(--el-color-danger); font-size: 12px">
                      {{ failedCount(row) }} 集失败
                    </span>
                    <span v-else class="lc-muted">—</span>
                  </template>
                  <template v-else-if="isScanRow(row)">
                    <el-tooltip
                      v-if="row.task.message"
                      :content="row.task.message"
                      placement="top"
                      effect="dark"
                    >
                      <span class="lc-muted" style="font-size: 12px">
                        {{ row.task.message.slice(0, 40) }}{{ row.task.message.length > 40 ? '…' : '' }}
                      </span>
                    </el-tooltip>
                    <span v-else class="lc-muted">—</span>
                  </template>
                  <template v-else>
                    <el-tooltip v-if="childError(row)" :content="childError(row)!" placement="top" effect="dark">
                      <span style="color: var(--el-color-danger); font-size: 12px">
                        {{ childError(row)!.slice(0, 40) }}{{ childError(row)!.length > 40 ? '…' : '' }}
                      </span>
                    </el-tooltip>
                    <span v-else-if="childAttempts(row)" class="lc-muted" style="font-size: 12px">
                      已重试 {{ childAttempts(row) }} 次
                    </span>
                    <span v-else class="lc-muted">—</span>
                  </template>
                </template>
              </el-table-column>
              <el-table-column label="更新时间" width="150">
                <template #default="{ row }">
                  <template v-if="isParent(row)">{{ latestUpdate(row) }}</template>
                  <template v-else-if="isScanRow(row)">{{ formatTime(row.task.started_at) }}</template>
                  <template v-else>{{ formatTime(row.updated_at) }}</template>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="230" align="right">
                <template #default="{ row }">
                  <template v-if="isParent(row)">
                    <el-button
                      v-if="auth.isAdmin && row.media_id"
                      link
                      type="primary"
                      size="small"
                      :loading="busyIds.has(row.media_id)"
                      @click="onProbe(row)"
                    >
                      手动探测
                    </el-button>
                    <el-button
                      v-if="auth.isAdmin && row.media_id"
                      link
                      type="primary"
                      size="small"
                      @click="openAddDialog(row)"
                    >
                      加集
                    </el-button>
                    <el-button
                      v-if="row.media_id"
                      link
                      type="primary"
                      size="small"
                      @click="router.push(`/media/${row.media_id}`)"
                    >
                      影视详情
                    </el-button>
                    <el-button link type="primary" size="small" @click="openDetail(row)">查看详情</el-button>
                  </template>
                  <template v-else-if="isScanRow(row)">
                    <el-button link type="primary" size="small" @click="openScanDetail(row.parent, row.task)">
                      查看详情
                    </el-button>
                  </template>
                  <template v-else>
                    <!-- 操作可用性按状态：error/failed→重试；pending→置顶/取消；unmatched→跳过；ready→跳过/手动入队 -->
                    <template v-if="auth.isAdmin && row.id != null">
                      <el-button
                        v-if="row.tq_status === 'error' || (!row.tq_status && childNodeOf(row) === 'failed')"
                        link
                        type="primary"
                        size="small"
                        :loading="retryingIds.has(row.id)"
                        @click="onRetry(row.id)"
                      >
                        重试
                      </el-button>
                      <template v-if="row.tq_status === 'pending'">
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
                          @click="onCancel(row.id, row.episode || row.file_name || '该任务')"
                        >
                          取消
                        </el-button>
                      </template>
                      <el-button
                        v-if="row.tq_status === 'unmatched'"
                        link
                        type="warning"
                        size="small"
                        :loading="busyIds.has(row.id)"
                        @click="onSkip(row.id)"
                      >
                        跳过
                      </el-button>
                      <template v-if="row.tq_status === 'ready'">
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
                    </template>
                    <el-button link type="primary" size="small" @click="openChildDetail(row)">
                      查看详情
                    </el-button>
                  </template>
                </template>
              </el-table-column>
            </el-table>

            <div v-if="store.hasMore" style="text-align: center; margin-top: 16px">
              <el-button :loading="store.loading" @click="loadMore">加载更多</el-button>
            </div>
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
              <el-table-column label="分享码" width="120">
                <template #default="{ row }">
                  <span v-if="shareCodeShort(row.share_code)" class="lc-muted qv-share-code">
                    {{ shareCodeShort(row.share_code) }}
                  </span>
                  <span v-else class="lc-muted">—</span>
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

            <div v-if="store.downloadHasMore" style="text-align: center; margin-top: 16px">
              <el-button :loading="store.downloadLoading" @click="loadMore">加载更多</el-button>
            </div>
          </template>
        </div>
      </el-tab-pane>
    </el-tabs>

    <!-- 手动加集 Dialog：指定影视 + SxxExx 入探测队列 -->
    <el-dialog v-model="addVisible" title="手动加集" width="420px">
      <el-form label-width="80px" @submit.prevent="onAddSubmit">
        <el-form-item label="影视" required>
          <el-select v-model="addForm.media_id" placeholder="选择影视（当前队列中的影视）" style="width: 100%" filterable>
            <el-option v-for="m in mediaOptions" :key="m.value" :value="m.value" :label="m.label" />
          </el-select>
        </el-form-item>
        <el-form-item label="集号" required>
          <el-input v-model="addForm.episode" placeholder="如 S01E05；电影填 movie:片名" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="addVisible = false">取消</el-button>
        <el-button type="primary" :loading="addSubmitting" @click="onAddSubmit">加入探测队列</el-button>
      </template>
    </el-dialog>

    <!-- 任务详情 Drawer：横向五节点流程链 + 全部分集 -->
    <el-drawer v-model="detailVisible" title="任务详情" size="520px">
      <template v-if="detailMedia">
        <div class="qd-head">
          <div style="min-width: 0">
            <div class="qd-title">{{ detailMedia.title || '—' }}</div>
            <div class="lc-muted" style="font-size: 12px; margin-top: 4px">
              {{ mediaTypeLabel(detailMedia.media_type) }} · 已完成 {{ parentCounts(detailMedia) }}
            </div>
          </div>
          <el-tag size="small" :type="queueAggregateType(detailMedia.aggregate_status)" effect="plain">
            {{ queueAggregateLabel(detailMedia.aggregate_status) }}
          </el-tag>
        </div>
        <div style="margin-top: 10px">
          <el-button
            v-if="detailMedia.media_id"
            link
            type="primary"
            size="small"
            @click="router.push(`/media/${detailMedia.media_id}`)"
          >
            查看影视详情
          </el-button>
        </div>

        <template v-if="detailChild">
          <el-divider content-position="left">
            当前分集 · {{ detailChild.episode || detailChild.file_name || '—' }}
          </el-divider>
          <el-steps :active="childStepActive" align-center finish-status="success" class="qd-steps">
            <el-step v-for="n in QUEUE_FLOW_NODES" :key="n" :title="queueNodeLabel(n)" />
          </el-steps>
          <el-alert
            v-if="childError(detailChild)"
            type="error"
            :closable="false"
            show-icon
            :title="`失败诊断：${childError(detailChild)}`"
            style="margin-top: 12px"
          />
          <div class="qd-meta">
            <div class="qd-meta-item">
              <span class="label">重试次数</span>
              <span class="value">{{ detailChild.node_attempt ?? 0 }} 次</span>
            </div>
            <div class="qd-meta-item">
              <span class="label">文件大小</span>
              <span class="value">{{ formatBytes(detailChild.file_size) }}</span>
            </div>
            <div class="qd-meta-item">
              <span class="label">节点开始</span>
              <span class="value">{{ formatTime(detailChild.node_started_at) }}</span>
            </div>
            <div class="qd-meta-item">
              <span class="label">节点结束</span>
              <span class="value">{{ formatTime(detailChild.node_finished_at) }}</span>
            </div>
          </div>
        </template>
        <el-empty
          v-else-if="detailChildren.length === 0"
          description="暂无分集子任务"
          :image-size="80"
          style="margin-top: 12px"
        />

        <el-divider content-position="left">全部分集（{{ detailChildren.length }}）</el-divider>
        <div class="qd-children">
          <div
            v-for="c in detailChildren"
            :key="c.__key ?? c.id"
            class="qd-child"
            :class="{ active: c.__key === detailChildKey }"
            @click="selectChild(c)"
          >
            <span class="ep">{{ c.episode || c.file_name || '—' }}</span>
            <el-tag
              size="small"
              :type="c.tq_status ? taskQueueStatusType(c.tq_status) : queueNodeType(childNodeOf(c))"
              effect="plain"
              :style="tagStyle(c.tq_status ? taskQueueStatusColor(c.tq_status) : null)"
            >
              {{ c.tq_status ? taskQueueStatusLabel(c.tq_status) : queueNodeLabel(childNodeOf(c)) }}
            </el-tag>
            <span v-if="c.node_attempt" class="lc-muted" style="font-size: 12px">
              重试 {{ c.node_attempt }}
            </span>
            <span class="size lc-muted">{{ formatBytes(c.file_size) }}</span>
          </div>
        </div>
      </template>
    </el-drawer>

    <!-- 巡检详情 Drawer：结果摘要为主角 + 5 阶段流程链辅助 -->
    <el-drawer v-model="scanVisible" title="巡检详情" size="520px">
      <div v-if="scanDetail" v-loading="scanLoading">
        <div class="qd-head">
          <div style="min-width: 0">
            <div class="qd-title">{{ scanParentTitle }}</div>
            <div class="lc-muted" style="font-size: 12px; margin-top: 4px">
              {{ formatTime(scanDetail.started_at) }}
              <template v-if="scanDetail.duration_seconds != null">
                · 耗时 {{ Math.round(scanDetail.duration_seconds) }} 秒
              </template>
            </div>
          </div>
          <el-tag size="small" :type="taskStatusType(scanDetail.status)" effect="plain">
            {{ taskStatusLabel(scanDetail.status) }}
          </el-tag>
        </div>

        <!-- 5 阶段流程链（辅助）：失败红、跳过黄定位到具体节点 -->
        <template v-if="scanDetail.phases">
          <el-divider content-position="left">流程</el-divider>
          <el-steps :active="scanStepActive" align-center finish-status="success" class="qd-steps">
            <el-step
              v-for="n in SCAN_PHASE_NODES"
              :key="n"
              :title="scanPhaseLabel(n)"
              :status="scanStepStatus(n)"
              :class="{ 'is-skipped': scanStepSkipped(n) }"
            />
          </el-steps>
        </template>

        <!-- 结果摘要统计行 -->
        <template v-if="scanStats.length">
          <el-divider content-position="left">结果摘要</el-divider>
          <div class="qd-meta" style="margin-top: 0">
            <div v-for="s in scanStats" :key="s.label" class="qd-meta-item">
              <span class="label">{{ s.label }}</span>
              <span class="value">{{ s.value }}</span>
            </div>
          </div>
        </template>

        <!-- 缺集明细列表 -->
        <template v-if="scanMissingItems.length">
          <el-divider content-position="left">缺集明细（{{ scanMissingItems.length }}）</el-divider>
          <div class="qd-children">
            <div v-for="(m, i) in scanMissingItems" :key="m.episode ?? i" class="qd-child static">
              <span class="ep">{{ m.episode || '—' }}</span>
              <el-tag size="small" :type="scanResultType(m.result)" effect="plain">
                {{ scanResultLabel(m.result) }}
              </el-tag>
            </div>
          </div>
        </template>

        <!-- 失败 / 跳过诊断 -->
        <el-alert
          v-if="scanDetail.status === 'error' && scanDetail.message"
          type="error"
          :closable="false"
          show-icon
          :title="`失败诊断：${scanDetail.message}`"
          style="margin-top: 12px"
        />
        <el-alert
          v-else-if="scanDetail.status === 'skipped' && scanDetail.message"
          type="warning"
          :closable="false"
          show-icon
          :title="scanDetail.message"
          style="margin-top: 12px"
        />

        <div style="margin-top: 16px; text-align: right">
          <el-button
            type="primary"
            :loading="rescanning"
            :disabled="!scanMediaId"
            @click="onRescan"
          >
            重新巡检
          </el-button>
        </div>
      </div>
    </el-drawer>
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

.qv-silent-countdown {
  font-size: 12px;
  color: #8b87a8;
  margin-top: 4px;
}

/* 探测中 tag 脉冲动画（状态色约定：probing/transferring 蓝脉冲） */
.qv-pulse {
  animation: qv-pulse 1.6s ease-in-out infinite;
}

@keyframes qv-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.55;
  }
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

/* ---------- 详情 Drawer（沿用既有样式） ---------- */
.qd-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.qd-title {
  font-weight: 600;
  font-size: 15px;
  word-break: break-all;
}

.qd-steps {
  margin: 6px 0 2px;
}

.qd-steps :deep(.el-step__title) {
  font-size: 12px;
}

/* 巡检链：skipped 阶段节点黄色呈现（对齐「跳过=warning」语义） */
.qd-steps :deep(.el-step.is-skipped .el-step__icon) {
  color: var(--el-color-warning);
  border-color: var(--el-color-warning);
}

.qd-steps :deep(.el-step.is-skipped .el-step__title) {
  color: var(--el-color-warning);
}

.qd-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 24px;
  margin-top: 14px;
}

.qd-meta-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.qd-meta-item .label {
  font-size: 12px;
  color: var(--lc-text-secondary, var(--el-text-color-secondary));
}

.qd-meta-item .value {
  font-size: 13px;
}

.qd-children {
  border: 1px solid var(--lc-border, var(--el-border-color-lighter));
  border-radius: 8px;
  overflow: hidden;
}

.qd-child {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  cursor: pointer;
  font-size: 13px;
  border-bottom: 1px solid var(--lc-border, var(--el-border-color-lighter));
  transition: background 0.15s ease;
}

.qd-child:last-child {
  border-bottom: none;
}

.qd-child:hover {
  background: var(--lc-hover-bg, var(--el-fill-color-light));
}

.qd-child.active {
  background: var(--lc-accent-soft, var(--el-color-primary-light-9));
}

/* 静态明细行（巡检缺集列表）：不可点击，无 hover 反馈 */
.qd-child.static {
  cursor: default;
}

.qd-child.static:hover {
  background: transparent;
}

.qd-child .ep {
  flex: 1;
  min-width: 0;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.qd-child .size {
  font-size: 12px;
  flex-shrink: 0;
}
</style>
