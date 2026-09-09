import { describe, expect, it } from 'vitest'
import { SETTING_FIELD_META, getSettingMeta } from './settingsMeta'

// 必填 6 键：default 统一为「必填」，不含长文案
const REQUIRED_KEYS = [
  'alist_base_url',
  'cloudsaver_base_url',
  'aria2_rpc_url',
  'nastools_base_url',
  'emby_base_url',
  'tmdb_api_key',
]

// 可选 5 键：default 统一为「可选」
const OPTIONAL_KEYS = [
  'tmdb_proxy',
  'tmdb_http_proxy',
  'pushplus_token',
  'quark_default_folder',
  'emby_series_library_ids',
]

describe('settingsMeta 必填文案精简', () => {
  it.each(REQUIRED_KEYS)('%s 的 default 为「必填」', (key) => {
    expect(SETTING_FIELD_META[key]?.default).toBe('必填')
  })

  it.each(REQUIRED_KEYS)('%s 的 default 不含长文案', (key) => {
    expect(SETTING_FIELD_META[key]?.default).not.toMatch(/否则|无法|不可用/)
  })

  it.each(OPTIONAL_KEYS)('%s 的 default 为「可选」', (key) => {
    expect(SETTING_FIELD_META[key]?.default).toBe('可选')
  })
})

describe('settingsMeta 废弃项移除', () => {
  it('SETTING_FIELD_META 不含 scan_interval_minutes', () => {
    expect('scan_interval_minutes' in SETTING_FIELD_META).toBe(false)
  })

  it('getSettingMeta(scan_interval_minutes) 回退为英文键名（非「已废弃」文案）', () => {
    const meta = getSettingMeta('scan_interval_minutes')
    expect(meta.label).toBe('scan_interval_minutes')
    expect(meta.desc).toBe('')
  })

  it('internal 键「自动生成，无需修改」保留', () => {
    expect(SETTING_FIELD_META.internal_aria2_webhook_secret?.default).toBe('自动生成，无需修改')
  })

  it('业务参数「默认 XX」标签保留', () => {
    expect(SETTING_FIELD_META.quark_quota_gb?.default).toMatch(/^默认/)
  })
})
