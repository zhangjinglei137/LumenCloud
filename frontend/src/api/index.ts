import axios, { AxiosError, InternalAxiosRequestConfig } from 'axios'

const api = axios.create({ baseURL: '/api' })

api.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = localStorage.getItem('token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

api.interceptors.response.use(
  res => res,
  (err: AxiosError) => {
    if (err.response?.status === 401) {
      localStorage.removeItem('token')
      window.location.href = '/login'
    }
    return Promise.reject(err)
  }
)

export default api

export const authAPI = {
  login: (username: string, password: string) => api.post('/auth/login', { username, password }),
  me: () => api.get('/auth/me')
}

export const mediaAPI = {
  search: (keyword: string, page: number = 1) => api.get('/media/search', { params: { keyword, page } }),
  detail: (id: string | number) => api.get(`/media/${id}`),
  list: (params?: any) => api.get('/media/', { params })
}

export const subscriptionAPI = {
  subscribe: (mediaId: string | number) => api.post(`/subscriptions/${mediaId}`),
  unsubscribe: (mediaId: string | number) => api.delete(`/subscriptions/${mediaId}`),
  list: () => api.get('/subscriptions/'),
  vote: (mediaId: string | number) => api.post(`/subscriptions/${mediaId}/vote`),
  unvote: (mediaId: string | number) => api.delete(`/subscriptions/${mediaId}/vote`)
}

export const interactionAPI = {
  rate: (mediaId: string | number, score: number) => api.post(`/interactions/rating/${mediaId}`, { score }),
  getRating: (mediaId: string | number) => api.get(`/interactions/rating/${mediaId}`),
  setStatus: (mediaId: string | number, status: string) => api.put(`/interactions/status/${mediaId}?status=${status}`),
  getStatus: (mediaId: string | number) => api.get(`/interactions/status/${mediaId}`),
  listStatus: (status: string) => api.get('/interactions/status', { params: { status } })
}

export const adminAPI = {
  subscriptions: () => api.get('/admin/subscriptions'),
  approve: (mediaId: string | number, scanFrequencyHours: number = 24) => api.post('/admin/approve', { media_id: mediaId, scan_frequency_hours: scanFrequencyHours }),
  deleteMedia: (mediaId: string | number) => api.delete(`/admin/media/${mediaId}`)
}

export const taskAPI = {
  list: (status?: string) => api.get('/tasks/', { params: { status } }),
  detail: (id: string | number) => api.get(`/tasks/${id}`)
}

export const notificationAPI = {
  list: (unreadOnly: boolean = false) => api.get('/notifications/', { params: { unread_only: unreadOnly } }),
  markRead: (id: string | number) => api.put(`/notifications/${id}/read`),
  markAllRead: () => api.put('/notifications/read-all')
}
