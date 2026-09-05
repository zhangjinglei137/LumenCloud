<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useSettingsStore } from '../stores/settings'

const store = useSettingsStore()

const generateCount = ref(1)
const savingKeys = ref<Set<string>>(new Set())

const SERVICE_LABELS: Record<string, string> = {
  tmdb: 'TMDB',
  emby: 'Emby',
  cloudsaver: 'cloudSaver',
  alist: 'alist',
  aria2: 'aria2',
  nastools: 'NasTools',
  pushplus: 'PushPlus 通知',
}

// 凭据类 key 永不展示（后端契约不返回，这里做纵深防御）
const SENSITIVE_PATTERN = /token|secret|password|credential|api_key|apikey/i

const configEntries = computed<[string, unknown][]>(() =>
  Object.entries(store.settings?.config ?? {}).filter(([k]) => !SENSITIVE_PATTERN.test(k)),
)

const notifyEntries = computed<[string, unknown][]>(() =>
  configEntries.value.filter(([k]) => k.startsWith('notify_')),
)

const otherEntries = computed<[string, unknown][]>(() =>
  configEntries.value.filter(([k]) => !k.startsWith('notify_')),
)

const serviceEntries = computed<[string, boolean][]>(() =>
  Object.entries(store.settings?.services ?? {}),
)

onMounted(async () => {
  await Promise.all([store.fetchSettings(), store.fetchInvites()])
})

async function saveKey(key: string, value: unknown) {
  const next = new Set(savingKeys.value)
  next.add(key)
  savingKeys.value = next
  try {
    await store.patchConfig({ [key]: value })
    ElMessage.success(`「${key}」已保存`)
  } catch {
    // 拦截器已提示
  } finally {
    const done = new Set(savingKeys.value)
    done.delete(key)
    savingKeys.value = done
  }
}

async function generate() {
  const codes = await store.createInvites(generateCount.value)
  ElMessage.success(`已生成 ${codes.length} 个邀请码`)
}

async function removeInvite(code: string) {
  await ElMessageBox.confirm(`确定删除邀请码 ${code} 吗？`, '删除邀请码', {
    confirmButtonText: '删除',
    cancelButtonText: '取消',
    type: 'warning',
  })
  await store.deleteInvite(code)
  ElMessage.success('已删除')
}

function serviceLabel(key: string): string {
  return SERVICE_LABELS[key] ?? key
}
</script>

<template>
  <div v-loading="store.loading && !store.settings" class="lc-page">
    <template v-if="store.settings">
      <!-- 服务配置状态 -->
      <div class="lc-panel">
        <h3 class="lc-panel-title">外部服务配置状态</h3>
        <div class="service-grid">
          <div v-for="[key, ok] in serviceEntries" :key="key" class="service-item">
            <span class="name">{{ serviceLabel(key) }}</span>
            <el-tag :type="ok ? 'success' : 'info'" effect="plain" size="small">
              {{ ok ? '已配置' : '未配置' }}
            </el-tag>
          </div>
          <el-empty
            v-if="serviceEntries.length === 0"
            description="暂无服务状态信息"
            :image-size="60"
          />
        </div>
        <el-alert
          type="info"
          :closable="false"
          show-icon
          title="服务凭据通过环境变量配置，如需修改请更新部署环境的 .env 文件后重启服务。"
          style="margin-top: 14px"
        />
      </div>

      <!-- 通知开关 -->
      <div v-if="notifyEntries.length > 0" class="lc-panel">
        <h3 class="lc-panel-title">通知开关</h3>
        <div class="notify-list">
          <div v-for="[key, value] in notifyEntries" :key="key" class="notify-item">
            <span class="key">{{ key }}</span>
            <el-switch
              :model-value="Boolean(value)"
              :loading="savingKeys.has(key)"
              @change="(v: boolean) => saveKey(key, v)"
            />
          </div>
        </div>
      </div>

      <!-- 系统参数 -->
      <div class="lc-panel">
        <h3 class="lc-panel-title">系统参数</h3>
        <el-empty v-if="otherEntries.length === 0" description="暂无配置项" :image-size="60" />
        <div v-else class="config-list">
          <div v-for="[key, value] in otherEntries" :key="key" class="config-item">
            <span class="key" :title="key">{{ key }}</span>
            <template v-if="typeof value === 'boolean'">
              <el-switch
                :model-value="value"
                :loading="savingKeys.has(key)"
                @change="(v: boolean) => saveKey(key, v)"
              />
            </template>
            <template v-else-if="typeof value === 'number'">
              <el-input-number
                :model-value="value"
                @change="(v: number) => v !== undefined && saveKey(key, v)"
              />
            </template>
            <template v-else>
              <el-input
                :model-value="String(value ?? '')"
                style="max-width: 360px"
                @change="(v: string) => saveKey(key, v)"
              />
            </template>
          </div>
        </div>
      </div>

      <!-- 邀请码管理 -->
      <div class="lc-panel">
        <div class="lc-toolbar" style="margin-bottom: 16px">
          <h3 class="lc-panel-title" style="margin: 0">邀请码管理</h3>
          <div class="right">
            <el-input-number v-model="generateCount" :min="1" :max="20" size="small" style="width: 120px" />
            <el-button type="primary" size="small" @click="generate">生成邀请码</el-button>
          </div>
        </div>
        <el-empty v-if="store.invites.length === 0" description="暂无邀请码" :image-size="60" />
        <el-table v-else :data="store.invites" size="small">
          <el-table-column label="邀请码" min-width="160">
            <template #default="{ row }">
              <span style="font-family: monospace">{{ row.code }}</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag v-if="row.used_by" type="info" size="small" effect="plain">已使用</el-tag>
              <el-tag v-else type="success" size="small" effect="plain">可用</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="使用者" width="140">
            <template #default="{ row }">{{ row.used_by ?? '—' }}</template>
          </el-table-column>
          <el-table-column label="使用时间" width="160">
            <template #default="{ row }">{{ row.used_at ?? '—' }}</template>
          </el-table-column>
          <el-table-column label="操作" width="90" align="right">
            <template #default="{ row }">
              <el-button
                v-if="!row.used_by"
                size="small"
                link
                type="danger"
                @click="removeInvite(row.code)"
              >
                删除
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </template>
  </div>
</template>

<style scoped>
.service-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 12px;
}

.service-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border: 1px solid var(--lc-border);
  border-radius: 10px;
  padding: 12px 14px;
}

.service-item .name {
  font-weight: 600;
  font-size: 14px;
}

.notify-list,
.config-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.notify-item,
.config-item {
  display: flex;
  align-items: center;
  gap: 16px;
}

.key {
  width: 260px;
  flex-shrink: 0;
  font-size: 13px;
  color: var(--lc-text-regular, #c3c9cf);
  font-family: monospace;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
