<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { AxiosError } from 'axios'
import { useQueueStore } from '../stores/queue'
import { useAuthStore } from '../stores/auth'
import type { QueueChildTask, QueueMediaTask } from '../types'
import {
  QUEUE_FLOW_NODES,
  formatBytes,
  formatGb,
  formatTime,
  mediaTypeLabel,
  queueAggregateLabel,
  queueAggregateType,
  queueNodeLabel,
  queueNodeType,
} from '../utils/format'

const router = useRouter()
const store = useQueueStore()
const auth = useAuthStore()

const retryingIds = ref<Set<number>>(new Set())
const refreshingCapacity = ref(false)

// ---------- 行类型与展示辅助 ----------

function isParent(row: unknown): row is QueueMediaTask {
  return Array.isArray((row as QueueMediaTask).children)
}

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
  return p.children?.filter((c) => childNodeOf(c) === 'failed').length ?? 0
}

/** 父级更新时间取子任务最新一次 */
function latestUpdate(p: QueueMediaTask): string {
  const times = (p.children ?? []).map((c) => c.updated_at).filter((t): t is string => !!t)
  return formatTime(times.length ? times.reduce((a, b) => (a > b ? a : b)) : null)
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
          :data="store.items"
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
              {{ isParent(row) ? latestUpdate(row) : formatTime(row.updated_at) }}
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
