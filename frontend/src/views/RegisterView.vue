<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()

const formRef = ref<FormInstance>()
const form = ref({ username: '', password: '', confirm: '', inviteCode: '' })
const loading = ref(false)

const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 2, max: 32, message: '长度为 2-32 个字符', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, message: '密码至少 6 位', trigger: 'blur' },
  ],
  confirm: [
    { required: true, message: '请再次输入密码', trigger: 'blur' },
    {
      validator: (_r, value: string, cb) => {
        if (value !== form.value.password) cb(new Error('两次输入的密码不一致'))
        else cb()
      },
      trigger: 'blur',
    },
  ],
  inviteCode: [{ required: true, message: '请输入邀请码', trigger: 'blur' }],
}

async function submit() {
  if (!formRef.value) return
  await formRef.value.validate()
  loading.value = true
  try {
    await auth.register(form.value.username, form.value.password, form.value.inviteCode)
    ElMessage.success('注册成功，请登录')
    router.push('/login')
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
        <h1>注册账号</h1>
        <p class="lc-muted">注册需要管理员提供的邀请码</p>
      </div>
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent>
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="2-32 个字符" autocomplete="username" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="至少 6 位"
            show-password
            autocomplete="new-password"
          />
        </el-form-item>
        <el-form-item label="确认密码" prop="confirm">
          <el-input
            v-model="form.confirm"
            type="password"
            placeholder="再次输入密码"
            show-password
            autocomplete="new-password"
          />
        </el-form-item>
        <el-form-item label="邀请码" prop="inviteCode">
          <el-input v-model="form.inviteCode" placeholder="请输入邀请码" @keyup.enter="submit" />
        </el-form-item>
        <el-button type="primary" size="large" class="auth-submit" :loading="loading" @click="submit">
          注 册
        </el-button>
      </el-form>
      <div class="auth-footer">
        <span class="lc-muted">已有账号？</span>
        <router-link to="/login" class="auth-link">直接登录</router-link>
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
  margin-bottom: 24px;
}

.auth-brand .mark {
  display: inline-flex;
  width: 56px;
  height: 56px;
  border-radius: 16px;
  background: linear-gradient(135deg, var(--lc-accent), #b97f1e);
  color: #1a1408;
  font-family: var(--lc-font-display);
  font-size: 30px;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  margin-bottom: 12px;
}

.auth-brand h1 {
  margin: 0 0 4px;
  font-size: 24px;
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
