<template>
  <SetupWizard v-if="showSetup" />
  <div v-else class="login-page">
    <el-card class="login-card" shadow="always">
      <template #header>
        <div class="login-header">
          <h1 class="title">拾光云映</h1>
          <p class="subtitle">使用 Emby 账号登录</p>
        </div>
      </template>
      <el-form :model="form" :rules="rules" ref="formRef" @keyup.enter="handleLogin">
        <el-form-item prop="username">
          <el-input
            v-model="form.username"
            placeholder="用户名"
            size="large"
            :prefix-icon="User"
          />
        </el-form-item>
        <el-form-item prop="password">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="密码"
            size="large"
            show-password
            :prefix-icon="Lock"
          />
        </el-form-item>
        <el-form-item>
          <el-button
            type="primary"
            size="large"
            class="login-btn"
            :loading="loading"
            @click="handleLogin"
          >
            登录
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { User, Lock } from '@element-plus/icons-vue'
import axios from 'axios'
import { useAuthStore } from '../stores/auth'
import SetupWizard from './SetupWizard.vue'

const router = useRouter()
const authStore = useAuthStore()
const formRef = ref<any>(null)
const loading = ref(false)
const showSetup = ref(false)

const form = reactive({ username: '', password: '' })

onMounted(async () => {
  try {
    const { data } = await axios.get('/api/setup/status')
    if (data.needs_setup) {
      showSetup.value = true
    }
  } catch {
    // ignore: proceed to normal login if backend is unreachable
  }
})

const rules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }]
}

async function handleLogin() {
  if (!formRef.value) return
  try {
    await formRef.value.validate()
  } catch {
    return
  }

  loading.value = true
  try {
    await authStore.login(form.username, form.password)
    ElMessage.success('登录成功')
    router.push('/')
  } catch (err: any) {
    ElMessage.error(err.response?.data?.detail || '登录失败')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
  padding: 20px;
}
.login-card {
  width: 420px;
  border-radius: 16px;
}
.login-header {
  text-align: center;
}
.title {
  margin: 0;
  font-size: 32px;
  font-weight: 700;
  color: #1a1a2e;
}
.subtitle {
  margin: 8px 0 0;
  color: #666;
  font-size: 14px;
}
.login-btn {
  width: 100%;
}
</style>
