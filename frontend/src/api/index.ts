import http from './http'
import { getToken } from './http'
import axios from 'axios'
import type {
  ApprovalItem,
  Capacity,
  ChangePasswordRequest,
  ChangePasswordResponse,
  EmbyLibraryQuery,
  EmbyLibraryResponse,
  InviteCode,
  LogItem,
  LoginResponse,
  MediaDetail,
  MediaItem,
  MediaPatch,
  NotificationList,
  QueueItem,
  SettingsResponse,
  TmdbSearchResult,
  User,
} from '../types'

// ---------- 认证 ----------
export function loginApi(username: string, password: string) {
  return http.post<LoginResponse>('/auth/login', { username, password }).then((r) => r.data)
}

export function registerApi(username: string, password: string, inviteCode: string) {
  return http
    .post<User>('/auth/register', { username, password, invite_code: inviteCode })
    .then((r) => r.data)
}

export function fetchMeApi() {
  return http.get<User>('/auth/me').then((r) => r.data)
}

/**
 * 修改密码。
 * 注意：后端对「旧密码错误」也返回 401，不能走全局 http 实例
 * （其 401 拦截器会强制跳转登录页），因此用独立 axios 请求，
 * 由调用方根据状态码自行提示。
 */
export async function changePasswordApi(body: ChangePasswordRequest) {
  const res = await axios.post<ChangePasswordResponse>('/api/auth/change-password', body, {
    timeout: 30000,
    headers: { Authorization: `Bearer ${getToken() ?? ''}` },
  })
  return res.data
}

// ---------- 影视 ----------
export function listMediaApi() {
  return http.get<MediaItem[]>('/media').then((r) => r.data)
}

export function getMediaApi(id: number) {
  return http.get<MediaDetail>(`/media/${id}`).then((r) => r.data)
}

export function createMediaApi(data: {
  title: string
  tmdb_id: number
  media_type: string
  poster_path?: string | null
}) {
  return http.post<MediaItem>('/media', data).then((r) => r.data)
}

export function patchMediaApi(id: number, patch: MediaPatch) {
  return http.patch<MediaItem>(`/media/${id}`, patch).then((r) => r.data)
}

export function deleteMediaApi(id: number) {
  return http.delete(`/media/${id}`).then((r) => r.data)
}

export function scanMediaApi(id: number) {
  return http.post<{ ok: boolean; task_run_id: number }>(`/media/${id}/scan`).then((r) => r.data)
}

// ---------- TMDB ----------
export function searchTmdbApi(q: string) {
  return http.get<TmdbSearchResult[]>('/tmdb/search', { params: { q } }).then((r) => r.data)
}

// ---------- 队列与容量 ----------
export function listQueueApi(limit = 50, offset = 0) {
  return http
    .get<QueueItem[]>('/queue', { params: { limit, offset } })
    .then((r) => r.data)
}

export function retryQueueItemApi(id: number) {
  return http.post(`/queue/${id}/retry`).then((r) => r.data)
}

/**
 * 容量状态。
 * force=true 时携带 ?force=1：与后端可选的强制刷新契约对齐——
 * 当前后端版本仍会返回 30s TTL 内的缓存值（忽略该参数），
 * 待后端支持 force 后同一调用即变为真正绕过缓存。
 */
export function getCapacityApi(force = false) {
  return http
    .get<Capacity>('/capacity', { params: force ? { force: 1 } : undefined })
    .then((r) => r.data)
}

// ---------- 审批 ----------
export function listApprovalsApi() {
  return http.get<ApprovalItem[]>('/approvals').then((r) => r.data)
}

export function createApprovalApi(data: {
  title: string
  tmdb_id: number
  media_type: string
  poster_path?: string | null
}) {
  return http.post<ApprovalItem>('/approvals', data).then((r) => r.data)
}

export function approveApi(id: number) {
  return http.post(`/approvals/${id}/approve`).then((r) => r.data)
}

export function rejectApi(id: number, rejectReason: string) {
  return http.post(`/approvals/${id}/reject`, { reject_reason: rejectReason }).then((r) => r.data)
}

// ---------- 设置 / 邀请码 ----------
export function getSettingsApi() {
  return http.get<SettingsResponse>('/settings').then((r) => r.data)
}

export function patchSettingsApi(patch: Record<string, unknown>) {
  return http.patch<SettingsResponse>('/settings', patch).then((r) => r.data)
}

export function listInvitesApi() {
  return http.get<InviteCode[]>('/admin/invites').then((r) => r.data)
}

export function createInvitesApi(count = 1) {
  return http.post<{ codes: string[] }>('/admin/invites', { count }).then((r) => r.data)
}

export function deleteInviteApi(code: string) {
  return http.delete(`/admin/invites/${encodeURIComponent(code)}`).then((r) => r.data)
}

// ---------- Emby 影视库 ----------
/**
 * 拉取 Emby 媒体库列表。
 * 契约：GET /api/emby/library
 *   item_type=movie|series（缺省 = 全部）
 *   status=continuing|ended（在更/完结，后端用 SeriesStatus 参数，仅对剧集生效）
 *   anime=true（限定动漫库，后端按 Name 关键词匹配，忽略 item_type）
 *   200 → EmbyLibraryResponse
 *   503 → {"detail": {"msg": "...", "code": "emby_not_configured" | "emby_unreachable"}}
 *         （code 由前端区分「未配置空态」与「不可达错误态」）
 */
export function listEmbyLibraryApi(params?: EmbyLibraryQuery) {
  const { itemType, status, anime } = params ?? {}
  const query: Record<string, string | boolean> = {}
  if (itemType) query.item_type = itemType
  if (status) query.status = status
  if (anime) query.anime = anime
  return http
    .get<EmbyLibraryResponse>('/emby/library', {
      params: Object.keys(query).length ? query : undefined,
    })
    .then((r) => r.data)
}

// ---------- 日志 ----------
export function listLogsApi(params: {
  task_type?: string
  status?: string
  media_id?: number
  tmdb_id?: number
  title?: string
  limit?: number
  offset?: number
}) {
  // 仅将非空参数传给后端（undefined/null/空串一律剔除）
  const query = Object.fromEntries(
    Object.entries(params).filter(([, value]) => value !== undefined && value !== null && value !== ''),
  )
  return http.get<LogItem[]>('/logs', { params: query }).then((r) => r.data)
}

// ---------- 通知 ----------
export function listNotificationsApi() {
  return http.get<NotificationList>('/notifications').then((r) => r.data)
}

export function markNotificationReadApi(id: number) {
  return http.post(`/notifications/${id}/read`).then((r) => r.data)
}

export function markAllNotificationsReadApi() {
  return http.post('/notifications/read-all').then((r) => r.data)
}
