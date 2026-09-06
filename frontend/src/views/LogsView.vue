<script setup lang="ts">
import { onMounted, reactive } from 'vue'
import { useLogsStore, type LogFilter } from '../stores/logs'
import { formatTime, taskStatusLabel, taskStatusType, taskTypeLabel, taskTypeType } from '../utils/format'

const store = useLogsStore()

const filter = reactive<LogFilter>({
  task_type: undefined,
  status: undefined,
  media_id: undefined,
})

// 与后端任务类型取值对齐（backend/app/tasks/*）；media_scan/recovery 为历史别名
const taskTypes = ['scan_media', 'scan_all_media', 'media_scan', 'transfer', 'transfer_retry', 'download', 'cleanup', 'nastools_sync', 'notification_scan', 'capacity_alert', 'recover', 'recovery']

onMounted(() => {
  store.fetchPage(filter)
})

async function search() {
  await store.fetchPage(filter)
}

async function loadMore() {
  await store.fetchPage(filter, true)
}
</script>

<template>
  <div class="lc-page">
    <div class="lc-panel">
      <div class="lc-toolbar">
        <div class="left">
          <el-select
            v-model="filter.task_type"
            placeholder="任务类型"
            clearable
            style="width: 180px"
            @change="search"
          >
            <el-option v-for="t in taskTypes" :key="t" :label="taskTypeLabel(t)" :value="t" />
          </el-select>
          <el-select
            v-model="filter.status"
            placeholder="状态"
            clearable
            style="width: 140px"
            @change="search"
          >
            <el-option label="成功" value="success" />
            <el-option label="失败" value="failed" />
            <el-option label="运行中" value="running" />
          </el-select>
          <el-input-number
            v-model="filter.media_id"
            placeholder="影视 ID"
            :controls="false"
            :min="1"
            style="width: 130px"
            @change="search"
          />
          <el-button type="primary" :loading="store.loading" @click="search">
            <el-icon style="vertical-align: -2px"><Search /></el-icon>&nbsp;查询
          </el-button>
        </div>
      </div>

      <el-empty v-if="!store.loading && store.items.length === 0" description="没有符合条件的日志" :image-size="100" />
      <template v-else>
        <el-table v-loading="store.loading && store.items.length === 0" :data="store.items" style="width: 100%; margin-top: 16px">
          <el-table-column label="ID" width="80">
            <template #default="{ row }">{{ row.id }}</template>
          </el-table-column>
          <el-table-column label="任务类型" width="170">
            <template #default="{ row }">
              <el-tooltip
                v-if="taskTypeLabel(row.task_type) === row.task_type"
                :content="`未知类型：${row.task_type}`"
                placement="top"
              >
                <el-tag size="small" type="info" effect="plain">{{ row.task_type }}</el-tag>
              </el-tooltip>
              <el-tag v-else size="small" :type="taskTypeType(row.task_type)" effect="plain">
                {{ taskTypeLabel(row.task_type) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="影视 ID" width="90" align="center">
            <template #default="{ row }">
              <router-link v-if="row.media_id" :to="`/media/${row.media_id}`" class="media-link">
                {{ row.media_id }}
              </router-link>
              <span v-else class="lc-muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="taskStatusType(row.status)" effect="plain">
                {{ taskStatusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="信息" min-width="260">
            <template #default="{ row }">
              <el-tooltip v-if="row.message" :content="row.message" placement="top" effect="dark">
                <span class="lc-muted msg">{{ row.message }}</span>
              </el-tooltip>
              <span v-else class="lc-muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="开始时间" width="160">
            <template #default="{ row }">{{ formatTime(row.started_at) }}</template>
          </el-table-column>
          <el-table-column label="耗时" width="100" align="right">
            <template #default="{ row }">
              <span v-if="row.started_at && row.finished_at" class="lc-muted">
                {{
                  Math.max(
                    0,
                    Math.round((new Date(row.finished_at).getTime() - new Date(row.started_at).getTime()) / 1000),
                  )
                }}s
              </span>
              <span v-else class="lc-muted">—</span>
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

<style scoped>
.msg {
  display: inline-block;
  max-width: 420px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  vertical-align: bottom;
  font-size: 12px;
}

.media-link {
  color: var(--lc-accent);
  text-decoration: none;
}

.media-link:hover {
  text-decoration: underline;
}
</style>
