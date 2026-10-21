// @vitest-environment jsdom
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import MediaDiagnosisCard from './MediaDiagnosisCard.vue'
import type { MediaDiagnosis } from '../types'

const STUBS = {
  'el-tag': { template: '<span class="el-tag"><slot /></span>' },
  'el-icon': { template: '<span><slot /></span>' },
}

function diag(overrides: Partial<MediaDiagnosis> = {}): MediaDiagnosis {
  return {
    has_scan: true,
    scan_started_at: '2026-10-20T10:00:00Z',
    scan_finished_at: '2026-10-20T10:01:00Z',
    scan_status: 'success',
    skip_reason: 'no_missing',
    skip_reason_text: '没有发现缺失集',
    missing_aired_total: 0,
    missing_episodes: [],
    missing_more: 0,
    gated_out_count: 0,
    baseline_suspect: false,
    baseline_source: 'both',
    baseline_degraded: false,
    baseline_detail: null,
    intervention: { level: 'waiting', label: '等待下一轮巡检', reason: '无缺失' },
    next_scan_at: '2026-10-21T10:00:00Z',
    not_scheduled_reason: null,
    active_episode: null,
    active_stage: null,
    ...overrides,
  }
}

async function mountCard(d: MediaDiagnosis, expand = true) {
  const wrapper = mount(MediaDiagnosisCard, { props: { diagnosis: d }, global: { stubs: STUBS } })
  if (expand && !wrapper.find('.diag-detail').exists()) {
    await wrapper.find('.diag-summary').trigger('click')
  }
  return wrapper
}

describe('MediaDiagnosisCard（media-detail-update-diagnosis 2.x/4.x）', () => {
  it('has_scan=false → 空态「尚未巡检」，不出现缺失/失败字样', async () => {
    const wrapper = await mountCard(
      diag({
        has_scan: false,
        scan_status: null,
        skip_reason_text: null,
        intervention: { level: 'waiting', label: '等待下一轮巡检', reason: '等待巡检' },
      }),
    )
    const text = wrapper.text()
    expect(text).toContain('尚未巡检')
    expect(text).not.toContain('缺失')
    expect(text).not.toContain('失败')
  })

  it('intervention 级别默认展开并高亮，文案直接取后端 intervention.label', async () => {
    const wrapper = await mountCard(
      diag({
        intervention: { level: 'intervention', label: '需要人工处理', reason: '连续无候选' },
        skip_reason_text: '连续多轮未搜到资源',
      }),
    )
    expect(wrapper.text()).toContain('需要人工处理')
    // 展开：详情区可见
    expect(wrapper.find('.diag-detail').exists()).toBe(true)
    // 高亮标记
    expect(wrapper.find('.diagnosis-card--intervention').exists()).toBe(true)
  })

  it('waiting/processing 默认折叠为一行摘要，点击展开', async () => {
    const wrapper = await mountCard(diag(), false)
    expect(wrapper.find('.diag-detail').exists()).toBe(false)
    await wrapper.find('.diag-summary').trigger('click')
    expect(wrapper.find('.diag-detail').exists()).toBe(true)
  })

  it('缺失集：数量 + 前 10 集 + 等 N 集', async () => {
    const eps = Array.from({ length: 10 }, (_, i) => `S01E${String(i + 1).padStart(2, '0')}`)
    const wrapper = await mountCard(
      diag({ missing_aired_total: 12, missing_episodes: eps, missing_more: 2 }),
    )
    const text = wrapper.text()
    expect(text).toContain('缺失 12 集')
    expect(text).toContain('S01E01')
    expect(text).toContain('等 2 集')
  })

  it('当前环节：active_episode / active_stage 展示', async () => {
    const wrapper = await mountCard(diag({ active_episode: 'S01E03', active_stage: '搜索源站' }))
    const text = wrapper.text()
    expect(text).toContain('S01E03')
    expect(text).toContain('搜索源站')
  })

  it('上次尝试：scan_started_at 相对时间 + 状态标签', async () => {
    const wrapper = await mountCard(diag({ scan_status: 'error' }))
    expect(wrapper.text()).toContain('异常')
  })

  it('基线状态：baseline_suspect / 来源 / degraded 展示', async () => {
    const wrapper = await mountCard(
      diag({ baseline_suspect: true, baseline_source: 'tmdb_axis', baseline_degraded: true }),
    )
    const text = wrapper.text()
    expect(text).toContain('基线异常')
    expect(text).toContain('TMDB 全集轴基线')
  })

  it('基线状态：has_scan 但无 baseline_detail/source 且非 suspect → 无基线数据（不是基线正常）', async () => {
    const wrapper = await mountCard(
      diag({ baseline_suspect: false, baseline_source: null, baseline_detail: null }),
    )
    const text = wrapper.text()
    expect(text).toContain('无基线数据')
    expect(text).not.toContain('基线正常')
  })

  it('level 由 waiting 变为 intervention 后自动展开', async () => {
    const wrapper = await mountCard(diag(), false)
    expect(wrapper.find('.diag-detail').exists()).toBe(false)
    await wrapper.setProps({
      diagnosis: diag({
        intervention: { level: 'intervention', label: '已失败需干预', reason: '连续无候选' },
      }),
    })
    expect(wrapper.find('.diag-detail').exists()).toBe(true)
    // 回到 waiting → 按 level 重置为折叠
    await wrapper.setProps({ diagnosis: diag() })
    expect(wrapper.find('.diag-detail').exists()).toBe(false)
  })

  it('下次巡检：next_scan_at + 相对时间；not_scheduled_reason 非空时显示原因且不显示下次时间', async () => {
    const scheduled = await mountCard(diag())
    expect(scheduled.text()).toContain('下次巡检')

    const paused = await mountCard(diag({ next_scan_at: null, not_scheduled_reason: 'paused' }))
    expect(paused.text()).toContain('已暂停')
    expect(paused.text()).not.toContain('下次巡检')
  })
})
