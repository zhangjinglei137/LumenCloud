import { defineStore } from 'pinia'
import {
  addQueueTaskApi,
  cancelQueueItemApi,
  getCapacityApi,
  getDownloadProgressApi,
  getDownloadQueueStateApi,
  listDownloadQueueApi,
  listQueueApi,
  pauseDownloadQueueApi,
  prioritizeQueueItemApi,
  probeMediaApi,
  promoteQueueItemApi,
  resumeDownloadQueueApi,
  retryQueueItemApi,
  skipQueueItemApi,
  sortQueueItemApi,
} from '../api'
import type {
  AddQueueTaskRequest,
  Capacity,
  DownloadProgressEntry,
  DownloadQueueItem,
  PauseState,
  QueueChildTask,
  QueueMediaTask,
  QueueSortDirection,
} from '../types'

/** 旧扁平结构的 status → 五节点值（用于后端未完成改造时的映射） */
function mapLegacyStatusToNode(status?: string | null): string {
  switch (status) {
    case 'pending':
      return 'transfer'
    case 'transferring':
      return 'download'
    case 'downloading':
      return 'downloading'
    case 'done':
      return 'done'
    case 'failed':
      return 'failed'
    default:
      return 'idle'
  }
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null
}

function normalizeParent(raw: QueueMediaTask, index: number): QueueMediaTask {
  const children = (raw.children ?? []).map((c, j) => ({
    ...c,
    __key: `c-${c.id ?? j}`,
  }))
  return { ...raw, children, __key: `m-${raw.media_id ?? index}` }
}

/** 旧扁平结构：按 media_id 分组合成影视父级，并推算聚合状态 */
function groupLegacyItems(items: QueueChildTask[]): QueueMediaTask[] {
  const groups = new Map<string, { mediaId: number | null; children: QueueChildTask[] }>()
  items.forEach((item, idx) => {
    const mediaId = typeof item.media_id === 'number' ? item.media_id : null
    const groupKey = mediaId === null ? `single-${item.id ?? idx}` : String(mediaId)
    if (!groups.has(groupKey)) groups.set(groupKey, { mediaId, children: [] })
    groups.get(groupKey)!.children.push({
      ...item,
      node: item.node ?? mapLegacyStatusToNode(item.status),
      node_error: item.node_error ?? item.error ?? null,
      __key: `c-${item.id ?? idx}`,
    })
  })
  return [...groups.entries()].map(([groupKey, g], i) => {
    const children = g.children
    const done = children.filter((c) => c.node === 'done').length
    const failed = children.filter((c) => c.node === 'failed').length
    const running = children.some((c) =>
      ['transfer', 'download', 'downloading', 'scrape', 'library'].includes(c.node ?? ''),
    )
    const aggregate =
      children.length > 0 && done === children.length
        ? 'all_done'
        : failed > 0 && done + failed === children.length
          ? 'partial_failed'
          : running
            ? 'running'
            : 'waiting'
    return {
      media_id: g.mediaId,
      // 旧结构无标题，给可辨识的占位；有 media_id 时可点「影视详情」跳转
      title: g.mediaId === null ? '未关联影视' : `影视 #${g.mediaId}`,
      media_type: null,
      aggregate_status: aggregate,
      total_count: children.length,
      done_count: done,
      children,
      __key: `m-${groupKey}-${i}`,
    }
  })
}

/**
 * 归一化 /api/queue 返回：
 * - 新结构（元素含 children 数组）→ 影视任务树
 * - 旧扁平结构（QueueItem[]）→ 按 media_id 聚合的合成树
 * 混合返回（部分迁移）也能逐项处理；非数组返回空列表。
 */
export function normalizeQueueTree(payload: unknown): QueueMediaTask[] {
  if (!Array.isArray(payload)) return []
  const treeParents: QueueMediaTask[] = []
  const legacyItems: QueueChildTask[] = []
  payload.forEach((raw, i) => {
    if (isObject(raw) && Array.isArray((raw as QueueMediaTask).children)) {
      treeParents.push(normalizeParent(raw as QueueMediaTask, i))
    } else if (isObject(raw)) {
      legacyItems.push(raw as unknown as QueueChildTask)
    }
  })
  return [...treeParents, ...groupLegacyItems(legacyItems)]
}

export const useQueueStore = defineStore('queue', {
  state: () => ({
    /** 影视任务树（父级 + 可展开分集子任务） */
    items: [] as QueueMediaTask[],
    capacity: null as Capacity | null,
    loading: false,
    page: 1,
    pageSize: 20,
    hasMore: false,
    // ---------- 下载队列 Tab（扁平列表，§8.1） ----------
    downloadItems: [] as DownloadQueueItem[],
    downloadLoading: false,
    downloadHasMore: false,
    // ---------- 全局暂停 / 实时进度 ----------
    pauseState: { paused: false, in_flight: null } as PauseState,
    /** downloading 行实时进度：key = download_queue.id */
    progressMap: {} as Record<number, DownloadProgressEntry>,
  }),
  getters: {
    usagePercent: (s) => {
      if (!s.capacity || !s.capacity.total_gb) return 0
      return Math.min(100, Math.round((s.capacity.used_gb / s.capacity.total_gb) * 100))
    },
    /** 容量三段条：已用 GB */
    usedGb(): number {
      return this.capacity?.used_gb ?? 0
    },
    /** 容量三段条：预留中 GB（后端增强字段缺省时回退队列预估占用） */
    reservedGb(): number {
      const c = this.capacity
      if (!c) return 0
      return c.reserved_gb ?? c.pending_estimate ?? 0
    },
    /** 容量三段条：可用 GB（优先后端可用预测，否则按总量 - 已用 - 预留推算） */
    availableGb(): number {
      const c = this.capacity
      if (!c || !c.total_gb) return 0
      if (c.available_gb != null) return Math.max(0, c.available_gb)
      return Math.max(0, c.total_gb - c.used_gb - this.reservedGb)
    },
    /** 下载队列中处于 downloading 的条目（进度局部轮询的目标） */
    downloadingItems(s): DownloadQueueItem[] {
      return s.downloadItems.filter((d) => d.status === 'downloading')
    },
  },
  actions: {
    async fetchPage(append = false): Promise<void> {
      this.loading = true
      try {
        // 覆盖刷新（手动刷新 / 15s 慢刷）保持已加载条数：一次取回同等数量的最新数据，
        // 避免已加载的后续页被截断、用户浏览位置丢失（§8.1 体验修正）
        const limit = append ? this.pageSize : Math.max(this.items.length, this.pageSize)
        const offset = append ? this.items.length : 0
        const data = await listQueueApi(limit, offset)
        const normalized = normalizeQueueTree(data)
        this.items = append ? [...this.items, ...normalized] : normalized
        this.hasMore = data.length >= limit
      } finally {
        this.loading = false
      }
    },
    async fetchCapacity(force = false): Promise<void> {
      try {
        this.capacity = await getCapacityApi(force)
      } catch {
        // 容量接口失败不阻塞队列展示；拦截器已提示
      }
    },
    /** 重试分集子任务（id 为子任务 id；旧结构下即原 QueueItem.id） */
    async retry(id: number): Promise<void> {
      await retryQueueItemApi(id)
    },

    // ---------- 下载队列 Tab ----------

    /** 拉取下载队列扁平列表；失败保留旧数据，拦截器已提示，不抛错 */
    async fetchDownloadPage(append = false): Promise<void> {
      this.downloadLoading = true
      try {
        // 覆盖刷新保持已加载条数（同 fetchPage），避免轮询截断列表
        const limit = append ? this.pageSize : Math.max(this.downloadItems.length, this.pageSize)
        const offset = append ? this.downloadItems.length : 0
        const data = await listDownloadQueueApi(limit, offset)
        this.downloadItems = append ? [...this.downloadItems, ...data] : data
        this.downloadHasMore = data.length >= limit
      } catch {
        // 失败（网络/500 等）：保留旧数据，拦截器已提示
        if (!append && !this.downloadItems.length) this.downloadItems = []
      } finally {
        this.downloadLoading = false
      }
    },

    // ---------- 全局暂停 / 恢复 ----------

    /** 查询暂停状态（GET /api/queue/download/state，后端已上线） */
    async fetchPauseState(): Promise<void> {
      try {
        this.pauseState = await getDownloadQueueStateApi()
      } catch {
        // 失败保留当前状态，拦截器已提示，不阻塞页面
      }
    },
    /** 暂停整条下载队列（不取新+在途继续）；失败抛给调用方提示 */
    async pause(): Promise<void> {
        const res = await pauseDownloadQueueApi()
        this.pauseState = { ...this.pauseState, paused: res?.paused ?? true }
    },
    /** 恢复整条下载队列 */
    async resume(): Promise<void> {
        const res = await resumeDownloadQueueApi()
        this.pauseState = { ...this.pauseState, paused: res?.paused ?? false }
    },

    // ---------- 单任务控制面（失败均抛给调用方，由 http 拦截器统一提示） ----------

    /** 取消单任务（不可逆，释放预留容量） */
    async cancel(id: number): Promise<void> {
      await cancelQueueItemApi(id)
    },
    /** 置顶/优先 */
    async prioritize(id: number): Promise<void> {
      await prioritizeQueueItemApi(id)
    },
    /** 跳过某集（写 skipped 防重终态） */
    async skip(id: number): Promise<void> {
      await skipQueueItemApi(id)
    },
    /** 手动入队（ready → promote 进下载队列；后端已实现） */
    async promote(id: number): Promise<void> {
      await promoteQueueItemApi(id)
    },
    /** 手动触发单影视探测 */
    async probe(mediaId: number): Promise<void> {
      await probeMediaApi(mediaId)
    },
    /** 手动加集（SxxExx） */
    async addTask(body: AddQueueTaskRequest): Promise<void> {
      await addQueueTaskApi(body)
    },
    /** 排序（pending 内 up/down/top） */
    async sort(id: number, direction: QueueSortDirection): Promise<void> {
      await sortQueueItemApi(id, direction)
    },

    /** downloading 行进度局部轮询；失败静默忽略，不影响列表渲染 */
    async fetchProgress(): Promise<void> {
      try {
        const entries = await getDownloadProgressApi()
        const map: Record<number, DownloadProgressEntry> = {}
        for (const e of entries ?? []) {
          if (typeof e.id === 'number') map[e.id] = e
        }
        this.progressMap = map
      } catch {
        // 进度查询失败：静默降级（进度条显示动画等待态），不打断轮询
      }
    },
  },
})
