<script setup lang="ts">
import { ref } from 'vue'
import { searchTmdbApi } from '../api'
import { TMDB_POSTER_BASE, type TmdbSearchResult } from '../types'
import { mediaTypeLabel } from '../utils/format'

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
const selectedId = ref<number | null>(null)

async function search() {
  const q = keyword.value.trim()
  if (!q) return
  searching.value = true
  try {
    results.value = await searchTmdbApi(q)
    searched.value = true
    selectedId.value = null
  } finally {
    searching.value = false
  }
}

function select(item: TmdbSearchResult) {
  selectedId.value = item.tmdb_id
  emit('select', item)
}

function posterUrl(p: string | null): string | null {
  return p ? `${TMDB_POSTER_BASE}${p}` : null
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
          <img v-if="posterUrl(item.poster_path)" :src="posterUrl(item.poster_path)!" :alt="item.title" loading="lazy" />
          <span v-else>暂无海报</span>
        </div>
        <div class="name">
          <div>
            <div>{{ item.title }}</div>
            <el-tag size="small" effect="plain" style="margin-top: 4px">
              {{ mediaTypeLabel(item.media_type) }}
            </el-tag>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
