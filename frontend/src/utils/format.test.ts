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

import { formatTime, timeAgo, timeUntil } from './format'

describe('东八区时间格式化（docker-timezone）', () => {
  it('naive UTC 输入按 UTC 解释并渲染东八区', () => {
    // 09:15 UTC → 17:15 东八区
    expect(formatTime('2026-09-09T09:15:00')).toBe('2026-09-09 17:15')
  })
  it('带 Z 输入解析为绝对时刻后渲染东八区', () => {
    expect(formatTime('2026-09-09T09:15:00Z')).toBe('2026-09-09 17:15')
  })
  it('带 +08:00 偏移输入渲染东八区一致', () => {
    expect(formatTime('2026-09-09T17:15:00+08:00')).toBe('2026-09-09 17:15')
  })
  it('空值与非法输入回退', () => {
    expect(formatTime(null)).toBe('—')
    expect(formatTime(undefined)).toBe('—')
    expect(formatTime('not-a-date')).toBe('not-a-date')
  })
  it('timeAgo 基于绝对时刻差，跨时区一致', () => {
    // 固定 now：2026-09-09T10:00:00Z，输入为 5 分钟前的绝对时刻
    const now = Date.parse('2026-09-09T10:00:00Z')
    const fiveMinAgoUtc = new Date(now - 5 * 60000).toISOString()
    expect(timeAgo(fiveMinAgoUtc, now)).toBe('5 分钟前')
  })
  it('timeUntil 已过期返回 null', () => {
    const now = Date.parse('2026-09-09T10:00:00Z')
    expect(timeUntil('2026-09-09T09:00:00Z', now)).toBeNull()
  })
})
