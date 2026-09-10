import { describe, expect, it, vi, beforeEach } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useEmbyStore } from './emby'
import { listEmbyLibrariesApi, listEmbyLibraryApi } from '../api'
import type { EmbyLibraryFolder, EmbyLibraryItem } from '../types'

vi.mock('../api', () => ({
  listEmbyLibrariesApi: vi.fn(),
  listEmbyLibraryApi: vi.fn(),
}))

const mockedList = vi.mocked(listEmbyLibrariesApi)
const mockedListLibrary = vi.mocked(listEmbyLibraryApi)

function item(emby_id: string, title: string): EmbyLibraryItem {
  return {
    emby_id,
    title,
    type: 'movie',
    year: 2026,
    poster_url: null,
    community_rating: null,
    tmdb_id: null,
    emby_web_url: null,
    in_media: false,
    media_id: null,
  }
}

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

  it('is_anime 仅对 tvshows 生效：非 tvshows 库即使标记 anime 也不进动漫分组', async () => {
    mockedList.mockResolvedValue({
      libraries: [
        lib('f1', 'movies', true),
        lib('m1', 'mixed', true),
        lib('n1', null, true),
        lib('t1', 'tvshows', false),
        lib('a1', 'tvshows', true),
      ],
      total: 5,
    })
    const store = useEmbyStore()
    await store.fetchLibraries()

    expect(store.libraryGroups.movie.map((l) => l.id)).toEqual(['f1'])
    expect(store.libraryGroups.series.map((l) => l.id)).toEqual(['t1'])
    expect(store.libraryGroups.anime.map((l) => l.id)).toEqual(['a1'])
    expect(store.libraryGroups.all.map((l) => l.id)).toEqual(['m1', 'n1'])
  })
})

describe('useEmbyStore.fetchLibrary（去分页数据流）', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mockedListLibrary.mockReset()
  })

  it('大列表全量保留：fetchLibrary 不做客户端截断/分页', async () => {
    const big = Array.from({ length: 150 }, (_, i) => item(`emby-${i}`, `条目 ${i}`))
    mockedListLibrary.mockResolvedValue({ items: big, total: 150, item_type: 'movie' })

    const store = useEmbyStore()
    await store.fetchLibrary({ library_id: 'm1', itemType: 'movie' })

    expect(store.items).toHaveLength(150)
    expect(store.items[0].emby_id).toBe('emby-0')
    expect(store.items[149].emby_id).toBe('emby-149')
    expect(store.error).toBeNull()
  })
})
