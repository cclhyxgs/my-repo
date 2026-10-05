<script setup>
// F-401~404 批量诊断页（H5 前端）：参数表单 → 触发诊断 → SSE 进度 → 五组结果表 → 报告导出。
// 后端契约见 server/api/diagnosis.py；数据字段与 F-402 worker 的 summary/results 一致。
import { ref, reactive, computed, onUnmounted } from 'vue'
import { ElMessage } from 'element-plus'
import {
  startDiagnosis,
  getDiagnosis,
  cancelDiagnosis,
  openDiagnosisEvents,
} from '../api/diagnosis'
import DiagnosisReport from '../diagnosis/DiagnosisReport.vue'
import AiInterpret from '../components/AiInterpret.vue'
import { GROUP_KEYS, GROUP_LABELS } from '../diagnosis/exportReport'

// 五组配色（红涨绿跌体系下：买入=机会暖色，回避/错误=风险色）
const GROUP_CLASS = {
  buy: 'grp-buy',
  watch: 'grp-watch',
  avoid: 'grp-avoid',
  position: 'grp-position',
  error: 'grp-error',
}

const form = reactive({
  text: '',
  market: 'stock',
  direction: '',
  scheme_name: '',
  k_type: '日K',
})

const taskId = ref('')
const status = ref('idle') // idle | running | completed | cancelled | failed
const progress = reactive({ current: 0, total: 0, pct: 0, message: '' })
const summary = ref(null)
const results = ref([])
let esRef = null

const isRunning = computed(() => status.value === 'running')
const canExport = computed(
  () => summary.value && (status.value === 'completed' || status.value === 'cancelled'),
)

// 按五组分类的结果（兜底未知 category 归入 error）
const grouped = computed(() => {
  const g = {}
  for (const k of GROUP_KEYS) g[k] = []
  for (const r of results.value) {
    const key = GROUP_KEYS.includes(r.category) ? r.category : 'error'
    g[key].push(r)
  }
  return g
})

function buildPayload() {
  const payload = {
    text: form.text,
    market: form.market,
    k_type: form.k_type || '日K',
  }
  if (form.direction) payload.direction = form.direction
  if (form.scheme_name.trim()) payload.scheme_name = form.scheme_name.trim()
  return payload
}

async function onStart() {
  const text = form.text.trim()
  if (!text) {
    ElMessage.warning('请粘贴自选股列表（每行一只）')
    return
  }
  status.value = 'running'
  progress.current = 0
  progress.total = 0
  progress.pct = 0
  progress.message = '启动中…'
  summary.value = null
  results.value = []

  try {
    taskId.value = await startDiagnosis(buildPayload())
    esRef = openDiagnosisEvents(taskId.value, {
      onStage: (d) => {
        if (d && d.message) progress.message = d.message
      },
      onProgress: (d) => {
        if (!d) return
        progress.current = d.current || 0
        progress.total = d.total || 0
        progress.pct = d.pct || 0
        progress.message = d.message || ''
      },
      onDone: () => loadResult(),
      onError: () => {
        status.value = 'failed'
        ElMessage.error('诊断连接中断')
      },
    })
  } catch (e) {
    status.value = 'failed'
    ElMessage.error(e.message || '启动失败')
  }
}

async function loadResult() {
  try {
    const data = await getDiagnosis(taskId.value)
    status.value = data.status || 'completed'
    summary.value = data.summary || {}
    results.value = Array.isArray(data.results) ? data.results : []
    progress.total = data.total || results.value.length
    progress.current = data.current != null ? data.current : progress.total
    progress.pct = progress.total ? Math.round((progress.current / progress.total) * 100) : 100
  } catch (e) {
    status.value = 'failed'
    ElMessage.error(e.message || '结果获取失败')
  } finally {
    if (esRef) {
      esRef.close()
      esRef = null
    }
  }
}

async function onCancel() {
  if (!taskId.value) return
  try {
    await cancelDiagnosis(taskId.value)
    ElMessage.info('已发送取消请求（最多再处理 1 只）')
  } catch (e) {
    ElMessage.error(e.message || '取消失败')
  }
}

onUnmounted(() => {
  if (esRef) esRef.close()
})
</script>

<template>
  <div class="diag">
    <header class="diag-head">
      <h2 class="diag-title">批量诊断</h2>
      <p class="diag-sub">对自选股列表逐只分析，按「买入 / 关注 / 回避 / 持仓 / 错误」五组归类</p>
    </header>

    <!-- 参数表单 -->
    <section class="diag-form panel">
      <el-form label-width="84px" label-position="left">
        <el-form-item label="自选列表">
          <el-input
            v-model="form.text"
            type="textarea"
            :rows="5"
            placeholder="每行一只，如：&#10;sh600519 贵州茅台&#10;sz000001 平安银行"
            :disabled="isRunning"
          />
        </el-form-item>
        <el-form-item label="市场">
          <el-radio-group v-model="form.market" :disabled="isRunning">
            <el-radio value="stock">A股</el-radio>
            <el-radio value="futures">期货</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="方向">
          <el-select v-model="form.direction" placeholder="不限" clearable :disabled="isRunning" style="width: 160px">
            <el-option label="不限" value="" />
            <el-option label="做多" value="long" />
            <el-option label="做空" value="short" />
          </el-select>
        </el-form-item>
        <el-form-item label="分析周期">
          <el-input v-model="form.k_type" :disabled="isRunning" style="width: 160px" />
        </el-form-item>
        <el-form-item label="指定方案">
          <el-input v-model="form.scheme_name" placeholder="留空=按市场自动" :disabled="isRunning" style="width: 240px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="isRunning" @click="onStart">开始诊断</el-button>
          <el-button type="danger" :disabled="!isRunning" @click="onCancel">取消</el-button>
        </el-form-item>
      </el-form>
    </section>

    <!-- 进度 -->
    <section v-if="status !== 'idle'" class="diag-progress panel">
      <div class="prog-row">
        <el-progress :percentage="progress.pct" :stroke-width="14" />
        <span class="prog-text">
          {{ progress.current }}/{{ progress.total }} · {{ progress.message }}
        </span>
      </div>
      <div class="prog-status" :class="`st-${status}`">
        状态：{{ {running:'运行中', completed:'已完成', cancelled:'已取消', failed:'失败'}[status] || status }}
      </div>
    </section>

    <!-- 五组汇总 -->
    <section v-if="summary" class="diag-summary">
      <div
        v-for="k in GROUP_KEYS"
        :key="k"
        class="sum-card"
        :class="GROUP_CLASS[k]"
      >
        <div class="sum-label">{{ GROUP_LABELS[k] }}</div>
        <div class="sum-count">{{ summary[k] || 0 }}</div>
      </div>
    </section>

    <!-- 五组结果表 -->
    <section v-if="results.length" class="diag-result panel">
      <el-tabs>
        <el-tab-pane v-for="k in GROUP_KEYS" :key="k" :label="`${GROUP_LABELS[k]} (${grouped[k].length})`">
          <el-table :data="grouped[k]" size="small" empty-text="无">
            <el-table-column prop="code" label="代码" width="140" />
            <el-table-column prop="name" label="名称" />
            <el-table-column label="分类" width="120">
              <template #default="{ row }">{{ GROUP_LABELS[row.category] || row.category || '-' }}</template>
            </el-table-column>
            <el-table-column label="操作" width="110" fixed="right">
              <template #default="{ row }">
                <AiInterpret scene="diagnosis" :payload="row" label="AI解读" />
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </section>

    <!-- 报告导出（F-404） -->
    <section v-if="canExport" class="diag-export panel">
      <DiagnosisReport :summary="summary" :results="results" />
    </section>
  </div>
</template>

<style scoped>
.diag {
  max-width: 920px;
  margin: 0 auto;
  padding: 20px 16px 40px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.diag-head { margin-bottom: 4px; }
.diag-title { margin: 0; font-size: 20px; color: var(--mbull-text); }
.diag-sub { margin: 4px 0 0; font-size: 12px; color: var(--mbull-text-dim); }

.panel {
  background: var(--mbull-panel);
  border: 1px solid var(--mbull-border);
  border-radius: 8px;
  padding: 16px;
}

.diag-progress .prog-row { display: flex; align-items: center; gap: 12px; }
.diag-progress .prog-text { font-size: 12px; color: var(--mbull-text-dim); white-space: nowrap; }
.prog-status { margin-top: 8px; font-size: 13px; }
.st-completed { color: var(--mbull-down); }
.st-cancelled { color: var(--mbull-text-dim); }
.st-failed { color: var(--mbull-up); }
.st-running { color: var(--mbull-accent); }

.diag-summary { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; }
.sum-card {
  background: var(--mbull-panel);
  border: 1px solid var(--mbull-border);
  border-radius: 8px;
  padding: 12px;
  text-align: center;
}
.sum-label { font-size: 12px; color: var(--mbull-text-dim); }
.sum-count { font-size: 24px; font-weight: 600; margin-top: 4px; }
.grp-buy .sum-count { color: var(--mbull-up); }
.grp-watch .sum-count { color: #f5a623; }
.grp-avoid .sum-count { color: var(--mbull-down); }
.grp-position .sum-count { color: var(--mbull-accent); }
.grp-error .sum-count { color: #b2b5be; }

.diag-result { padding: 8px 16px 16px; }
</style>
