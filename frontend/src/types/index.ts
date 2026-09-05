/** 后端 API 契约类型定义（docs/新系统设计.md §9） */

export type Role = 'admin' | 'guest'

export interface User {
  id: number
  username: string
  role: Role
}

export interface LoginResponse {
  access_token: string
  user: User
}

export interface EpisodeStats {
  available?: number
  total?: number
  missing?: number
  downloaded?: number
  [key: string]: unknown
}

export interface TaskRunBrief {
  id?: number
  task_type?: string
  status?: string
  message?: string
  started_at?: string
  finished_at?: string
  [key: string]: unknown
}

export type MediaType = 'tv' | 'movie'

export interface MediaItem {
  id: number
  title: string
  tmdb_id: number
  media_type: MediaType | string
  status: string
  in_emby: boolean
  last_scan_at: string | null
  max_episode_size_gb: number | null
  max_movie_size_gb: number | null
  scan_interval_minutes?: number | null
  episode_stats?: EpisodeStats | null
  last_task_run?: TaskRunBrief | null
}

export interface EpisodeState {
  id?: number
  media_id?: number
  season?: number | null
  episode?: number | null
  status?: string
  size_gb?: number | null
  share_code_tail?: string | null
  created_at?: string
  updated_at?: string
  [key: string]: unknown
}

export interface QueueSummaryItem {
  id?: number
  status?: string
  file_name?: string
  file_size?: number | null
  episode?: string | null
  updated_at?: string
  [key: string]: unknown
}

export interface MediaDetail extends MediaItem {
  poster_path?: string | null
  episode_state?: EpisodeState[]
  transfer_queue?: QueueSummaryItem[]
  [key: string]: unknown
}

export interface MediaPatch {
  max_episode_size_gb?: number | null
  max_movie_size_gb?: number | null
  scan_interval_minutes?: number | null
  status?: string
}

export type QueueStatus =
  | 'pending'
  | 'transferring'
  | 'downloading'
  | 'done'
  | 'failed'
  | string

export interface QueueItem {
  id: number
  status: QueueStatus
  file_name: string
  file_size: number | null
  episode: string | null
  media_id: number | null
  quota_reject_count: number
  error: string | null
  enqueued_at: string | null
  updated_at: string | null
  share_code_tail?: string | null
}

export interface Capacity {
  total_gb: number
  used_gb: number
  source: string
  checked_at: string | null
  pending_estimate: number | null
}

export type ApprovalStatus = 'pending' | 'approved' | 'rejected' | string

export interface ApprovalItem {
  id: number
  title: string
  tmdb_id: number
  media_type: MediaType | string
  poster_path: string | null
  status: ApprovalStatus
  reject_reason: string | null
  created_at: string | null
  requested_by: string | null
}

export interface SettingsResponse {
  config: Record<string, unknown>
  services: Record<string, boolean>
}

export interface LogItem {
  id: number
  task_type: string
  media_id: number | null
  status: string
  message: string | null
  started_at: string | null
  finished_at: string | null
}

export interface InviteCode {
  code: string
  used_by?: string | null
  used_at?: string | null
  created_at?: string | null
}

export interface NotificationItem {
  id: number
  title?: string
  message?: string
  level?: string
  read?: boolean
  created_at?: string
  [key: string]: unknown
}

export interface NotificationList {
  items: NotificationItem[]
  unread_count: number
}

export interface TmdbSearchResult {
  title: string
  tmdb_id: number
  media_type: MediaType | string
  poster_path: string | null
}

export const TMDB_POSTER_BASE = 'https://image.tmdb.org/t/p/w500'
