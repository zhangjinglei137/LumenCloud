/** 通用格式化工具 */

export function formatGb(gb: number | null | undefined): string {
  if (typeof gb !== 'number' || Number.isNaN(gb)) return '—'
  if (gb >= 1) return `${gb.toFixed(1)} GB`
  return `${(gb * 1024).toFixed(0)} MB`
}

/** file_size 字段按字节计（契约：队列/集数 file_size 为字节，后端另附 file_size_gb 与 size_gb 的 GB 值） */
export function formatSize(fileSize: number | null | undefined): string {
  if (fileSize === null || fileSize === undefined) return '—'
  return formatBytes(fileSize)
}

/** 字节 → 可读大小（GB/MB），供 transfer_queue.file_size / episode_state.file_size 展示 */
export function formatBytes(bytes: number | null | undefined): string {
  if (typeof bytes !== 'number' || Number.isNaN(bytes) || bytes < 0) return '—'
  const gb = bytes / 1024 ** 3
  if (gb >= 1) return `${gb.toFixed(2)} GB`
  const mb = bytes / 1024 ** 2
  if (mb >= 1) return `${mb.toFixed(1)} MB`
  return `${bytes} B`
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return '—'
  const diff = Date.now() - new Date(iso).getTime()
  if (Number.isNaN(diff)) return iso
  const min = Math.floor(diff / 60000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min} 分钟前`
  const hour = Math.floor(min / 60)
  if (hour < 24) return `${hour} 小时前`
  const day = Math.floor(hour / 24)
  if (day < 30) return `${day} 天前`
  return formatTime(iso).slice(0, 10)
}

/** 媒体状态 → 中文标签 + Element Plus tag type */
const MEDIA_STATUS_MAP: Record<string, [string, string]> = {
  tracking: ['订阅中', 'success'],
  downloading: ['下载中', 'primary'],
  active: ['订阅中', 'success'],
  paused: ['已暂停', 'info'],
  completed: ['已完成', 'primary'],
  archived: ['已归档', 'info'],
  error: ['异常', 'danger'],
}

export function mediaStatusLabel(status: string | null | undefined): string {
  if (!status) return '—'
  return MEDIA_STATUS_MAP[status]?.[0] ?? status
}

export function mediaStatusType(status: string | null | undefined): string {
  if (!status) return 'info'
  return MEDIA_STATUS_MAP[status]?.[1] ?? 'info'
}

const QUEUE_STATUS_MAP: Record<string, [string, string]> = {
  pending: ['待转存', 'info'],
  transferring: ['转存中', 'warning'],
  downloading: ['下载中', 'primary'],
  done: ['已完成', 'success'],
  failed: ['失败', 'danger'],
}

export function queueStatusLabel(status: string | null | undefined): string {
  if (!status) return '—'
  return QUEUE_STATUS_MAP[status]?.[0] ?? status
}

export function queueStatusType(status: string | null | undefined): string {
  if (!status) return 'info'
  return QUEUE_STATUS_MAP[status]?.[1] ?? 'info'
}

/** 五节点线性流程（含终态 done）：transfer → download → downloading → scrape → library → done */
export const QUEUE_FLOW_NODES: readonly string[] = [
  'transfer',
  'download',
  'downloading',
  'scrape',
  'library',
  'done',
]

/** 节点值 → [中文标签, Element Plus tag type]；未知值回退原值 / info */
const QUEUE_NODE_MAP: Record<string, [string, string]> = {
  idle: ['待开始', 'info'],
  transfer: ['转存', 'warning'],
  download: ['推送下载', 'primary'],
  downloading: ['下载中', 'primary'],
  scrape: ['刮削', 'warning'],
  library: ['入库确认', 'primary'],
  failed: ['失败', 'danger'],
  done: ['完成', 'success'],
}

export function queueNodeLabel(node: string | null | undefined): string {
  if (!node) return '—'
  return QUEUE_NODE_MAP[node]?.[0] ?? node
}

export function queueNodeType(node: string | null | undefined): string {
  if (!node) return 'info'
  return QUEUE_NODE_MAP[node]?.[1] ?? 'info'
}

/** 影视任务聚合状态 → [中文标签, tag type] */
const QUEUE_AGGREGATE_MAP: Record<string, [string, string]> = {
  all_done: ['全部完成', 'success'],
  partial_failed: ['部分失败', 'danger'],
  running: ['进行中', 'primary'],
  waiting: ['等待中', 'info'],
}

export function queueAggregateLabel(status: string | null | undefined): string {
  if (!status) return '—'
  return QUEUE_AGGREGATE_MAP[status]?.[0] ?? status
}

export function queueAggregateType(status: string | null | undefined): string {
  if (!status) return 'info'
  return QUEUE_AGGREGATE_MAP[status]?.[1] ?? 'info'
}

const TASK_STATUS_MAP: Record<string, [string, string]> = {
  success: ['成功', 'success'],
  failed: ['失败', 'danger'],
  running: ['运行中', 'primary'],
  pending: ['待执行', 'info'],
}

export function taskStatusLabel(status: string | null | undefined): string {
  if (!status) return '—'
  return TASK_STATUS_MAP[status]?.[0] ?? status
}

export function taskStatusType(status: string | null | undefined): string {
  if (!status) return 'info'
  return TASK_STATUS_MAP[status]?.[1] ?? 'info'
}

/** 任务类型 → 中文标签 + Element Plus tag type（未知类型回退原值 / info） */
const TASK_TYPE_MAP: Record<string, [string, string]> = {
  scan_media: ['影视巡检', 'primary'],
  media_scan: ['影视巡检', 'primary'],
  scan_all_media: ['定时巡检', 'primary'],
  transfer: ['转存', 'warning'],
  transfer_retry: ['转存重试', 'warning'],
  download: ['下载', 'primary'],
  nastools_sync: ['目录同步入库', 'success'],
  cleanup: ['空间清理', 'info'],
  notification_scan: ['通知扫描', 'info'],
  capacity_alert: ['容量告警', 'danger'],
  recover: ['超时恢复', 'danger'],
  recovery: ['超时恢复', 'danger'],
}

export function taskTypeLabel(type: string | null | undefined): string {
  if (!type) return '—'
  return TASK_TYPE_MAP[type]?.[0] ?? type
}

export function taskTypeType(type: string | null | undefined): string {
  if (!type) return 'info'
  return TASK_TYPE_MAP[type]?.[1] ?? 'info'
}

export function mediaTypeLabel(t: string | null | undefined): string {
  if (t === 'tv') return '剧集'
  if (t === 'movie') return '电影'
  return t || '—'
}

/** 影视状态（TMDB 为准）→ [中文标签, Element Plus tag type] */
const SERIES_STATUS_MAP: Record<string, [string, string]> = {
  // 剧集归一化值
  continuing: ['更新中', 'success'],
  ended: ['已完结', 'info'],
  'returning series': ['更新中', 'success'],
  // 电影 TMDB 原值（归一化后小写）
  released: ['已上映', 'success'],
  'in production': ['制作中', 'warning'],
  'post production': ['后期制作', 'warning'],
  rumored: ['筹备中', 'info'],
  planned: ['计划中', 'info'],
  canceled: ['已取消', 'danger'],
  cancelled: ['已取消', 'danger'], // 英式拼写兜底
}

/** 归一化：trim + 小写 + 下划线/连字符 → 空格（兼容 "Returning Series" / "in_production" 等形态） */
function normalizeSeriesStatus(status: string): string {
  return status.trim().toLowerCase().replace(/[_-]/g, ' ')
}

/** 影视状态中文标签；空值时 movie→'已上映'（电影兜底），tv→'—'；未知值回退原值 */
export function seriesStatusLabel(status: string | null | undefined, mediaType?: string | null): string {
  if (!status) return mediaType === 'movie' ? '已上映' : '—'
  return SERIES_STATUS_MAP[normalizeSeriesStatus(status)]?.[0] ?? status
}

/** 影视状态 tag type；空值时 movie→'success'，tv→'info'；未知值回退 'info' */
export function seriesStatusType(status: string | null | undefined, mediaType?: string | null): string {
  if (!status) return mediaType === 'movie' ? 'success' : 'info'
  return SERIES_STATUS_MAP[normalizeSeriesStatus(status)]?.[1] ?? 'info'
}
