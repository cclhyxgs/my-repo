<script setup>
// q-ghost · 幽灵规则（镜像 index.html fillGhostList L3947-3973，控件 G-01~G-07）。
// 注意：ma_confirm 空值 → 界面兜底显示 sma_20，但不写盘（configToQuant 已保留该语义）。
import { computed } from 'vue'

const props = defineProps({
  model: { type: Object, required: true },
  direction: { type: String, default: 'long' },
})
const emit = defineEmits(['dirty'])

const data = props.model
const gh = data.ghost
const defaults = data.ghostDefaults

const byKey = computed(() => Object.fromEntries(gh.map((g) => [g.key, g])))

function keyDisplay(key) {
  const g = byKey.value[key]
  if (!g) return
  if (key === 'ma_confirm') return g.val || 'sma_20'
  return g.val ?? ''
}
function setVal(key, v) {
  const g = byKey.value[key]
  if (!g) return
  if (key === 'grace_period_days') g.val = Math.trunc(Number(v) || 0)
  else g.val = v
  emit('dirty')
}
function resetAll() {
  gh.forEach((g) => (g.val = defaults[g.key] ?? g.val))
  emit('dirty')
}
const maOptions = byKey.value.ma_confirm?.options || []
const descText = `默认：宽限 ${defaults.grace_period_days} 天 / 证伪 ${defaults.profit_threshold}% / 加仓 ${defaults.add_profit_threshold}% / RSI ${defaults.rsi_confirm} / ${defaults.ma_confirm}`
</script>

<template>
  <div class="pane">
    <div class="ghost-note">{{ descText }}</div>
    <div class="rows">
      <div v-for="g in gh" :key="g.key" class="row">
        <span class="label">{{ g.label }}</span>
        <el-select v-if="g.isSelect" size="small" style="width:150px" :model-value="keyDisplay(g.key)"
          @change="(v) => setVal(g.key, v)">
          <el-option v-for="o in g.options" :key="o" :value="o" :label="o" />
        </el-select>
        <el-input v-else size="small" class="input" :model-value="keyDisplay(g.key)" @change="(v) => setVal(g.key, v)" />
        <span v-if="g.suffix" class="suffix">{{ g.suffix }}</span>
        <span class="hint">{{ g.hint }}</span>
      </div>
    </div>
    <div class="actions">
      <el-button size="small" @click="resetAll">恢复默认</el-button>
    </div>
  </div>
</template>

<style scoped>
.pane { display: flex; flex-direction: column; gap: 10px; }
.ghost-note { font-size: 12px; color: var(--mbull-text-dim, #787b86); }
.rows { display: flex; flex-direction: column; }
.row { display: flex; align-items: center; gap: 10px; padding: 7px 4px; border-bottom: 1px solid var(--mbull-border, rgba(255, 255, 255, 0.07)); font-size: 13px; }
.label { width: 170px; }
.input { width: 110px; }
.suffix { color: var(--mbull-text-dim, #787b86); margin-left: -4px; }
.hint { color: var(--mbull-text-dim, #787b86); font-size: 12px; }
.actions { margin-top: 4px; }
</style>