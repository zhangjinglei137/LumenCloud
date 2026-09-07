import { defineStore } from 'pinia'
import { getCapacityApi, listQueueApi, retryQueueItemApi } from '../api'
import type { Capacity, QueueChildTask, QueueMediaTask } from '../types'

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
  }),
  getters: {
    usagePercent: (s) => {
      if (!s.capacity || !s.capacity.total_gb) return 0
      return Math.min(100, Math.round((s.capacity.used_gb / s.capacity.total_gb) * 100))
    },
  },
  actions: {
    async fetchPage(append = false): Promise<void> {
      this.loading = true
      try {
        const offset = append ? this.items.length : 0
        const data = await listQueueApi(this.pageSize, offset)
        const normalized = normalizeQueueTree(data)
        this.items = append ? [...this.items, ...normalized] : normalized
        this.hasMore = data.length >= this.pageSize
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
  },
})
