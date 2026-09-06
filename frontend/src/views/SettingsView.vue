<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import type { AxiosError } from 'axios'
import { useSettingsStore } from '../stores/settings'
import { useAuthStore } from '../stores/auth'
import {
  CRED_GROUP_LABELS,
  CRED_GROUP_ORDER,
  getSettingMeta,
} from '../config/settingsMeta'

const store = useSettingsStore()
const auth = useAuthStore()

const generateCount = ref(1)
const savingKeys = ref<Set<string>>(new Set())

const SERVICE_LABELS: Record<string, string> = {
  tmdb: 'TMDB 元数据',
  emby: 'Emby 媒体库',
  cloudsaver: 'cloudSaver 网盘搜索',
  alist: 'AList 网盘网关',
  aria2: 'aria2 下载器',
  nastools: 'NasTools 目录同步',
  pushplus: 'PushPlus 微信通知',
}

// 凭据类 key 永不展示（后端契约不返回，这里做纵深防御）
const SENSITIVE_PATTERN = /token|secret|password|credential|api_key|apikey/i

const editableKeys = computed<string[]>(() => store.settings?.editable_keys ?? [])

const systemConfig = computed<Record<string, unknown>>(
  () => store.settings?.system_config ?? store.settings?.config ?? {},
)

const configEntries = computed<[string, unknown][]>(() =>
  Object.entries(systemConfig.value).filter(
    ([k]) => !SENSITIVE_PATTERN.test(k) && !editableKeys.value.includes(k),
  ),
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

// ---------- 服务凭据表单 ----------

/** 后端对敏感值的回显占位，绝不作为真实值提交 */
const MASK = '***'

/** 凭据输入框当前值；敏感项回显为 *** */
const credValues = reactive<Record<string, string>>({})

/** 凭据初始值（fetch 后的快照），用于判断「是否已修改」 */
const credOriginal = reactive<Record<string, string>>({})

/** 正在清除单个凭据的键集合 */
const savingCredKeys = ref<Set<string>>(new Set())

const credGroups = computed<{ prefix: string; label: string; keys: string[] }[]>(() => {
  const map = new Map<string, string[]>()
  for (const key of editableKeys.value) {
    const prefix = key.split('_')[0] ?? ''
    const list = map.get(prefix)
    if (list) {
      list.push(key)
    } else {
      map.set(prefix, [key])
    }
  }
  const groups: { prefix: string; label: string; keys: string[] }[] = []
  for (const prefix of CRED_GROUP_ORDER) {
    const keys = map.get(prefix)
    if (keys) {
      groups.push({ prefix, label: CRED_GROUP_LABELS[prefix] ?? prefix, keys })
      map.delete(prefix)
    }
  }
  for (const [prefix, keys] of map) {
    groups.push({ prefix, label: CRED_GROUP_LABELS[prefix] ?? prefix, keys })
  }
  return groups
})

/** 敏感类键渲染为密码框（可显隐切换）；优先用映射表标注，未知键按命名约定兜底 */
function isSecretKey(key: string): boolean {
  return (
    getSettingMeta(key).sensitive === true ||
    /password|token|secret|api_key|apikey|folder/i.test(key)
  )
}

function syncCredValues(): void {
  const sys = systemConfig.value
  for (const key of editableKeys.value) {
    const v = sys[key]
    const s = typeof v === 'string' ? v : v == null ? '' : String(v)
    credValues[key] = s
    credOriginal[key] = s
  }
}

/**
 * 是否已修改（未保存）。以初始快照为基准比较原始字符串；
 * 输入框里只剩空白也算未修改，避免误提交。
 */
function isCredModified(key: string): boolean {
  return (credValues[key] ?? '') !== (credOriginal[key] ?? '')
}

/**
 * 待提交的键集合：已修改、非空、且不再是掩码 *** 的字段。
 * 未改动 / 留空 / 仍为 *** 的字段绝不提交，避免误清空其它服务的凭据。
 */
const dirtyCredKeys = computed<string[]>(() =>
  editableKeys.value.filter((key) => {
    const v = (credValues[key] ?? '').trim()
    return v !== '' && v !== MASK && isCredModified(key)
  }),
)

const savingAll = ref(false)

/** 保存全部：一次性 PATCH 所有已修改字段 */
async function saveAll(): Promise<void> {
  const keys = dirtyCredKeys.value
  if (keys.length === 0 || savingAll.value) return
  const patch: Record<string, string> = {}
  for (const key of keys) {
    patch[key] = (credValues[key] ?? '').trim()
  }
  savingAll.value = true
  try {
    await store.patchConfig(patch)
    ElMessage.success(`已保存 ${keys.length} 项配置（生效无需重启）`)
    await store.fetchSettings()
    syncCredValues()
  } catch {
    // 拦截器已提示
  } finally {
    savingAll.value = false
  }
}

/** 显式清空某凭据（发送空字符串） */
async function clearCred(key: string): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确定清除「${getSettingMeta(key).label}」吗？清除后对应服务将不可用，可重新填写并「保存全部」恢复。`,
      '清除凭据',
      { confirmButtonText: '清除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  const next = new Set(savingCredKeys.value)
  next.add(key)
  savingCredKeys.value = next
  try {
    await store.patchConfig({ [key]: '' })
    ElMessage.success('已清除该凭据')
    await store.fetchSettings()
    syncCredValues()
  } catch {
    // 拦截器已提示
  } finally {
    const done = new Set(savingCredKeys.value)
    done.delete(key)
    savingCredKeys.value = done
  }
}

// ---------- 修改密码 ----------

const pwdVisible = ref(false)
const pwdRef = ref<FormInstance>()
const pwdForm = ref({ old_password: '', new_password: '', confirm: '' })
const pwdSaving = ref(false)

const pwdRules: FormRules = {
  old_password: [{ required: true, message: '请输入旧密码', trigger: 'blur' }],
  new_password: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    { min: 6, max: 128, message: '长度需在 6-128 位之间', trigger: 'blur' },
  ],
  confirm: [
    { required: true, message: '请再次输入新密码', trigger: 'blur' },
    {
      validator: (_rule, value: string, callback) => {
        if (value !== pwdForm.value.new_password) {
          callback(new Error('两次输入的密码不一致'))
        } else {
          callback()
        }
      },
      trigger: 'blur',
    },
  ],
}

function resetPwd(): void {
  pwdForm.value = { old_password: '', new_password: '', confirm: '' }
  pwdRef.value?.clearValidate()
}

async function submitPwd(): Promise<void> {
  if (!pwdRef.value) return
  await pwdRef.value.validate()
  pwdSaving.value = true
  try {
    await auth.changePassword(pwdForm.value.old_password, pwdForm.value.new_password)
    ElMessage.success('密码已修改')
    pwdVisible.value = false
  } catch (e) {
    // changePasswordApi 不走全局拦截器，按状态码自行提示
    const status = (e as AxiosError)?.response?.status
    if (status === 401) {
      ElMessage.error('旧密码错误')
    } else if (status === 422) {
      ElMessage.error('新密码长度需在 6-128 位之间')
    } else if (status === 409) {
      ElMessage.error('密码刚被其他会话修改，请重试')
    } else {
      ElMessage.error('修改失败，请稍后重试')
    }
  } finally {
    pwdSaving.value = false
  }
}

// ---------- 通用 ----------

onMounted(async () => {
  await Promise.all([store.fetchSettings(), store.fetchInvites()])
  syncCredValues()
})

async function saveKey(key: string, value: unknown) {
  const next = new Set(savingKeys.value)
  next.add(key)
  savingKeys.value = next
  try {
    await store.patchConfig({ [key]: value })
    ElMessage.success(`「${getSettingMeta(key).label}」已保存`)
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
      <!-- 顶部工具栏 -->
      <div class="lc-toolbar page-toolbar">
        <h3 class="lc-panel-title" style="margin: 0">系统设置</h3>
        <div class="right">
          <el-tooltip
            content="首次启动的初始密码记录在服务日志中，建议登录后立即修改"
            placement="left"
          >
            <el-button size="small" @click="pwdVisible = true">修改密码</el-button>
          </el-tooltip>
        </div>
      </div>

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
          title="服务凭据在下方表单配置，保存后立即生效（无需重启）。敏感值仅显示掩码，不会回显真实内容。"
          style="margin-top: 14px"
        />
      </div>

      <!-- 服务凭据配置 -->
      <div v-if="editableKeys.length > 0" class="lc-panel">
        <div class="lc-toolbar" style="margin-bottom: 12px">
          <div>
            <h3 class="lc-panel-title" style="margin: 0">服务凭据配置</h3>
            <p class="lc-muted cred-hint">
              一次填好所有要改的字段，点右上角「保存全部」统一提交；未改动 / 留空 / 仍为
              *** 的字段不会提交，敏感值只显示掩码。
            </p>
          </div>
          <div class="right">
            <el-button
              type="primary"
              :disabled="dirtyCredKeys.length === 0"
              :loading="savingAll"
              @click="saveAll"
            >
              保存全部{{ dirtyCredKeys.length > 0 ? `（${dirtyCredKeys.length} 项）` : '' }}
            </el-button>
          </div>
        </div>
        <div v-for="group in credGroups" :key="group.prefix" class="cred-group">
          <el-divider content-position="left">{{ group.label }}</el-divider>
          <div class="cred-list">
            <div
              v-for="key in group.keys"
              :key="key"
              class="cred-field"
              :class="{ modified: isCredModified(key) }"
            >
              <div class="cred-field-main">
                <div class="cred-label-row">
                  <span class="mod-dot" aria-hidden="true" />
                  <span class="cred-label">{{ getSettingMeta(key).label }}</span>
                  <el-tag v-if="getSettingMeta(key).default" size="small" effect="plain" type="info">
                    {{ getSettingMeta(key).default }}
                  </el-tag>
                  <el-tag v-if="isCredModified(key)" size="small" type="warning" effect="plain">
                    已修改
                  </el-tag>
                </div>
                <div class="cred-desc">{{ getSettingMeta(key).desc }}</div>
              </div>
              <el-input
                v-model="credValues[key]"
                :type="isSecretKey(key) ? 'password' : 'text'"
                :show-password="isSecretKey(key)"
                :placeholder="getSettingMeta(key).placeholder ?? ''"
                class="cred-input"
                @keyup.enter="saveAll"
              />
              <el-button
                size="small"
                link
                type="danger"
                :disabled="savingCredKeys.has(key)"
                @click="clearCred(key)"
              >
                清除
              </el-button>
            </div>
          </div>
        </div>
      </div>

      <!-- 通知开关 -->
      <div v-if="notifyEntries.length > 0" class="lc-panel">
        <h3 class="lc-panel-title">通知开关</h3>
        <div class="notify-list">
          <div v-for="[key, value] in notifyEntries" :key="key" class="notify-item">
            <div class="config-info">
              <span class="config-label">{{ getSettingMeta(key).label }}</span>
              <span v-if="getSettingMeta(key).desc" class="config-desc">
                {{ getSettingMeta(key).desc }}
              </span>
            </div>
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
            <div class="config-info">
              <span class="config-label">{{ getSettingMeta(key).label }}</span>
              <span v-if="getSettingMeta(key).desc" class="config-desc">
                {{ getSettingMeta(key).desc }}
              </span>
            </div>
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

    <!-- 修改密码对话框 -->
    <el-dialog
      v-model="pwdVisible"
      title="修改密码"
      width="420px"
      :close-on-click-modal="false"
      @closed="resetPwd"
    >
      <el-form ref="pwdRef" :model="pwdForm" :rules="pwdRules" label-position="top" @submit.prevent>
        <el-form-item label="旧密码" prop="old_password">
          <el-input
            v-model="pwdForm.old_password"
            type="password"
            show-password
            autocomplete="current-password"
          />
        </el-form-item>
        <el-form-item label="新密码" prop="new_password">
          <el-input
            v-model="pwdForm.new_password"
            type="password"
            show-password
            autocomplete="new-password"
          />
        </el-form-item>
        <el-form-item label="确认新密码" prop="confirm">
          <el-input
            v-model="pwdForm.confirm"
            type="password"
            show-password
            autocomplete="new-password"
            @keyup.enter="submitPwd"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="pwdVisible = false">取消</el-button>
        <el-button type="primary" :loading="pwdSaving" @click="submitPwd">确认修改</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page-toolbar {
  margin-bottom: 4px;
}

.cred-hint {
  margin: 0 0 8px;
  font-size: 12px;
}

.cred-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.cred-field {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 10px 12px;
  border-radius: 10px;
  border: 1px solid transparent;
  transition:
    background-color 0.2s ease,
    border-color 0.2s ease;
}

.cred-field:hover {
  background: var(--lc-hover-bg, rgba(128, 128, 128, 0.06));
}

.cred-field.modified {
  background: rgba(230, 162, 60, 0.08);
  border-color: rgba(230, 162, 60, 0.4);
}

.cred-field-main {
  width: 340px;
  flex-shrink: 0;
}

.cred-label-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.mod-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: transparent;
  flex-shrink: 0;
  transition: background-color 0.2s ease;
}

.cred-field.modified .mod-dot {
  background: #e6a23c;
}

.cred-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--lc-text-primary, #e8eaed);
}

.cred-field.modified .cred-label {
  color: #e6a23c;
}

.cred-desc {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--lc-text-secondary, #9aa0a6);
}

.cred-input {
  max-width: 420px;
  flex: 1;
  margin-top: 2px;
}

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

.config-info {
  width: 340px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.config-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--lc-text-primary, #e8eaed);
}

.config-desc {
  font-size: 12px;
  line-height: 1.5;
  color: var(--lc-text-secondary, #9aa0a6);
}
</style>
