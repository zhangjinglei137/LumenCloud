/** 后端 API 契约类型定义（docs/新系统设计.md §9） */

export type Role = 'admin' | 'guest'

export interface User {
  id: number
  username: string
  role: Role
}

export interface LoginResponse {
  access_token: string
  /** Phase 8 起后端返回 token_type；缺省按 Bearer 处理 */
  token_type?: string
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
  /** TMDB 海报路径（含前缀 / 的相对路径；配合 TMDB_POSTER_BASE 拼完整 URL；可为空） */
  poster_path?: string | null
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

/** 设置字段的中文元数据（label/desc/placeholder/默认值提示/sensitive） */
export interface SettingFieldMeta {
  /** 中文标签 */
  label: string
  /** 字段下方灰字说明 */
  desc: string
  /** 输入框占位（中文说明性占位） */
  placeholder?: string
  /** 默认值 / 可选性提示（如「默认 60 分钟」「可选，留空 = 官方地址」） */
  default?: string
  /** 敏感凭据：后端以 *** 掩码回显，未改动时不提交 */
  sensitive?: boolean
}

export interface SettingsResponse {
  /** Phase 8 起后端返回 system_config；敏感键值为 "***" 占位 */
  system_config?: Record<string, unknown>
  /** 旧字段名，向后兼容 */
  config?: Record<string, unknown>
  services: Record<string, boolean>
  /** 前端可配置的凭据键清单（snake_case） */
  editable_keys?: string[]
}

export interface ChangePasswordRequest {
  old_password: string
  new_password: string
}

export interface ChangePasswordResponse {
  ok: boolean
}

export interface LogItem {
  id: number
  task_type: string
  media_id: number | null
  /** 关联 media 的影视名称（join media 表，media 已删除时为 null） */
  media_title: string | null
  /** 关联 media 的 TMDB ID（无关联时为 null） */
  tmdb_id: number | null
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
  /** 年份（movie=release_date / tv=first_air_date 的前 4 位；person 或无年份时为 null） */
  year?: string | null
}

export const TMDB_POSTER_BASE = 'https://image.tmdb.org/t/p/w500'

// ---------- Emby 影视库 ----------
/** Emby 条目类型：movie=电影，series=剧集 */
export type EmbyItemType = 'movie' | 'series'

/** 剧集在更状态筛选：continuing=仅在更，ended=已完结（仅对剧集/动漫 Tab 生效） */
export type EmbySeriesStatus = 'continuing' | 'ended'

/** Emby 库查询参数（GET /api/emby/library） */
export interface EmbyLibraryQuery {
  /** 类型筛选：movie/series，缺省全部 */
  itemType?: EmbyItemType
  /** 剧集状态筛选：continuing 仅在更 / ended 已完结（后端用 SeriesStatus 参数） */
  status?: EmbySeriesStatus
  /** 限定动漫库（后端按 Name 关键词匹配 VirtualFolder，忽略 itemType 过滤） */
  anime?: boolean
}

/** Emby 库单条媒体（GET /api/emby/library 的元素，字段由后端 DTO 保证） */
export interface EmbyLibraryItem {
  /** Emby Item Id（字符串） */
  emby_id: string
  title: string
  type: EmbyItemType
  /** 发行/首播年份，无则 null */
  year: number | null
  /** 海报完整 URL（后端已拼好 Emby Image 端点）；无海报为 null，前端走 fallback */
  poster_url: string | null
  /** Emby 社区评分 0~10，无则 null */
  community_rating: number | null
  /** Emby Web 详情页完整 URL（点击卡片新窗口打开） */
  emby_web_url: string
  /** 是否已收录进本地影视清单（media 表存在同 tmdb_id 记录） */
  in_media: boolean
  /** 本地 Media 记录 id；in_media=false 时为 null */
  media_id: number | null
}

export interface EmbyLibraryResponse {
  items: EmbyLibraryItem[]
  total: number
  /** 库条目类型筛选回显（全部时为 null） */
  item_type: EmbyItemType | null
}
