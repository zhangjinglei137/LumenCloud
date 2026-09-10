import http from './http'
import { getToken } from './http'
import axios from 'axios'
import type {
  ApprovalItem,
  Capacity,
  ChangePasswordRequest,
  ChangePasswordResponse,
  DownloadProgressEntry,
  DownloadQueueItem,
  EmbyLibraryQuery,
  EmbyLibraryResponse,
  EmbyLibrariesResponse,
  InviteCode,
  LogItem,
  LoginResponse,
  MediaDetail,
  MediaItem,
  MediaPatch,
  NotificationList,
  PauseState,
  QueueSortDirection,
  QueueTaskItem,
  QuarkVerifyResult,
  SettingsResponse,
  TmdbSearchResult,
  User,
  UserItem,
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
  /** Q2①/Q12：可为 null（Emby 条目无 TMDB 关联时传 null） */
  tmdb_id: number | null
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
  return http.post<{ ok: boolean; task_run_id: number | null }>(`/media/${id}/scan`).then((r) => r.data)
}

// ---------- TMDB ----------
export function searchTmdbApi(q: string) {
  return http.get<TmdbSearchResult[]>('/tmdb/search', { params: { q } }).then((r) => r.data)
}

// ---------- 队列与容量 ----------
/** 队列分页响应契约：{items, total}（后端 Task 1-3 已落地） */
export interface QueueListResponse<T> {
  items: T[]
  total: number
}

/**
 * 任务队列。
 * 契约：GET /api/queue?limit&offset → {items, total}；items 为
 * TaskQueue 活跃行 ∪ DownloadQueue 活跃行，
 * 扁平行（巡检 Tab）含 {id, media_id, title, episode, status, node,
 * file_name, file_size, share_code, size_estimated, updated_at, enqueued_at}
 * —— share_url 仅 download 视图（?type=download）返回，flat 行不含；
 * 终态（done/failed/skipped）已被后端剔除。
 */
export function listQueueApi(limit = 50, offset = 0) {
  return http
    .get<QueueListResponse<QueueTaskItem>>('/queue', { params: { limit, offset } })
    .then((r) => r.data)
}

export function retryQueueItemApi(id: number) {
  return http.post(`/queue/${id}/retry`).then((r) => r.data)
}

// ---------- 队列人工控制面（docs/影视下载两队列重设计.md §8.2；后端已全部落地） ----------

/**
 * 下载队列扁平列表（下载队列 Tab）。
 * 契约：GET /api/queue?type=download&limit&offset → {items, total}；仅返回活跃态。
 */
export function listDownloadQueueApi(limit = 50, offset = 0) {
  return http
    .get<QueueListResponse<DownloadQueueItem>>('/queue', { params: { type: 'download', limit, offset } })
    .then((r) => r.data)
}

/** 整条下载队列暂停（暂停=不取新+在途继续，§8.2 语义） */
export function pauseDownloadQueueApi() {
  return http.post<{ paused: boolean }>('/queue/download/pause').then((r) => r.data)
}

/** 整条下载队列恢复 */
export function resumeDownloadQueueApi() {
  return http.post<{ paused: boolean }>('/queue/download/resume').then((r) => r.data)
}

/**
 * 查询暂停状态。
 * 契约：GET /api/queue/download/state → { paused, in_flight }（后端已实现）。
 */
export function getDownloadQueueStateApi() {
  return http.get<PauseState>('/queue/download/state').then((r) => r.data)
}

/** 取消单任务（不可逆：删 aria2 任务 + 清理夸克残留 + 释放预留容量） */
export function cancelQueueItemApi(id: number) {
  return http.post(`/queue/${id}/cancel`).then((r) => r.data)
}

/** 置顶/优先（提升 pending 的准入顺序） */
export function prioritizeQueueItemApi(id: number) {
  return http.post(`/queue/${id}/prioritize`).then((r) => r.data)
}

/** 跳过某集（写 skipped 防重终态，防 scan 重新入队） */
export function skipQueueItemApi(id: number) {
  return http.post(`/queue/${id}/skip`).then((r) => r.data)
}

/**
 * 手动入队（ready → promote 进下载队列）。
 * 契约：POST /api/queue/{id}/promote（后端已实现，§8.1 操作可用性约定的落地端点）。
 */
export function promoteQueueItemApi(id: number) {
  return http.post(`/queue/${id}/promote`).then((r) => r.data)
}

/** 排序（调整准入顺序；direction=up/down/top，移动端降级上下移按钮同源） */
export function sortQueueItemApi(id: number, direction: QueueSortDirection) {
  return http.post(`/queue/${id}/sort`, { direction }).then((r) => r.data)
}

/**
 * downloading 行实时进度（2-3s 局部轮询用）。
 * 契约：GET /api/queue/download/progress → DownloadProgressEntry[]（tellStatus 聚合，后端已实现）。
 * 失败由调用方静默忽略，不影响列表渲染。
 */
export function getDownloadProgressApi() {
  return http.get<DownloadProgressEntry[]>('/queue/download/progress').then((r) => r.data)
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

/**
 * 设置页一键验证：比对已保存的 quark_default_folder 与 AList 夸克挂载的 root_folder_id。
 * 后端恒 200、无请求体；失败（网络/401 等）由 http 拦截器统一提示。
 */
export function verifyQuarkFolderApi() {
  return http.post<QuarkVerifyResult>('/settings/verify-quark').then((r) => r.data)
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

// ---------- 用户管理（Q11，管理员） ----------
/** 用户列表；409 等错误原样抛出（自改角色 / 唯一管理员 / 校验失败），由调用方提示后端文案 */
export function listUsersApi() {
  return http.get<UserItem[]>('/admin/users').then((r) => r.data)
}

export function patchUserRoleApi(id: number, role: 'admin' | 'guest') {
  return http.patch<{ ok: boolean }>(`/admin/users/${id}`, { role }).then((r) => r.data)
}

export function deleteUserApi(id: number) {
  return http.delete<{ ok: boolean }>(`/admin/users/${id}`).then((r) => r.data)
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

/**
 * 获取 Emby 媒体库列表（设置页「剧集页可见的 Emby 媒体库」多选下拉选项来源）。
 * 契约：GET /api/emby/libraries
 *   200 → EmbyLibrariesResponse（libraries: EmbyLibraryFolder[]，含 item_id/name/collection_type）
 *   503 → 后端 emby 未配置/不可达时由 http 拦截器统一提示
 */
export async function listEmbyLibrariesApi(): Promise<EmbyLibrariesResponse> {
  return http.get<EmbyLibrariesResponse>('/emby/libraries').then((r) => r.data)
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
