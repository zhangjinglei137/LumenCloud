<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { AxiosError } from 'axios'
import { getScanTaskDetailApi, scanMediaApi } from '../api'
import { useQueueStore } from '../stores/queue'
import { useAuthStore } from '../stores/auth'
import type { QueueChildTask, QueueMediaTask, ScanTaskDetail, ScanTaskSummary } from '../types'
import {
  QUEUE_FLOW_NODES,
  SCAN_PHASE_NODES,
  formatBytes,
  formatGb,
  formatTime,
  mediaTypeLabel,
  queueAggregateLabel,
  queueAggregateType,
  queueNodeLabel,
  queueNodeType,
  scanPhaseLabel,
  scanResultLabel,
  scanResultType,
  taskStatusLabel,
  taskStatusType,
  timeAgo,
} from '../utils/format'

const router = useRouter()
const store = useQueueStore()
const auth = useAuthStore()

const retryingIds = ref<Set<number>>(new Set())
const refreshingCapacity = ref(false)

// ---------- 行类型与展示辅助 ----------

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

/** 失败诊断文案：后端 _child_dto 只返回 node_error */
function childError(c: QueueChildTask): string | null {
  return c.node_error || null
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
    p.children?.filter((c) => !isScanRow(c) && childNodeOf(c) === 'failed').length ?? 0
  )
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
  const failed = children.find((c) => childNodeOf(c) === 'failed')
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

let timer: ReturnType<typeof setInterval> | undefined

onMounted(async () => {
  await Promise.all([store.fetchPage(), store.fetchCapacity()])
  // 队列是高频变化的数据，15 秒自动刷新
  timer = setInterval(() => {
    store.fetchPage()
    store.fetchCapacity()
  }, 15000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})

async function onRetry(id: number) {
  const next = new Set(retryingIds.value)
  next.add(id)
  retryingIds.value = next
  try {
    await store.retry(id)
    ElMessage.success('已重新加入队列')
    await store.fetchPage()
  } catch (e) {
    // 统一拦截器已按后端 detail 提示；此处按状态码补充「同步列表」语义
    const status = (e as AxiosError)?.response?.status
    if (status === 404) {
      // 任务不存在或已被消费/状态已变更 → 刷新列表消除陈旧行
      ElMessage.warning('任务不存在或状态已变更，已为你刷新列表')
      await store.fetchPage()
    } else if (status === 409) {
      // episode_state 双表状态不一致（拦截器已提示「请稍后重试」）→ 同步列表
      await store.fetchPage()
    }
  } finally {
    const done = new Set(retryingIds.value)
    done.delete(id)
    retryingIds.value = done
  }
}

async function loadMore() {
  await store.fetchPage(true)
}
</script>

<template>
  <div class="lc-page">
    <!-- 容量概览 -->
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
      <el-progress
        :percentage="store.usagePercent"
        :stroke-width="14"
        :status="
          store.usagePercent >= 90 ? 'exception' : store.usagePercent >= 70 ? 'warning' : undefined
        "
        striped
        striped-flow
      />
      <div class="lc-stat-row" style="margin-top: 14px">
        <div class="lc-stat">
          <span class="label">已用</span>
          <span class="value">{{ formatGb(store.capacity.used_gb) }}</span>
        </div>
        <div class="lc-stat">
          <span class="label">总量</span>
          <span class="value">{{ formatGb(store.capacity.total_gb) }}</span>
        </div>
        <div class="lc-stat">
          <span class="label">队列预估占用</span>
          <span class="value">{{ formatGb(store.capacity.pending_estimate) }}</span>
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

    <!-- 任务队列列表（影视任务树，可展开分集子任务） -->
    <div class="lc-panel">
      <div class="lc-toolbar" style="margin-bottom: 14px">
        <h3 class="lc-panel-title" style="margin: 0">任务队列</h3>
        <el-button size="small" :loading="store.loading" @click="store.fetchPage()">
          <el-icon style="vertical-align: -2px"><Refresh /></el-icon>&nbsp;刷新
        </el-button>
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
                <div style="font-weight: 600; font-size: 13px">{{ row.episode || '—' }}</div>
                <div class="lc-muted" style="font-size: 12px; margin-top: 2px; word-break: break-all">
                  {{ row.file_name || '—' }}
                </div>
              </template>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="120">
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
              <el-tag v-else size="small" :type="queueNodeType(childNodeOf(row))" effect="plain">
                {{ queueNodeLabel(childNodeOf(row)) }}
              </el-tag>
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
                <span v-else-if="row.node_attempt" class="lc-muted" style="font-size: 12px">
                  已重试 {{ row.node_attempt }} 次
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
          <el-table-column label="操作" width="170" align="right">
            <template #default="{ row }">
              <template v-if="isParent(row)">
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
                <el-button
                  v-if="auth.isAdmin && childNodeOf(row) === 'failed' && row.id != null"
                  link
                  type="primary"
                  size="small"
                  :loading="retryingIds.has(row.id)"
                  @click="onRetry(row.id)"
                >
                  重试
                </el-button>
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
            <el-tag size="small" :type="queueNodeType(childNodeOf(c))" effect="plain">
              {{ queueNodeLabel(childNodeOf(c)) }}
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
