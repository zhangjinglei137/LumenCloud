import { describe, expect, it, vi, beforeEach } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useLogsStore } from './logs'
import { listLogsApi } from '../api'
import type { LogItem } from '../types'

vi.mock('../api', () => ({
  listLogsApi: vi.fn(),
}))

const mockedListLogsApi = vi.mocked(listLogsApi)

function item(id: number): LogItem {
  return {
    id,
    task_type: 'scan',
    media_id: null,
    media_title: null,
    tmdb_id: null,
    status: 'done',
    message: null,
    started_at: null,
    finished_at: null,
  }
}

describe('useLogsStore.fetchPage（消费后端真实 total）', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mockedListLogsApi.mockReset()
  })

  it('limit 传精确 pageSize（不 +1 探测），total 直接用后端真实值', async () => {
    const items = [item(1)]
    mockedListLogsApi.mockResolvedValue({ items, total: 57 })
    const store = useLogsStore()

    await store.fetchPage({ task_type: 'scan' }, 2, 30)

    expect(mockedListLogsApi).toHaveBeenCalledWith({ task_type: 'scan', limit: 30, offset: 30 })
    expect(store.items).toEqual(items)
    expect(store.total).toBe(57)
    expect(store.page).toBe(2)
    expect(store.pageSize).toBe(30)
  })

  it('total 不做估算：页内少于 pageSize 时仍保留后端 total', async () => {
    const items = Array.from({ length: 20 }, (_, i) => item(100 + i))
    mockedListLogsApi.mockResolvedValue({ items, total: 50 })
    const store = useLogsStore()

    await store.fetchPage({}, 2, 30)

    expect(store.items).toHaveLength(20)
    expect(store.total).toBe(50)
  })

  it('末页越界：返回空 items 且 page>1 时回退一页重取，total 取重取结果', async () => {
    mockedListLogsApi
      .mockResolvedValueOnce({ items: [], total: 0 })
      .mockResolvedValueOnce({ items: [item(2)], total: 35 })
    const store = useLogsStore()

    await store.fetchPage({}, 3, 30)

    expect(mockedListLogsApi).toHaveBeenNthCalledWith(1, { limit: 30, offset: 60 })
    expect(mockedListLogsApi).toHaveBeenNthCalledWith(2, { limit: 30, offset: 30 })
    expect(store.items).toEqual([item(2)])
    expect(store.page).toBe(2)
    expect(store.total).toBe(35)
  })
})
