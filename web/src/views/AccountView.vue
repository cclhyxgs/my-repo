<template>
  <div class="account-view">
    <!-- 未登录：账号登录 / 注册（F-1403，取代桌面激活码入口） -->
    <el-card v-if="!loading && !isLoggedIn" class="panel" shadow="never">
      <template #header>
        <div class="panel-title">账号 · 授权激活</div>
      </template>

      <el-radio-group v-model="tab" class="auth-tab">
        <el-radio-button value="login">登录</el-radio-button>
        <el-radio-button value="register">注册</el-radio-button>
      </el-radio-group>

      <el-form label-position="top" class="auth-form" @submit.prevent="submitAuth">
        <el-form-item :label="tab === 'register' ? '注册账号（邮箱/手机号）' : '账号'">
          <el-input v-model="identifier" placeholder="请输入账号" autocomplete="username" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="pwd" type="password" placeholder="至少 6 位" show-password autocomplete="current-password"
            @keyup.enter="submitAuth" />
        </el-form-item>
        <div class="auth-submit">
          <el-button type="primary" :loading="submitting" class="submit-btn" @click="submitAuth">
            {{ tab === 'register' ? '注册并登录' : '登录' }}
          </el-button>
        </div>
      </el-form>

      <el-alert v-if="authError" :title="authError" type="error" show-icon :closable="false" class="tip" />
      <p class="hint">注册即创建试用授权（7 天）。设备绑定上限 2 台，失败时提示解绑或登录。</p>
    </el-card>

    <!-- 已登录：授权状态 + 设备管理 -->
    <div v-else-if="isLoggedIn" class="authed">
      <el-card class="panel" shadow="never">
        <template #header>
          <div class="panel-title-row">
            <div class="panel-title">授权状态</div>
            <el-button size="small" text @click="loadApp">刷新</el-button>
          </div>
        </template>

        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="账号">{{ account.identifier }}</el-descriptions-item>
          <el-descriptions-item label="授权类型">
            <el-tag :type="licenseTagType" size="small">{{ license_type_label }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item v-if="license?.expires_at" label="到期时间">{{ license.expires_at }}</el-descriptions-item>
          <el-descriptions-item v-else-if="license?.trial" label="试用剩余">
            {{ license.trial_days_left }} 天
          </el-descriptions-item>
          <el-descriptions-item label="生效范围">
            <el-tag size="small" type="success">不限次</el-tag>
          </el-descriptions-item>
        </el-descriptions>

        <el-alert v-if="expired" title="授权已过期，请重新登录或续费后继续使用。" type="warning" show-icon
          :closable="false" class="tip" />
      </el-card>

      <el-card class="panel" shadow="never">
        <template #header>
          <div class="panel-title-row">
            <div class="panel-title">已绑定设备（上限 2 台 · F-1403）</div>
            <el-button size="small" text :loading="deviceLoading" @click="loadDevices">刷新</el-button>
          </div>
        </template>

        <div v-if="!devices.length && !deviceLoading" class="empty">暂无绑定设备</div>
        <div v-else class="device-list">
          <div v-for="d in devices" :key="d.device_id" class="device-row">
            <div class="device-info">
              <div class="device-name">
                {{ d.ua || '设备 ' + d.device_id }}
                <el-tag v-if="d.current" size="small" type="success">当前</el-tag>
              </div>
              <div class="device-meta">
                绑定 {{ d.bound_at || '-' }} · 最近活跃 {{ d.last_seen_at || '-' }}
              </div>
            </div>
            <el-button size="small" type="danger" plain :disabled="d.current || revoking === d.device_id" :loading="revoking === d.device_id"
              @click="onRevoke(d)">解绑</el-button>
          </div>
        </div>
      </el-card>

      <el-button type="danger" plain :loading="loggingOut" class="logout" @click="onLogout">退出登录</el-button>
    </div>

    <el-card v-else class="panel" shadow="never"><el-skeleton :rows="3" animated /></el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  getAppInfo, listDevices, revokeDevice, login, register, logout,
} from '../api/auth.js'

const loading = ref(true)
const info = ref(null)
const devices = ref([])
const deviceLoading = ref(false)
const revoking = ref(null)
const loggingOut = ref(false)

// —— 登录 / 注册表单（F-1403）——
const tab = ref('login')
const identifier = ref('')
const pwd = ref('')
const submitting = ref(false)
const authError = ref('')

const account = computed(() => info.value?.account || null)
const license = computed(() => info.value?.license || null)
const isLoggedIn = computed(() => !!account.value)

const license_type_label = computed(() => {
  if (!license.value) return '无'
  return license.value.status === 'trial' ? '免费试用' : (license.value.tier || license.value.status || '订阅')
})
const licenseTagType = computed(() => {
  if (expired.value) return 'danger'
  if (license.value?.status === 'trial') return 'warning'
  return 'success'
})
const expired = computed(() => {
  const lic = license.value
  if (!lic) return false
  if (lic.status === 'expired') return true
  if (lic.status === 'trial' && (lic.trial_days_left ?? 0) <= 0) return true
  return false
})

async function loadApp() {
  loading.value = true
  try {
    info.value = await getAppInfo()
    if (account.value) await loadDevices()
  } finally {
    loading.value = false
  }
}

async function loadDevices() {
  deviceLoading.value = true
  try {
    devices.value = await listDevices()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    deviceLoading.value = false
  }
}

async function submitAuth() {
  const id = identifier.value.trim()
  const pw = pwd.value
  if (!id || pw.length < 6) {
    authError.value = '请输入账号且密码至少 6 位'
    return
  }
  submitting.value = true
  authError.value = ''
  try {
    if (tab.value === 'register') await register(id, pw)
    else await login(id, pw)
    ElMessage.success(tab.value === 'register' ? '注册成功' : '登录成功')
    pwd.value = ''
    await loadApp()
  } catch (e) {
    authError.value = e.message || '操作失败'
  } finally {
    submitting.value = false
  }
}

async function onLogout() {
  loggingOut.value = true
  try {
    await logout()
    info.value = null
    devices.value = []
    ElMessage.success('已退出登录')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loggingOut.value = false
  }
}

async function onRevoke(d) {
  if (d.current) return
  try {
    await ElMessageBox.confirm(`确定解绑设备「${d.ua || '设备 ' + d.device_id}」？`, '解绑确认', { type: 'warning' })
  } catch { return }
  revoking.value = d.device_id
  try {
    await revokeDevice(d.device_id)
    ElMessage.success('已解绑')
    await loadDevices()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    revoking.value = null
  }
}

onMounted(loadApp)
</script>

<style scoped>
.account-view { display: flex; flex-direction: column; gap: 12px; max-width: 720px; }
.panel-title { font-weight: 600; }
.panel-title-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.auth-tab { margin-bottom: 14px; }
.auth-form { max-width: 420px; }
.auth-submit { margin-top: 4px; }
.submit-btn { width: 100%; }
.tip { margin-top: 12px; }
.hint { color: var(--mbull-text-dim, #787b86); font-size: 12px; margin-top: 12px; }
.authed { display: flex; flex-direction: column; gap: 12px; }
.logout { align-self: flex-start; }
.device-list { display: flex; flex-direction: column; gap: 8px; }
.device-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 12px; border: 1px solid var(--mbull-border, #2a2e39); border-radius: 6px; }
.device-name { font-weight: 600; }
.device-meta { color: var(--mbull-text-dim, #787b86); font-size: 12px; margin-top: 2px; }
.empty { color: var(--mbull-text-dim, #787b86); }
</style>