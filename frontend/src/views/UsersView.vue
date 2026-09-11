<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '../stores/auth'
import { useUsersStore } from '../stores/users'
import type { UserItem } from '../types'
import { formatTime } from '../utils/format'

const store = useUsersStore()
const auth = useAuthStore()

/** 正在删除的用户 id */
const removingIds = ref<Set<number>>(new Set())

onMounted(() => {
  store.fetchList()
})

function roleTagType(role: string): 'warning' | 'info' {
  return role === 'admin' ? 'warning' : 'info'
}

function roleLabel(role: string): string {
  return role === 'admin' ? '管理员' : '访客'
}

/** 管理员总数（列表统计，用于判断「唯一管理员」禁删） */
const adminCount = computed(() => store.items.filter((u) => u.role === 'admin').length)

/** 前端本地分页（用户量通常不大；接口无分页参数，客户端切片，单页时分页自动隐藏） */
const currentPage = ref(1)
const pageSize = ref(20)
const pagedItems = computed<UserItem[]>(() => {
  const start = (currentPage.value - 1) * pageSize.value
  return store.items.slice(start, start + pageSize.value)
})

/** 删除按钮禁用原因；返回 null 表示允许删除 */
function removeDisabledReason(row: UserItem): string | null {
  if (row.id === auth.user?.id) return '不能删除自己'
  if (row.role === 'admin' && adminCount.value === 1) return '至少保留一个管理员'
  return null
}

async function onRemove(row: UserItem): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确定删除用户「${row.username}」吗？删除后该用户无法登录，且操作不可恢复。`,
      '删除用户',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  removingIds.value.add(row.id)
  try {
    await store.remove(row.id)
    ElMessage.success(`已删除用户 ${row.username}`)
  } catch {
    // 409（自己 / 唯一管理员 / 存在关联记录）已由拦截器提示后端文案
  } finally {
    removingIds.value.delete(row.id)
  }
}
</script>

<template>
  <div class="lc-page">
    <div class="lc-panel">
      <div class="lc-toolbar" style="margin-bottom: 16px">
        <div>
          <h3 class="lc-panel-title" style="margin: 0">用户管理</h3>
          <p class="lc-muted" style="margin: 4px 0 0; font-size: 12px">
            角色仅作展示；删除受限制：不能删除自己、最后一个管理员、或存在关联记录（审批 / 通知 / 邀请码）的用户。
          </p>
        </div>
        <div class="right">
          <el-button size="small" :loading="store.loading" @click="store.fetchList()">
            <el-icon style="vertical-align: -2px"><Refresh /></el-icon>&nbsp;刷新
          </el-button>
        </div>
      </div>

      <el-table v-loading="store.loading && store.items.length === 0" :data="pagedItems" size="small">
        <el-table-column label="用户名" min-width="160">
          <template #default="{ row }">
            <span>{{ row.username }}</span>
            <el-tag v-if="auth.user?.id === row.id" size="small" effect="plain" type="info" style="margin-left: 6px">
              当前用户
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="角色" width="150">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" :type="roleTagType(row.role)">
              {{ roleLabel(row.role) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="注册时间" width="160">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="注册邀请码" width="140">
          <template #default="{ row }">
            <span style="font-family: monospace">{{ row.invite_code ?? '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="90" align="right">
          <template #default="{ row }">
            <el-tooltip
              :disabled="removeDisabledReason(row) === null"
              :content="removeDisabledReason(row) ?? ''"
              placement="top"
            >
              <span style="display: inline-block">
                <el-button
                  size="small"
                  link
                  type="danger"
                  :loading="removingIds.has(row.id)"
                  :disabled="removingIds.has(row.id) || removeDisabledReason(row) !== null"
                  @click="onRemove(row)"
                >
                  删除
                </el-button>
              </span>
            </el-tooltip>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无用户" :image-size="80" />
        </template>
      </el-table>

      <el-pagination
        v-if="store.items.length > 0"
        v-model:current-page="currentPage"
        v-model:page-size="pageSize"
        class="lc-pagination"
        style="margin-top: 16px"
        background
        layout="total, sizes, prev, pager, next"
        :total="store.items.length"
        :page-sizes="[20, 50, 100]"
        hide-on-single-page
      />
    </div>
  </div>
</template>
