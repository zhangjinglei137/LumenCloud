<template>
  <el-affix>
    <el-header class="top-header">
      <div class="nav-inner">
        <div class="brand">拾光云映</div>
        <el-menu
          :default-active="active"
          class="nav-menu"
          mode="horizontal"
          router
          background-color="#1a1a2e"
          text-color="#e0e0e0"
          active-text-color="#409eff"
        >
          <el-menu-item index="/">影视广场</el-menu-item>
          <el-menu-item index="/my-list">我的片单</el-menu-item>
          <el-menu-item index="/notifications">
            通知
            <el-badge
              v-if="notificationStore.unreadCount > 0"
              :value="notificationStore.unreadCount"
              class="nav-badge"
            />
          </el-menu-item>
          <el-menu-item v-if="authStore.user?.is_admin" index="/admin">管理</el-menu-item>
        </el-menu>
        <div class="nav-actions">
          <el-button type="danger" size="small" plain @click="logout">退出</el-button>
        </div>
      </div>
    </el-header>
  </el-affix>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useNotificationStore } from '../stores/notification'

defineProps<{ active: string }>()

const router = useRouter()
const authStore = useAuthStore()
const notificationStore = useNotificationStore()

onMounted(() => {
  authStore.fetchUser()
  notificationStore.fetchUnreadCount()
})

function logout() {
  authStore.logout()
  router.push('/login')
}
</script>

<style scoped>
.top-header {
  background: #1a1a2e;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  padding: 0 24px;
  height: 64px;
  display: flex;
  align-items: center;
}
.nav-inner {
  max-width: 1400px;
  width: 100%;
  margin: 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.brand {
  color: #fff;
  font-size: 20px;
  font-weight: 700;
  margin-right: 40px;
}
.nav-menu {
  flex: 1;
  border-bottom: none;
}
.nav-badge {
  margin-left: 6px;
  line-height: 1;
}
.nav-actions {
  margin-left: 24px;
}
</style>
