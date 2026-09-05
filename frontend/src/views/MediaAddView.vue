<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import TmdbSearch from '../components/TmdbSearch.vue'
import { useMediaStore } from '../stores/media'
import type { TmdbSearchResult } from '../types'
import { mediaTypeLabel } from '../utils/format'

const router = useRouter()
const store = useMediaStore()

const selected = ref<TmdbSearchResult | null>(null)
const submitting = ref(false)

function onSelect(item: TmdbSearchResult) {
  selected.value = item
}

async function submit() {
  if (!selected.value) {
    ElMessage.warning('请先搜索并选择一部影视')
    return
  }
  submitting.value = true
  try {
    const created = await store.create({
      title: selected.value.title,
      tmdb_id: selected.value.tmdb_id,
      media_type: selected.value.media_type,
    })
    ElMessage.success(`已添加《${created.title}》`)
    router.push(`/media/${created.id}`)
  } catch {
    // 拦截器已提示（如已存在会返回 400/409）
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="lc-page">
    <div class="lc-panel">
      <h3 class="lc-panel-title">搜索 TMDB</h3>
      <TmdbSearch @select="onSelect" />
    </div>

    <div v-if="selected" class="lc-panel">
      <h3 class="lc-panel-title">确认添加</h3>
      <div class="confirm-row">
        <div>
          <div style="font-size: 18px; font-weight: 600">{{ selected.title }}</div>
          <div class="lc-muted" style="margin-top: 6px">
            TMDB ID：{{ selected.tmdb_id }} · {{ mediaTypeLabel(selected.media_type) }}
          </div>
        </div>
        <el-button type="primary" size="large" :loading="submitting" @click="submit">
          添加到影视库
        </el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.confirm-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
</style>
