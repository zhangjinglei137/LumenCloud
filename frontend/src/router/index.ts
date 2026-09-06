import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '../stores/auth'

declare module 'vue-router' {
  interface RouteMeta {
    title: string
    requiresAdmin?: boolean
    public?: boolean
  }
}

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('../views/LoginView.vue'),
    meta: { title: '登录', public: true },
  },
  {
    path: '/register',
    name: 'register',
    component: () => import('../views/RegisterView.vue'),
    meta: { title: '注册', public: true },
  },
  {
    path: '/',
    component: () => import('../layouts/MainLayout.vue'),
    children: [
      {
        path: '',
        name: 'media-list',
        component: () => import('../views/MediaListView.vue'),
        meta: { title: '影视库' },
      },
      {
        path: 'media/add',
        name: 'media-add',
        component: () => import('../views/MediaAddView.vue'),
        meta: { title: '添加影视', requiresAdmin: true },
      },
      {
        path: 'media/:id',
        name: 'media-detail',
        component: () => import('../views/MediaDetailView.vue'),
        meta: { title: '影视详情' },
      },
      {
        path: 'emby',
        name: 'emby-library',
        component: () => import('../views/EmbyLibraryView.vue'),
        meta: { title: 'Emby 影视库' },
      },
      {
        path: 'queue',
        name: 'queue',
        component: () => import('../views/QueueView.vue'),
        meta: { title: '转存队列' },
      },
      {
        path: 'approvals',
        name: 'approvals',
        component: () => import('../views/ApprovalsView.vue'),
        meta: { title: '想看审批' },
      },
      {
        path: 'users',
        name: 'users',
        component: () => import('../views/UsersView.vue'),
        meta: { title: '用户管理', requiresAdmin: true },
      },
      {
        path: 'settings',
        name: 'settings',
        component: () => import('../views/SettingsView.vue'),
        meta: { title: '设置', requiresAdmin: true },
      },
      {
        path: 'logs',
        name: 'logs',
        component: () => import('../views/LogsView.vue'),
        meta: { title: '运行日志', requiresAdmin: true },
      },
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach(async (to) => {
  document.title = `${to.meta.title} · LumenCloud 拾光云映`

  if (to.meta.public) {
    // 已登录用户访问登录/注册页时回到首页
    if (to.name === 'login' || to.name === 'register') {
      const auth = useAuthStore()
      if (auth.token && (auth.user || (await auth.fetchMe()))) {
        return { path: '/' }
      }
    }
    return true
  }

  const auth = useAuthStore()
  if (!auth.token) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  // 有 token 但内存中无用户信息时，先拉取 /auth/me（失败会清 token 并跳登录）
  if (!auth.user) {
    const ok = await auth.fetchMe()
    if (!ok) {
      return { path: '/login', query: { redirect: to.fullPath } }
    }
  }
  if (to.meta.requiresAdmin && auth.user?.role !== 'admin') {
    ElMessage.warning('该页面仅管理员可访问')
    return { path: '/' }
  }
  return true
})

export default router
