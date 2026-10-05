<script setup>
// q-position · 仓位管理（镜像 index.html fillPositionList L3786-3851，控件 P-01~P-14）。
// 直接绑定 quantData；保存经 quantToConfig 归一化（entry_params/add_params/reduce_params/market_gate/risk_params）。
import { computed, ref } from 'vue'
import { CIRCUIT_NAME_OPTIONS } from '../quantModel'

const props = defineProps({
  model: { type: Object, required: true },
  direction: { type: String, default: 'long' },
})
const emit = defineEmits(['dirty'])

const data = props.model
const isShort = computed(() => props.direction === 'short')

function tierLabel(key) {
  return data.posTypes.find((p) => p.key === key)?.label || key
}

// P-07 各档位目标仓位：长单隐藏 top_reversal，短单隐藏 rebound/panic_rebound
const entryVisible = computed(() =>
  data.entryPos.filter((p) => (isShort.value ? !(p.key === 'rebound' || p.key === 'panic_rebound') : p.key !== 'top_reversal')),
)
const posLabel = (key) => posLabelMap[key] || key
const posLabelMap = {
  strong: '强势', standard: '标准', test: '试探', observe: '观望',
  none: '空仓', rebound: '博反弹', panic_rebound: '恐慌反转', top_reversal: '顶部回落',
}
function setPos(p, v) {
  p.val = (Number(v) || 0) / 100
  emit('dirty')
}
function posPct(p) {
  return Math.round((p.val || 0) * 100)
}

// P-06 门控
function toggleGate() {
  data.marketGateEnabled = !data.marketGateEnabled
  emit('dirty')
}
function setGateVal(g, key, v) {
  g[key] = v === '' ? '' : Number(v)
  emit('dirty')
}

// P-02/P-03/P-04/P-05 直写
function setNum(obj, key, v) {
  obj[key] = Number(v) || 0
  emit('dirty')
}

// P-09 加仓 / P-10 减仓 档位格
const addOpts = computed(() => data.addTriggerOptions)
const redOpts = computed(() => data.reduceTriggerOptions)
function tierCodes(t) {
  return t.codes || []
}
function toggleCode(arr, code) {
  const i = arr.indexOf(code)
  if (i >= 0) arr.splice(i, 1)
  else arr.push(code)
  emit('dirty')
}
// 触发参数（含 % 等单位）
function needParam(code) {
  const opts = addOpts.value.find((o) => o.code === code) || redOpts.value.find((o) => o.code === code)
  return opts && opts.param ? opts : null
}
function setTierParam(t, code, v) {
  if (!t.params) t.params = {}
  t.params[code] = Number.isNaN(Number(v)) ? v : Number(v)
  emit('dirty')
}
function tierRatio(t) {
  return t.ratio ?? 0
}
function setRatio(t, v) {
  t.ratio = Number(v) || 0
  emit('dirty')
}
function setReduceAction(t, v) {
  t.action = v
  if (v === '清仓') { t.ratio = 100; t.codes = t.codes || [] }
  emit('dirty')
}

// P-11 顶部减仓参数
function toggleTop(r) {
  r.enabled = !r.enabled
  emit('dirty')
}
function setTopVal(r, v) {
  r.val = Number(v) || ''
  emit('dirty')
}

// P-12 风控
function setRisk(r, v) {
  r.val = v
  emit('dirty')
}

const gateRows = data.marketGate
const addRows = Object.entries(data.addTiers)
const reduceRows = Object.entries(data.reduceTiers)
const tierKeys = ['t1', 't2', 't3']
const reduceActions = data.reduceActions || ['减仓', '清仓']
</script>

<template>
  <div class="pane">
    <!-- P-14/P-02/P-03 反转信号阈值 -->
    <div class="card">
      <div class="qh">反转信号阈值</div>
      <div class="qb">
        <div class="row">
          <span class="label">博反弹反转信号阈值</span>
          <el-input-number size="small" :model-value="data.reversal_score_threshold" :step="0.5" @change="(v) => setNum(data, 'reversal_score_threshold', v)" />
        </div>
        <div class="row">
          <span class="label">恐慌反转信号阈值</span>
          <el-input-number size="small" :model-value="data.panic_reversal_threshold" :step="0.5" @change="(v) => setNum(data, 'panic_reversal_threshold', v)" />
        </div>
      </div>
    </div>

    <!-- P-07 各档位目标仓位 -->
    <div class="card">
      <div class="qh">各档位目标仓位</div>
      <div class="qb">
        <div class="pos-grid">
          <div v-for="p in entryVisible" :key="p.key" class="row">
            <span class="label">{{ p.label }}</span>
            <el-input-number size="small" :model-value="posPct(p)" :min="0" :max="100" @change="(v) => setPos(p, v)" />
            <span class="unit">%</span>
          </div>
        </div>
      </div>
    </div>

    <!-- P-06 环境门控 + P-08 系数表 -->
    <div class="card">
      <div class="qh gate" @click="toggleGate">
        <span class="check" :class="{ on: data.marketGateEnabled }">✓</span> 启用全局环境门控系数
      </div>
      <div v-if="data.marketGateEnabled" class="qb">
        <div class="mg-head"><span>环境</span><span>阈值系数</span><span>上限</span></div>
        <div v-for="g in gateRows" :key="g.key" class="mg-row">
          <span class="label">{{ g.label }}</span>
          <el-input size="small" class="mg-input" :model-value="g.factor" placeholder="系数"
            @change="setGateVal(g, 'factor', $event)" />
          <el-input size="small" class="mg-input" :model-value="g.limit" placeholder="上限"
            @change="setGateVal(g, 'limit', $event)" />
        </div>
      </div>
    </div>

    <!-- P-04/P-05 加仓基础 -->
    <div class="card">
      <div class="qh">加仓参数</div>
      <div class="qb">
        <div class="row">
          <span class="label">放量突破倍数</span>
          <el-input-number size="small" :model-value="data.volumeBreakoutMultiplier" :step="0.1" @change="(v) => setNum(data, 'volumeBreakoutMultiplier', v)" />
        </div>
        <div class="row">
          <span class="label">单次加仓上限(%)</span>
          <el-input-number size="small" :model-value="data.maxAddPctPerStep" :min="0" :max="100" @change="(v) => setNum(data, 'maxAddPctPerStep', v)" />
          <span class="unit">%</span>
        </div>
      </div>
    </div>

    <!-- P-09 加仓档位表 -->
    <div class="card">
      <div class="qh">加仓档位</div>
      <div class="qb">
        <table class="tbl">
          <thead><tr><th>股票类型</th><th>第一档</th><th>第二档</th><th>第三档</th></tr></thead>
          <tbody>
            <tr v-for="[rowKey, tiers] in addRows" :key="rowKey">
              <td class="rowlabel">{{ tierLabel(rowKey) }}</td>
              <td v-for="tk in tierKeys" :key="tk">
                <div class="tier-cell">
                  <el-select size="small" multiple collapse-tags :model-value="tierCodes(tiers[tk])"
                    placeholder="触发条件" @change="(v) => (tiers[tk].codes = v, emit('dirty'))">
                    <el-option v-for="o in addOpts" :key="o.code || 'none'" :value="o.code" :label="o.label" />
                  </el-select>
                  <template v-for="code in tierCodes(tiers[tk])" :key="code">
                    <div v-if="needParam(code)">
                      <span class="plabel">{{ needParam(code).label }} {{ needParam(code).param }}</span>
                      <el-input-number size="small" :model-value="(tiers[tk].params || {})[code] ?? needParam(code).default"
                        :step="1" @change="(v) => setTierParam(tiers[tk], code, v)" />
                    </div>
                  </template>
                  <div class="ratio-row">
                    <span class="plabel">比例</span>
                    <el-input-number size="small" :model-value="tierRatio(tiers[tk])" :min="0" :max="100" @change="(v) => setRatio(tiers[tk], v)" />
                    <span class="unit">%</span>
                  </div>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- P-10 减仓档位表 -->
    <div class="card">
      <div class="qh">减仓档位</div>
      <div class="qb">
        <table class="tbl">
          <thead><tr><th>股票类型</th><th>第一档</th><th>第二档</th><th>第三档</th></tr></thead>
          <tbody>
            <tr v-for="[rowKey, tiers] in reduceRows" :key="rowKey">
              <td class="rowlabel">{{ tierLabel(rowKey) }}</td>
              <td v-for="tk in tierKeys" :key="tk">
                <div class="tier-cell">
                  <el-select size="small" multiple collapse-tags :model-value="tierCodes(tiers[tk])"
                    placeholder="触发条件" @change="(v) => (tiers[tk].codes = v, emit('dirty'))">
                    <el-option v-for="o in redOpts" :key="o.code || 'none'" :value="o.code" :label="o.label" />
                  </el-select>
                  <template v-for="code in tierCodes(tiers[tk])" :key="code">
                    <div v-if="needParam(code)">
                      <span class="plabel">{{ needParam(code).label }} {{ needParam(code).param }}</span>
                      <el-input-number size="small" :model-value="(tiers[tk].params || {})[code] ?? needParam(code).default"
                        :step="1" @change="(v) => setTierParam(tiers[tk], code, v)" />
                    </div>
                  </template>
                  <div class="ratio-row">
                    <el-select size="small" :model-value="tiers[tk].action" style="width:80px"
                      @change="(v) => setReduceAction(tiers[tk], v)">
                      <el-option v-for="a in reduceActions" :key="a" :value="a" :label="a" />
                    </el-select>
                    <el-input-number size="small" :model-value="tiers[tk].action === '清仓' ? 100 : tierRatio(tiers[tk])"
                      :min="0" :max="100" :disabled="tiers[tk].action === '清仓'" @change="(v) => setRatio(tiers[tk], v)" />
                    <span class="unit">%</span>
                  </div>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- P-11 顶部减仓参数 -->
    <div class="card">
      <div class="qh">顶部减仓参数（止损）</div>
      <div class="qb">
        <div v-for="r in data.reduceTop" :key="r.key" class="row">
          <span class="check" :class="{ on: r.enabled }" @click="toggleTop(r)">✓</span>
          <span class="label">{{ r.label }}</span>
          <el-input-number size="small" :model-value="r.val" :min="r.min" :max="r.max" :step="r.step"
            :disabled="!r.enabled" @change="(v) => setTopVal(r, v)" />
          <span class="unit">{{ r.suffix }}</span>
          <span class="desc">{{ r.desc }}</span>
        </div>
      </div>
    </div>

    <!-- P-12 风控 + P-01 熔断基准指数 -->
    <div class="card">
      <div class="qh">风控参数</div>
      <div class="qb">
        <div v-for="r in data.risk" :key="r.key" class="row">
          <span class="label">{{ r.label }}</span>
          <el-input size="small" class="risk-input" :model-value="r.val" placeholder="留空不启用"
            @change="setRisk(r, $event)" />
          <span class="desc">{{ r.desc }}</span>
        </div>
        <div class="row">
          <span class="label">熔断基准指数</span>
          <el-select size="small" :model-value="data.circuitBreakerIndex" style="width:130px" @change="(v) => setNum(data, 'circuitBreakerIndex', v)">
            <el-option v-for="n in CIRCUIT_NAME_OPTIONS" :key="n" :value="n" :label="n" />
          </el-select>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
export default { expose: [] }
</script>

<style scoped>
.pane { display: flex; flex-direction: column; gap: 10px; }
.card { border: 1px solid var(--mbull-border, rgba(255, 255, 255, 0.08)); border-radius: 6px; overflow: hidden; }
.qh { padding: 8px 12px; font-weight: 600; font-size: 13px; background: rgba(255, 255, 255, 0.04); }
.gate { display: flex; align-items: center; gap: 6px; cursor: pointer; user-select: none; }
.qb { padding: 10px 12px; }
.row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; font-size: 13px; }
.row .label { width: 170px; }
.pos-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 4px 18px; }
.add { margin-left: auto; }
.unit { color: var(--mbull-text-dim, #787b86); }
.desc { color: var(--mbull-text-dim, #787b86); font-size: 12px; }
.check { width: 18px; height: 18px; border: 1px solid rgba(255, 255, 255, 0.25); border-radius: 4px; text-align: center; line-height: 16px; color: transparent; font-size: 12px; }
.check.on { background: var(--mbull-accent, #e0522c); border-color: var(--mbull-accent, #e0522c); color: #fff; }
.mg-head { display: grid; grid-template-columns: 100px 130px 130px; gap: 8px; font-size: 12px; color: var(--mbull-text-dim, #787b86); padding: 4px 0; }
.mg-row { display: grid; grid-template-columns: 100px 130px 130px; gap: 8px; align-items: center; padding: 4px 0; }
.mg-row .label { font-size: 13px; }
.mg-input { width: 130px; }
.tbl { width: 100%; border-collapse: collapse; }
.tbl th, .tbl td { border: 1px solid var(--mbull-border, rgba(255, 255, 255, 0.08)); padding: 6px; font-size: 12.5px; text-align: left; vertical-align: top; }
.tbl th { background: rgba(255, 255, 255, 0.05); }
.rowlabel { font-weight: 600; }
.tier-cell { display: flex; flex-direction: column; gap: 5px; }
.plabel { font-size: 11px; color: var(--mbull-text-dim, #787b86); }
.ratio-row { display: flex; align-items: center; gap: 5px; }
.risk-input { width: 130px; }
</style>