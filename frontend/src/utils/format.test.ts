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

import { buildEpisodeGroups, episodeDisplayName } from './format'

describe('集数分组生成（media-detail-ui）', () => {
  it('350 集 → 1-100/101-200/201-300/301-350', () => {
    expect(buildEpisodeGroups(350)).toEqual([
      { label: '1-100', start: 1, end: 100 },
      { label: '101-200', start: 101, end: 200 },
      { label: '201-300', start: 201, end: 300 },
      { label: '301-350', start: 301, end: 350 },
    ])
  })
  it('恰好 100 集 → 单组 1-100', () => {
    expect(buildEpisodeGroups(100)).toEqual([{ label: '1-100', start: 1, end: 100 }])
  })
  it('99 集 → 末组收缩到 1-99', () => {
    expect(buildEpisodeGroups(99)).toEqual([{ label: '1-99', start: 1, end: 99 }])
  })
  it('101 集 → 两组 [1-100, 101-101]', () => {
    expect(buildEpisodeGroups(101)).toEqual([
      { label: '1-100', start: 1, end: 100 },
      { label: '101-101', start: 101, end: 101 },
    ])
  })
  it('0 / 负数 → 空数组', () => {
    expect(buildEpisodeGroups(0)).toEqual([])
    expect(buildEpisodeGroups(-5)).toEqual([])
  })
})

describe('集数名称回退（media-detail-ui）', () => {
  it('有 name 返回 name', () => {
    expect(episodeDisplayName({ name: '第一集' })).toBe('第一集')
  })
  it('无 name 但有 season/episode_number → SxxExx', () => {
    expect(episodeDisplayName({ season: 1, episode_number: 4 })).toBe('S01E04')
  })
  it('无 name 且无集号 → —', () => {
    expect(episodeDisplayName({})).toBe('—')
  })
})

import { formatFileSize } from './format'

describe('文件大小约标注（formatFileSize，queue-inspection-rework）', () => {
  it('null / undefined → —', () => {
    expect(formatFileSize(null)).toBe('—')
    expect(formatFileSize(undefined)).toBe('—')
  })
  it('非估算值原样输出（1 GB → "1.00 GB"）', () => {
    expect(formatFileSize(1024 ** 3, false)).toBe('1.00 GB')
  })
  it('估算值加「约 」前缀', () => {
    expect(formatFileSize(1024 ** 3, true)).toBe('约 1.00 GB')
  })
  it('十进制 1e9 按 formatBytes 1024 进制换算为 MB', () => {
    expect(formatFileSize(1e9, false)).toBe('953.7 MB')
  })
  it('0 与负数视为无记录 → —（不显示 0 B）', () => {
    expect(formatFileSize(0)).toBe('—')
    expect(formatFileSize(0, true)).toBe('—')
    expect(formatFileSize(-5)).toBe('—')
  })
})

import { episodeSummaryText } from './format'

describe('episodeSummaryText（已有 N 缺失 M）', () => {
  it('tv + available/missing/total 已知 → 已有 N 缺失 M', () => {
    expect(episodeSummaryText({ available: 5, total: 20, missing: 15 }, 'tv', null)).toBe('已有 5 缺失 15')
  })
  it('missing 缺失 → 回退旧「已有 N / total 集」', () => {
    expect(episodeSummaryText({ available: 5, total: 20 }, 'tv', null)).toBe('已有 5 / 20 集')
  })
  it('total 未知 → 已有 N 集', () => {
    expect(episodeSummaryText({ available: 5 }, 'tv', null)).toBe('已有 5 集')
  })
  it('movie → seriesStatusLabel 回退', () => {
    expect(episodeSummaryText(null, 'movie', 'ended')).toBe('已完结')
  })
  it('无统计 → 占位符', () => {
    expect(episodeSummaryText(null, 'tv', null)).toBe('—')
  })
})
