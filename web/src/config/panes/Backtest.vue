<script setup>
// q-backtest · 回测验证（镜像 index.html L1528-1612，控件 B-01~B-28）。
// 参数表单绑定 quantData.backtest（经 quantToConfig.backtest 保存）；运行复用 /api/backtest。
// btLog 日志（SSE）+ btResult 结果产物（F-705 元素 id 契约）。
import { reactive, ref, computed, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import {
  submitBacktest, cancelBacktest, getBacktestArtifacts, downloadArtifact, openBacktestEvents,
} from '../../api/backtest'

const props = defineProps({
  model: { type: Object, required: true },
  market: { type: String, default: 'stock' }, // stock / futures（期货回测专有字段仅期货市场使用）
  version: { type: Number, default: 0 },
  emitSave: { type: Function, default: null }, // 运行前把编辑器内存态写盘（ensureConfigSaved）
})
const emit = defineEmits(['dirty'])

const data = props.model
const bt = data.backtest

const UNIV_OPTIONS = ['上证50+创业50+科创50', '全市场', '自选股列表']
const PERIOD_OPTIONS = ['日K', '周K', '月K', '分钟K']
function universeToPool(u) {
  return { '全市场': 'full', '自选股列表': 'watchlist' }[u] || '148'
}

const isFutures = computed(() => props.market === 'futures')

// —— 运行状态 ——
const taskId = ref('')
const progress = reactive({ status: '', pct: 0 })
const logs = ref([])
const artifacts = ref([])
const running = computed(() => ['pending', 'running'].includes(progress.status))
const completedMode = ref('')

let es = null
function pushLog(msg) {
  logs.value.push(`[${new Date().toLocaleTimeString()}] ${msg}`)
  if (logs.value.length > 300) logs.value = logs.value.slice(-300)
}

function defaultBacktestMode(isFut) {
  return isFut ? 'futures' : 'strategy'
}
const mode = ref(defaultBacktestMode(false))

async function refreshArtifacts() {
  if (!taskId.value) return
  const r = await getBacktestArtifacts(taskId.value)
  artifacts.value = r.artifacts
}

/** 运行前先写盘（原 index ensureConfigSaved），再组装 runParams。 */
async function ensureSaved() {
  if (props.emitSave) await props.emitSave()
}

async function onStart() {
  try {
    await ensureSaved()
    const isFut = isFutures.value
    const runParams = isFut
      ? {
          pool: bt.futPool || 'all',
          dir: bt.futDir || 'long',
          period: bt.futPeriod || '日K',
          days: bt.futDays || 300,
          hold: bt.forwardDays || 30,
          scan: bt.scanInterval || 5,
          multi_horizon: !!bt.futMultiHorizon,
          prefix: bt.outputPrefix || 'bt',
          offline: true,
        }
      : {
          pool: universeToPool(bt.universe),
          hold: bt.forwardDays || 30,
          scan: bt.scanInterval || 5,
          prefix: `bt_${mode.value}`,
          offline: true,
        }
    const res = await submitBacktest(mode.value, runParams)
    taskId.value = res.task_id
    artifacts.value = []
    logs.value = []
    completedMode.value = mode.value
    startStream()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function onCancel() {
  if (!taskId.value) return
  try {
    await cancelBacktest(taskId.value)
  } catch (e) {
    ElMessage.error(e.message)
  }
}

function onDownload(a) {
  downloadArtifact(taskId.value, a.name)
}

function startStream() {
  if (es) es.close()
  es = openBacktestEvents(taskId.value, {
    onProgress: (d) => { if (d) { progress.status = d.status; progress.pct = d.pct } },
    onLog: (d) => { if (d && d.message) pushLog(d.message) },
    onDone: async (d) => {
      if (!d) return
      progress.status = d.status
      progress.pct = d.pct
      if (d.status === 'completed') await refreshArtifacts()
    },
    onError: () => { /* SSE 关闭后产物刷新走手动重试 */ },
  })
}

function mark(v, key) {
  onNumeric(key)
  emit('dirty')
}
function onNumeric() { /* 数值已由 el-input-number 双向写回 bt，仅标记脏 */ }

onBeforeUnmount(() => { if (es) es.close() })
</script>

<template>
  <div class="pane">
    <div class="bt-note">运行前自动采用本页编辑中的参数并写盘，再启动回测。</div>

    <!-- A股组（B-01~B-04） -->
    <div v-if="!isFutures" class="card">
      <div class="qh">A股回测参数</div>
      <div class="qb">
        <div class="row">
          <span class="label">股票池</span>
          <el-select v-model="bt.universe" size="small" style="width:200px">
            <el-option v-for="o in UNIV_OPTIONS" :key="o" :value="o" :label="o" />
          </el-select>
          <span class="desc">（B-01）</span>
        </div>
        <div class="row">
          <span class="label">前瞻周期(天)</span>
          <el-input-number v-model="bt.forwardDays" size="small" :min="5" :max="60" :step="1" @change="mark" />
          <span class="desc">5-60（B-02）</span>
        </div>
        <div class="row">
          <span class="label">扫描间隔</span>
          <el-input-number v-model="bt.scanInterval" size="small" :min="1" :max="20" :step="1" @change="mark" />
          <span class="desc">1-20（B-03）</span>
        </div>
        <div class="row">
          <span class="label">输出前缀</span>
          <el-input v-model="bt.outputPrefix" size="small" style="width:140px" placeholder="bt" @change="emit('dirty')" />
          <span class="desc">（B-04）</span>
        </div>
      </div>
    </div>

    <!-- 期货组（B-06~B-13） -->
    <div v-else class="card">
      <div class="qh">期货回测参数</div>
      <div class="qb">
        <div class="row">
          <span class="label">回测池</span>
          <el-select v-model="bt.futPool" size="small" style="width:200px">
            <el-option value="all" label="期货全市场" />
            <el-option value="watchlist" label="期货自选" />
          </el-select>
        </div>
        <div class="row">
          <span class="label">方向</span>
          <el-select v-model="bt.futDir" size="small" style="width:140px">
            <el-option value="long" label="多单" />
            <el-option value="short" label="空单" />
          </el-select>
        </div>
        <div class="row">
          <span class="label">周期</span>
          <el-select v-model="bt.futPeriod" size="small" style="width:140px">
            <el-option v-for="o in PERIOD_OPTIONS" :key="o" :value="o" :label="o" />
          </el-select>
        </div>
        <div class="row">
          <span class="label">回看天数</span>
          <el-input-number v-model="bt.futDays" size="small" :min="30" :max="1200" :step="10" @change="mark" />
          <span class="desc">30-1200（B-09）</span>
        </div>
        <div class="row">
          <span class="label">前瞻周期(天)</span>
          <el-input-number v-model="bt.forwardDays" size="small" :min="5" :max="60" :step="1" @change="mark" />
          <span class="desc">（B-10 共用）</span>
        </div>
        <div class="row">
          <span class="label">扫描间隔</span>
          <el-input-number v-model="bt.scanInterval" size="small" :min="1" :max="20" :step="1" @change="mark" />
          <span class="desc">（B-11 共用）</span>
        </div>
        <div class="row">
          <span class="label">多周期衰减</span>
          <el-checkbox v-model="bt.futMultiHorizon" @change="emit('dirty')">开启（5/10/15/20/30天IC）</el-checkbox>
        </div>
      </div>
    </div>

    <!-- 运行按钮（B-14~B-19） -->
    <div class="actions">
      <template v-if="!isFutures">
        <el-button type="primary" size="small" :loading="running && mode === 'strategy'" :disabled="running"
          @click="mode = 'strategy'; onStart()">① 策略回测</el-button>
        <el-button type="primary" size="small" :loading="running && mode === 'scoreic'" :disabled="running"
          @click="mode = 'scoreic'; onStart()">② 评分IC</el-button>
        <el-button type="primary" size="small" :loading="running && mode === 'factoric'" :disabled="running"
          @click="mode = 'factoric'; onStart()">③ 因子IC</el-button>
      </template>
      <template v-else>
        <el-button type="primary" size="small" :loading="running && mode === 'futures'" :disabled="running"
          @click="mode = 'futures'; onStart()">期货策略回测</el-button>
        <el-button type="primary" size="small" :loading="running && mode === 'futuresic'" :disabled="running"
          @click="mode = 'futuresic'; onStart()">期货因子IC</el-button>
      </template>
      <el-button type="danger" size="small" plain :disabled="!running" @click="onCancel">■ 停止</el-button>
      <el-button v-if="completedMode === 'factoric' || completedMode === 'futuresic'" size="small" @click="$emit('dirty')">应用因子IC结果</el-button>
    </div>

    <!-- 日志（B-25 btLog） -->
    <div class="card">
      <div class="qh">运行日志</div>
      <div class="qb">
        <div id="btLog" class="bt-log">
          <div v-for="(l, i) in logs" :key="i" class="bt-log-line">{{ l }}</div>
          <div v-if="!logs.length" class="bt-empty">尚无日志</div>
        </div>
      </div>
    </div>

    <!-- 结果产物（B-26 btResult；B-21~24 下载入口合并） -->
    <div v-if="taskId" class="card">
      <div class="qh">回测结果</div>
      <div class="qb">
        <div id="btResult" class="bt-result">
          <div v-if="!artifacts.length" class="bt-empty">暂无产物</div>
          <div v-else v-for="a in artifacts" :key="a.name" class="art-row">
            <span class="art-name">{{ a.name }}</span>
            <span class="art-size">{{ (a.size / 1024).toFixed(1) }} KB</span>
            <a :href="a.url" :download="a.name" target="_blank" rel="noopener">预览</a>
            <span class="link" @click="onDownload(a)">下载</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pane { display: flex; flex-direction: column; gap: 10px; }
.bt-note { font-size: 12px; color: var(--mbull-text-dim, #787b86); }
.card { border: 1px solid var(--mbull-border, rgba(255, 255, 255, 0.08)); border-radius: 6px; overflow: hidden; }
.qh { padding: 8px 12px; font-weight: 600; font-size: 13px; background: rgba(255, 255, 255, 0.04); }
.qb { padding: 10px 12px; }
.row { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; font-size: 13px; }
.row .label { width: 110px; }
.desc { color: var(--mbull-text-dim, #787b86); font-size: 12px; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; }
.bt-log { max-height: 180px; overflow: auto; font-family: ui-monospace, monospace; font-size: 12px; color: #b2b5be; }
.bt-log-line { white-space: pre-wrap; border-bottom: 1px dashed #1f2530; padding: 2px 0; }
.bt-empty { color: var(--mbull-text-dim, #787b86); font-size: 12px; }
.bt-result { display: flex; flex-direction: column; gap: 4px; }
.art-row { display: flex; align-items: center; gap: 12px; font-size: 13px; }
.art-name { color: #e0e3eb; }
.art-size { color: var(--mbull-text-dim, #787b86); font-size: 12px; }
.link { color: #26a69a; cursor: pointer; }
</style>