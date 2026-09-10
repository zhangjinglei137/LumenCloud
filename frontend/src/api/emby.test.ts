import { describe, expect, it, vi, beforeEach } from 'vitest'
import { listEmbyLibraryApi, listEmbyLibrariesApi } from './index'

vi.mock('./http', () => ({ default: { get: vi.fn() } }))
import http from './http'
const mockedGet = vi.mocked(http.get)

describe('Emby API 契约', () => {
  beforeEach(() => mockedGet.mockReset())

  it('listEmbyLibraryApi 序列化 library_id/itemType/status', async () => {
    mockedGet.mockResolvedValue({ data: { items: [], total: 0, item_type: null } })
    await listEmbyLibraryApi({ library_id: 'm1', itemType: 'movie', status: 'continuing' })
    expect(mockedGet).toHaveBeenCalledWith('/emby/library', {
      params: { library_id: 'm1', item_type: 'movie', status: 'continuing' },
    })
  })

  it('listEmbyLibraryApi 缺省不传多余参数', async () => {
    mockedGet.mockResolvedValue({ data: { items: [], total: 0, item_type: null } })
    await listEmbyLibraryApi({ library_id: 'x1' })
    expect(mockedGet).toHaveBeenCalledWith('/emby/library', { params: { library_id: 'x1' } })
  })

  it('listEmbyLibrariesApi 返回 is_anime 字段', async () => {
    mockedGet.mockResolvedValue({
      data: { libraries: [{ id: 't1', name: '动漫', collection_type: 'tvshows', is_anime: true }], total: 1 },
    })
    const res = await listEmbyLibrariesApi()
    expect(res.libraries[0].is_anime).toBe(true)
    expect(mockedGet).toHaveBeenCalledWith('/emby/libraries')
  })
})
