import { describe, it, expect } from 'vitest'
import {
  episodeStatusLabel,
  episodeStatusType,
  formatBytes,
} from './format'

describe('归一集数状态映射（episode-status-cache）', () => {
  it('覆盖五状态文案', () => {
    expect(episodeStatusLabel('in_library')).toBe('已在库')
    expect(episodeStatusLabel('error')).toBe('异常')
    expect(episodeStatusLabel('scanning')).toBe('巡检中')
    expect(episodeStatusLabel('not_aired')).toBe('未开播')
    expect(episodeStatusLabel('pending')).toBe('待定')
  })
  it('未知状态回退原文', () => {
    expect(episodeStatusLabel('whatever')).toBe('whatever')
  })
  it('五状态对应 Element tag type', () => {
    expect(episodeStatusType('in_library')).toBe('success')
    expect(episodeStatusType('error')).toBe('danger')
    expect(episodeStatusType('scanning')).toBe('warning')
    expect(episodeStatusType('not_aired')).toBe('info')
    expect(episodeStatusType('pending')).toBe('info')
    expect(episodeStatusType('whatever')).toBe('info')
  })
  it('formatBytes 格式化真实大小', () => {
    expect(formatBytes(2 * 1024 ** 3)).toBe('2.00 GB')
    expect(formatBytes(null)).toBe('—')
  })
})
