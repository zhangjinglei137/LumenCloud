/**
 * 系统设置字段中文映射表。
 *
 * 后端不返回默认值与中文文案，此处静态维护「键 → 中文标签 / 说明 / 占位 / 默认值提示」。
 * 默认值与 backend/app/config.py 的 Settings 默认值保持一致：
 *   QUARK_QUOTA_GB=10、MAX_EPISODE_SIZE_GB=1.5、MAX_MOVIE_SIZE_GB=5、
 *   SCAN_INTERVAL_MINUTES=60、NASTOOLS_SYNC_COOLDOWN_MINUTES=30、
 *   EPISODE_STATE_TIMEOUT_HOURS=2、CAPACITY_SAFETY_MARGIN_GB=0.05、
 *   CAPACITY_ALERT_THRESHOLD=0.90（代码常量）。
 */
import type { SettingFieldMeta } from '../types'

export const SETTING_FIELD_META: Record<string, SettingFieldMeta> = {
  // ---------- AList 网盘网关 ----------
  alist_base_url: {
    label: 'AList 服务地址',
    desc: '视频网盘网关，提供 /quark 挂载、直链与空间释放。填 alist 的部署地址，需能被本系统访问。',
    placeholder: 'http://主机IP:5244',
    default: '必填（否则转存与直链不可用）',
  },
  alist_token: {
    label: 'AList 管理令牌',
    desc: 'AList 后台生成的令牌，系统用它操作 /quark 挂载目录。',
    placeholder: '已配置则显示 ***，不修改可留空',
    default: '必填',
    sensitive: true,
  },

  // ---------- cloudSaver 网盘搜索 ----------
  cloudsaver_base_url: {
    label: 'cloudSaver 服务地址',
    desc: '网盘搜索 + 转存服务的部署地址，用于搜索夸克分享码并转存到中转目录。',
    placeholder: 'http://主机IP:端口',
    default: '必填（否则搜索与转存不可用）',
  },
  cloudsaver_username: {
    label: 'cloudSaver 账号',
    desc: '登录 cloudSaver 的账号名。',
    placeholder: '登录账号',
    default: '必填',
  },
  cloudsaver_password: {
    label: 'cloudSaver 密码',
    desc: '登录 cloudSaver 的密码，用于搜索分享码和发起转存。',
    placeholder: '已配置则显示 ***，不修改可留空',
    default: '必填',
    sensitive: true,
  },

  // ---------- aria2 下载器 ----------
  aria2_rpc_url: {
    label: 'aria2 RPC 地址',
    desc: '下载器 aria2 的 RPC 接口地址，系统通过它把分享内容真实下载到本地。',
    placeholder: 'http://主机IP:6800/jsonrpc',
    default: '必填（否则无法下载）',
  },
  aria2_token: {
    label: 'aria2 RPC 密钥',
    desc: 'aria2 配置里的 rpc-secret 密钥，与 RPC 地址配对使用。',
    placeholder: '已配置则显示 ***，不修改可留空',
    default: '必填',
    sensitive: true,
  },

  // ---------- NasTools 目录同步入库 ----------
  nastools_base_url: {
    label: 'NasTools 服务地址',
    desc: '目录同步入库服务。下载完成后通知 NasTools，把影视文件整理并同步进 Emby 媒体库。',
    placeholder: 'http://主机IP:3000',
    default: '必填（否则不会自动入库）',
  },
  nastools_username: {
    label: 'NasTools 账号',
    desc: '登录 NasTools 的账号名（若 NasTools 未开启认证可留空）。',
    placeholder: '登录账号',
    default: '可选',
  },
  nastools_password: {
    label: 'NasTools 密码',
    desc: '登录 NasTools 的密码。',
    placeholder: '已配置则显示 ***，不修改可留空',
    default: '可选',
    sensitive: true,
  },

  // ---------- Emby 媒体库 ----------
  emby_base_url: {
    label: 'Emby 服务地址',
    desc: 'Emby 媒体服务器地址，用于展示「是否已入库」状态。',
    placeholder: 'http://主机IP:8096',
    default: '必填（否则无法判定入库状态）',
  },
  emby_api_key: {
    label: 'Emby API 密钥',
    desc: 'Emby 后台「高级 → API 密钥」里生成的密钥，与地址配对用于查询媒体库。',
    placeholder: '已配置则显示 ***，不修改可留空',
    default: '必填',
    sensitive: true,
  },

  // ---------- TMDB 元数据 ----------
  tmdb_api_key: {
    label: 'TMDB API 密钥',
    desc: '影视元数据搜索用，需要在 TMDB 官网（themoviedb.org）免费申请。⚠️ 注意：不要把科学上网代理端口填到这里；如需代理请在容器或系统网络层配置。',
    placeholder: '在 TMDB 官网申请后粘贴到这里',
    default: '必填（否则无法搜索影视信息）',
    sensitive: true,
  },
  tmdb_proxy: {
    label: 'TMDB 镜像根地址',
    desc: '可选。仅当你自建了 TMDB 反代镜像时才填写；镜像优先——已配置则所有 TMDB 请求走该镜像根地址；留空 = 使用官方地址 https://api.themoviedb.org。⚠️ 不要填科学上网代理端口（如 192.168.3.31:7897），那样会返回 400 错误；科学上网代理请填到「TMDB 出口代理」。',
    placeholder: '留空 = 官方地址 https://api.themoviedb.org',
    default: '可选，留空 = 官方地址',
  },
  tmdb_http_proxy: {
    label: 'TMDB 出口代理',
    desc: '可选。设置 HTTP(S) 出口代理（如 http://127.0.0.1:7890），TMDB 请求经其出口访问。镜像优先：已配置「TMDB 镜像根地址」时镜像请求同样可走该出口；无镜像时配合官方地址直连官方。',
    placeholder: 'http://127.0.0.1:7890（留空 = 直连）',
    default: '可选，留空 = 直连',
  },

  // ---------- PushPlus 通知 ----------
  pushplus_token: {
    label: 'PushPlus 推送令牌',
    desc: '可选。用于把任务结果推送到微信；留空则只用站内通知。',
    placeholder: '已配置则显示 ***，不修改可留空',
    default: '可选，留空 = 只用站内通知',
    sensitive: true,
  },

  // ---------- 夸克网盘 ----------
  quark_default_folder: {
    label: '夸克中转目录 folderId',
    desc: '转存默认落到的夸克目录 ID（即 alist Quark 驱动的 root_folder_id，可用 alist 管理 API /api/admin/storage/list 查询）。留空 = 不指定目录。可在本页点「验证 folderId」一键检测与 AList 挂载根目录是否一致。',
    placeholder: '已配置则显示 ***，不修改可留空',
    default: '可选，留空 = 不指定',
    sensitive: true,
  },

  // ---------- 业务参数 ----------
  quark_quota_gb: {
    label: '夸克网盘容量上限（GB）',
    desc: '夸克中转网盘的可用容量上限，用于计算剩余空间并决定是否继续转存。',
    default: '默认 10 GB',
  },
  capacity_safety_margin_gb: {
    label: '容量安全余量（GB）',
    desc: '容量计算时额外预留的余量，避免因估算误差把网盘写满。',
    default: '默认 0.05 GB',
  },
  capacity_alert_threshold: {
    label: '容量告警阈值（0~1）',
    desc: '容量使用率达到该比例时触发告警，提醒你清理中转目录。0.9 表示 90%。',
    default: '默认 0.90（90%）',
  },
  max_episode_size_gb: {
    label: '剧集单集大小上限（GB）',
    desc: '新订阅剧集的默认单集大小上限，超过的文件会被拒绝下载。',
    default: '默认 1.5 GB',
  },
  max_movie_size_gb: {
    label: '电影大小上限（GB）',
    desc: '新订阅电影的默认大小上限，超过的文件会被拒绝下载。',
    default: '默认 5.0 GB',
  },
  scan_interval_minutes: {
    label: '扫描间隔（分钟）',
    desc: '系统轮询检查更新、下载与转存状态的间隔时间。',
    default: '默认 60 分钟',
  },
  nastools_sync_cooldown_minutes: {
    label: 'NasTools 同步冷却（分钟）',
    desc: '两次触发 NasTools 同步入库之间的最小间隔，避免频繁调用。',
    default: '默认 30 分钟',
  },
  episode_state_timeout_hours: {
    label: '剧集状态超时（小时）',
    desc: '某集长时间停留在下载/转存中且无更新时，超过该时长判定为超时并允许重试。',
    default: '默认 2 小时',
  },
  scheduler_enabled: {
    label: '定时调度总开关',
    desc: '所有定时任务（扫描、同步等）的总开关；关闭后定时任务全部停止，只保留手动触发。',
    default: '默认关闭',
  },
  scan_baseline_required: {
    label: 'Emby 防重基线缺失时跳过巡检',
    desc: 'Emby 未收录该剧集（防重基线缺失）时的行为：开启 = 本轮跳过（强防重，防盲入占中转空间）；关闭 = 照常搜索下载（仅转搜索到的具体文件，推荐默认）。',
    default: '默认关闭（照常搜索下载）',
    // Q4：布尔但语义为二选一行为，以下拉呈现
    selectOptions: [
      { value: true, label: '开启' },
      { value: false, label: '关闭' },
    ],
  },
}

/** 取字段元数据；未知键回退为原始键名（保证后端新增键时页面不崩） */
export function getSettingMeta(key: string): SettingFieldMeta {
  return SETTING_FIELD_META[key] ?? { label: key, desc: '' }
}

/** 服务凭据分组（按 key 前缀），组标题全中文 */
export const CRED_GROUP_ORDER = [
  'alist',
  'cloudsaver',
  'aria2',
  'nastools',
  'emby',
  'tmdb',
  'pushplus',
  'quark',
]

export const CRED_GROUP_LABELS: Record<string, string> = {
  alist: '网盘网关 · AList',
  cloudsaver: '网盘搜索 · cloudSaver',
  aria2: '下载器 · aria2',
  nastools: '目录同步入库 · NasTools',
  emby: '媒体库 · Emby',
  tmdb: '元数据 · TMDB',
  pushplus: '通知 · PushPlus',
  quark: '夸克网盘',
}
