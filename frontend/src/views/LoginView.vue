<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()

const formRef = ref<FormInstance>()
const form = ref({ username: '', password: '' })
const loading = ref(false)

const rules: FormRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

async function submit() {
  if (!formRef.value) return
  await formRef.value.validate()
  loading.value = true
  try {
    await auth.login(form.value.username, form.value.password)
    ElMessage.success('登录成功')
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    router.push(redirect)
  } catch {
    // 拦截器已提示错误
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="auth-page">
    <div class="auth-card lc-panel">
      <div class="auth-brand">
        <span class="mark">映</span>
        <h1>拾光云映</h1>
        <p class="lc-muted">LumenCloud 影视下载管理系统</p>
      </div>
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent>
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="请输入用户名" autocomplete="username" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="请输入密码"
            show-password
            autocomplete="current-password"
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-button type="primary" size="large" class="auth-submit" :loading="loading" @click="submit">
          登 录
        </el-button>
      </el-form>
      <div class="auth-footer">
        <span class="lc-muted">没有账号？</span>
        <router-link to="/register" class="auth-link">使用邀请码注册</router-link>
      </div>
    </div>
  </div>
</template>

<style scoped>
.auth-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.auth-card {
  width: 400px;
  padding: 36px 36px 28px;
  box-shadow: 0 30px 80px -30px rgba(0, 0, 0, 0.8);
}

.auth-brand {
  text-align: center;
  margin-bottom: 28px;
}

.auth-brand .mark {
  display: inline-flex;
  width: 56px;
  height: 56px;
  border-radius: 16px;
  background: linear-gradient(135deg, var(--lc-accent), #b97f1e);
  color: var(--lc-accent-ink);
  font-family: var(--lc-font-display);
  font-size: 30px;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  margin-bottom: 12px;
}

.auth-brand h1 {
  margin: 0 0 4px;
  font-size: 26px;
  letter-spacing: 0.1em;
}

.auth-brand p {
  margin: 0;
  font-size: 13px;
}

.auth-submit {
  width: 100%;
  margin-top: 8px;
  letter-spacing: 0.3em;
  font-weight: 600;
}

.auth-footer {
  margin-top: 20px;
  text-align: center;
  font-size: 13px;
}

.auth-link {
  color: var(--lc-accent);
  text-decoration: none;
  margin-left: 6px;
}

.auth-link:hover {
  text-decoration: underline;
}
</style>
