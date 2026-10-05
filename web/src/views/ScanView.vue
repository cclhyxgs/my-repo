<template>
  <div class="scan-view">
    <el-card class="panel" shadow="never">
      <template #header>
        <div class="panel-title">全市场扫描（F-501~506）</div>
      </template>

      <el-form :model="form" label-width="84px" class="scan-form" :disabled="running">
        <el-form-item label="市场">
          <el-radio-group v-model="form.market">
            <el-radio value="stock">A股</el-radio>
            <el-radio value="futures">期货</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="方向">
          <el-radio-group v-model="form.direction">
            <el-radio :value="null">全部</el-radio>
            <el-radio value="long">多头</el-radio>
            <el-radio value="short">空头</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="周期">
          <el-radio-group v-model="form.k_type">
            <el-radio value="日K">日K</el-radio>
            <el-radio value="周K">周K</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="方案">
          <el-input v-model="form.scheme_name" placeholder="留空=按市场+方向解析" clearable />
        </el-form-item>
        <el-form-item label="缓存复用">
          <el-switch v-model="form.use_cache" />
          <span class="hint">24h 内缓存可直接复用（F-504）</span>
        </el-form-item>
      </el-form>

      <div class="actions">
        <el-button type="primary" :loading="running" @click="onStart" :disabled="running || paused">
          {{ reused ? '复用缓存' : '开始扫描' }}
        </el-button>
        <el-button v-if="running && !paused" @click="onPause">暂停</el-button>
        <el-button v-if="paused" @click="onResume">继续</el-button>
        <el-button v-if="running" type="danger" plain @click="onCancel">取消</el-button>
      </div>

      <div v-if="taskId" class="progress">
        <el-progress :percentage="pct" :status="progressStatus" />
        <div class="progress-meta">
          <span>{{ statusText }}</span>
          <span v-if="progress.current">已处理 {{ progress.current }} / {{ progress.total }}</span>
          <span v-if="progress.message">{{ progress.message }}</span>
        </div>
      </div>
    </el-card>

    <el-card v-if="sectors.length" class="panel" shadow="never">
      <template #header><div class="panel-title">板块强度（F-505）</div></template>
      <el-table :data="sectors" size="small" max-height="260" :default-sort="{ prop: 'avg_score', order: 'descending' }">
        <el-table-column prop="sector" label="板块" min-width="120" />
        <el-table-column prop="avg_score" label="均分" width="90" sortable />
        <el-table-column prop="count" label="成分" width="80" sortable />
        <el-table-column prop="strong_count" label="强势数" width="90" sortable />
        <el-table-column prop="level" label="强度" width="80">
          <template #default="{ row }">
            <span :class="['level', 'lv-' + row.level]">{{ row.level }}</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card v-if="taskId" class="panel" shadow="never">
      <template #header>
        <div class="panel-title">
          扫描结果（F-503）
          <el-select v-model="ratingFilter" size="small" style="width: 120px; margin-left: 12px" @change="reloadResults">
            <el-option :value="null" label="全部星级" />
            <el-option :value="5" label="⭐≥5" />
            <el-option :value="4" label="⭐≥4" />
            <el-option :value="3" label="⭐≥3" />
          </el-select>
          <el-button size="small" type="success" plain style="margin-left: 12px" :disabled="!completed" @click="onExport">导出 CSV</el-button>
          <el-button size="small" type="primary" plain style="margin-left: 8px" :disabled="!completed" @click="onCopyWatchlist">复制到自选</el-button>
          <AiInterpret
            scene="scan"
            :payload="items"
            label="AI概览"
            style="margin-left: 8px"
            :disabled="!completed || !items.length"
          />
        </div>
      </template>

      <el-table
        :data="items"
        size="small"
        height="420"
        stripe
        @sort-change="onSort"
        @row-dblclick="() => {}"
      >
        <el-table-column prop="code" label="代码" width="100" />
        <el-table-column prop="name" label="名称" width="110" />
        <el-table-column prop="price" label="现价" width="90" sortable="custom" />
        <el-table-column prop="stock_score" label="技术分" width="90" sortable="custom" />
        <el-table-column prop="final_score" label="综合分" width="90" sortable="custom" />
        <el-table-column prop="stars" label="星级" width="90" />
        <el-table-column prop="level" label="等级" width="90" />
        <el-table-column prop="sector" label="板块" min-width="120" />
        <el-table-column prop="entry_tier_label" label="触发档位" min-width="100" />
      </el-table>

      <div class="result-foot">
        <span>共 {{ filteredTotal }} 条（已加载 {{ items.length }}）</span>
        <el-button size="small" :disabled="!hasMore" @click="loadMore">加载更多</el-button>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import {
  startScan, getScanProgress, getScanResults, getSectors,
  pauseScan, resumeScan, cancelScan, exportScanUrl, openScanEvents,
} from '../api/scan.js'
import AiInterpret from '../components/AiInterpret.vue'

const form = reactive({
  market: 'stock',
  direction: null,
  k_type: '日K',
  scheme_name: '',
  use_cache: true,
})

const taskId = ref('')
const progress = reactive({ status: '', current: 0, total: 0, pct: 0, message: '', market: 'stock', reused: false })
const running = computed(() => ['pending', 'running'].includes(progress.status))
const paused = computed(() => progress.status === 'paused')
const completed = computed(() => progress.status === 'completed')
const reused = computed(() => progress.reused)
const progressStatus = computed(() => {
  if (progress.status === 'completed') return 'success'
  if (progress.status === 'failed' || progress.status === 'cancelled') return 'exception'
  return ''
})
const statusText = computed(() => {
  const map = { pending: '排队中', running: '扫描中', paused: '已暂停', completed: '完成', cancelled: '已取消', failed: '失败' }
  return map[progress.status] || progress.status
})

const items = ref([])
const filteredTotal = ref(0)
const offset = ref(0)
const limit = 200
const ratingFilter = ref(null)
const sortField = ref('final_score')
const sortOrder = ref('desc')
const hasMore = computed(() => items.value.length < filteredTotal.value)

const sectors = ref([])
let es = null

function resetResults() {
  items.value = []
  offset.value = 0
  filteredTotal.value = 0
}

async function reloadResults() {
  resetResults()
  await loadMore()
}

async function loadMore() {
  if (!taskId.value) return
  const data = await getScanResults(taskId.value, {
    offset: offset.value,
    limit,
    rating: ratingFilter.value,
    sort: sortField.value,
    order: sortOrder.value,
  })
  filteredTotal.value = data.filtered_total
  items.value = offset.value === 0 ? data.items : [...items.value, ...data.items]
  offset.value += limit
}

function onSort({ prop, order }) {
  if (!prop) return
  sortField.value = prop
  sortOrder.value = order === 'ascending' ? 'asc' : 'desc'
  reloadResults()
}

async function onStart() {
  try {
    const res = await startScan({
      market: form.market,
      direction: form.direction,
      k_type: form.k_type,
      scheme_name: form.scheme_name || null,
      use_cache: form.use_cache,
    })
    taskId.value = res.task_id
    progress.reused = res.reused
    resetResults()
    startPolling()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function onPause() {
  if (!taskId.value) return
  await pauseScan(taskId.value)
  await syncProgress()
}
async function onResume() {
  if (!taskId.value) return
  await resumeScan(taskId.value)
  startPolling()
}
async function onCancel() {
  if (!taskId.value) return
  await cancelScan(taskId.value)
  await syncProgress()
}
function onExport() {
  if (!taskId.value) return
  const a = document.createElement('a')
  a.href = exportScanUrl(taskId.value)
  a.download = ''
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
}

/**
 * 复制筛选结果到剪贴板，供自选诊断批量精查。
 * 格式：每行一只「代码,名称」——与后端 parse_watchlist 按逗号拆字段契约一致。
 */
async function onCopyWatchlist() {
  if (!taskId.value) return
  try {
    // limit=0 → 后端返回当前星级筛选/排序口径下的全量结果（read_results 对 limit=0 返回全量）
    const data = await getScanResults(taskId.value, {
      offset: 0,
      limit: 0,
      rating: ratingFilter.value,
      sort: sortField.value,
      order: sortOrder.value,
    })
    const rows = data.items || []
    if (!rows.length) {
      ElMessage.warning('当前筛选下没有可复制的股票')
      return
    }
    const text = rows.map((r) => `${r.code},${r.name}`).join('\n')
    await copyToClipboard(text)
    const scope = ratingFilter.value ? `⭐≥${ratingFilter.value}` : '全部星级'
    ElMessage.success(`已复制 ${rows.length} 条（${scope}）到剪贴板，可直接粘贴到自选诊断`)
  } catch (e) {
    ElMessage.error(`复制失败：${e.message || e}`)
  }
}

async function copyToClipboard(text) {
  const cb = navigator.clipboard
  if (cb && cb.writeText) {
    try {
      await cb.writeText(text)
      return
    } catch {
      // 继续走降级路径（非 https 等场景）
    }
  }
  if (!document.execCommand) throw new Error('浏览器不支持复制')
  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.position = 'fixed'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  ta.select()
  let ok = false
  try {
    ok = document.execCommand('copy')
  } finally {
    document.body.removeChild(ta)
  }
  if (!ok) throw new Error('复制被拒绝')
}

async function syncProgress() {
  if (!taskId.value) return
  const p = await getScanProgress(taskId.value)
  progress.status = p.status
  progress.current = p.current
  progress.total = p.total
  progress.pct = p.pct
  progress.message = p.message
  progress.market = p.market
  progress.reused = p.reused
  if (completed.value) {
    await reloadResults()
    await refreshSectors()
  }
}

async function refreshSectors() {
  if (!taskId.value) return
  const r = await getSectors({ market: form.market, taskId: taskId.value })
  sectors.value = r.sectors
}

function startPolling() {
  if (es) es.close()
  es = openScanEvents(taskId.value, {
    onProgress: (d) => { if (d) { progress.current = d.current; progress.total = d.total; progress.pct = d.pct; progress.message = d.message } },
    onDone: async () => { await syncProgress() },
    onError: () => { /* SSE 关闭后回退轮询 */ void syncProgress() },
  })
}

onBeforeUnmount(() => { if (es) es.close() })
</script>

<style scoped>
.scan-view { display: flex; flex-direction: column; gap: 14px; padding: 14px; }
.panel { background: var(--mbull-bg, #131722); border: 1px solid #2a2e39; color: #d1d4dc; }
.panel-title { font-weight: 600; color: #e0e3eb; display: flex; align-items: center; }
.scan-form { margin-bottom: 8px; }
.hint { color: #787b86; font-size: 12px; margin-left: 10px; }
.actions { display: flex; gap: 10px; margin: 6px 0 10px; }
.progress { margin-top: 4px; }
.progress-meta { display: flex; gap: 16px; color: #b2b5be; font-size: 12px; margin-top: 6px; flex-wrap: wrap; }
.level { font-weight: 600; }
.lv-强 { color: #ef5350; }
.lv-中 { color: #f5a623; }
.lv-弱 { color: #26a69a; }
.lv-极弱 { color: #787b86; }
.result-foot { display: flex; justify-content: space-between; align-items: center; margin-top: 8px; color: #787b86; font-size: 12px; }
:deep(.el-table), :deep(.el-table__expanded-cell) { background: transparent; color: #d1d4dc; }
:deep(.el-table tr), :deep(.el-table th), :deep(.el-table td) { background: transparent; color: #d1d4dc; }
:deep(.el-table--striped .el-table__body tr.el-table__row--striped td) { background: #1a1e29; }
</style>
