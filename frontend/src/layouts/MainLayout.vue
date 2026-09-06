<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { useAuthStore } from '../stores/auth'
import { useNotificationsStore } from '../stores/notifications'
import { useQueueStore } from '../stores/queue'
import { formatGb, timeAgo } from '../utils/format'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const notifications = useNotificationsStore()
const queueStore = useQueueStore()

const activeMenu = computed(() => {
  if (route.path.startsWith('/media')) return '/'
  return route.path
})

const pageTitle = computed(() => route.meta.title ?? '')

let timer: ReturnType<typeof setInterval> | undefined

onMounted(() => {
  notifications.fetchList()
  queueStore.fetchCapacity()
  timer = setInterval(() => {
    notifications.fetchList()
    queueStore.fetchCapacity()
  }, 30000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})

async function handleLogout() {
  await ElMessageBox.confirm('确定要退出登录吗？', '退出登录', {
    confirmButtonText: '退出',
    cancelButtonText: '取消',
    type: 'warning',
  })
  auth.logout()
  router.push('/login')
}

async function onNotificationClick(id: number, read: boolean | undefined) {
  if (!read) {
    await notifications.markRead(id)
  }
}

async function onReadAll() {
  await notifications.markAllRead()
}
</script>

<template>
  <el-container style="min-height: 100vh">
    <el-aside width="224px" class="lc-aside">
      <div class="lc-logo" @click="router.push('/')">
        <span class="lc-logo-mark">映</span>
        <div class="lc-logo-text">
          <div class="name">拾光云映</div>
          <div class="sub">LumenCloud</div>
        </div>
      </div>
      <el-menu :default-active="activeMenu" router class="lc-menu" :border="false">
        <el-menu-item index="/">
          <el-icon><Film /></el-icon>
          <span>影视库</span>
        </el-menu-item>
        <el-menu-item index="/emby">
          <el-icon><Monitor /></el-icon>
          <span>Emby 影视库</span>
        </el-menu-item>
        <el-menu-item index="/queue">
          <el-icon><List /></el-icon>
          <span>转存队列</span>
        </el-menu-item>
        <el-menu-item index="/approvals">
          <el-icon><Stamp /></el-icon>
          <span>想看审批</span>
        </el-menu-item>
        <template v-if="auth.isAdmin">
          <!-- 「添加影视」入口统一收拢到影视库页右上角按钮（/media/add 路由保留可用），避免双入口 -->
          <!-- <el-menu-item index="/media/add">
            <el-icon><CirclePlus /></el-icon>
            <span>添加影视</span>
          </el-menu-item> -->
          <el-menu-item index="/settings">
            <el-icon><Setting /></el-icon>
            <span>设置</span>
          </el-menu-item>
          <el-menu-item index="/logs">
            <el-icon><Document /></el-icon>
            <span>运行日志</span>
          </el-menu-item>
        </template>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="lc-header" height="60px">
        <h2 class="lc-page-title">{{ pageTitle }}</h2>
        <div class="lc-header-right">
          <div v-if="queueStore.capacity" class="lc-capacity">
            <el-progress
              type="circle"
              :width="30"
              :stroke-width="4"
              :percentage="queueStore.usagePercent"
              :status="queueStore.usagePercent >= 90 ? 'exception' : undefined"
            />
            <div class="text">
              夸克容量 {{ formatGb(queueStore.capacity.used_gb) }} /
              {{ formatGb(queueStore.capacity.total_gb) }}
            </div>
          </div>

          <el-popover placement="bottom-end" :width="360" trigger="click">
            <template #reference>
              <el-badge :value="notifications.unreadCount" :hidden="notifications.unreadCount === 0" :max="99">
                <el-button circle aria-label="通知">
                  <el-icon><Bell /></el-icon>
                </el-button>
              </el-badge>
            </template>
            <div class="lc-notify-panel">
              <div class="lc-notify-header">
                <span>通知</span>
                <el-button
                  v-if="notifications.unreadCount > 0"
                  link
                  type="primary"
                  size="small"
                  @click="onReadAll"
                >
                  全部已读
                </el-button>
              </div>
              <el-scrollbar max-height="360px">
                <el-empty
                  v-if="notifications.items.length === 0"
                  description="暂无通知"
                  :image-size="60"
                />
                <div
                  v-for="item in notifications.items"
                  :key="item.id"
                  class="lc-notify-item"
                  :class="{ unread: !item.read }"
                  @click="onNotificationClick(item.id, item.read)"
                >
                  <div class="title">
                    <span class="dot" />
                    {{ item.title || '通知' }}
                  </div>
                  <div class="msg">{{ item.message }}</div>
                  <div class="time">{{ timeAgo(item.created_at) }}</div>
                </div>
              </el-scrollbar>
            </div>
          </el-popover>

          <el-dropdown trigger="click">
            <div class="lc-user">
              <el-avatar :size="32" class="lc-avatar">{{ auth.user?.username?.slice(0, 1) }}</el-avatar>
              <span class="username">{{ auth.user?.username }}</span>
              <el-tag size="small" :type="auth.isAdmin ? 'warning' : 'info'" effect="plain">
                {{ auth.isAdmin ? '管理员' : '访客' }}
              </el-tag>
            </div>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="handleLogout">
                  <el-icon><SwitchButton /></el-icon>
                  退出登录
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>

      <el-main class="lc-main">
        <router-view v-slot="{ Component }">
          <transition name="fade-slide" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.lc-aside {
  background: rgba(10, 13, 17, 0.6);
  border-right: 1px solid var(--lc-border);
  display: flex;
  flex-direction: column;
  backdrop-filter: blur(8px);
}

.lc-logo {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 18px 16px;
  cursor: pointer;
  border-bottom: 1px solid var(--lc-border);
}

.lc-logo-mark {
  width: 38px;
  height: 38px;
  border-radius: 10px;
  background: linear-gradient(135deg, var(--lc-accent), #b97f1e);
  color: #1a1408;
  font-family: var(--lc-font-display);
  font-size: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
}

.lc-logo-text .name {
  font-family: var(--lc-font-display);
  font-size: 16px;
  letter-spacing: 0.05em;
}

.lc-logo-text .sub {
  font-size: 11px;
  color: var(--lc-text-secondary);
  letter-spacing: 0.12em;
}

.lc-menu {
  border-right: none;
  background: transparent;
  --el-menu-text-color: var(--lc-text-secondary);
  --el-menu-hover-bg-color: rgba(255, 255, 255, 0.05);
  --el-menu-active-color: var(--lc-accent);
  flex: 1;
}

.lc-menu :deep(.el-menu-item.is-active) {
  background: var(--lc-accent-soft);
  border-radius: 8px;
  margin: 0 8px;
}

.lc-menu :deep(.el-menu-item) {
  margin: 2px 8px;
  border-radius: 8px;
  height: 44px;
}

.lc-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--lc-border);
  background: rgba(14, 17, 22, 0.7);
  backdrop-filter: blur(8px);
  padding: 0 20px;
}

.lc-page-title {
  margin: 0;
  font-size: 18px;
}

.lc-header-right {
  display: flex;
  align-items: center;
  gap: 18px;
}

.lc-user {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  outline: none;
}

.lc-avatar {
  background: var(--lc-accent-soft);
  color: var(--lc-accent);
  font-weight: 600;
}

.username {
  font-size: 14px;
}

.lc-main {
  padding: 20px 24px 40px;
}

.lc-notify-panel {
  margin: -12px;
}

.lc-notify-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 14px;
  border-bottom: 1px solid var(--lc-border);
  font-weight: 600;
}

.lc-notify-item {
  padding: 10px 14px;
  border-bottom: 1px solid var(--lc-border);
  cursor: pointer;
  transition: background 0.15s ease;
}

.lc-notify-item:hover {
  background: rgba(255, 255, 255, 0.03);
}

.lc-notify-item .title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
}

.lc-notify-item .dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: transparent;
}

.lc-notify-item.unread .dot {
  background: var(--lc-accent);
}

.lc-notify-item .msg {
  font-size: 12px;
  color: var(--lc-text-secondary);
  margin-top: 4px;
  line-height: 1.5;
}

.lc-notify-item .time {
  font-size: 11px;
  color: var(--lc-text-secondary);
  margin-top: 4px;
  opacity: 0.7;
}
</style>
