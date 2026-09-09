/**
 * TMDB 海报相对路径 → 后端海报代理地址。
 * 空/null 返回 null（调用方已有标题占位兜底）；路径经 encodeURIComponent 编码。
 */
export function posterUrl(path: string | null | undefined): string | null {
  if (!path) return null
  return `/api/poster?p=${encodeURIComponent(path)}`
}
