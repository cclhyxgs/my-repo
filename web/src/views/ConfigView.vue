<template>
  <div class="config-view">
    <!-- AI 配置（DeepSeek） -->
    <el-card class="panel" shadow="never">
      <template #header><div class="panel-title">AI 解读配置（DeepSeek）</div></template>
      <div class="row">
        <el-input v-model="aiKey" type="password" show-password placeholder="输入 DeepSeek API Key（platform.deepseek.com 申请）" class="ai-key-input" clearable />
        <el-select v-model="aiModel" placeholder="模型" class="ai-model-select">
          <el-option label="deepseek-flash（推荐，便宜）" value="deepseek-flash" />
          <el-option label="deepseek-v4-pro（更强，更贵）" value="deepseek-v4-pro" />
        </el-select>
        <el-button type="primary" :loading="aiSaving" @click="onSaveAi">保存</el-button>
        <span v-if="aiConfigured" class="ai-ok-tag">已配置</span>
        <span v-else class="ai-miss-tag">未配置</span>
      </div>
      <div class="row">
        <el-button size="small" :loading="aiTesting" :disabled="!aiConfigured" @click="onTestAi">测试连接</el-button>
        <span v-if="aiTestMsg" class="ai-test-msg">{{ aiTestMsg }}</span>
      </div>
    </el-card>

    <!-- 自然语言 → 配方案（AI） -->
    <el-card class="panel" shadow="never">
      <template #header><div class="panel-title">AI 配方案（自然语言 → 筛选条件/因子权重）</div></template>
      <div class="row">
        <el-input v-model="nlText" placeholder="如：帮我配 RSI 大于 80、MACD 金叉、放量上涨；更看重动量" class="ai-key-input" clearable />
        <el-button type="primary" :loading="nlLoading" :disabled="!aiConfigured || !quantData" @click="onNlGenerate">生成</el-button>
        <span v-if="!aiConfigured" class="ai-miss-tag">需先配置 DeepSeek Key</span>
        <span v-else-if="!quantData" class="ai-miss-tag">需先选择方案</span>
      </div>

      <div v-if="nlFilters.length" class="nl-block">
        <div class="row"><b>扫描筛选条件（预览）：</b></div>
        <div class="nl-list">
          <div v-for="(c, i) in nlFilters" :key="i" class="nl-chip">
            <span v-if="c.signal !== undefined">{{ c.signal }}</span>
            <span v-else>{{ indLabel(c.indicator) }} {{ c.op }} {{ c.value }}</span>
            <el-button size="small" text type="danger" @click="nlFilters.splice(i, 1)">×</el-button>
          </div>
        </div>
        <div class="row"><el-button size="small" type="success" :loading="nlLoading" @click="applyNlFilters">应用到扫描筛选</el-button></div>
      </div>

      <div v-if="Object.keys(nlTweaks).length" class="nl-block">
        <div class="row"><b>因子权重（预览）：</b></div>
        <div class="nl-list">
          <div v-for="(v, name) in nlTweaks" :key="name" class="nl-chip">
            <span>{{ factorLabel(name) }} → 权重 {{ v }}</span>
          </div>
        </div>
        <div class="row"><el-button size="small" type="success" @click="applyNlTweaks">应用到因子权重</el-button></div>
      </div>

      <div v-if="nlUnsupported.length" class="nl-unsupported">
        <b>以下指标本工具暂不支持，已忽略：</b>
        <span v-for="(u, i) in nlUnsupported" :key="i">{{ u }}</span>
      </div>
    </el-card>

    <el-card class="panel" shadow="never">
      <template #header><div class="panel-title">模型配置 · 方案管理（F-601）+ 8 子页签（F-603）</div></template>

      <!-- 方案选择 / 管理（F-601） -->
      <div class="row">
        <el-select v-model="current" placeholder="选择方案" class="scheme-select" :loading="loading" filterable @change="onSelect">
          <el-option v-for="s in schemes" :key="s.name" :label="s.name" :value="s.name">
            <span class="opt-name">{{ s.name }}</span>
            <span class="opt-meta">{{ s.market || '-' }} / {{ s.direction || '-' }} / {{ s.period || '-' }}</span>
          </el-option>
        </el-select>
        <el-button :loading="loading" @click="refresh">刷新</el-button>
        <el-button type="primary" @click="dialog = 'create'">新建</el-button>
        <el-button :disabled="!current" @click="dialog = 'rename'">重命名</el-button>
        <el-button :disabled="!current" type="danger" plain @click="onDelete">删除</el-button>
      </div>

      <div class="row">
        <input ref="fileInput" type="file" accept="application/json,.json" class="hidden-input" @change="onFile" />
        <el-button @click="pickFile">导入 JSON</el-button>
        <el-input v-model="importName" placeholder="单方案导入时可指定名称（留空自动生成）" class="import-name" clearable />
        <el-button :disabled="!current" @click="onExport">导出</el-button>
        <el-button :disabled="!current" @click="onCopy">复制到剪贴板</el-button>
      </div>

      <el-alert v-if="message" :title="message" :type="messageType" show-icon :closable="false" class="tip" />

      <el-descriptions v-if="detail" :column="3" border size="small" class="detail">
        <el-descriptions-item label="名称">{{ detail.name }}</el-descriptions-item>
        <el-descriptions-item label="因子 Profile">{{ detail.factor_profile || '-' }}</el-descriptions-item>
        <el-descriptions-item label="版本">v{{ detail.version }}</el-descriptions-item>
        <el-descriptions-item label="市场">{{ detail.market || '-' }}</el-descriptions-item>
        <el-descriptions-item label="方向">{{ detail.direction || '-' }}</el-descriptions-item>
        <el-descriptions-item label="周期">{{ detail.period || '-' }}</el-descriptions-item>
        <el-descriptions-item label="用法模式">{{ detail.mode || '-' }}</el-descriptions-item>
        <el-descriptions-item label="说明" :span="2">{{ detail.desc || '-' }}</el-descriptions-item>
      </el-descriptions>
    </el-card>

    <!-- 8 子页签（F-603） -->
    <el-card v-if="quantData" class="panel" shadow="never">
      <template #header>
        <div class="panel-title-row">
          <div class="subtabs">
            <div v-for="t in tabs" :key="t.key" class="subtab" :class="{ active: tab === t.key }"
              :style="t.gated && isBasic ? { display: 'none' } : {}" @click="tab = t.key">{{ t.label }}</div>
          </div>
          <div class="save-row">
            <el-button type="primary" size="small" :disabled="!dirty" :loading="saving" @click="onSave">保存</el-button>
            <span v-if="dirty" class="dirty-tag">有未保存修改</span>
            <span v-else class="dirty-ok">已保存</span>
          </div>
        </div>
      </template>

      <div class="pane-body">
        <FactorManage v-show="tab === 'q-manage'" :model="quantData" @dirty="dirtyChange" />
        <WeightDir v-show="tab === 'q-weight'" :model="quantData" @dirty="dirtyChange" />
        <StatsScale v-show="tab === 'q-stats'" :model="quantData" @dirty="dirtyChange" />
        <Penalty v-show="tab === 'q-penalty'" :model="quantData" @dirty="dirtyChange" />
        <Threshold v-show="tab === 'q-threshold'" :model="quantData" :direction="direction" :mode="detail.mode" @dirty="dirtyChange" />
        <Position v-show="tab === 'q-position'" :model="quantData" :direction="direction" @dirty="dirtyChange" />
        <Backtest v-show="tab === 'q-backtest'" :model="quantData" :market="detail.market" :version="detail.version"
          :emit-save="onSave" @dirty="dirtyChange" />
        <Ghost v-show="tab === 'q-ghost'" :model="quantData" :direction="direction" @dirty="dirtyChange" />
      </div>
    </el-card>

    <el-card v-else class="panel" shadow="never">
      <el-empty description="请先选择方案以编辑配置" :image-size="70" />
    </el-card>

    <el-dialog v-model="dialogVisible" :title="dialog === 'create' ? '新建方案' : '重命名方案'" width="420px">
      <el-input v-model="formName" placeholder="方案名称（禁止含 '-'，≤50 字）" @keyup.enter="submitDialog" />
      <template #footer>
        <el-button @click="dialog = null">取消</el-button>
        <el-button type="primary" :loading="busy" @click="submitDialog">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createScheme, deleteScheme, fetchSchemeExport, getScheme, importScheme, listSchemes, renameScheme, updateScheme,
} from '../api/scheme'
import { configToQuant, quantToConfig, AND_INDICATORS } from '../config/quantModel'
import { getAiConfig, saveAiConfig, aiSchemeConditions } from '../api/ai'
import FactorManage from '../config/panes/FactorManage.vue'
import WeightDir from '../config/panes/WeightDir.vue'
import StatsScale from '../config/panes/StatsScale.vue'
import Penalty from '../config/panes/Penalty.vue'
import Threshold from '../config/panes/Threshold.vue'
import Position from '../config/panes/Position.vue'
import Backtest from '../config/panes/Backtest.vue'
import Ghost from '../config/panes/Ghost.vue'

const tabs = [
  { key: 'q-manage', label: '因子管理', gated: true },
  { key: 'q-weight', label: '权重 & 方向', gated: true },
  { key: 'q-stats', label: 'z-score & 缩放', gated: true },
  { key: 'q-penalty', label: '冲突惩罚', gated: true },
  { key: 'q-threshold', label: '状态分界', gated: false },
  { key: 'q-position', label: '仓位管理', gated: false },
  { key: 'q-backtest', label: '回测验证', gated: false },
  { key: 'q-ghost', label: '幽灵规则', gated: false },
]

const schemes = ref([])
const current = ref('')
const detail = ref(null)
const loading = ref(false)
const busy = ref(false)
const message = ref('')
const messageType = ref('info')

const dialog = ref(null)
const formName = ref('')
const fileInput = ref(null)
const importName = ref('')

// —— AI 配置 ——
const aiKey = ref('')
const aiModel = ref('deepseek-flash')
const aiConfigured = ref(false)
const aiSaving = ref(false)
const aiTesting = ref(false)
const aiTestMsg = ref('')

async function refreshAi() {
  try {
    const cfg = await getAiConfig()
    aiConfigured.value = !!cfg.configured
    aiModel.value = cfg.model || 'deepseek-flash'
    aiKey.value = '' // 不透出 key
  } catch (e) {
    /* 忽略，保持默认 */
  }
}

async function onSaveAi() {
  aiSaving.value = true
  aiTestMsg.value = ''
  try {
    const res = await saveAiConfig({ api_key: aiKey.value, model: aiModel.value })
    aiConfigured.value = !!res.configured
    aiModel.value = res.model
    aiKey.value = ''
    tip(res.configured ? 'AI 配置已保存' : '已保存（未填 Key，AI 解读暂不可用）', 'success')
  } catch (e) {
    tip(e.message, 'error')
  } finally {
    aiSaving.value = false
  }
}

async function onTestAi() {
  aiTesting.value = true
  aiTestMsg.value = ''
  try {
    // 借 analyze 端点校验 key 是否有效（用确定性标的 sh600519）
    const { aiAnalyze } = await import('../api/ai')
    await aiAnalyze({ code: 'sh600519', k_type: '日K' })
    aiTestMsg.value = '连接成功，配置可用'
  } catch (e) {
    aiTestMsg.value = e.message || '连接失败'
  } finally {
    aiTesting.value = false
  }
}

// —— 自然语言 → 配方案 ——
const nlText = ref('')
const nlLoading = ref(false)
const nlFilters = ref([])
const nlTweaks = ref({})
const nlUnsupported = ref([])

function indLabel(key) {
  return AND_INDICATORS[key] || key
}
function factorLabel(name) {
  const f = quantData.value?.factors?.find((x) => x.name === name)
  return f?.label || name
}

async function onNlGenerate() {
  const text = (nlText.value || '').trim()
  if (!text) {
    tip('请输入自然语言描述', 'warning')
    return
  }
  nlLoading.value = true
  try {
    const res = await aiSchemeConditions(text)
    nlFilters.value = res?.filters || []
    nlTweaks.value = res?.factor_tweaks || {}
    nlUnsupported.value = res?.unsupported || []
    if (!nlFilters.value.length && !Object.keys(nlTweaks.value).length) {
      tip(nlUnsupported.value.length ? '仅识别到暂不支持的指标，请换用技术指标描述' : 'AI 未解析出可配置条件，请换个说法', 'warning')
    } else {
      tip('已生成，请预览后点击应用', 'success')
    }
  } catch (e) {
    tip(e.message, 'error')
  } finally {
    nlLoading.value = false
  }
}

function applyNlFilters() {
  if (!quantData.value) return
  // 合并（去重）进 scanFilter，走现有 quantToConfig 序列化
  if (!Array.isArray(quantData.value.scanFilter)) quantData.value.scanFilter = []
  for (const c of nlFilters.value) {
    if (!quantData.value.scanFilter.some((x) => JSON.stringify(x) === JSON.stringify(c))) {
      quantData.value.scanFilter.push({ ...c })
    }
  }
  dirtyChange()
  tip(`已应用 ${nlFilters.value.length} 条扫描筛选条件，记得保存方案`, 'success')
}

function applyNlTweaks() {
  if (!quantData.value) return
  for (const [name, v] of Object.entries(nlTweaks.value)) {
    const f = quantData.value.factors?.find((x) => x.name === name)
    if (f) {
      f.enabled = true
      f.weight = Number(v) || f.weight
    }
  }
  dirtyChange()
  tip('已应用因子权重，记得保存方案', 'success')
}

// —— F-603 配置页签 ——
const tab = ref('q-manage')
const quantData = ref(null)
const dirty = ref(false)
const saving = ref(false)

const direction = computed(() => (detail.value?.direction === 'short' ? 'short' : 'long'))
const isBasic = computed(() => detail.value?.mode === 'basic')

const dialogVisible = computed({
  get: () => dialog.value !== null,
  set: (v) => { if (!v) dialog.value = null },
})

function tip(text, type = 'info') {
  message.value = text
  messageType.value = type
}
function dirtyChange() {
  dirty.value = true
}

/** 方案选中后初始化 quantData（镜像 _config_to_quant_data）。 */
function bootstrapDetail() {
  if (!detail.value || !detail.value.config || typeof detail.value.config !== 'object') {
    quantData.value = null
    return
  }
  quantData.value = reactive(configToQuant(detail.value.config, direction.value))
  dirty.value = false
}

async function refresh() {
  loading.value = true
  try {
    schemes.value = await listSchemes()
    if (current.value && !schemes.value.some((s) => s.name === current.value)) current.value = ''
    if (!current.value && schemes.value.length) current.value = schemes.value[0].name
    await loadDetail()
  } catch (e) {
    tip(e.message, 'error')
  } finally {
    loading.value = false
  }
}

async function loadDetail() {
  if (!current.value) {
    detail.value = null
    quantData.value = null
    return
  }
  try {
    detail.value = await getScheme(current.value)
    bootstrapDetail()
  } catch (e) {
    detail.value = null
    quantData.value = null
    tip(e.message, 'error')
  }
}

function onSelect() {
  tip('')
  loadDetail()
}

/** F-602 保存：quantToConfig → PUT /api/scheme/{name}（乐观锁 version）。 */
async function onSave() {
  if (!current.value || !quantData.value || !dirty.value) return true
  saving.value = true
  try {
    const config = quantToConfig(quantData.value, direction.value)
    const res = await updateScheme(current.value, { version: detail.value.version, config })
    detail.value.version = res.version
    dirty.value = false
    tip(`已保存 v${res.version}${res.updated_factors?.length ? `（更新因子 ${res.updated_factors.length} 个）` : ''}`, 'success')
    return true
  } catch (e) {
    tip(e.message, 'error')
    ElMessage.error(e.status === 409 ? '版本冲突，请刷新后再保存' : e.message)
    return false
  } finally {
    saving.value = false
  }
}

async function submitDialog() {
  const name = formName.value.trim()
  if (!name) { tip('请输入方案名称', 'warning'); return }
  busy.value = true
  try {
    if (dialog.value === 'create') {
      await createScheme({ name })
      tip(`已新建：${name}`, 'success')
      current.value = name
    } else {
      const old = current.value
      await renameScheme(old, name)
      tip(`已重命名为：${name}`, 'success')
      current.value = name
    }
    dialog.value = null
    formName.value = ''
    await refresh()
  } catch (e) {
    tip(e.message, 'error')
  } finally {
    busy.value = false
  }
}

async function onDelete() {
  try {
    await ElMessageBox.confirm(`确定删除方案「${current.value}」？`, '删除确认', { type: 'warning' })
  } catch { return }
  try {
    await deleteScheme(current.value)
    tip(`已删除：${current.value}`, 'success')
    current.value = ''
    await refresh()
  } catch (e) {
    tip(e.message, 'error')
  }
}

function pickFile() { fileInput.value?.click() }

async function onFile(ev) {
  const file = ev.target.files?.[0]
  ev.target.value = ''
  if (!file) return
  try {
    const form = {}
    if (importName.value.trim()) form.scheme_name = importName.value.trim()
    const res = await importScheme(file, form)
    const skipped = res.skipped?.length ? `，跳过 ${res.skipped.length} 条（非法名）` : ''
    tip(`导入成功：${res.name}${skipped}`, 'success')
    await refresh()
  } catch (e) {
    tip(e.message, 'error')
  }
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

async function onExport() {
  try {
    const { blob, filename } = await fetchSchemeExport(current.value)
    downloadBlob(blob, filename)
    tip(`已导出：${filename}`, 'success')
  } catch (e) {
    tip(e.message, 'error')
  }
}

async function onCopy() {
  try {
    const { text, filename } = await fetchSchemeExport(current.value)
    await copyText(text)
    tip(`已复制方案 JSON（${filename}）到剪贴板`, 'success')
  } catch (e) {
    tip(e.message, 'error')
  }
}

async function copyText(text) {
  if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(text); return }
  const ta = document.createElement('textarea')
  ta.value = text
  document.body.appendChild(ta)
  ta.select()
  try { document.execCommand('copy') } finally { document.body.removeChild(ta) }
}

onMounted(() => {
  refresh()
  refreshAi()
})
</script>

<style scoped>
.config-view { display: flex; flex-direction: column; gap: 12px; }
.panel-title { font-weight: 600; }
.panel-title-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.subtabs { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
.subtab { padding: 6px 12px; font-size: 13px; cursor: pointer; color: var(--mbull-text-dim, #787b86); border-bottom: 2px solid transparent; user-select: none; }
.subtab:hover { color: var(--mbull-text, #d1d4dc); }
.subtab.active { color: var(--mbull-accent, #e0522c); border-bottom-color: var(--mbull-accent, #e0522c); font-weight: 600; }
.save-row { display: flex; align-items: center; gap: 8px; }
.dirty-tag { font-size: 12px; color: #ffb3b3; }
.dirty-ok { font-size: 12px; color: var(--mbull-text-dim, #787b86); }
.pane-body { min-height: 200px; }
.row { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
.scheme-select { width: 260px; }
.import-name { width: 280px; }
.opt-name { margin-right: 10px; }
.opt-meta { color: var(--mbull-text-dim, #787b86); font-size: 12px; }
.hidden-input { display: none; }
.tip { margin: 6px 0 10px; }
.detail { margin-top: 8px; }
.ai-key-input { width: 360px; }
.ai-model-select { width: 240px; }
.ai-ok-tag { color: var(--mbull-down, #26a69a); font-size: 13px; }
.ai-miss-tag { color: var(--mbull-text-dim, #787b86); font-size: 13px; }
.ai-test-msg { font-size: 12px; color: var(--mbull-text-dim, #787b86); }
.nl-block { margin-top: 10px; }
.nl-list { display: flex; flex-wrap: wrap; gap: 8px; margin: 6px 0; }
.nl-chip {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 4px 8px; border: 1px solid var(--mbull-border, #2a2e39);
  border-radius: 4px; font-size: 12px; color: var(--mbull-text, #d1d4dc);
}
.nl-unsupported { margin-top: 10px; font-size: 12px; color: var(--mbull-up, #ef5350); }
.nl-unsupported span { margin-left: 6px; }
</style>