<script setup lang="ts">
import { ref } from 'vue'
import { searchTmdbApi } from '../api'
import type { TmdbSearchResult } from '../types'
import { mediaTypeLabel } from '../utils/format'
import { posterUrl } from '../utils/poster'

const props = withDefaults(
  defineProps<{
    placeholder?: string
  }>(),
  { placeholder: '输入影视名称搜索 TMDB' },
)

const emit = defineEmits<{
  select: [item: TmdbSearchResult]
}>()

const keyword = ref('')
const results = ref<TmdbSearchResult[]>([])
const searching = ref(false)
const searched = ref(false)
/** C11（审查 A13）：搜索失败错误态——展示内联反馈 + 清空上次结果。
 * 选择说明：具体报错文案仍由 http 拦截器统一 toast（全站一致约定），组件内
 * 联 alert 作为状态兜底（拦截器是全局单例，组件测试无法断言 toast；且内联
 * 提示让「搜索失败」与「无结果」两种状态在 UI 上可区分）。 */
const error = ref(false)
const selectedId = ref<number | null>(null)

async function search() {
  const q = keyword.value.trim()
  if (!q) return
  searching.value = true
  error.value = false
  try {
    results.value = await searchTmdbApi(q)
    searched.value = true
    selectedId.value = null
  } catch {
    // 搜索失败：置错误态并清空结果，不残留上次搜索的误导性结果
    error.value = true
    searched.value = false
    results.value = []
    selectedId.value = null
  } finally {
    searching.value = false
  }
}

function select(item: TmdbSearchResult) {
  selectedId.value = item.tmdb_id
  emit('select', item)
}

function posterSrc(p: string | null): string | null {
  return posterUrl(p)
}
</script>

<template>
  <div>
    <div class="lc-toolbar">
      <div class="left" style="flex: 1">
        <el-input
          v-model="keyword"
          :placeholder="placeholder"
          clearable
          style="max-width: 420px"
          @keyup.enter="search"
        >
          <template #append>
            <el-button :icon="'Search'" :loading="searching" @click="search">搜索</el-button>
          </template>
        </el-input>
      </div>
    </div>
    <el-alert
      v-if="error && !searching"
      type="error"
      :closable="false"
      show-icon
      title="搜索失败，请稍后重试"
      style="max-width: 420px; margin-top: 12px"
    />
    <el-empty
      v-if="searched && results.length === 0 && !searching"
      description="没有找到相关影视"
      :image-size="80"
    />
    <div v-loading="searching" class="lc-tmdb-results">
      <div
        v-for="item in results"
        :key="item.tmdb_id"
        class="lc-tmdb-item"
        :class="{ selected: selectedId === item.tmdb_id }"
        @click="select(item)"
      >
        <div class="poster">
          <img v-if="posterSrc(item.poster_path)" :src="posterSrc(item.poster_path)!" :alt="item.title" loading="lazy" />
          <span v-else>暂无海报</span>
        </div>
        <div class="name">
          <div>
            <div>
              {{ item.title }}<span v-if="item.year" class="year">（{{ item.year }}）</span>
            </div>
            <el-tag size="small" effect="plain" style="margin-top: 4px">
              {{ mediaTypeLabel(item.media_type) }}
            </el-tag>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.year {
  color: var(--lc-text-secondary, #9aa0a6);
  font-weight: 400;
}
</style>
