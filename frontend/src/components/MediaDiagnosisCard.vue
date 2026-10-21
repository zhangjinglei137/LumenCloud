<script setup lang="ts">
// 更新诊断卡片（media-detail-update-diagnosis D2/D6）：
// intervention 默认展开并高亮；processing/waiting 折叠为一行摘要。
// 所有文案（label/结论）直接取自后端，前端只做颜色与空态呈现。
import { computed, ref, watch } from 'vue'
import type { MediaDiagnosis } from '../types'
import {
  baselineSourceLabel,
  diagnosisLevelType,
  formatTime,
  notScheduledReasonLabel,
  taskStatusLabel,
  taskStatusType,
  timeAgo,
  timeUntil,
} from '../utils/format'

const props = defineProps<{ diagnosis: MediaDiagnosis }>()
const d = computed(() => props.diagnosis)

const isIntervention = computed(() => d.value.intervention?.level === 'intervention')
const expanded = ref(isIntervention.value)
// 数据刷新后 level 变化（如 waiting → intervention）时重置展开态，避免异常被折叠隐藏
watch(
  () => d.value.intervention?.level,
  () => {
    expanded.value = isIntervention.value
  },
)
function toggle() {
  expanded.value = !expanded.value
}

const noScheduleReason = computed(() => notScheduledReasonLabel(d.value.not_scheduled_reason))
</script>

<template>
  <div class="lc-panel diagnosis-card" :class="{ 'diagnosis-card--intervention': isIntervention }">
    <!-- 摘要行（始终可见，点击切换展开） -->
    <div class="diag-summary" role="button" tabindex="0" @click="toggle" @keydown.enter="toggle">
      <el-tag size="small" :type="diagnosisLevelType(d.intervention?.level)" effect="plain">
        {{ d.intervention?.label ?? '更新诊断' }}
      </el-tag>
      <span class="lc-muted diag-summary-text">{{ d.intervention?.reason ?? '' }}</span>
      <span v-if="d.has_scan && d.scan_started_at" class="lc-muted diag-summary-time">
        上次巡检 {{ timeAgo(d.scan_started_at) }}
      </span>
      <span class="diag-toggle lc-muted">{{ expanded ? '收起' : '展开' }}</span>
    </div>

    <!-- 详情区（展开时渲染） -->
    <div v-if="expanded" class="diag-detail">
      <!-- 从未巡检：空态，不展示缺失/失败等任何误导信息 -->
      <p v-if="!d.has_scan" class="lc-muted diag-empty">
        尚未巡检：触发一次巡检后，这里会展示诊断结论。
      </p>

      <template v-else>
        <!-- 巡检结论 -->
        <div v-if="d.skip_reason_text" class="diag-row">
          <span class="diag-label">巡检结论</span>
          <span>{{ d.skip_reason_text }}</span>
        </div>

        <!-- 缺失集 -->
        <div v-if="d.missing_aired_total > 0" class="diag-row">
          <span class="diag-label">缺失集数</span>
          <span>
            缺失 {{ d.missing_aired_total }} 集
            <template v-if="d.missing_episodes.length > 0">
              ：{{ d.missing_episodes.join('、')
              }}<template v-if="d.missing_more > 0"> 等 {{ d.missing_more }} 集</template>
            </template>
          </span>
        </div>

        <!-- 当前环节 -->
        <div v-if="d.active_episode || d.active_stage" class="diag-row">
          <span class="diag-label">当前环节</span>
          <span>
            <template v-if="d.active_episode">{{ d.active_episode }}</template>
            <template v-if="d.active_episode && d.active_stage"> · </template>
            <template v-if="d.active_stage">{{ d.active_stage }}</template>
          </span>
        </div>

        <!-- 上次尝试 -->
        <div v-if="d.scan_started_at" class="diag-row">
          <span class="diag-label">上次尝试</span>
          <span>
            {{ formatTime(d.scan_started_at) }}（{{ timeAgo(d.scan_started_at) }}）
            <el-tag size="small" :type="taskStatusType(d.scan_status)" effect="plain">
              {{ taskStatusLabel(d.scan_status) }}
            </el-tag>
          </span>
        </div>

        <!-- 基线状态 -->
        <div class="diag-row">
          <span class="diag-label">基线状态</span>
          <span>
            <template v-if="d.baseline_suspect">基线异常</template>
            <template v-else-if="!d.baseline_detail && !d.baseline_source">无基线数据</template>
            <template v-else>基线正常</template>
            <template v-if="d.baseline_source"> · {{ baselineSourceLabel(d.baseline_source) }}</template>
            <template v-if="d.baseline_degraded"> · 已降级</template>
          </span>
        </div>
      </template>

      <!-- 下次巡检（与是否巡检过无关，独立展示） -->
      <div class="diag-row">
        <span class="diag-label">巡检调度</span>
        <span v-if="noScheduleReason">{{ noScheduleReason }}</span>
        <span v-else-if="d.next_scan_at">
          下次巡检 {{ formatTime(d.next_scan_at) }}<template v-if="timeUntil(d.next_scan_at)">（约 {{ timeUntil(d.next_scan_at) }}）</template>
        </span>
        <span v-else>—</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.diagnosis-card--intervention {
  border-left: 3px solid var(--el-color-danger);
}

.diag-summary {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  cursor: pointer;
  user-select: none;
}

.diag-summary-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.diag-summary-time {
  font-size: 12px;
  flex-shrink: 0;
}

.diag-toggle {
  font-size: 12px;
  flex-shrink: 0;
}

.diag-detail {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.diag-row {
  display: flex;
  gap: 10px;
  align-items: baseline;
  font-size: 13px;
  flex-wrap: wrap;
}

.diag-label {
  flex-shrink: 0;
  width: 64px;
  color: var(--lc-text-secondary, #909399);
}

.diag-empty {
  margin: 0;
  font-size: 13px;
}
</style>
