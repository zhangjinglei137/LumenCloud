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
  /** 影视状态（TMDB 原值，如 Released / Returning Series；后端 _media_dto 回传；存量可能为 null） */
  series_status?: string | null
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
  /** 该集是否已在 Emby 媒体库（后端契约新增；未上线时为 undefined，前端按 false 处理） */
  in_emby?: boolean
  /** TMDB 首播日期 "YYYY-MM-DD"（后端契约新增；null = 未知/未提供） */
  air_date?: string | null
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

export interface TmdbEpisode {
  season?: number | null
  episode?: number | null
  air_date?: string | null
  name?: string | null
  in_emby?: boolean
}

export interface MediaDetail extends MediaItem {
  poster_path?: string | null
  episode_state?: EpisodeState[]
  transfer_queue?: QueueSummaryItem[]
  tmdb_episodes?: TmdbEpisode[]
  [key: string]: unknown
}

export interface MediaPatch {
  max_episode_size_gb?: number | null
  max_movie_size_gb?: number | null
  /** @deprecated 已废弃：巡检改为全局统一调度，该字段仅为兼容历史数据保留，不再生效 */
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

// ---------- 扁平任务列表（Task 9 契约：TaskQueue 活跃行 ∪ DownloadQueue 活跃行） ----------
/** 任务队列扁平行：影视名 - SxxExx 的直接展示单位 */
export interface QueueTaskItem {
  id: number
  media_id: number | null
  /** 影视标题（media 已删时可能为 null） */
  title: string | null
  episode: string | null
  /** 任务状态（TaskQueue 或 DownloadQueue 活跃态） */
  status: string
  /** 执行视图节点；探测视图（TaskQueue）为 null，执行视图（DownloadQueue）与 status 同值 */
  node: string | null
  file_name: string | null
  /** 字节 */
  file_size: number | null
  updated_at: string | null
  enqueued_at: string | null
  [key: string]: unknown
}

// ---------- 任务队列（五节点流程，影视任务树；已废弃，仅作历史兼容） ----------
/**
 * 子任务节点（线性状态机）：
 * idle（待开始）→ transfer（转存）→ download（已推送 aria2）→ downloading（下载中）
 * → scrape（刮削）→ library（入库确认）→ done（完成）；failed 为失败终态。
 * 用 string 兜底，后端新增节点值时前端可原样展示。
 */
export type QueueNode =
  | 'idle'
  | 'transfer'
  | 'download'
  | 'downloading'
  | 'scrape'
  | 'library'
  | 'failed'
  | 'done'
  | string

/** 队列子任务（单集分集）。字段按后端 DTO 设计；实际以后端为准，缺失时前端显示「—」 */
export interface QueueChildTask {
  id: number
  /** 集号（如 S01E10），无集号时前端回退展示 file_name */
  episode?: string | null
  /** 当前节点 */
  node?: QueueNode | null
  /** 当前节点已重试次数 */
  node_attempt?: number | null
  /** 节点失败诊断文案 */
  node_error?: string | null
  node_started_at?: string | null
  node_finished_at?: string | null
  file_name?: string | null
  /** 字节 */
  file_size?: number | null
  updated_at?: string | null
  /** 两队列新契约：探测层状态（pending/probing/ready/unmatched/error/done）；存在时优先于 node 展示 */
  tq_status?: TaskQueueStatus | null
  /** unmatched 静默到期时间（相对倒计时「2 天后重试」的数据源） */
  silent_until?: string | null
  /** 探测到的分享码（12 位；缩略展示由前端处理） */
  share_code?: string | null
  /** 旧扁平结构兼容字段（后端未完成改造时由前端映射） */
  status?: string | null
  error?: string | null
  enqueued_at?: string | null
  share_code_tail?: string | null
  quota_reject_count?: number | null
  media_id?: number | null
  /** 前端内部表格行 key */
  __key?: string
}

/** 队列父级（影视任务）聚合状态 */
export type QueueAggregateStatus = 'all_done' | 'partial_failed' | 'running' | 'waiting' | string

// ---------- 巡检任务（影视级一次性任务，挂队列影视父级下） ----------

/** 巡检任务摘要（GET /api/queue 影视父级 scan_tasks 元素；后端取最近 1 条） */
export interface ScanTaskSummary {
  id: number
  /** running → success / skipped / error */
  status?: string | null
  /** 人话结果文案（如「已入队 2 集」「未找到源」） */
  message?: string | null
  started_at?: string | null
  duration_seconds?: number | null
}

/** 巡检阶段状态：wait 待执行 / process 进行中 / done 完成 / skipped 跳过 / error 异常 */
export type ScanPhaseStatus = 'wait' | 'process' | 'done' | 'skipped' | 'error' | string

/** 单个巡检阶段 */
export interface ScanPhase {
  status?: ScanPhaseStatus | null
  started_at?: string | null
  finished_at?: string | null
  [key: string]: unknown
}

/** 巡检 5 阶段（check→查缺 / search→搜索源站 / match→匹配文件 / enqueue→写入队列 / finish→完成） */
export interface ScanPhases {
  check?: ScanPhase | null
  search?: ScanPhase | null
  match?: ScanPhase | null
  enqueue?: ScanPhase | null
  finish?: ScanPhase | null
  [key: string]: ScanPhase | null | undefined
}

/** 缺集明细结果：enqueued 已入队 / not_found 未找到源 / unaired 未播出跳过 / already 已收录 */
export interface ScanMissingItem {
  episode?: string | null
  result?: 'enqueued' | 'not_found' | 'unaired' | 'already' | string | null
  [key: string]: unknown
}

/** 巡检结果明细（GET /api/logs/{id} 的 scan_detail 字段） */
export interface ScanDetail {
  /** 缺失集总数 */
  missing_total?: number | null
  /** 入队集数 */
  enqueued?: number | null
  /** 已在库跳过数 */
  existing_skipped?: number | null
  /** 大小过滤数 */
  size_filtered?: number | null
  /** 未匹配文件数 */
  unmatched?: number | null
  /** 非视频文件数 */
  non_video?: number | null
  /** 失败/跳过定位到的阶段 key（check/search/match/enqueue/finish），无则 null */
  failed_phase?: string | null
  /** 缺集明细列表 */
  missing_items?: ScanMissingItem[] | null
  [key: string]: unknown
}

/** 巡检任务详情（GET /api/logs/{id}） */
export interface ScanTaskDetail extends ScanTaskSummary {
  task_type?: string | null
  media_id?: number | null
  media_title?: string | null
  finished_at?: string | null
  phases?: ScanPhases | null
  scan_detail?: ScanDetail | null
  [key: string]: unknown
}

/** 队列父级（影视任务树节点） */
export interface QueueMediaTask {
  media_id?: number | null
  title?: string | null
  media_type?: MediaType | string | null
  /** 聚合状态：all_done / partial_failed / running / waiting */
  aggregate_status?: QueueAggregateStatus | null
  /** 总集数 */
  total_count?: number | null
  /** 完成集数 */
  done_count?: number | null
  /** 分集子任务列表 */
  children?: QueueChildTask[]
  /** 两队列新契约：探测层聚合计数（待探/就绪/静默）；缺省时前端按 children.tq_status 推算 */
  probe_counts?: { pending?: number; ready?: number; unmatched?: number } | null
  /** 最近巡检记录（后端取最近 1 条；无记录时缺省/空数组） */
  scan_tasks?: ScanTaskSummary[]
  /** 前端内部表格行 key */
  __key?: string
  [key: string]: unknown
}

export interface Capacity {  total_gb: number
  used_gb: number
  source: string
  checked_at: string | null
  pending_estimate: number | null
  /** §8.2 容量展示增强：队列预留中（GB）；后端未增强时缺省，前端回退 pending_estimate */
  reserved_gb?: number | null
  /** §8.2 容量展示增强：可用预测（GB）；缺省时前端按 total - used - reserved 推算 */
  available_gb?: number | null
}

// ---------- 两队列重设计（docs/影视下载两队列重设计.md §3/§4/§8） ----------

/** TaskQueue 探测层状态：pending 待探测 / probing 探测中 / ready 就绪 / unmatched 静默 / error 异常 / done 完成 */
export type TaskQueueStatus =
  | 'pending'
  | 'probing'
  | 'ready'
  | 'unmatched'
  | 'error'
  | 'done'
  | string

/** DownloadQueue 执行层状态机（§4.2） */
export type DownloadQueueStatus =
  | 'pending'
  | 'transferring'
  | 'downloading'
  | 'scrape'
  | 'library'
  | 'quota_wait'
  | 'done'
  | 'skipped'
  | 'failed'
  | string

/**
 * 下载队列扁平条目（GET /api/queue?type=download 元素）。
 * 字段对齐 download_queue 表 + 展示所需的影视标题。
 */
export interface DownloadQueueItem {
  id: number
  media_id?: number | null
  /** 影视标题（join media；展示用） */
  media_title?: string | null
  /** SxxExx / movie:<title> */
  episode?: string | null
  file_name?: string | null
  /** 字节 */
  file_size?: number | null
  /** 分享码（12 位）；缩略展示由前端处理 */
  share_code?: string | null
  status?: DownloadQueueStatus | null
  node_attempt?: number | null
  node_error?: string | null
  retry_count?: number | null
  /** quota_wait 排队原因（后端可选直出，如「还差 3.2G」）；缺省时前端按容量推算 */
  quota_hint?: string | null
  aria2_gid?: string | null
  enqueued_at?: string | null
  updated_at?: string | null
  [key: string]: unknown
}

/** 全局暂停状态（GET /api/queue/download/state，后端已实现） */
export interface PauseState {
  paused: boolean
  /** 暂停时在途继续完成的任务数（横幅文案用） */
  in_flight?: number | null
}

/** downloading 行实时进度（GET /api/queue/download/progress 元素） */
export interface DownloadProgressEntry {
  /** download_queue.id（或 gid 二选一，后端契约以 id 为准） */
  id?: number | null
  gid?: string | null
  /** 字节/秒 */
  speed?: number | null
  /** 0~100 */
  progress?: number | null
  [key: string]: unknown
}

/** 手动加集请求体（POST /api/queue/tasks） */
export interface AddQueueTaskRequest {
  media_id: number
  /** SxxExx（剧集）或 movie:<title>（电影） */
  episode: string
}

/** 排序方向（POST /api/queue/{id}/sort；移动端降级上下移按钮的语义） */
export type QueueSortDirection = 'up' | 'down' | 'top'

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
  /** Q10：申请人用户名（join 用户表；用户已删除或无申请人为 null），展示优先于 requested_by id */
  requested_by_username: string | null
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
  /** 敏感凭据：后端已改为全明文回显（仅 jwt_secret/init_admin_password 隐藏），前端不再消费它渲染密码框，该标记仅作纵深防御参考 */
  sensitive?: boolean
  /** Q4：布尔字段以下拉形式呈现（如 scan_baseline_required）；未标注的布尔仍为开关 */
  selectOptions?: { value: boolean; label: string }[]
  /** 多选下拉控件标记（如 emby_series_library_ids） */
  multiSelect?: true
  /** 多选/下拉选项来源标识（'embyLibraries' = GET /api/emby/libraries 媒体库清单） */
  optionsSource?: 'embyLibraries'
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

/** POST /api/settings/verify-quark 诊断结果：quark_default_folder 与 AList 夸克挂载根目录比对 */
export interface QuarkVerifyResult {
  /** AList 地址+令牌是否已配置 */
  alist_configured: boolean
  /** 是否在 alist 找到夸克挂载（/quark 或 driver=Quark）；探测失败时 false */
  quark_mount_found: boolean | null
  /** 匹配到的挂载路径 */
  quark_mount_path: string | null
  quark_driver: string | null
  /** alist 夸克驱动 root_folder_id（addition 中，空→null） */
  root_folder_id: string | null
  /** 已保存的 quark_default_folder（未配置→null） */
  configured_folder_id: string | null
  /** true=一致 false=不一致 null=无法判定 */
  match: boolean | null
  root_folder_status: string | null
  /** alist /quark 目录能否列出 */
  fs_list_ok: boolean | null
  fs_error: string | null
  /** /quark 现有条目名（前 20） */
  quark_files: string[]
  quark_file_count: number
  /** alist 存储总数（int） */
  storage_total: number
  storages: { mount_path: string | null; driver: string | null }[]
}

/** Q11：用户管理（GET /api/admin/users 元素） */
export interface UserItem {
  id: number
  username: string
  role: 'admin' | 'guest' | string
  /** 注册时使用的邀请码 */
  invite_code: string | null
  created_at: string | null
}

/** Q11：修改用户角色（PATCH /api/admin/users/{id}） */
export interface PatchUserRoleRequest {
  role: 'admin' | 'guest'
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
  /** Q8①：任务真实耗时（秒，job 入口 time.monotonic() 计时；历史记录为 null） */
  duration_seconds?: number | null
}

export interface InviteCode {
  code: string
  used_by?: string | null
  /** Q6：使用者用户名（join 用户表；用户已删除为 null），展示优先于 used_by id */
  used_by_username?: string | null
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
  /** Q12：关联的 TMDB ID（Emby ProviderIds 无 TMDB 关联时为 null，订阅时原样传 null） */
  tmdb_id: number | null
  /** Emby Web 详情页完整 URL（点击卡片新窗口打开）；D-1：后端 serverId 获取失败时为 null，前端隐藏入口并阻止跳转 */
  emby_web_url: string | null
  /** 是否已收录进本地影视清单（media 表存在同 tmdb_id 记录） */
  in_media: boolean
  /** 本地 Media 记录 id；in_media=false 时为 null */
  media_id: number | null
  /** Q12：剧集在更状态（continuing=在更 / ended=已完结；电影或无状态为 null），后端即将返回 */
  series_status?: 'continuing' | 'ended' | string | null
}

export interface EmbyLibraryResponse {
  items: EmbyLibraryItem[]
  total: number
  /** 库条目类型筛选回显（全部时为 null） */
  item_type: EmbyItemType | null
}

/** Emby 媒体库（VirtualFolder）信息：设置页多选下拉的选项 */
export interface EmbyLibraryFolder {
  item_id: string
  name: string
  collection_type: string | null
}

/** GET /api/emby/libraries 响应 */
export interface EmbyLibrariesResponse {
  libraries: EmbyLibraryFolder[]
  total: number
}
