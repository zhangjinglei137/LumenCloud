<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { AxiosError } from 'axios'
import { useQueueStore } from '../stores/queue'
import { useAuthStore } from '../stores/auth'
import type { QueueItem } from '../types'
import { formatBytes, formatGb, formatTime, queueStatusLabel, queueStatusType } from '../utils/format'

const router = useRouter()
const store = useQueueStore()
const auth = useAuthStore()

const retryingIds = ref<Set<number>>(new Set())
const refreshingCapacity = ref(false)

// ---------- 任务详情 Drawer ----------

const detailVisible = ref(false)
/** 当前查看详情的行（打开时的快照；列表 15s 自动刷新不影响已打开内容） */
const detailRow = ref<QueueItem | null>(null)

/** 哪些状态提供「查看详情」入口（done 只读灰标、未知状态显示占位 —） */
const VIEW_DETAIL_STATUSES = ['pending', 'transferring', 'downloading', 'failed']

function canViewDetail(status: string): boolean {
  return VIEW_DETAIL_STATUSES.includes(status)
}

function openDetail(row: QueueItem): void {
  detailRow.value = row
  detailVisible.value = true
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

    <!-- 队列列表 -->
    <div class="lc-panel">
      <div class="lc-toolbar" style="margin-bottom: 14px">
        <h3 class="lc-panel-title" style="margin: 0">转存队列</h3>
        <el-button size="small" :loading="store.loading" @click="store.fetchPage()">
          <el-icon style="vertical-align: -2px"><Refresh /></el-icon>&nbsp;刷新
        </el-button>
      </div>

      <el-empty v-if="!store.loading && store.items.length === 0" description="队列为空" :image-size="100" />
      <template v-else>
        <el-table v-loading="store.loading && store.items.length === 0" :data="store.items" style="width: 100%">
          <el-table-column label="文件名" min-width="240">
            <template #default="{ row }">
              <div style="font-weight: 600; font-size: 13px">{{ row.file_name }}</div>
              <div v-if="row.episode" class="lc-muted" style="font-size: 12px; margin-top: 2px">
                {{ row.episode }}
              </div>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="110">
            <template #default="{ row }">
              <el-tag size="small" :type="queueStatusType(row.status)" effect="plain">
                {{ queueStatusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="大小" width="110" align="right">
            <template #default="{ row }">{{ formatBytes(row.file_size) }}</template>
          </el-table-column>
          <el-table-column label="影视" width="100" align="center">
            <template #default="{ row }">
              <el-button
                v-if="row.media_id"
                link
                type="primary"
                size="small"
                @click="router.push(`/media/${row.media_id}`)"
              >
                影视详情
              </el-button>
              <span v-else class="lc-muted">—</span>
            </template>
          </el-table-column>
          <el-table-column v-if="auth.isAdmin" label="分享码" width="110" align="center">
            <template #default="{ row }">
              <span
                v-if="row.share_code_tail"
                class="lc-muted"
                style="font-family: monospace"
                :title="`****${row.share_code_tail}`"
              >
                ****{{ row.share_code_tail }}
              </span>
              <span v-else class="lc-muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="入队时间" width="150">
            <template #default="{ row }">{{ formatTime(row.enqueued_at) }}</template>
          </el-table-column>
          <el-table-column label="更新时间" width="150">
            <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
          </el-table-column>
          <el-table-column label="备注" min-width="180">
            <template #default="{ row }">
              <el-tooltip v-if="row.error" :content="row.error" placement="top" effect="dark">
                <span style="color: var(--el-color-danger); font-size: 12px">
                  {{ row.error.slice(0, 40) }}{{ row.error.length > 40 ? '…' : '' }}
                </span>
              </el-tooltip>
              <span v-else-if="row.quota_reject_count > 0" class="lc-muted" style="font-size: 12px">
                容量拒绝 {{ row.quota_reject_count }} 次
              </span>
              <span v-else class="lc-muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="160" align="right">
            <template #default="{ row }">
              <!-- admin + failed：重试；done：只读灰标；其余状态：查看详情；未知状态：占位 — -->
              <el-button
                v-if="auth.isAdmin && row.status === 'failed'"
                size="small"
                link
                type="primary"
                :loading="retryingIds.has(row.id)"
                @click="onRetry(row.id)"
              >
                重试
              </el-button>
              <el-tag v-else-if="row.status === 'done'" size="small" type="info" effect="plain">
                已完成
              </el-tag>
              <span v-else-if="!canViewDetail(row.status)" class="lc-muted">—</span>
              <el-button
                v-if="canViewDetail(row.status)"
                size="small"
                link
                type="primary"
                @click="openDetail(row)"
              >
                查看详情
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <div v-if="store.hasMore" style="text-align: center; margin-top: 16px">
          <el-button :loading="store.loading" @click="loadMore">加载更多</el-button>
        </div>
      </template>
    </div>

    <!-- 任务详情 Drawer（admin + guest 均可用；guest 隐藏分享码） -->
    <el-drawer v-model="detailVisible" title="任务详情" size="400px">
      <template v-if="detailRow">
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="文件名">
            <span style="word-break: break-all">{{ detailRow.file_name }}</span>
          </el-descriptions-item>
          <el-descriptions-item v-if="detailRow.episode" label="分集">
            {{ detailRow.episode }}
          </el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag size="small" :type="queueStatusType(detailRow.status)" effect="plain">
              {{ queueStatusLabel(detailRow.status) }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="文件大小">
            {{ formatBytes(detailRow.file_size) }}
          </el-descriptions-item>
          <el-descriptions-item label="影视">
            <el-button
              v-if="detailRow.media_id"
              link
              type="primary"
              size="small"
              @click="router.push(`/media/${detailRow.media_id}`)"
            >
              查看影视详情
            </el-button>
            <span v-else class="lc-muted">—</span>
          </el-descriptions-item>
          <el-descriptions-item v-if="auth.isAdmin" label="分享码">
            <span v-if="detailRow.share_code_tail" style="font-family: monospace">
              ****{{ detailRow.share_code_tail }}
            </span>
            <span v-else class="lc-muted">—</span>
          </el-descriptions-item>
          <el-descriptions-item label="入队时间">
            {{ formatTime(detailRow.enqueued_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="更新时间">
            {{ formatTime(detailRow.updated_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="容量拒绝次数">
            {{ detailRow.quota_reject_count }} 次
          </el-descriptions-item>
          <el-descriptions-item v-if="detailRow.error" label="错误信息">
            <span style="color: var(--el-color-danger); font-size: 12px; word-break: break-all">
              {{ detailRow.error }}
            </span>
          </el-descriptions-item>
        </el-descriptions>
        <el-alert
          v-if="detailRow.error"
          type="error"
          :closable="false"
          show-icon
          :title="detailRow.error"
          style="margin-top: 14px"
        />
      </template>
    </el-drawer>
  </div>
</template>
