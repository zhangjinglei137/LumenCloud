import { createRouter, createWebHistory } from 'vue-router'
import axios from 'axios'
import { useAuthStore } from '../stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('../views/LoginView.vue') },
    { path: '/', name: 'square', component: () => import('../views/MediaSquareView.vue'), meta: { requiresAuth: true } },
    { path: '/media/:id', name: 'detail', component: () => import('../views/MediaDetailView.vue'), meta: { requiresAuth: true } },
    { path: '/media/emby/:id', name: 'media-emby-detail', component: () => import('../views/MediaDetailView.vue'), meta: { requiresAuth: true } },
    { path: '/my-list', name: 'mylist', component: () => import('../views/MyList.vue'), meta: { requiresAuth: true } },
    { path: '/notifications', name: 'notifications', component: () => import('../views/NotificationCenter.vue'), meta: { requiresAuth: true } },
    { path: '/admin', name: 'admin', component: () => import('../views/AdminDashboard.vue'), meta: { requiresAuth: true, requiresAdmin: true } },
    { path: '/admin/tasks', name: 'tasks', component: () => import('../views/TaskMonitor.vue'), meta: { requiresAuth: true, requiresAdmin: true } },
  ]
})

router.beforeEach(async (to, _from, next) => {
  // 始终允许访问 login
  if (to.path === '/login') { next(); return }

  const token = localStorage.getItem('token')

  // 检查是否需要首次设置
  try {
    const { data } = await axios.get('/api/setup/status')
    if (data.needs_setup) {
      next('/login')  // LoginView 会检测 setup 状态并显示向导
      return
    }
  } catch { /* ignore */ }

  if (to.meta.requiresAuth && !token) {
    next('/login')
    return
  }

  if (to.meta.requiresAdmin && token) {
    const authStore = useAuthStore()
    if (!authStore.user) await authStore.fetchUser()
    if (!authStore.user?.is_admin) {
      next('/')
      return
    }
  }

  next()
})

export default router
