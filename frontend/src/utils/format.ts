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

// ---------- 两队列状态文案（docs/影视下载两队列重设计.md §8.1 状态色约定） ----------

/**
 * TaskQueue 探测层状态 → [中文标签, tag type, 自定义色]
 * 状态色约定：pending 灰 / probing 蓝 / ready 青 / unmatched 灰紫（弱化）/ error 红 / done 绿。
 * Element tag type 无法表达「青」「灰紫」，第三元素为自定义 hex，
 * 由视图层 el-tag color 属性应用（designer Top-5 约定）。
 */
const TASK_QUEUE_STATUS_MAP: Record<string, [string, string, string | null]> = {
  pending: ['待探测', 'info', null],
  probing: ['探测中', 'primary', null],
  ready: ['就绪', 'success', '#0e9f9f'],
  error: ['异常', 'danger', null],
  done: ['已完成', 'success', null],
}

export function taskQueueStatusLabel(status: string | null | undefined): string {
  if (!status) return '—'
  return TASK_QUEUE_STATUS_MAP[status]?.[0] ?? status
}

export function taskQueueStatusType(status: string | null | undefined): string {
  if (!status) return 'info'
  return TASK_QUEUE_STATUS_MAP[status]?.[1] ?? 'info'
}

/** 自定义状态色（无则 null，用 Element 默认配色） */
export function taskQueueStatusColor(status: string | null | undefined): string | null {
  if (!status) return null
  return TASK_QUEUE_STATUS_MAP[status]?.[2] ?? null
}

/**
 * DownloadQueue 执行层状态 → [中文标签, tag type, 自定义色]
 * 状态色约定：pending 灰 / transferring 蓝 / downloading 主色 / quota_wait 琥珀 /
 * unmatched 灰紫 / failed 红 / done/skipped 绿。
 * quota_wait 的用户化文案（「等待容量：还差 X G」）由视图层拼装（需要容量数据）。
 */
const DOWNLOAD_QUEUE_STATUS_MAP: Record<string, [string, string, string | null]> = {
  pending: ['排队中', 'info', null],
  transferring: ['转存中', 'primary', null],
  downloading: ['下载中', 'primary', null],
  scrape: ['刮削中', 'primary', null],
  library: ['入库确认', 'primary', null],
  quota_wait: ['等待容量', 'warning', '#d9822b'],
  done: ['已完成', 'success', null],
  skipped: ['已跳过', 'success', null],
  failed: ['失败', 'danger', null],
}

export function downloadQueueStatusLabel(status: string | null | undefined): string {
  if (!status) return '—'
  return DOWNLOAD_QUEUE_STATUS_MAP[status]?.[0] ?? status
}

export function downloadQueueStatusType(status: string | null | undefined): string {
  if (!status) return 'info'
  return DOWNLOAD_QUEUE_STATUS_MAP[status]?.[1] ?? 'info'
}

export function downloadQueueStatusColor(status: string | null | undefined): string | null {
  if (!status) return null
  return DOWNLOAD_QUEUE_STATUS_MAP[status]?.[2] ?? null
}

// ---------- 集数状态 4 色分类（影视详情页「集数状态」列） ----------

/** 集数状态分类 tag 结果 */
export interface EpisodeStateTag {
  /** tag 文案：已在库 / 未开播 / 已开播 / 异常 */
  label: string
  /** Element Plus tag type：绿 / 灰 / 黄 / 红 */
  type: 'success' | 'info' | 'warning' | 'danger'
  /** 分类原因说明（tooltip 详情首行） */
  reason: string
}

/** "YYYY-MM-DD" 是否为未来日期（本地 0 点粒度比较；避免 Date.parse 纯日期串被按 UTC 解析的偏移坑） */
function isFutureDate(yyyymmdd: string, today: Date): boolean {
  const d = new Date(`${yyyymmdd}T00:00:00`)
  if (Number.isNaN(d.getTime())) return false
  const todayStart = new Date(today.getFullYear(), today.getMonth(), today.getDate())
  return d.getTime() > todayStart.getTime()
}

/**
 * 集数状态 4 色分类（纯函数，判定优先级自上而下，短路返回）：
 *   绿「已在库」 in_emby === true
 *   灰「未开播」 air_date 存在且 > 今天（日期粒度；air_date ≤ 今天视为已开播）
 *   红「异常」   status ∈ {failed, error}（任务失败）；
 *                或 season / episode_number 缺失（非标准集号，如 "92完.mp4" 找不到剧集信息）
 *   黄「已开播」 其余（有标准集号、已到开播日、未入 Emby、非失败）
 * 后端字段未上线时拿到 undefined/null：一律按空值继续向下判定，不 crash。
 *
 * @param row episode_state 行（松散结构；in_emby / air_date 为可选新契约字段）
 * @param today 当前时间（默认 new Date()；注入便于测试）
 */
export function episodeStateTag(row: Record<string, unknown>, today: Date = new Date()): EpisodeStateTag {
  if (row.in_emby === true) {
    return { label: '已在库', type: 'success', reason: '该集已在 Emby 媒体库' }
  }
  const airDate = typeof row.air_date === 'string' && row.air_date !== '' ? row.air_date : null
  if (airDate && isFutureDate(airDate, today)) {
    return { label: '未开播', type: 'info', reason: `TMDB 首播日期 ${airDate}，尚未播出` }
  }
  const status = typeof row.status === 'string' ? row.status : ''
  if (status === 'failed' || status === 'error') {
    return {
      label: '异常',
      type: 'danger',
      reason: `任务失败（执行状态：${downloadQueueStatusLabel(status)}）`,
    }
  }
  const season = row.season ?? row.season_number
  const episodeNumber = row.episode_number
  if (season === null || season === undefined || episodeNumber === null || episodeNumber === undefined) {
    return { label: '异常', type: 'danger', reason: '未匹配到标准集号（文件名无法对应剧集信息）' }
  }
  return { label: '已开播', type: 'warning', reason: '已播出，等待下载入库' }
}

/**
 * 集数 tag 的 hover 详情行（分类原因 / Emby 状态 / TMDB 首播 / 原始执行状态 / 大小），
 * 保证用户从「原始执行状态列」切换到分类 tag 后仍能看清细节。
 */
export function episodeStateTooltip(row: Record<string, unknown>): string[] {
  const tag = episodeStateTag(row)
  const lines: string[] = [tag.reason]
  lines.push(`Emby：${row.in_emby === true ? '已在库' : '未入库'}`)
  const air = typeof row.air_date === 'string' && row.air_date !== '' ? row.air_date : '—'
  lines.push(`TMDB 首播：${air}`)
  const status = typeof row.status === 'string' && row.status !== '' ? row.status : null
  lines.push(`执行状态：${downloadQueueStatusLabel(status)}`)
  const size = row.size_gb
  if (typeof size === 'number' && !Number.isNaN(size)) {
    lines.push(`大小：${formatGb(size)}`)
  }
  return lines
}

/** 下载队列「活跃」状态集合（「仅看活跃」开关过滤用） */
export const DOWNLOAD_ACTIVE_STATUSES: readonly string[] = [
  'transferring',
  'downloading',
  'scrape',
  'library',
  'quota_wait',
]

/** 未来时间的相对倒计时（unmatched 静默「2 天后重试」）；已过期返回 null */
export function timeUntil(iso: string | null | undefined): string | null {
  if (!iso) return null
  const diff = new Date(iso).getTime() - Date.now()
  if (Number.isNaN(diff) || diff <= 0) return null
  const min = Math.ceil(diff / 60000)
  if (min < 60) return `${min} 分钟后`
  const hour = Math.floor(min / 60)
  if (hour < 24) return `${hour} 小时后`
  const day = Math.floor(hour / 24)
  if (day < 30) return `${day} 天后`
  return formatTime(iso).slice(0, 10)
}

/** 下载速度（字节/秒 → 可读） */
export function formatSpeed(bytesPerSec: number | null | undefined): string {
  if (typeof bytesPerSec !== 'number' || Number.isNaN(bytesPerSec) || bytesPerSec < 0) return '—'
  if (bytesPerSec >= 1024 ** 2) return `${(bytesPerSec / 1024 ** 2).toFixed(1)} MB/s`
  if (bytesPerSec >= 1024) return `${(bytesPerSec / 1024).toFixed(0)} KB/s`
  return `${bytesPerSec} B/s`
}

/** 分享码缩略（12 位 → 前 4…后 4；无值返回 null） */
export function shareCodeShort(code: string | null | undefined): string | null {
  if (!code) return null
  if (code.length <= 8) return code
  return `${code.slice(0, 4)}…${code.slice(-4)}`
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
  skipped: ['跳过', 'warning'],
  error: ['异常', 'danger'],
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

// ---------- 巡检任务（5 阶段流程链 + 缺集结果） ----------

/** 巡检五阶段线性流程（对齐转存链 QUEUE_FLOW_NODES 的展示模式）：查缺 → 搜索源站 → 匹配文件 → 写入队列 → 完成 */
export const SCAN_PHASE_NODES: readonly string[] = [
  'check',
  'search',
  'match',
  'enqueue',
  'finish',
]

/** 巡检阶段值 → [中文标签, Element Plus tag type]；未知值回退原值 / info */
const SCAN_PHASE_MAP: Record<string, [string, string]> = {
  check: ['查缺', 'info'],
  search: ['搜索源站', 'primary'],
  match: ['匹配文件', 'warning'],
  enqueue: ['写入队列', 'primary'],
  finish: ['完成', 'success'],
}

export function scanPhaseLabel(phase: string | null | undefined): string {
  if (!phase) return '—'
  return SCAN_PHASE_MAP[phase]?.[0] ?? phase
}

export function scanPhaseType(phase: string | null | undefined): string {
  if (!phase) return 'info'
  return SCAN_PHASE_MAP[phase]?.[1] ?? 'info'
}

/** 缺集明细结果 → [中文标签, Element Plus tag type]；未知值回退原值 / info */
const SCAN_RESULT_MAP: Record<string, [string, string]> = {
  enqueued: ['已入队', 'success'],
  not_found: ['未找到源', 'danger'],
  unaired: ['未播出跳过', 'warning'],
  already: ['已收录', 'info'],
}

export function scanResultLabel(result: string | null | undefined): string {
  if (!result) return '—'
  return SCAN_RESULT_MAP[result]?.[0] ?? result
}

export function scanResultType(result: string | null | undefined): string {
  if (!result) return 'info'
  return SCAN_RESULT_MAP[result]?.[1] ?? 'info'
}
