import { describe, expect, it } from 'vitest'
import { posterUrl } from './poster'

describe('posterUrl', () => {
  it('null / undefined / 空串返回 null', () => {
    expect(posterUrl(null)).toBeNull()
    expect(posterUrl(undefined)).toBeNull()
    expect(posterUrl('')).toBeNull()
  })

  it('TMDB 原始格式路径自动注入 /t/p/w500 size 段', () => {
    expect(posterUrl('/m7aoxGa7NWw2abNIoWL8NxYzpO8.jpg')).toBe(
      '/api/poster?p=%2Ft%2Fp%2Fw500%2Fm7aoxGa7NWw2abNIoWL8NxYzpO8.jpg',
    )
  })

  it('已带 /t/p/ 前缀的路径不重复拼接', () => {
    expect(posterUrl('/t/p/w500/x.jpg')).toBe('/api/poster?p=%2Ft%2Fp%2Fw500%2Fx.jpg')
  })

  it('特殊字符路径注入 size 段后正确编码', () => {
    expect(posterUrl('/a b?c&d=1')).toBe(
      '/api/poster?p=%2Ft%2Fp%2Fw500%2Fa%20b%3Fc%26d%3D1',
    )
  })
})