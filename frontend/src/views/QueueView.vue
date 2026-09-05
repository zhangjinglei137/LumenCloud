<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useQueueStore } from '../stores/queue'
import { useAuthStore } from '../stores/auth'
import { formatBytes, formatGb, formatTime, queueStatusLabel, queueStatusType } from '../utils/format'

const router = useRouter()
const store = useQueueStore()
const auth = useAuthStore()

const retryingIds = ref<Set<number>>(new Set())

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
  } catch {
    // 拦截器已提示
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
        <el-button size="small" @click="store.fetchCapacity()">
          <el-icon style="vertical-align: -2px"><Refresh /></el-icon>&nbsp;刷新
        </el-button>
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
                查看详情
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
          <el-table-column v-if="auth.isAdmin" label="操作" width="90" align="right">
            <template #default="{ row }">
              <el-button
                v-if="row.status === 'failed'"
                size="small"
                link
                type="primary"
                :loading="retryingIds.has(row.id)"
                @click="onRetry(row.id)"
              >
                重试
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <div v-if="store.hasMore" style="text-align: center; margin-top: 16px">
          <el-button :loading="store.loading" @click="loadMore">加载更多</el-button>
        </div>
      </template>
    </div>
  </div>
</template>
