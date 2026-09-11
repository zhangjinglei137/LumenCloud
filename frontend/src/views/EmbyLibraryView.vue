<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import TmdbSearch from '../components/TmdbSearch.vue'
import { useEmbyStore } from '../stores/emby'
import { useAuthStore } from '../stores/auth'
import { createMediaApi, listAllEmbyLibraryApi, listEmbyLibraryApi, scanMediaApi } from '../api'
import type {
  EmbyLibraryFolder,
  EmbyLibraryItem,
  EmbyLibraryQuery,
  EmbySeriesStatus,
  TmdbSearchResult,
} from '../types'

const router = useRouter()
const store = useEmbyStore()
const auth = useAuthStore()

/** 从 axios 错误解析后端约定的 detail.code（store 内实现未导出且 store 禁止改动，故本地副本） */
function parseEmbyErrorCode(err: unknown): 'not_configured' | 'unavailable' {
  const detail = (err as { response?: { data?: { detail?: { code?: string } | string } } })?.response?.data?.detail
  if (detail && typeof detail === 'object') {
    if (detail.code === 'emby_not_configured') return 'not_configured'
    if (detail.code === 'emby_unreachable') return 'unavailable'
  }
  return 'unavailable'
}

/** 分类 Tab：全部 / 电影 / 剧集 / 动漫（基于媒体库真实 CollectionType + is_anime） */
type Category = 'all' | 'movie' | 'series' | 'anime'
const category = ref<Category>('all')
/** 下钻：当前分类下选中的具体媒体库 id，null=聚合该分类全部库 */
const selectedLibraryId = ref<string | null>(null)

/** G15：连载状态筛选组（不限 / 连载中 / 已完结；走后端 status 参数） */
const statusFilter = ref<'' | EmbySeriesStatus>('')

/** G15：纳入状态筛选组（全部 / 已纳入 / 未纳入；in_media 本地过滤） */
const inclusionFilter = ref<'' | 'in' | 'out'>('')

/** 标题关键字（客户端即时过滤，不额外请求） */
const keyword = ref('')

onMounted(async () => {
  // 先拉媒体库清单（聚合视图依赖库分组），再按当前分类拉条目
  store.loading = true
  await store.fetchLibraries()
  fetchCurrent()
})

/** 当前分类对应的库列表（用于下拉下钻） */
const categoryLibraries = computed<EmbyLibraryFolder[]>(() => store.libraryGroups[category.value])

/** 下钻下拉占位文案（如「全部剧集库」；全部分类为「全部库」） */
const categoryLabel = computed(() => ({ all: '', movie: '电影', series: '剧集', anime: '动漫' })[category.value])

/**
 * 按当前分类 + 下钻库 + 状态筛选发请求。
 * 聚合态：逐库请求、按 emby_id 合并去重；逐库独立 catch，失败跳过；
 * 全部失败置错误态，部分失败保留成功库并提示（全局拦截器已对单次失败 toast）。
 */
async function fetchCurrent() {
  if (store.librariesError === 'not_configured') {
    store.items = []
    store.error = 'not_configured'
    store.loading = false
    return
  }
  if (store.librariesError === 'unavailable') {
    store.items = []
    store.error = 'unavailable'
    store.loading = false
    return
  }
  const libs = categoryLibraries.value
  if (selectedLibraryId.value) {
    await fetchSingle(selectedLibraryId.value)
    return
  }
  if (category.value === 'all') {
    // 全部聚合态：后端聚合端点单次请求（多库去重由服务端保证）
    store.loading = true
    try {
      const res = await listAllEmbyLibraryApi({ status: statusFilter.value || undefined })
      store.items = res.items
      store.error = null
    } catch (err) {
      store.items = []
      store.error = parseEmbyErrorCode(err)
    } finally {
      store.loading = false
    }
    return
  }
  if (libs.length === 0) {
    // 该分类没有对应媒体库：清空展示（走「库为空」空态兜底）
    store.items = []
    store.error = null
    store.loading = false
    return
  }
  store.loading = true
  try {
    const seen = new Map<string, EmbyLibraryItem>()
    let failedCount = 0
    let firstError: unknown = null
    for (const lib of libs) {
      try {
        const res = await listEmbyLibraryApi(buildQuery(lib))
        for (const item of res.items) {
          if (!seen.has(item.emby_id)) seen.set(item.emby_id, item)
        }
      } catch (err) {
        failedCount += 1
        if (firstError === null) firstError = err
      }
    }
    if (seen.size === 0) {
      // 全部库请求失败：置错误态（解析首个失败的错误码）
      store.items = []
      store.error = parseEmbyErrorCode(firstError)
    } else {
      store.items = [...seen.values()]
      store.error = null
      if (failedCount > 0) ElMessage.warning('部分媒体库加载失败')
    }
  } finally {
    store.loading = false
  }
}

/** 单库查询（下钻态）：复用 store.fetchLibrary（含错误态解析） */
async function fetchSingle(libraryId: string) {
  const lib = store.libraries.find((l) => l.id === libraryId)
  if (!lib) return
  await store.fetchLibrary(buildQuery(lib))
}

/** 按分类/库类型构造查询参数：movie→itemType=movie；series→itemType=series；anime/all 不传（查全部类型） */
function buildQuery(lib: EmbyLibraryFolder): EmbyLibraryQuery {
  const query: EmbyLibraryQuery = { library_id: lib.id }
  if (category.value === 'movie') query.itemType = 'movie'
  else if (category.value === 'series') query.itemType = 'series'
  if (statusFilter.value) query.status = statusFilter.value
  return query
}

/** 切换分类：清空下钻、回到聚合态并重新拉取 */
function onCategoryChange() {
  selectedLibraryId.value = null
  fetchCurrent()
}

/** 下钻选中/清空具体库（清空=回到聚合态） */
function onLibraryChange(v: string | undefined) {
  selectedLibraryId.value = v ?? null
  fetchCurrent()
}

/** G15：当前生效筛选下的可见条目（关键字 + 纳入状态，本地组合） */
const filteredItems = computed<EmbyLibraryItem[]>(() => {
  const kw = keyword.value.trim().toLowerCase()
  return store.items.filter((it) => {
    if (kw && !it.title.toLowerCase().includes(kw)) return false
    if (inclusionFilter.value === 'in' && !it.in_media) return false
    if (inclusionFilter.value === 'out' && it.in_media) return false
    return true
  })
})

/** D11：当前筛选结果中尚未纳入的条目（批量加入的目标） */
const pendingItems = computed<EmbyLibraryItem[]>(() =>
  filteredItems.value.filter((it) => !it.in_media),
)

/** 海报加载失败记录（emby_id → 展示标题占位，避免破图） */
const posterErrors = reactive<Record<string, boolean>>({})

function onPosterError(embyId: string) {
  posterErrors[embyId] = true
}

function typeLabel(type: 'movie' | 'series'): string {
  return type === 'movie' ? '电影' : '剧集'
}

function openInEmby(item: EmbyLibraryItem) {
  // D-1：serverId 获取失败时后端返回 null，无详情页地址则不跳转
  if (!item.emby_web_url) return
  window.open(item.emby_web_url, '_blank', 'noopener')
}

/** Q12：正在「加入订阅」的 Emby 条目 id 集合，用于按钮 loading 与防重复提交 */
const subscribing = ref<Set<string>>(new Set())

/** 单条订阅成功后局部更新该行（G16：不整页刷新、不重置筛选、不丢滚动位置） */
function markSubscribed(item: EmbyLibraryItem, mediaId: number) {
  const target = store.items.find((it) => it.emby_id === item.emby_id)
  if (target) {
    target.in_media = true
    target.media_id = mediaId
  }
}

/**
 * Q12：Emby 库一键订阅——复用 POST /api/media。
 * 注意：后端 media_type 仅接受 movie/tv，Emby 的 series 须映射为 tv；
 * Emby 海报是完整 URL，与 poster_path（TMDB 相对路径）语义不同，订阅时不传。
 */
async function subscribe(m: EmbyLibraryItem): Promise<boolean> {
  if (subscribing.value.has(m.emby_id)) return false
  subscribing.value.add(m.emby_id)
  try {
    const created = await createMediaApi({
      title: m.title,
      tmdb_id: m.tmdb_id,
      media_type: m.type === 'movie' ? 'movie' : 'tv', // series → tv（后端契约）
    })
    ElMessage.success(`已加入订阅：${m.title}`)
    // 触发一次巡检，立即补齐集数状态（fire-and-forget，不阻塞；结果见运行日志）
    scanMediaApi(created.id).catch(() => {})
    markSubscribed(m, created.id)
    return true
  } catch {
    // 拦截器已提示（如 tmdb_id 已存在时 409「该影视已在影视库」）
    return false
  } finally {
    subscribing.value.delete(m.emby_id)
  }
}

/** G17：无 tmdb_id 条目 → TMDB 搜索对话框 */
const tmdbDialogVisible = ref(false)
const tmdbTarget = ref<EmbyLibraryItem | null>(null)
const tmdbSelected = ref<TmdbSearchResult | null>(null)
const tmdbSubmitting = ref(false)

/** 点击「加入订阅」：有 tmdb_id 直接订阅；无则打开 TMDB 搜索对话框 */
function onSubscribeClick(m: EmbyLibraryItem) {
  if (batchRunning.value) return
  if (m.tmdb_id === null || m.tmdb_id === undefined) {
    tmdbTarget.value = m
    tmdbSelected.value = null
    tmdbDialogVisible.value = true
    return
  }
  subscribe(m)
}

function onTmdbSelect(item: TmdbSearchResult) {
  tmdbSelected.value = item
}

/** G17：选中 TMDB 结果后创建 media 并触发巡检，成功后局部更新该行 */
async function submitTmdbSubscribe() {
  const item = tmdbTarget.value
  const selected = tmdbSelected.value
  if (!item || !selected) return
  tmdbSubmitting.value = true
  try {
    const created = await createMediaApi({
      title: selected.title,
      tmdb_id: selected.tmdb_id,
      media_type: selected.media_type === 'movie' ? 'movie' : 'tv',
      poster_path: selected.poster_path ?? null,
    })
    scanMediaApi(created.id).catch(() => {})
    markSubscribed(item, created.id)
    ElMessage.success(`已加入订阅：${selected.title}`)
    tmdbDialogVisible.value = false
    tmdbTarget.value = null
    tmdbSelected.value = null
  } catch {
    // 拦截器已提示，对话框保持打开便于重试/改选
  } finally {
    tmdbSubmitting.value = false
  }
}

/** D11：批量加入订阅（串行执行，逐条提示进度，失败统一汇总） */
const batchRunning = ref(false)
const batchDone = ref(0)
const batchTotal = ref(0)

async function subscribeAll() {
  const targets = pendingItems.value
  if (targets.length === 0 || batchRunning.value) return
  batchRunning.value = true
  batchDone.value = 0
  batchTotal.value = targets.length
  const failed: string[] = []
  const needTmdb: string[] = []
  let okCount = 0
  try {
    for (const m of targets) {
      // 无 tmdb_id 的条目无法自动匹配，留待 TMDB 搜索对话框手动处理
      if (m.tmdb_id === null || m.tmdb_id === undefined) {
        needTmdb.push(m.title)
      } else {
        const ok = await subscribe(m)
        if (ok) okCount += 1
        else failed.push(m.title)
      }
      batchDone.value += 1
    }
    if (okCount > 0 && failed.length === 0 && needTmdb.length === 0) {
      ElMessage.success(`已加入订阅 ${okCount} 部`)
    } else {
      const parts: string[] = []
      if (okCount > 0) parts.push(`成功 ${okCount} 部`)
      if (failed.length > 0) parts.push(`失败 ${failed.length} 部：${failed.slice(0, 3).join('、')}${failed.length > 3 ? ` 等 ${failed.length} 部` : ''}`)
      if (needTmdb.length > 0) parts.push(`需手动匹配 TMDB ${needTmdb.length} 部：${needTmdb.slice(0, 3).join('、')}${needTmdb.length > 3 ? ' 等' : ''}`)
      ElMessage.warning(parts.join('；'))
    }
    // 完成后重新拉取，保留当前筛选状态
    fetchCurrent()
  } finally {
    batchRunning.value = false
  }
}
</script>

<template>
  <div class="lc-page">
    <div class="lc-toolbar emby-toolbar">
      <div class="left filter-groups">
        <!-- 分类 Tab（按媒体库真实 CollectionType + is_anime 分组；可下钻单库） -->
        <el-radio-group :model-value="category" size="small" @change="(v: Category) => { category = v; onCategoryChange() }">
          <el-radio-button value="all">全部</el-radio-button>
          <el-radio-button value="movie">电影</el-radio-button>
          <el-radio-button value="series">剧集</el-radio-button>
          <el-radio-button value="anime">动漫</el-radio-button>
        </el-radio-group>
        <!-- 下钻：分类下媒体库数 >1 时显示；选中单库查询，清空回聚合 -->
        <el-select
          v-if="categoryLibraries.length > 1"
          :model-value="selectedLibraryId"
          :placeholder="`全部${categoryLabel}库`"
          clearable
          size="small"
          style="width: 160px"
          @change="onLibraryChange"
        >
          <el-option
            v-for="lib in categoryLibraries"
            :key="lib.id"
            :label="lib.name"
            :value="lib.id"
          />
        </el-select>
        <!-- G15：连载状态筛选（可与类型组合，如 动漫 + 连载中） -->
        <el-radio-group :model-value="statusFilter" size="small" @change="(v: '' | EmbySeriesStatus) => { statusFilter = v; fetchCurrent() }">
          <el-radio-button value="">不限状态</el-radio-button>
          <el-radio-button value="continuing">连载中</el-radio-button>
          <el-radio-button value="ended">已完结</el-radio-button>
        </el-radio-group>
        <!-- G15：纳入状态筛选（in_media 本地过滤） -->
        <el-radio-group v-model="inclusionFilter" size="small">
          <el-radio-button value="">全部</el-radio-button>
          <el-radio-button value="in">已纳入</el-radio-button>
          <el-radio-button value="out">未纳入</el-radio-button>
        </el-radio-group>
        <span class="lc-muted count-text">共 {{ filteredItems.length }} 部</span>
      </div>
      <div class="right">
        <!-- D11：当前筛选结果存在未纳入条目时出现 -->
        <el-button
          v-if="pendingItems.length > 0 && !store.loading && !store.error"
          type="primary"
          size="small"
          :loading="batchRunning"
          :disabled="subscribing.size > 0 && !batchRunning"
          @click="subscribeAll"
        >
          {{ batchRunning ? `正在加入 ${batchDone}/${batchTotal}` : `全部加入订阅（${pendingItems.length}）` }}
        </el-button>
        <el-input
          v-model="keyword"
          placeholder="搜索标题"
          clearable
          size="small"
          style="width: 180px"
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>
        <el-button :icon="'Refresh'" :loading="store.loading" @click="fetchCurrent">
          刷新
        </el-button>
      </div>
    </div>

    <div v-loading="store.loading && !store.error" style="min-height: 200px">
      <!-- Emby 未配置：引导去设置页 -->
      <el-empty v-if="!store.loading && store.error === 'not_configured'" description="Emby 尚未配置">
        <template #description>
          <p class="lc-muted" style="margin: 0 0 8px">
            Emby 尚未配置，暂无法浏览媒体库
          </p>
        </template>
        <el-button v-if="auth.isAdmin" type="primary" @click="router.push('/settings')">
          去设置页配置 Emby
        </el-button>
        <p v-else class="lc-muted" style="margin: 0; font-size: 12px">
          请联系管理员在设置页配置 Emby 地址与 API Key
        </p>
      </el-empty>

      <!-- Emby 不可达：提示检查凭据，可重试 -->
      <el-empty v-else-if="!store.loading && store.error === 'unavailable'" description="Emby 服务不可达">
        <template #description>
          <p class="lc-muted" style="margin: 0 0 8px">
            无法连接 Emby，请检查设置页中的 Emby 地址与 API Key 是否正确
          </p>
        </template>
        <el-button type="primary" @click="fetchCurrent">重试</el-button>
      </el-empty>

      <!-- 已配置但库为空 -->
      <el-empty v-else-if="!store.loading && store.items.length === 0" description="Emby 库中暂无内容" />

      <!-- 卡片墙：复用影视库的 lc-media-grid / lc-poster 视觉（全量渲染，无分页） -->
      <div v-else class="lc-media-grid">
        <div
          v-for="m in filteredItems"
          :key="m.emby_id"
          class="lc-media-card"
          @click="openInEmby(m)"
        >
          <div class="lc-poster">
            <img
              v-if="m.poster_url && !posterErrors[m.emby_id]"
              :src="m.poster_url"
              :alt="m.title"
              loading="lazy"
              @error="onPosterError(m.emby_id)"
            />
            <div v-else class="lc-poster-fallback">{{ m.title }}</div>
            <el-tag
              class="lc-poster-type"
              size="small"
              effect="dark"
              :type="m.type === 'movie' ? 'warning' : 'primary'"
            >
              {{ typeLabel(m.type) }}
            </el-tag>
            <!-- 已纳入管理角标：本地 Media 表已收录（in_media=true） -->
            <el-tag
              v-if="m.in_media"
              class="lc-poster-in-media"
              size="small"
              effect="dark"
              type="success"
            >
              已纳入管理
            </el-tag>
          </div>
          <div class="lc-media-card-body">
            <h3 class="lc-media-card-title" :title="m.title">{{ m.title }}</h3>
            <div class="lc-media-card-meta">
              <!-- D10：TMDB 标签 + 连载状态 -->
              <div class="row tags-row">
                <el-tag
                  v-if="m.series_status === 'continuing'"
                  size="small"
                  type="success"
                  effect="plain"
                >
                  连载中
                </el-tag>
                <el-tag
                  v-else-if="m.series_status === 'ended'"
                  size="small"
                  type="info"
                  effect="plain"
                >
                  已完结
                </el-tag>
                <el-tag v-if="m.tmdb_id" size="small" effect="plain" class="tmdb-tag">
                  TMDB {{ m.tmdb_id }}
                </el-tag>
                <span v-else class="lc-muted">未关联 TMDB</span>
              </div>
              <div class="row">
                <span v-if="m.year">{{ m.year }}</span>
                <span v-if="m.community_rating !== null" class="lc-muted">
                  评分 {{ m.community_rating.toFixed(1) }}
                </span>
              </div>
              <div class="row">
                <!-- 未收录条目一键订阅；@click.stop 阻止冒泡触发 openInEmby -->
                <el-button
                  v-if="!m.in_media"
                  size="small"
                  type="primary"
                  :loading="subscribing.has(m.emby_id)"
                  :disabled="batchRunning"
                  @click.stop="onSubscribeClick(m)"
                >
                  {{ m.tmdb_id ? '加入订阅' : '匹配 TMDB 后订阅' }}
                </el-button>
                <span v-else class="subscribed-hint">
                  <el-icon style="vertical-align: -2px"><CircleCheckFilled /></el-icon>
                  已订阅
                </span>
                <!-- D-1：serverId 获取失败（emby_web_url 为 null）时隐藏入口 -->
                <span v-if="m.emby_web_url" class="lc-muted open-hint">
                  <el-icon style="vertical-align: -2px"><Monitor /></el-icon>
                  在 Emby 中打开
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 有结果但关键字/筛选过滤后为空 -->
      <el-empty v-if="!store.loading && store.items.length > 0 && filteredItems.length === 0" description="没有匹配的条目" />
    </div>

    <!-- G17：无 tmdb_id 条目的 TMDB 搜索对话框 -->
    <el-dialog
      v-model="tmdbDialogVisible"
      title="匹配 TMDB 后加入订阅"
      width="720px"
      top="6vh"
      destroy-on-close
    >
      <p v-if="tmdbTarget" class="lc-muted dialog-tip">
        「{{ tmdbTarget.title }}」尚未关联 TMDB，请搜索并选择对应的 TMDB 条目：
      </p>
      <TmdbSearch
        v-if="tmdbTarget"
        :placeholder="`搜索「${tmdbTarget.title}」在 TMDB 上的条目`"
        @select="onTmdbSelect"
      />
      <div v-if="tmdbSelected" class="selected-bar">
        已选择：<strong>{{ tmdbSelected.title }}</strong>
        <span class="lc-muted">（TMDB {{ tmdbSelected.tmdb_id }}）</span>
      </div>
      <template #footer>
        <el-button @click="tmdbDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="tmdbSubmitting" :disabled="!tmdbSelected" @click="submitTmdbSubscribe">
          加入订阅
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.emby-toolbar {
  flex-wrap: wrap;
  row-gap: 10px;
}

.filter-groups {
  flex-wrap: wrap;
  row-gap: 8px;
}

.count-text {
  font-size: 12px;
  white-space: nowrap;
}

.open-hint {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  opacity: 0.75;
}

.subscribed-hint {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--el-color-success);
}

.tags-row {
  justify-content: flex-start !important;
  gap: 6px !important;
  flex-wrap: wrap;
}

.tmdb-tag {
  font-family: var(--lc-font-body);
}

/* 「已纳入管理」角标：海报右上角，与左上角类型标签（.lc-poster-type）对称 */
.lc-poster-in-media {
  position: absolute;
  top: 10px;
  right: 10px;
}

.dialog-tip {
  margin: 0 0 12px;
  font-size: 13px;
}

.selected-bar {
  margin-top: 14px;
  padding: 10px 14px;
  border-radius: 8px;
  background: var(--lc-accent-soft);
  font-size: 13px;
}
</style>
