import { describe, expect, it } from 'vitest'
import { posterUrl } from './poster'

describe('posterUrl', () => {
  it('null / undefined / 空串返回 null', () => {
    expect(posterUrl(null)).toBeNull()
    expect(posterUrl(undefined)).toBeNull()
    expect(posterUrl('')).toBeNull()
  })

  it('合法路径编码为代理 query 参数', () => {
    expect(posterUrl('/t/p/w500/x.jpg')).toBe('/api/poster?p=%2Ft%2Fp%2Fw500%2Fx.jpg')
  })

  it('特殊字符路径正确编码', () => {
    expect(posterUrl('/t/p/w500/a b?c&d=1')).toBe(
      '/api/poster?p=%2Ft%2Fp%2Fw500%2Fa%20b%3Fc%26d%3D1',
    )
  })
})
