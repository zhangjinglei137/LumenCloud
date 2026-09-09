/**
 * TMDB 海报相对路径 → 后端海报代理地址。
 *
 * 后端海报代理校验 `/t/p/` 前缀（SSRF 防护白名单），而库里存的是 TMDB 原始
 * 格式（如 `/m7aoxGa7NWw2abNIoWL8NxYzpO8.jpg`，无 size 段），因此这里统一
 * 注入 `/t/p/w500` size 段，保证请求形如 `/t/p/w500{poster_path}` 通过校验，
 * 且回源 URL 带清晰尺寸。已带 `/t/p/` 前缀的路径直接透传（幂等）。
 * 空/null 返回 null（调用方已有标题占位兜底）；路径经 encodeURIComponent 编码。
 */
const POSTER_SIZE = '/t/p/w500'

export function posterUrl(path: string | null | undefined): string | null {
  if (!path) return null
  const p = path.startsWith('/t/p/') ? path : `${POSTER_SIZE}${path}`
  return `/api/poster?p=${encodeURIComponent(p)}`
}
