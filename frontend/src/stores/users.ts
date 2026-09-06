import { defineStore } from 'pinia'
import { deleteUserApi, listUsersApi, patchUserRoleApi } from '../api'
import type { UserItem } from '../types'

/** Q11：用户管理（管理员）。修改角色 / 删除的限制（自改、唯一管理员、关联记录）由后端 409 拦截，全局拦截器展示后端文案 */
export const useUsersStore = defineStore('users', {
  state: () => ({
    items: [] as UserItem[],
    loading: false,
  }),
  actions: {
    async fetchList(): Promise<void> {
      this.loading = true
      try {
        this.items = await listUsersApi()
      } finally {
        this.loading = false
      }
    },
    async patchRole(id: number, role: 'admin' | 'guest'): Promise<void> {
      await patchUserRoleApi(id, role)
      await this.fetchList()
    },
    async remove(id: number): Promise<void> {
      await deleteUserApi(id)
      await this.fetchList()
    },
  },
})
