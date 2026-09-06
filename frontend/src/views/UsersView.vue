<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '../stores/auth'
import { useUsersStore } from '../stores/users'
import type { UserItem } from '../types'
import { formatTime } from '../utils/format'

const store = useUsersStore()
const auth = useAuthStore()

/** 正在修改角色的用户 id（el-select loading）；失败时回滚选中值 */
const patchingRoleIds = ref<Set<number>>(new Set())
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

async function onRoleChange(row: UserItem, role: string): Promise<void> {
  if (role !== 'admin' && role !== 'guest') return
  if (role === row.role) return
  patchingRoleIds.value.add(row.id)
  try {
    await store.patchRole(row.id, role)
    ElMessage.success(`已把 ${row.username} 调整为「${roleLabel(role)}」`)
  } catch {
    // 409（不能修改自己的角色 / 唯一管理员）等已由拦截器提示后端文案；刷新使选中值回退
    await store.fetchList()
  } finally {
    patchingRoleIds.value.delete(row.id)
  }
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
            调整角色立即生效；删除受限制：不能删除自己、最后一个管理员、或存在关联记录（审批 / 通知 / 邀请码）的用户。
          </p>
        </div>
        <div class="right">
          <el-button size="small" :loading="store.loading" @click="store.fetchList()">
            <el-icon style="vertical-align: -2px"><Refresh /></el-icon>&nbsp;刷新
          </el-button>
        </div>
      </div>

      <el-table v-loading="store.loading && store.items.length === 0" :data="store.items" size="small">
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
            <el-select
              :model-value="row.role"
              size="small"
              :loading="patchingRoleIds.has(row.id)"
              :disabled="patchingRoleIds.has(row.id)"
              @change="(v: string) => onRoleChange(row, v)"
            >
              <el-option value="admin" :label="`管理员`" />
              <el-option value="guest" :label="`访客`" />
            </el-select>
            <el-tag size="small" effect="plain" :type="roleTagType(row.role)" style="margin-left: 6px">
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
            <el-button
              size="small"
              link
              type="danger"
              :loading="removingIds.has(row.id)"
              :disabled="removingIds.has(row.id)"
              @click="onRemove(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无用户" :image-size="80" />
        </template>
      </el-table>
    </div>
  </div>
</template>
