import { describe, expect, it, vi, beforeEach } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useEmbyStore } from './emby'
import { listEmbyLibrariesApi } from '../api'
import type { EmbyLibraryFolder } from '../types'

vi.mock('../api', () => ({
  listEmbyLibrariesApi: vi.fn(),
  listEmbyLibraryApi: vi.fn(),
}))

const mockedList = vi.mocked(listEmbyLibrariesApi)

function lib(id: string, collection_type: string | null, is_anime = false): EmbyLibraryFolder {
  return { id, name: id, collection_type, is_anime }
}

describe('useEmbyStore.libraryGroups', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mockedList.mockReset()
  })

  it('按 collection_type/is_anime 分组：movies→电影，tvshows+anime→动漫，tvshows→剧集，其余→全部', async () => {
    mockedList.mockResolvedValue({
      libraries: [
        lib('m1', 'movies'),
        lib('s1', 'tvshows'),
        lib('a1', 'tvshows', true),
        lib('x1', null),
        lib('x2', 'mixed'),
      ],
      total: 5,
    })
    const store = useEmbyStore()
    await store.fetchLibraries()

    expect(store.libraryGroups.movie.map((l) => l.id)).toEqual(['m1'])
    expect(store.libraryGroups.series.map((l) => l.id)).toEqual(['s1'])
    expect(store.libraryGroups.anime.map((l) => l.id)).toEqual(['a1'])
    expect(store.libraryGroups.all.map((l) => l.id)).toEqual(['x1', 'x2'])
  })

  it('无媒体库时四个分组均为空', async () => {
    mockedList.mockResolvedValue({ libraries: [], total: 0 })
    const store = useEmbyStore()
    await store.fetchLibraries()

    expect(store.libraryGroups.movie).toEqual([])
    expect(store.libraryGroups.series).toEqual([])
    expect(store.libraryGroups.anime).toEqual([])
    expect(store.libraryGroups.all).toEqual([])
  })
})
