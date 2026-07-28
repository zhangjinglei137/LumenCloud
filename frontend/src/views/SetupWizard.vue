<template>
  <div class="setup-container">
    <el-card class="setup-card">
      <h1>拾光云映 · 首次配置</h1>
      <el-steps :active="step" align-center finish-status="success" style="margin-bottom: 24px">
        <el-step title="欢迎" />
        <el-step title="管理员" />
        <el-step title="TMDB" />
        <el-step title="Emby" />
        <el-step title="CloudSaver" />
        <el-step title="Aria2" />
        <el-step title="其他" />
        <el-step title="完成" />
      </el-steps>

      <!-- 步骤 0: 欢迎 -->
      <div v-if="step === 0" class="step-content">
        <el-result icon="success" title="欢迎使用拾光云映！" sub-title="系统检测到您是首次启动，请完成以下配置。">
          <template #extra>
            <el-alert type="warning" :closable="false" show-icon>
              <template #title>
                查看后端控制台日志获取临时管理员密码，或直接设置新密码
              </template>
            </el-alert>
            <el-button type="primary" @click="step = 1" style="margin-top: 20px">开始配置</el-button>
          </template>
        </el-result>
      </div>

      <!-- 步骤 1: 管理员账户 -->
      <div v-if="step === 1" class="step-content">
        <h3>管理员账户</h3>
        <el-alert type="info" :closable="false" style="margin-bottom: 20px">
          当前临时管理员: <b>admin</b>。请设置新的用户名和密码。
        </el-alert>
        <el-form label-width="100px">
          <el-form-item label="用户名">
            <el-input v-model="config.new_username" placeholder="admin" />
          </el-form-item>
          <el-form-item label="新密码">
            <el-input v-model="config.new_password" type="password" show-password placeholder="设置管理员密码" />
          </el-form-item>
        </el-form>
        <div style="margin-top:20px; text-align:right">
          <el-button @click="step = 0">上一步</el-button>
          <el-button type="primary" @click="step = 2">下一步</el-button>
        </div>
      </div>

      <!-- 步骤 2: TMDB -->
      <div v-if="step === 2" class="step-content">
        <h3>TMDB API</h3>
        <p>用于搜索影视信息和元数据</p>
        <el-form label-width="120px">
          <el-form-item label="API Key">
            <el-input v-model="config.tmdb_api_key" placeholder="https://www.themoviedb.org/settings/api" />
          </el-form-item>
        </el-form>
        <div style="margin-top:20px; text-align:right">
          <el-button @click="step = 1">上一步</el-button>
          <el-button type="primary" @click="step = 3">下一步</el-button>
        </div>
      </div>

      <!-- 步骤 3: Emby -->
      <div v-if="step === 3" class="step-content">
        <h3>Emby 媒体库</h3>
        <el-form label-width="120px">
          <el-form-item label="服务地址">
            <el-input v-model="config.emby_base_url" placeholder="http://192.168.3.31:8096" />
          </el-form-item>
          <el-form-item label="API Key">
            <el-input v-model="config.emby_api_key" placeholder="Emby API Key" />
          </el-form-item>
        </el-form>
        <div style="margin-top:20px; text-align:right">
          <el-button @click="step = 2">上一步</el-button>
          <el-button type="primary" @click="step = 4">下一步</el-button>
        </div>
      </div>

      <!-- 步骤 4: CloudSaver -->
      <div v-if="step === 4" class="step-content">
        <h3>CloudSaver 网盘搜索</h3>
        <el-form label-width="120px">
          <el-form-item label="服务地址">
            <el-input v-model="config.cloudsaver_base_url" placeholder="http://192.168.3.31:8008" />
          </el-form-item>
          <el-form-item label="用户名">
            <el-input v-model="config.cloudsaver_username" placeholder="admin" />
          </el-form-item>
          <el-form-item label="密码">
            <el-input v-model="config.cloudsaver_password" type="password" show-password />
          </el-form-item>
        </el-form>
        <div style="margin-top:20px; text-align:right">
          <el-button @click="step = 3">上一步</el-button>
          <el-button type="primary" @click="step = 5">下一步</el-button>
        </div>
      </div>

      <!-- 步骤 5: Aria2 -->
      <div v-if="step === 5" class="step-content">
        <h3>Aria2 下载器</h3>
        <el-form label-width="120px">
          <el-form-item label="RPC 地址">
            <el-input v-model="config.aria2_rpc_url" placeholder="http://192.168.3.31:6800/jsonrpc" />
          </el-form-item>
          <el-form-item label="Secret">
            <el-input v-model="config.aria2_secret" placeholder="Aria2 RPC Secret" />
          </el-form-item>
        </el-form>
        <div style="margin-top:20px; text-align:right">
          <el-button @click="step = 4">上一步</el-button>
          <el-button type="primary" @click="step = 6">下一步</el-button>
        </div>
      </div>

      <!-- 步骤 6: 其他服务 (可跳过) -->
      <div v-if="step === 6" class="step-content">
        <h3>其他服务（可选）</h3>
        <el-collapse v-model="activeNames">
          <el-collapse-item title="AList 网盘管理" name="alist">
            <el-form label-width="120px">
              <el-form-item label="服务地址">
                <el-input v-model="config.alist_base_url" />
              </el-form-item>
              <el-form-item label="Token">
                <el-input v-model="config.alist_token" />
              </el-form-item>
            </el-form>
          </el-collapse-item>
          <el-collapse-item title="NasTools 刮削" name="nastools">
            <el-form label-width="120px">
              <el-form-item label="服务地址">
                <el-input v-model="config.nastools_base_url" />
              </el-form-item>
              <el-form-item label="用户名">
                <el-input v-model="config.nastools_username" />
              </el-form-item>
              <el-form-item label="密码">
                <el-input v-model="config.nastools_password" type="password" show-password />
              </el-form-item>
            </el-form>
          </el-collapse-item>
          <el-collapse-item title="PushPlus 通知" name="pushplus">
            <el-form label-width="120px">
              <el-form-item label="Token">
                <el-input v-model="config.pushplus_token" />
              </el-form-item>
            </el-form>
          </el-collapse-item>
        </el-collapse>
        <div style="margin-top:20px; text-align:right">
          <el-button @click="step = 5">上一步</el-button>
          <el-button type="primary" @click="step = 7">下一步</el-button>
        </div>
      </div>

      <!-- 步骤 7: 确认并完成 -->
      <div v-if="step === 7" class="step-content">
        <h3>确认配置</h3>
        <el-descriptions :column="1" border>
          <el-descriptions-item label="管理员">{{ config.new_username || 'admin' }}</el-descriptions-item>
          <el-descriptions-item label="TMDB">{{ config.tmdb_api_key ? '已配置' : '未配置' }}</el-descriptions-item>
          <el-descriptions-item label="Emby">{{ config.emby_base_url }}</el-descriptions-item>
          <el-descriptions-item label="CloudSaver">{{ config.cloudsaver_base_url }}</el-descriptions-item>
          <el-descriptions-item label="Aria2">{{ config.aria2_rpc_url }}</el-descriptions-item>
        </el-descriptions>
        <div style="margin-top:20px; text-align:right">
          <el-button @click="step = 6">上一步</el-button>
          <el-button type="success" :loading="submitting" @click="submitSetup">完成配置</el-button>
        </div>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import axios from 'axios'

const router = useRouter()
const step = ref(0)
const submitting = ref(false)
const activeNames = ref<string[]>([])
const needsSetup = ref(false)

const config = ref({
  new_username: '',
  new_password: '',
  tmdb_api_key: '',
  emby_base_url: 'http://192.168.3.31:8096',
  emby_api_key: '',
  cloudsaver_base_url: 'http://192.168.3.31:8008',
  cloudsaver_username: 'admin',
  cloudsaver_password: '',
  aria2_rpc_url: 'http://192.168.3.31:6800/jsonrpc',
  aria2_secret: '',
  alist_base_url: 'http://192.168.3.31:5244',
  alist_token: '',
  nastools_base_url: 'http://192.168.3.31:3000',
  nastools_username: 'admin',
  nastools_password: '',
  pushplus_token: '',
})

onMounted(async () => {
  try {
    const { data } = await axios.get('/api/setup/status')
    if (!data.needs_setup) {
      router.replace('/login')
    } else {
      needsSetup.value = true
      // ponytail: pre-fill temp admin username if backend provides one
      config.value.new_username = data.temp_username || 'admin'
    }
  } catch {
    router.replace('/login')
  }
})

async function submitSetup() {
  submitting.value = true
  try {
    await axios.post('/api/setup/complete', config.value)
    router.replace('/login')
  } catch (e: any) {
    // ponytail: simple alert, wizard won't be spammed
    alert(e.response?.data?.detail || '配置失败，请重试')
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
.setup-container {
  display: flex;
  justify-content: center;
  align-items: flex-start;
  min-height: 100vh;
  padding-top: 60px;
  background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
}
.setup-card {
  width: 640px;
  max-width: 90vw;
}
.setup-card h1 { text-align: center; margin-bottom: 24px; color: #303133; }
.step-content { padding: 10px 0; }
.step-content h3 { margin-bottom: 16px; }
</style>
