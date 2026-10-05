<script setup>
// q-threshold · 状态分界（镜像 index.html fillThresholdList L3647-3688 + renderAndEditor L3429 + renderVetoMC L3706 + fillResonance L3694 + fillVetoParamPanel）。
// 每档位：阈值输入 + AND 技术信号编辑器 + 否决项多选；另含否决项参数面板与技术共振配置。
import { computed, ref } from 'vue'
import {
  AND_INDICATORS, AND_OPS, SIGNAL_OPTIONS, VETO_SCENES, VETO_PARAMS_SCHEMA,
  buildVetoOptions,
} from '../quantModel'

const props = defineProps({
  model: { type: Object, required: true },
  direction: { type: String, default: 'long' },
  mode: { type: String, default: 'advanced' }, // advanced / basic
})
const emit = defineEmits(['dirty'])

const data = props.model
const isShort = computed(() => props.direction === 'short')
const isBasic = computed(() => props.mode === 'basic')

// 空单隐藏「博反弹/恐慌线」，长单隐藏「顶部回落」
const visible = computed(() =>
  data.thresholds.filter((t) => (isShort.value ? !(t.key === 'rebound' || t.key === 'panic_rebound') : t.key !== 'top_reversal')),
)
const isRebound = (t) => t.key === 'rebound' || t.key === 'panic_rebound'

// 无信号（非反弹档）→ 显示 "—"，不渲染否决项
const isLeft = (t) => (t.signal === '—' || t.signal === 'none' || t.signal === null || t.signal === undefined) && !isRebound(t)

// 空单方向翻转信号中文标签（值保中性，镜像 SIGNAL_FLIP_LIST L3398-3403）
const signalOptions = computed(() => {
  if (!isShort.value) return SIGNAL_OPTIONS
  const flip = {
    空头排列: '多头排列', 跌破MA5: '站上MA5', 跌破MA20: '站上MA20', 跌破MA60: '站上MA60',
    KDJ死叉: 'KDJ金叉', MACD死叉: 'MACD金叉', 均线死叉: '均线金叉', 布林带下轨突破: '布林带上轨突破', 放量下跌: '放量上涨',
  }
  return SIGNAL_OPTIONS.map((s) => flip[s] || s)
})

// —— 阈值 ——
function setVal(t, v) {
  t.val = Number(v) || 0
  emit('dirty')
}
function toggleVetoOn(t) {
  t.veto_on = !t.veto_on
  emit('dirty')
}

// —— 否决项多选（veto_enabled）——
const vetoOptions = computed(() => buildVetoOptions(props.direction))
const vetoByScene = computed(() =>
  VETO_SCENES.map(([key, label]) => ({
    scene: key, label, items: vetoOptions.value.filter((v) => v.scene === key),
  })).filter((g) => g.items.length),
)
function toggleVetoItem(t, key) {
  t.veto_enabled[key] = !t.veto_enabled[key]
  emit('dirty')
}
function vetoCount(t) {
  return Object.values(t.veto_enabled || {}).filter(Boolean).length
}

// —— 否决项参数面板 ——
const showVetoPanel = ref(false)
const vetoParamKeys = computed(() =>
  Object.keys(VETO_PARAMS_SCHEMA).filter((k) => vetoOptions.value.some((v) => v.key === k)),
)
function vetoParamVal(key, param) {
  return data.vetoParams?.[key]?.[param] ?? VETO_PARAMS_SCHEMA[key][param].default
}
function setVetoParam(key, param, v) {
  if (!data.vetoParams) data.vetoParams = {}
  if (!data.vetoParams[key]) data.vetoParams[key] = {}
  data.vetoParams[key][param] = Number(v) || VETO_PARAMS_SCHEMA[key][param].default
  emit('dirty')
}

// —— AND 技术信号编辑器 ——
const panels = ref({})
function togglePanel(t) {
  panels.value[t.key] = !panels.value[t.key]
}
function ensureArr(t) {
  if (!Array.isArray(t.signal)) t.signal = t.signal && t.signal !== 'none' && t.signal !== '—' && t.signal !== null ? [{ signal: t.signal }] : []
  return t.signal
}
function condLabel(c) {
  if (!c) return ''
  if (c.signal !== undefined) return c.signal
  if (c.indicator !== undefined) return `${AND_INDICATORS[c.indicator] || c.indicator} ${c.op || ''} ${c.value ?? ''}`
  return JSON.stringify(c)
}
function hasSignal(t, s) {
  return ensureArr(t).some((c) => c && c.signal === s)
}
function toggleSignal(t, s) {
  const arr = ensureArr(t)
  const i = arr.findIndex((c) => c && c.signal === s)
  if (i >= 0) arr.splice(i, 1)
  else arr.push({ signal: s })
  emit('dirty')
}
function removeCond(t, i) {
  const arr = ensureArr(t)
  if (i >= 0 && i < arr.length) arr.splice(i, 1)
  emit('dirty')
}
function addIndicator(t, ind, op, val) {
  const n = Number(val)
  if (!ind || Number.isNaN(n)) return
  ensureArr(t).push({ indicator: ind, op, value: n })
  emit('dirty')
}

// —— 技术共振 ——
function setSignificant(v) {
  data.thresholdResonance.significantThreshold = Number(v) || 0
  emit('dirty')
}
function setStarCutoff(idx, v) {
  data.thresholdResonance.starCutoffs[idx] = Number(v) || 0
  emit('dirty')
}
</script>

<template>
  <div class="pane">
    <el-alert :closable="false" class="tip" type="warning">
      {{ isShort ? '阈值需与因子组合配套使用。强势/标准/试探/观望为「得分≥阈值」；顶部回落为「因子预期<阈值（越负=涨势猛/超买）」。' : isBasic ? '初级用法因子休眠，不按得分分档。请在强势/标准/试探/观望/博反弹/恐慌线各档配置技术条件或数值指标定档建仓；否决项红灯层同样可配置。' : '阈值需与因子组合配套使用。强势/标准/试探/观望为「得分≥阈值」；博反弹/恐慌为「因子预期<阈值（越负越极端）」。' }}
    </el-alert>

    <div class="card">
      <div class="qh">分类阈值 + 技术信号 + 否决项</div>
      <div class="qb">
        <div v-for="t in visible" :key="t.key" class="th-row">
          <span class="label">{{ t.label }}</span>
          <span v-if="t.dir" class="dir">{{ t.dir }}</span>
          <el-input v-if="!isBasic" v-model="t.val" size="small" class="val" @change="setVal(t, t.val)" />
          <span v-else class="dir">—</span>

          <!-- 信号区 -->
          <span v-if="isLeft(t)" class="signal-mut">—</span>
          <div v-else class="signal-box">
            <div class="chips">
              <span v-for="(c, ci) in ensureArr(t)" :key="ci" class="chip">
                {{ condLabel(c) }}<i class="chip-x" @click="removeCond(t, ci)" title="删除">×</i>
              </span>
              <el-button size="small" class="add" @click="togglePanel(t)">+ AND条件</el-button>
            </div>
            <div v-if="panels[t.key]" class="panel">
              <div class="sec">
                <div class="sec-title">中文信号（勾选即作为 AND 条件加入）</div>
                <label v-for="s in signalOptions" :key="s" class="and-check">
                  <input type="checkbox" :checked="hasSignal(t, s)" @change="toggleSignal(t, s)" />
                  {{ s }}
                </label>
              </div>
              <div class="sec">
                <div class="sec-title">数值指标条件</div>
                <el-select size="small" class="ind" :model-value="''" placeholder="选择指标" @change="(v) => (t._ind = v)">
                  <el-option v-for="(label, k) in AND_INDICATORS" :key="k" :value="k" :label="label" />
                </el-select>
                <el-select size="small" class="op" :model-value="'>='">
                  <el-option v-for="o in AND_OPS" :key="o" :value="o" :label="o" />
                </el-select>
                <el-input-number size="small" :model-value="70" :step="1" @update:model-value="(v) => (t._v = v)" />
                <el-button size="small" type="primary" @click="addIndicator(t, t._ind, t._op || '>=', t._v ?? 70)">确定</el-button>
              </div>
            </div>
          </div>

          <!-- 否决项 -->
          <div v-if="!(isLeft(t) || isRebound(t))" class="veto">
            <span class="check" :class="{ on: t.veto_on }" @click="toggleVetoOn(t)">否决项</span>
            <div v-if="t.veto_on" class="veto-detail">
              <div class="chips">
                <span v-for="v in vetoOptions.filter((x) => t.veto_enabled[x.key])" :key="v.key" class="chip">
                  {{ v.label }}<i class="chip-x" @click="toggleVetoItem(t, v.key)" title="移除">×</i>
                </span>
                <el-button v-if="t.veto_on" size="small" class="add">＋否决项（可多选）</el-button>
              </div>
              <div class="vlist">
                <div v-for="g in vetoByScene" :key="g.scene" class="vgroup">
                  <div class="vgroup-title">{{ g.label }}</div>
                  <label v-for="item in g.items" :key="item.key" class="and-check">
                    <input type="checkbox" :checked="!!t.veto_enabled[item.key]" @change="toggleVetoItem(t, item.key)" />
                    {{ item.label }}
                  </label>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="qh veto-toggle" @click="showVetoPanel = !showVetoPanel">
        否决项阈值参数面板 <span class="desc">{{ showVetoPanel ? '▾ 收起' : '▸ 展开' }}</span>
      </div>
      <div v-if="showVetoPanel" class="qb">
        <el-empty v-if="!vetoParamKeys.length" description="无可调参数" :image-size="50" />
        <div v-for="key in vetoParamKeys" :key="key" class="vparam">
          <span class="vparam-name">{{ VETO_PARAMS_SCHEMA[key] ? '' : '' }}{{ vetoOptions.find((v) => v.key === key)?.label || key }}</span>
          <div v-for="(p, pk) in VETO_PARAMS_SCHEMA[key] || {}" :key="pk" class="vparam-row">
            <span class="label">{{ p.label }}</span>
            <el-input-number size="small" :model-value="vetoParamVal(key, pk)" @change="(v) => setVetoParam(key, pk, v)" />
            <span v-if="p.unit" class="unit">{{ p.unit }}</span>
          </div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="qh">技术共振配置（纯技术面强度 · 可选）</div>
      <div class="qb">
        <div class="res-row">
          <span class="label">显著阈值</span>
          <el-input-number size="small" :model-value="data.thresholdResonance.significantThreshold" :step="0.5"
            @change="setSignificant" />
          <span class="desc">单因子 |贡献| 达标线，建议 5.0</span>
        </div>
        <div class="res-row">
          <span class="label">星级切点</span>
          <div class="bands">
            <el-input-number v-for="(c, i) in data.thresholdResonance.starCutoffs" :key="i" size="small" :model-value="c"
              :step="0.1" :min="0" :max="1" @change="(v) => setStarCutoff(i, v)" />
          </div>
          <span class="desc">{{ data.thresholdResonance.starCutoffs.length }} 个升序 0~1 切点，把技术共振占比切成 1★~{{ data.thresholdResonance.starCutoffs.length + 1 }}★</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pane { display: flex; flex-direction: column; gap: 10px; }
.tip { margin-bottom: 4px; }
.card { border: 1px solid var(--mbull-border, rgba(255, 255, 255, 0.08)); border-radius: 6px; overflow: hidden; }
.qh { padding: 8px 12px; font-weight: 600; font-size: 13px; background: rgba(255, 255, 255, 0.04); }
.veto-toggle { cursor: pointer; }
.qb { padding: 10px 12px; }
.th-row { display: grid; grid-template-columns: 72px 24px 84px 1fr 1fr; gap: 8px; align-items: start; padding: 7px 4px; border-bottom: 1px solid var(--mbull-border, rgba(255, 255, 255, 0.07)); font-size: 13px; }
.label { padding-top: 4px; }
.dir { color: var(--mbull-text-dim, #787b86); padding-top: 4px; }
.val { width: 84px; }
.signal-mut { color: var(--mbull-text-dim, #787b86); padding-top: 5px; }
.signal-box { border: 1px dashed rgba(255, 255, 255, 0.15); border-radius: 6px; padding: 8px; }
.chips { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.chip { background: rgba(41, 98, 255, 0.15); border: 1px solid rgba(41, 98, 255, 0.4); color: var(--mbull-accent, #e0522c); padding: 2px 8px; border-radius: 4px; font-size: 12px; display: inline-flex; align-items: center; gap: 6px; }
.chip-x { cursor: pointer; font-style: normal; opacity: 0.7; }
.chip-x:hover { opacity: 1; }
.add { margin-left: 2px; }
.panel { margin-top: 8px; border-top: 1px solid rgba(255, 255, 255, 0.1); padding-top: 8px; }
.sec { margin-bottom: 8px; }
.sec-title { font-size: 12px; color: var(--mbull-text-dim, #787b86); margin-bottom: 4px; }
.and-check { display: inline-flex; align-items: center; gap: 4px; margin-right: 10px; font-size: 12.5px; }
.ind { width: 150px; margin-right: 6px; }
.op { width: 80px; margin-right: 6px; }
.veto .check { cursor: pointer; color: var(--mbull-text-dim, #787b86); font-size: 12.5px; user-select: none; }
.veto .check.on { color: var(--mbull-accent, #e0522c); font-weight: 600; }
.veto-detail { margin-top: 6px; border: 1px dashed rgba(255, 255, 255, 0.15); border-radius: 6px; padding: 8px; }
.cell-gap { margin-top: 4px; }
.vlist { margin-top: 8px; border-top: 1px solid rgba(255, 255, 255, 0.1); padding-top: 8px; }
.vgroup { margin-bottom: 6px; }
.vgroup-title { font-size: 12px; color: var(--mbull-text-dim, #787b86); }
.vparam { border-bottom: 1px solid var(--mbull-border, rgba(255, 255, 255, 0.07)); padding: 6px 2px; }
.vparam-name { font-weight: 600; font-size: 12.5px; margin-right: 12px; }
.vparam-row { display: inline-flex; align-items: center; gap: 6px; margin-right: 18px; font-size: 12px; }
.vparam-row .label { color: var(--mbull-text-dim, #787b86); }
.unit { color: var(--mbull-text-dim, #787b86); }
.res-row { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; font-size: 13px; }
.res-row .label { font-weight: 600; width: 80px; }
.bands { display: flex; gap: 6px; }
.desc { color: var(--mbull-text-dim, #787b86); font-size: 12px; }
</style>