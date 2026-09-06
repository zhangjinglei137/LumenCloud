<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import type { AxiosError } from 'axios'
import { useSettingsStore } from '../stores/settings'
import { useAuthStore } from '../stores/auth'
import { verifyQuarkFolderApi } from '../api'
import type { QuarkVerifyResult } from '../types'
import { formatTime } from '../utils/format'
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

// ---------- 验证夸克中转 folderId（Q1 一键诊断） ----------

const quarkVerifyVisible = ref(false)
const quarkVerifyLoading = ref(false)
const quarkVerifyResult = ref<QuarkVerifyResult | null>(null)

async function runQuarkVerify(): Promise<void> {
  if (quarkVerifyLoading.value) return
  quarkVerifyLoading.value = true
  quarkVerifyResult.value = null
  quarkVerifyVisible.value = true
  try {
    quarkVerifyResult.value = await verifyQuarkFolderApi()
  } catch {
    // 网络/401 等失败已由 http 拦截器提示，直接关对话框
    quarkVerifyVisible.value = false
  } finally {
    quarkVerifyLoading.value = false
  }
}

interface VerifyAlert {
  type: 'success' | 'warning' | 'info' | 'error'
  title: string
  desc: string
}

/** 结论区：按优先级取最高一级结论（AList 未配置 → 未找到挂载 → 无法判定 → 一致/不一致） */
const verifyAlert = computed<VerifyAlert | null>(() => {
  const r = quarkVerifyResult.value
  if (!r) return null
  if (r.alist_configured === false) {
    return {
      type: 'warning',
      title: 'AList 尚未配置',
      desc: '请先在上方「网盘网关 · AList」组填写服务地址与管理令牌，点「保存全部」后再来验证。',
    }
  }
  if (r.quark_mount_found === false) {
    return {
      type: 'warning',
      title: 'alist 中未找到夸克挂载',
      desc: '在 alist 里没找到 /quark 挂载，也没有 driver=Quark 的存储。请在 AList 中添加夸克网盘存储、挂载路径设为 /quark，保存后重新验证。',
    }
  }
  if (r.match === null) {
    const parts: string[] = []
    if (!r.root_folder_id) {
      parts.push(
        'AList 夸克挂载的 root_folder_id 为空，表示挂载未指定根目录（默认落在夸克根目录）。如需固定中转目录，请在 AList 夸克驱动的 addition 里填写 root_folder_id。',
      )
    }
    if (!r.configured_folder_id) {
      parts.push(
        '设置页的 quark_default_folder 为空，表示转存时不指定目录（使用 cloudSaver 默认目录）。如需与 AList 根目录对齐，请填写对应 folderId 并保存。',
      )
    }
    return {
      type: 'info',
      title: '暂时无法判定两者是否一致',
      desc:
        parts.join(' ') ||
        '缺少可比对的信息（root_folder_id 或 quark_default_folder 有一侧读不到），暂时无法判定。',
    }
  }
  if (r.match === true) {
    return {
      type: 'success',
      title: '夸克中转目录与 AList 夸克挂载根目录一致',
      desc: '转存后的文件会出现在 /quark，等待落盘的检测可以正常命中。',
    }
  }
  return {
    type: 'error',
    title: 'folderId 与 AList 夸克挂载根目录不一致',
    desc: '转存后的文件不会出现在 /quark，会导致等待落盘超时、任务 failed，转存链路中断。请按下方的对照结果修正。',
  }
})

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

/** Q6：复制文本；navigator.clipboard 在非安全上下文（http 非 localhost）不可用，回退隐藏 textarea + execCommand */
async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    try {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      const ok = document.execCommand('copy')
      document.body.removeChild(ta)
      return ok
    } catch {
      return false
    }
  }
}

async function copyInviteCode(code: string): Promise<void> {
  if (await copyText(code)) {
    ElMessage.success('已复制邀请码')
  } else {
    ElMessage.error('复制失败，请手动复制')
  }
}

async function copyRegisterLink(code: string): Promise<void> {
  const link = `${location.origin}/register?code=${encodeURIComponent(code)}`
  if (await copyText(link)) {
    ElMessage.success('已复制注册链接')
  } else {
    ElMessage.error('复制失败，请手动复制')
  }
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
        <!-- Q5：jwt_secret 为后端首启自动生成的文件，状态「未配置」并非缺漏 -->
        <p class="lc-muted" style="margin: 10px 0 0; font-size: 12px">
          jwt_secret 已自动生成并落盘 .jwt_secret 文件，无需手动配置；如需轮换：删除该文件后重启服务。
        </p>
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
                <div class="cred-desc">
                  <el-tooltip
                    v-if="getSettingMeta(key).desc"
                    :content="getSettingMeta(key).desc"
                    placement="top"
                    :show-after="200"
                  >
                    <el-icon class="cred-desc-icon" aria-hidden="true"><Warning /></el-icon>
                  </el-tooltip>
                  <span>{{ getSettingMeta(key).desc }}</span>
                </div>
              </div>
              <el-input
                v-model="credValues[key]"
                :type="isSecretKey(key) ? 'password' : 'text'"
                :show-password="isSecretKey(key)"
                :placeholder="getSettingMeta(key).placeholder ?? ''"
                class="cred-input"
                @keyup.enter="saveAll"
              />
              <div class="cred-actions">
                <el-button
                  size="small"
                  link
                  type="danger"
                  :disabled="savingCredKeys.has(key)"
                  @click="clearCred(key)"
                >
                  清除
                </el-button>
                <el-tooltip
                  v-if="key === 'quark_default_folder'"
                  content="验证的是已保存的配置；刚修改过请先点「保存全部」"
                  placement="top"
                >
                  <el-button
                    size="small"
                    :loading="quarkVerifyLoading"
                    @click="runQuarkVerify"
                  >
                    验证 folderId
                  </el-button>
                </el-tooltip>
              </div>
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

      <!-- 业务参数 -->
      <div class="lc-panel">
        <h3 class="lc-panel-title">业务参数</h3>
        <p class="lc-muted" style="margin: 0 0 12px; font-size: 12px">
          业务运行参数：扫描频率、容量阈值、超时行为等；均为非敏感配置。
        </p>
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
              <!-- Q4：元数据标注 selectOptions 的布尔字段以下拉呈现（如 scan_baseline_required），其余仍为开关 -->
              <el-select
                v-if="getSettingMeta(key).selectOptions"
                :model-value="value"
                size="small"
                style="width: 120px"
                :disabled="savingKeys.has(key)"
                @change="(v: boolean) => saveKey(key, v)"
              >
                <el-option
                  v-for="opt in getSettingMeta(key).selectOptions"
                  :key="String(opt.value)"
                  :value="opt.value"
                  :label="opt.label"
                />
              </el-select>
              <el-switch
                v-else
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
            <template #default="{ row }">{{ row.used_by_username ?? row.used_by ?? '—' }}</template>
          </el-table-column>
          <el-table-column label="使用时间" width="160">
            <template #default="{ row }">{{ row.used_at ? formatTime(row.used_at) : '—' }}</template>
          </el-table-column>
          <el-table-column label="操作" min-width="200" align="right">
            <template #default="{ row }">
              <el-button size="small" link type="primary" @click="copyInviteCode(row.code)">
                复制
              </el-button>
              <el-button size="small" link type="primary" @click="copyRegisterLink(row.code)">
                复制注册链接
              </el-button>
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

    <!-- 验证夸克中转 folderId 诊断对话框 -->
    <el-dialog
      v-model="quarkVerifyVisible"
      title="验证夸克中转 folderId"
      width="520px"
    >
      <p class="verify-tip">这里验证的是<b>已保存</b>的配置；刚修改过请先点「保存全部」再验证。</p>
      <div v-loading="quarkVerifyLoading" class="verify-body">
        <template v-if="quarkVerifyResult">
          <!-- 结论区 -->
          <el-alert
            v-if="verifyAlert"
            :type="verifyAlert.type"
            show-icon
            :closable="false"
            class="verify-alert"
          >
            <template #title>{{ verifyAlert.title }}</template>
            <template #description>{{ verifyAlert.desc }}</template>
          </el-alert>

          <!-- 不一致：两个 id 对照 + 修正指引 -->
          <div v-if="quarkVerifyResult.match === false" class="verify-block">
            <div class="verify-compare">
              <div class="verify-compare-row">
                <span class="verify-compare-key">AList root_folder_id</span>
                <code>{{ quarkVerifyResult.root_folder_id ?? '（空，默认夸克根目录）' }}</code>
              </div>
              <div class="verify-compare-row">
                <span class="verify-compare-key">已保存 quark_default_folder</span>
                <code>{{ quarkVerifyResult.configured_folder_id ?? '（未填写）' }}</code>
              </div>
            </div>
            <p class="verify-guide">
              修正：把 AList 的 root_folder_id 填入上方「夸克中转目录 folderId」，再点「保存全部」（保存即生效，无需重启）。
            </p>
            <p
              v-if="
                quarkVerifyResult.quark_mount_path &&
                quarkVerifyResult.quark_mount_path !== '/quark'
              "
              class="verify-guide verify-guide-warn"
            >
              夸克驱动挂载路径为 {{ quarkVerifyResult.quark_mount_path }}（非
              /quark），转存链路同样不通，需在 AList 把挂载点调整为 /quark。
            </p>
          </div>

          <!-- 未找到挂载：列出 alist 现有存储供人工核对 -->
          <div
            v-if="
              quarkVerifyResult.quark_mount_found === false &&
              quarkVerifyResult.storages.length > 0
            "
            class="verify-block"
          >
            <p class="verify-sub-title">alist 现有存储（人工核对挂载名 / 挂载路径）：</p>
            <div v-for="(s, i) in quarkVerifyResult.storages" :key="i" class="verify-storage-row">
              <code>{{ s.driver ?? '—' }}</code>
              <span>{{ s.mount_path ?? '—' }}</span>
            </div>
          </div>
          <p
            v-if="quarkVerifyResult.quark_mount_found === false && quarkVerifyResult.storage_total === 0"
            class="verify-guide"
          >
            alist 里尚未配置任何存储。
          </p>

          <!-- 详情区 -->
          <el-descriptions :column="1" size="small" border class="verify-detail">
            <el-descriptions-item label="/quark 目录">
              <template v-if="quarkVerifyResult.fs_list_ok === true">
                <el-tag type="success" size="small" effect="plain">可列出</el-tag>
                <span class="verify-detail-text">现有 {{ quarkVerifyResult.quark_file_count }} 个条目</span>
              </template>
              <template v-else-if="quarkVerifyResult.fs_list_ok === false">
                <el-tag type="danger" size="small" effect="plain">不可列出</el-tag>
                <span v-if="quarkVerifyResult.fs_error" class="verify-detail-text verify-error-text">
                  {{ quarkVerifyResult.fs_error }}
                </span>
              </template>
              <template v-else>—</template>
            </el-descriptions-item>
            <el-descriptions-item label="挂载路径">
              {{ quarkVerifyResult.quark_mount_path ?? '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="alist 存储总数">
              {{ quarkVerifyResult.storage_total }}
            </el-descriptions-item>
          </el-descriptions>
        </template>
      </div>
      <template #footer>
        <el-button @click="quarkVerifyVisible = false">关闭</el-button>
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

/* Q4：凭据字段双列 grid，窄屏（<720px）回退单列 */
.cred-list {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

@media (max-width: 720px) {
  .cred-list {
    grid-template-columns: 1fr;
  }
}

.cred-field {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 8px;
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
  width: auto;
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
  background: var(--lc-warning, #e6a23c);
}

.cred-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--lc-text-primary, #e8eaed);
}

.cred-field.modified .cred-label {
  color: var(--lc-warning, #e6a23c);
}

.cred-desc {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--lc-text-secondary, #9aa0a6);
}

/* Q4：desc 提示的黄色带圈叹号（悬停显示完整说明） */
.cred-desc-icon {
  color: var(--lc-warning, #e6a23c);
  margin-top: 2px;
  flex-shrink: 0;
}

.cred-input {
  max-width: none;
  flex: none;
  margin-top: 0;
}

.cred-actions {
  display: flex;
  align-items: center;
  gap: 8px;
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

/* ---------- 验证夸克 folderId 对话框 ---------- */

.verify-tip {
  margin: 0 0 12px;
  font-size: 12px;
  color: var(--lc-text-secondary, #9aa0a6);
}

.verify-body {
  min-height: 120px;
}

.verify-alert {
  margin-bottom: 14px;
}

.verify-block {
  margin-bottom: 14px;
}

.verify-compare {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--lc-hover-bg, rgba(128, 128, 128, 0.06));
}

.verify-compare-row {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
}

.verify-compare-key {
  width: 190px;
  flex-shrink: 0;
  color: var(--lc-text-secondary, #9aa0a6);
}

.verify-compare-row code {
  font-family: monospace;
  font-size: 12px;
  color: var(--lc-text-primary, #e8eaed);
  word-break: break-all;
}

.verify-guide {
  margin: 8px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--lc-text-secondary, #9aa0a6);
}

.verify-guide-warn {
  color: var(--lc-warning, #e6a23c);
}

.verify-sub-title {
  margin: 0 0 6px;
  font-size: 12px;
  color: var(--lc-text-secondary, #9aa0a6);
}

.verify-storage-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 0;
  font-size: 12px;
  color: var(--lc-text-primary, #e8eaed);
}

.verify-storage-row code {
  min-width: 90px;
  font-family: monospace;
  color: var(--lc-text-secondary, #9aa0a6);
}

.verify-detail {
  margin-top: 4px;
}

.verify-detail-text {
  margin-left: 8px;
  font-size: 12px;
}

.verify-error-text {
  color: #f56c6c;
  word-break: break-all;
}
</style>
