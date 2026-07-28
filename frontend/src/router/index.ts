import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('../views/LoginView.vue') },
    { path: '/', name: 'square', component: () => import('../views/MediaSquareView.vue'), meta: { requiresAuth: true } },
    { path: '/media/:id', name: 'detail', component: () => import('../views/MediaDetailView.vue'), meta: { requiresAuth: true } },
    { path: '/my-list', name: 'mylist', component: () => import('../views/MyList.vue'), meta: { requiresAuth: true } },
    { path: '/notifications', name: 'notifications', component: () => import('../views/NotificationCenter.vue'), meta: { requiresAuth: true } },
    { path: '/admin', name: 'admin', component: () => import('../views/AdminDashboard.vue'), meta: { requiresAuth: true, requiresAdmin: true } },
    { path: '/admin/tasks', name: 'tasks', component: () => import('../views/TaskMonitor.vue'), meta: { requiresAuth: true, requiresAdmin: true } },
  ]
})

router.beforeEach(async (to, _from, next) => {
  const token = localStorage.getItem('token')
  if (to.meta.requiresAuth && !token) return next('/login')

  if (to.meta.requiresAdmin && token) {
    const authStore = useAuthStore()
    if (!authStore.user) await authStore.fetchUser()
    if (!authStore.user?.is_admin) return next('/')
  }

  next()
})

export default router
