import { defineStore } from 'pinia'
import {
  cancelQueueItemApi,
  getCapacityApi,
  getDownloadProgressApi,
  getDownloadQueueStateApi,
  listDownloadQueueApi,
  listQueueApi,
  pauseDownloadQueueApi,
  prioritizeQueueItemApi,
  promoteQueueItemApi,
  resumeDownloadQueueApi,
  retryQueueItemApi,
  skipQueueItemApi,
  sortQueueItemApi,
} from '../api'
import type {
  Capacity,
  DownloadProgressEntry,
  DownloadQueueItem,
  PauseState,
  QueueSortDirection,
  QueueTaskItem,
} from '../types'

export const useQueueStore = defineStore('queue', {
  state: () => ({
    /** 任务队列扁平列表（Task 9 契约） */
    items: [] as QueueTaskItem[],
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
        this.items = append ? [...this.items, ...data] : data
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
    /** 重试任务（兼容 DownloadQueue failed/skipped 与 TaskQueue error） */
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
