<template>
  <div class="backtest-view">
    <el-card class="panel" shadow="never">
      <template #header><div class="panel-title">回测（F-701~705）</div></template>

      <!-- 回测模式按钮（B-14~B-18）：strategy/scoreic/factoric/futures/futuresic -->
      <el-radio-group v-model="form.mode" class="mode-row" :disabled="running">
        <el-radio-button value="strategy">策略回测</el-radio-button>
        <el-radio-button value="scoreic">评分IC</el-radio-button>
        <el-radio-button value="factoric">因子IC</el-radio-button>
        <el-radio-button value="futures">期货回测</el-radio-button>
        <el-radio-button value="futuresic">期货IC</el-radio-button>
      </el-radio-group>

      <el-form :model="form" label-width="96px" class="bt-form" :disabled="running">
        <el-form-item label="股票池">
          <el-select v-model="form.pool" style="width: 200px">
            <el-option v-for="o in poolOptions" :key="o" :value="o" :label="poolLabel(o)" />
          </el-select>
        </el-form-item>
        <el-form-item label="前瞻周期(天)">
          <el-input-number v-model="form.hold" :min="5" :max="60" :step="1" />
          <span class="hint">5-60（B-02）</span>
        </el-form-item>
        <el-form-item label="扫描间隔">
          <el-input-number v-model="form.scan" :min="1" :max="20" :step="1" />
          <span class="hint">1-20</span>
        </el-form-item>
      </el-form>

      <div class="actions">
        <el-button type="primary" :loading="running" :disabled="running" @click="onStart">
          开始回测
        </el-button>
        <el-button v-if="running" type="danger" plain @click="onCancel">取消</el-button>
        <el-button v-if="completed && form.mode === 'factoric'" @click="onApplyIc">应用因子IC</el-button>
      </div>

      <div v-if="taskId" class="progress">
        <el-progress :percentage="pct" :status="progressStatus" />
        <div class="progress-meta">
          <span>{{ statusText }}</span>
          <span v-if="num && total">已处理 {{ num }} / {{ total }}</span>
        </div>
      </div>
    </el-card>

    <!-- 日志流（F-705：元素 id 为 btLog；SSE log 事件追加，限最近 300 条） -->
    <el-card class="panel" shadow="never">
      <template #header><div class="panel-title">回测日志</div></template>
      <div id="btLog" class="bt-log">
        <div v-for="(l, i) in logs" :key="i" class="bt-log-line">{{ l }}</div>
        <div v-if="!logs.length" class="bt-log-empty">尚无日志</div>
      </div>
    </el-card>

    <!-- 结果与产物（F-705：元素 id 为 btResult；产物经 URL 在线预览/下载） -->
    <el-card v-if="taskId" class="panel" shadow="never">
      <template #header><div class="panel-title">回测结果</div></template>
      <div id="btResult" class="bt-result">
        <div v-if="!artifacts.length" class="bt-log-empty">暂无产物</div>
        <div v-else class="artifact-list">
          <div v-for="a in artifacts" :key="a.name" class="artifact-row">
            <span class="artifact-name">{{ a.name }}</span>
            <span class="artifact-size">{{ (a.size / 1024).toFixed(1) }} KB</span>
            <a :href="a.url" :download="a.name" target="_blank" rel="noopener">预览</a>
            <button class="link-btn" type="button" @click="onDownload(a)">下载</button>
          </div>
        </div>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import {
  submitBacktest, getBacktestModes, cancelBacktest, getBacktestArtifacts,
  applyFactorIC, downloadArtifact, openBacktestEvents,
} from '../api/backtest.js'

const form = reactive({
  mode: 'factoric',
  pool: 'full',
  hold: 30,
  scan: 5,
})
const poolOptions = ['full', 'watchlist', '148', 'all']
function poolLabel(v) {
  return { full: '全市场', watchlist: '自选股', 148: '上证50+创业50+科创50', all: '期货全市场' }[v] || v
}

const taskId = ref('')
const progress = reactive({ status: '', current: 0, total: 0, pct: 0 })
const logs = ref([])
const artifacts = ref([])

const running = computed(() => ['pending', 'running'].includes(progress.status))
const completed = computed(() => progress.status === 'completed')
const pct = computed(() => progress.pct)
const num = computed(() => progress.current)
const total = computed(() => progress.total)
const progressStatus = computed(() => {
  if (progress.status === 'completed') return 'success'
  if (progress.status === 'failed' || progress.status === 'cancelled') return 'exception'
  return ''
})
const statusText = computed(() => {
  const map = { pending: '排队中', running: '回测中', completed: '完成', cancelled: '已取消', failed: '失败' }
  return map[progress.status] || progress.status
})

let es = null

/** F-705：SSE 日志限最近 300 条 */
function pushLog(message) {
  logs.value.push(`[${new Date().toLocaleTimeString()}] ${message}`)
  if (logs.value.length > 300) logs.value = logs.value.slice(-300)
}

async function refreshArtifacts() {
  if (!taskId.value) return
  const r = await getBacktestArtifacts(taskId.value)
  artifacts.value = r.artifacts
}

async function onStart() {
  try {
    const res = await submitBacktest(form.mode, {
      pool: form.pool,
      hold: form.hold,
      scan: form.scan,
      prefix: `bt_${form.mode}`,
    })
    taskId.value = res.task_id
    artifacts.value = []
    logs.value = []
    startStream()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function onCancel() {
  if (!taskId.value) return
  await cancelBacktest(taskId.value)
}

function onDownload(a) {
  downloadArtifact(taskId.value, a.name)
}

async function onApplyIc() {
  try {
    const r = await applyFactorIC({ profile: '', version: 0, factors: {} })
    ElMessage.success(`已应用 ${r.updated} 个因子`)
  } catch (e) {
    ElMessage.error(e.message)
  }
}

function startStream() {
  if (es) es.close()
  es = openBacktestEvents(taskId.value, {
    onProgress: (d) => {
      if (d) { progress.current = d.current; progress.total = d.total; progress.pct = d.pct }
    },
    onLog: (d) => {
      if (d && d.message) pushLog(d.message)
    },
    onDone: async (d) => {
      if (!d) return
      progress.status = d.status
      progress.current = d.current
      progress.total = d.total
      progress.pct = d.pct
      if (d.status === 'completed') await refreshArtifacts()
    },
    onError: () => { /* SSE 关闭后不再轮询；产物刷新走手动重试 */ },
  })
}

onBeforeUnmount(() => { if (es) es.close() })
</script>

<style scoped>
.backtest-view { display: flex; flex-direction: column; gap: 14px; padding: 14px; }
.panel { background: var(--mbull-bg, #131722); border: 1px solid #2a2e39; color: #d1d4dc; }
.panel-title { font-weight: 600; color: #e0e3eb; }
.mode-row { margin: 4px 0 14px; }
.bt-form { margin-bottom: 6px; }
.hint { color: #787b86; font-size: 12px; margin-left: 10px; }
.actions { display: flex; gap: 10px; margin: 6px 0 10px; }
.progress { margin-top: 4px; }
.progress-meta { display: flex; gap: 16px; color: #b2b5be; font-size: 12px; margin-top: 6px; }
.bt-log { max-height: 200px; overflow: auto; font-family: ui-monospace, monospace; font-size: 12px; color: #b2b5be; }
.bt-log-line { white-space: pre-wrap; border-bottom: 1px dashed #1f2530; padding: 2px 0; }
.bt-log-empty { color: #787b86; font-size: 12px; }
.bt-result { min-height: 40px; }
.artifact-list { display: flex; flex-direction: column; gap: 4px; }
.artifact-row { display: flex; align-items: center; gap: 12px; font-size: 13px; }
.artifact-name { color: #e0e3eb; }
.artifact-size { color: #787b86; font-size: 12px; }
.link-btn { background: none; border: none; color: #26a69a; cursor: pointer; font-size: 13px; padding: 0; }
</style>