<template>
  <div class="page">
    <NavBar active="/" />

    <div v-if="loading" class="loading-wrap">
      <el-skeleton :rows="10" animated />
    </div>

    <template v-else-if="media">
      <div class="detail-hero" :style="backdropStyle">
        <div class="hero-overlay">
          <div class="hero-content">
            <el-row :gutter="40">
              <el-col :xs="24" :sm="8" :md="6">
                <img
                  v-if="media.poster_path"
                  :src="`https://image.tmdb.org/t/p/w500${media.poster_path}`"
                  class="detail-poster"
                  :alt="media.title || media.name"
                />
                <div v-else class="detail-poster placeholder">暂无海报</div>
              </el-col>
              <el-col :xs="24" :sm="16" :md="18">
                <h1 class="detail-title">{{ media.title || media.name }}</h1>
                <div class="detail-meta">
                  <el-tag>{{ media.media_type === 'tv' ? '剧集' : '电影' }}</el-tag>
                  <span>{{ media.release_date || media.first_air_date || '未知日期' }}</span>
                  <span class="rating" v-if="media.vote_average">
                    <el-icon color="#f7ba2a"><StarFilled /></el-icon>
                    {{ media.vote_average.toFixed(1) }}
                  </span>
                </div>
                <p class="detail-overview">{{ media.overview || '暂无简介' }}</p>
                <div class="counts">
                  <span><el-icon><View /></el-icon> {{ media.watch_count || 0 }} 次观看</span>
                  <span><el-icon><User /></el-icon> {{ media.subscribe_count || 0 }} 人订阅</span>
                </div>
                <div class="actions">
                  <el-button
                    :type="isSubscribed ? 'danger' : 'primary'"
                    :loading="subLoading"
                    @click="toggleSubscribe"
                  >
                    {{ isSubscribed ? '取消订阅' : '订阅' }}
                  </el-button>
                  <el-button
                    :type="isVoted ? 'warning' : 'default'"
                    :loading="voteLoading"
                    @click="toggleVote"
                  >
                    <el-icon><Pointer /></el-icon>
                    {{ isVoted ? '取消投票' : '投票 +1' }}
                  </el-button>
                  <div class="rate-wrap">
                    <span class="rate-label">我的评分：</span>
                    <el-rate
                      v-model="myScore"
                      :max="10"
                      show-score
                      score-template="{value}"
                      :disabled="rateLoading"
                      @change="submitRating"
                    />
                  </div>
                </div>
              </el-col>
            </el-row>
          </div>
        </div>
      </div>

      <div class="content" v-if="media.seasons && media.seasons.length">
        <h2 class="section-title">剧集</h2>
        <div v-for="season in media.seasons" :key="season.season_number" class="season-block">
          <h3 class="season-title">{{ season.name }}</h3>
          <div class="episodes">
            <el-tag
              v-for="ep in season.episodes"
              :key="ep.episode_number"
              :type="ep.exists ? 'success' : 'info'"
              size="large"
              class="ep-tag"
              effect="dark"
            >
              {{ ep.episode_number }}
            </el-tag>
          </div>
        </div>
      </div>
    </template>

    <el-empty v-else description="未找到该影视" />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { StarFilled, View, User, Pointer } from '@element-plus/icons-vue'
import api, { mediaAPI, subscriptionAPI, interactionAPI } from '../api'
import { useNotificationStore } from '../stores/notification'
import NavBar from '../components/NavBar.vue'

const route = useRoute()
const notificationStore = useNotificationStore()

const mediaId = computed(() => route.params.id as string)
const media = ref<any>(null)
const loading = ref(false)
const subLoading = ref(false)
const voteLoading = ref(false)
const rateLoading = ref(false)
const isSubscribed = ref(false)
const isVoted = ref(false)
const myScore = ref(0)

const backdropStyle = computed(() => {
  if (!media.value?.backdrop_path) return {}
  return {
    backgroundImage: `linear-gradient(to right, rgba(26,26,46,0.95), rgba(22,33,62,0.7)), url(https://image.tmdb.org/t/p/w1280${media.value.backdrop_path})`,
    backgroundSize: 'cover',
    backgroundPosition: 'center'
  }
})

onMounted(async () => {
  notificationStore.fetchUnreadCount()
  await loadDetail()
  if (media.value?.local_media_id) {
    await checkStatus()
  }
})

async function loadDetail() {
  loading.value = true
  try {
    if (route.path.startsWith('/media/emby/')) {
      const { data } = await api.get(`/media/emby/${mediaId.value}`)
      // 归一化 Emby 返回字段以适配模板
      media.value = {
        ...data,
        media_type: data.type,
        // ponytail: emby 图片走前端 EMBY_BASE_URL 直连，不走代理
        poster_path: null,
        backdrop_path: null,
        vote_average: data.community_rating,
        release_date: data.premiere_date?.slice(0, 10),
        watch_count: data.play_count || 0,
        subscribe_count: data.subscription_count || 0,
      }
    } else {
      const { data } = await mediaAPI.detail(mediaId.value)
      media.value = data
    }
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '加载详情失败')
  } finally {
    loading.value = false
  }
}

async function checkStatus() {
  try {
    const [subRes, ratingRes] = await Promise.all([
      subscriptionAPI.list(),
      interactionAPI.getRating(mediaId.value)
    ])
    const subs = subRes.data || []
    const current = subs.find((s: any) => {
      const sid = s.media_id || s.media?.id
      return String(sid) === String(mediaId.value)
    })
    isSubscribed.value = !!current
    isVoted.value = current?.has_voted || current?.voted || false
    myScore.value = ratingRes.data?.score || 0
  } catch {
    // 静默失败，不影响主内容展示
  }
}

async function toggleSubscribe() {
  subLoading.value = true
  try {
    if (isSubscribed.value) {
      await subscriptionAPI.unsubscribe(mediaId.value)
      ElMessage.success('已取消订阅')
      isSubscribed.value = false
    } else {
      await subscriptionAPI.subscribe(mediaId.value)
      ElMessage.success('订阅成功')
      isSubscribed.value = true
    }
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '操作失败')
  } finally {
    subLoading.value = false
  }
}

async function toggleVote() {
  voteLoading.value = true
  try {
    if (isVoted.value) {
      await subscriptionAPI.unvote(mediaId.value)
      ElMessage.success('已取消投票')
      isVoted.value = false
    } else {
      await subscriptionAPI.vote(mediaId.value)
      ElMessage.success('投票成功')
      isVoted.value = true
    }
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '操作失败')
  } finally {
    voteLoading.value = false
  }
}

async function submitRating(score: number) {
  rateLoading.value = true
  try {
    await interactionAPI.rate(mediaId.value, score)
    ElMessage.success('评分已保存')
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '评分失败')
  } finally {
    rateLoading.value = false
  }
}
</script>

<style scoped>
.page {
  min-height: 100vh;
  background: #1a1a2e;
}
.loading-wrap {
  max-width: 1400px;
  margin: 0 auto;
  padding: 40px 24px;
}
.detail-hero {
  background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
}
.hero-overlay {
  background: rgba(26, 26, 46, 0.78);
}
.hero-content {
  max-width: 1400px;
  margin: 0 auto;
  padding: 48px 24px;
}
.detail-poster {
  width: 100%;
  border-radius: 12px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
  display: block;
}
.placeholder {
  width: 100%;
  aspect-ratio: 2/3;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #2a2a3e;
  color: #999;
  border-radius: 12px;
}
.detail-title {
  margin: 0 0 16px;
  font-size: 36px;
  color: #fff;
}
.detail-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 20px;
  color: #e0e0e0;
}
.rating {
  display: flex;
  align-items: center;
  gap: 4px;
  color: #f7ba2a;
}
.detail-overview {
  color: #d0d0d0;
  line-height: 1.7;
  font-size: 15px;
  margin-bottom: 20px;
}
.counts {
  display: flex;
  gap: 24px;
  color: #b0b0b0;
  margin-bottom: 24px;
}
.counts span {
  display: flex;
  align-items: center;
  gap: 6px;
}
.actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 16px;
}
.rate-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #e0e0e0;
}
.rate-label {
  white-space: nowrap;
}
.content {
  max-width: 1400px;
  margin: 0 auto;
  padding: 32px 24px;
}
.section-title {
  color: #fff;
  font-size: 24px;
  margin-bottom: 24px;
}
.season-block {
  margin-bottom: 32px;
}
.season-title {
  color: #e0e0e0;
  font-size: 18px;
  margin-bottom: 12px;
}
.episodes {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.ep-tag {
  min-width: 44px;
  text-align: center;
}
</style>
