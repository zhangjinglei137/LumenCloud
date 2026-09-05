<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import TmdbSearch from '../components/TmdbSearch.vue'
import { useApprovalsStore } from '../stores/approvals'
import { useAuthStore } from '../stores/auth'
import { TMDB_POSTER_BASE, type ApprovalItem, type TmdbSearchResult } from '../types'
import { formatTime, mediaTypeLabel } from '../utils/format'

const store = useApprovalsStore()
const auth = useAuthStore()

const tab = ref<'pending' | 'all'>('pending')
const dialogVisible = ref(false)
const submitting = ref(false)
const selected = ref<TmdbSearchResult | null>(null)

onMounted(() => {
  store.fetchList()
})

const pendingItems = () => store.items.filter((i) => i.status === 'pending')
const historyItems = () => store.items.filter((i) => i.status !== 'pending')

function visibleItems(): ApprovalItem[] {
  return tab.value === 'pending' ? pendingItems() : historyItems()
}

function statusTag(status: string): { label: string; type: string } {
  if (status === 'pending') return { label: '待审批', type: 'warning' }
  if (status === 'approved') return { label: '已通过', type: 'success' }
  if (status === 'rejected') return { label: '已拒绝', type: 'danger' }
  return { label: status, type: 'info' }
}

function poster(url: string | null): string | null {
  return url ? `${TMDB_POSTER_BASE}${url}` : null
}

async function onApprove(item: ApprovalItem) {
  await ElMessageBox.confirm(`批准《${item.title}》后将自动加入影视库。`, '批准申请', {
    confirmButtonText: '批准',
    cancelButtonText: '取消',
    type: 'success',
  })
  await store.approve(item.id)
  ElMessage.success('已批准并加入影视库')
}

async function onReject(item: ApprovalItem) {
  const { value } = await ElMessageBox.prompt('请输入拒绝原因', '拒绝申请', {
    confirmButtonText: '拒绝',
    cancelButtonText: '取消',
    inputPlaceholder: '例如：资源不合适 / 重复申请',
    inputValidator: (v: string) => (v && v.trim().length > 0) || '请填写拒绝原因',
  })
  await store.reject(item.id, value.trim())
  ElMessage.success('已拒绝')
}

function onSelect(item: TmdbSearchResult) {
  selected.value = item
}

async function submitRequest() {
  if (!selected.value) {
    ElMessage.warning('请先搜索并选择一部影视')
    return
  }
  submitting.value = true
  try {
    await store.create({
      title: selected.value.title,
      tmdb_id: selected.value.tmdb_id,
      media_type: selected.value.media_type,
      poster_path: selected.value.poster_path,
    })
    ElMessage.success('已提交，等待管理员审批')
    dialogVisible.value = false
    selected.value = null
  } catch {
    // 拦截器已提示
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="lc-page">
    <div class="lc-panel">
      <div class="lc-toolbar" style="margin-bottom: 16px">
        <el-radio-group v-model="tab" size="small">
          <el-radio-button value="pending">待审批（{{ pendingItems().length }}）</el-radio-button>
          <el-radio-button value="all">处理记录（{{ historyItems().length }}）</el-radio-button>
        </el-radio-group>
        <div class="right">
          <el-button size="small" :loading="store.loading" @click="store.fetchList()">
            <el-icon style="vertical-align: -2px"><Refresh /></el-icon>&nbsp;刷新
          </el-button>
          <el-button type="primary" @click="dialogVisible = true">
            <el-icon style="vertical-align: -2px"><Plus /></el-icon>&nbsp;提交想看
          </el-button>
        </div>
      </div>

      <el-empty
        v-if="!store.loading && visibleItems().length === 0"
        :description="tab === 'pending' ? '暂无待审批的申请' : '暂无处理记录'"
        :image-size="100"
      />
      <div v-else v-loading="store.loading && store.items.length === 0" class="approval-list">
        <div v-for="item in visibleItems()" :key="item.id" class="approval-item">
          <div class="poster">
            <img v-if="poster(item.poster_path)" :src="poster(item.poster_path)!" :alt="item.title" loading="lazy" />
            <span v-else>{{ item.title }}</span>
          </div>
          <div class="body">
            <div class="title-row">
              <span class="title">{{ item.title }}</span>
              <el-tag size="small" effect="plain">{{ mediaTypeLabel(item.media_type) }}</el-tag>
              <el-tag size="small" :type="statusTag(item.status).type" effect="plain">
                {{ statusTag(item.status).label }}
              </el-tag>
            </div>
            <div class="lc-muted meta">
              <span>TMDB：{{ item.tmdb_id }}</span>
              <span v-if="item.requested_by">申请人：{{ item.requested_by }}</span>
              <span>提交于 {{ formatTime(item.created_at) }}</span>
            </div>
            <div v-if="item.reject_reason" class="reject-reason">拒绝原因：{{ item.reject_reason }}</div>
          </div>
          <div v-if="auth.isAdmin && item.status === 'pending'" class="actions">
            <el-button type="success" size="small" @click="onApprove(item)">批准</el-button>
            <el-button type="danger" size="small" plain @click="onReject(item)">拒绝</el-button>
          </div>
        </div>
      </div>
    </div>

    <!-- 提交想看对话框 -->
    <el-dialog v-model="dialogVisible" title="提交想看" width="720px" top="6vh" destroy-on-close>
      <TmdbSearch @select="onSelect" />
      <div v-if="selected" class="selected-bar">
        已选择：<strong>{{ selected.title }}</strong>
        <span class="lc-muted">（TMDB {{ selected.tmdb_id }} · {{ mediaTypeLabel(selected.media_type) }}）</span>
      </div>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" :disabled="!selected" @click="submitRequest">
          提交申请
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.approval-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.approval-item {
  display: flex;
  gap: 16px;
  align-items: center;
  border: 1px solid var(--lc-border);
  border-radius: 12px;
  padding: 14px 16px;
  transition: border-color 0.2s ease;
}

.approval-item:hover {
  border-color: rgba(233, 180, 76, 0.35);
}

.poster {
  width: 56px;
  height: 80px;
  border-radius: 8px;
  overflow: hidden;
  background: #161b23;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  color: var(--lc-text-secondary);
  text-align: center;
}

.poster img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.body {
  flex: 1;
  min-width: 0;
}

.title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.title {
  font-size: 15px;
  font-weight: 600;
}

.meta {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  font-size: 12px;
  margin-top: 6px;
}

.reject-reason {
  margin-top: 6px;
  font-size: 12px;
  color: var(--el-color-danger);
}

.actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}

.selected-bar {
  margin-top: 14px;
  padding: 10px 14px;
  border-radius: 8px;
  background: var(--lc-accent-soft);
  font-size: 13px;
}
</style>
