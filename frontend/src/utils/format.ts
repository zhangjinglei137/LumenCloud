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
