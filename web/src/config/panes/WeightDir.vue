<script setup>
// q-weight · 权重 & 方向（镜像 index.html fillWeightList L3272-3308 + icWeight/normalize/equal L3309-3329）。
import { computed } from 'vue'
import { ElMessage } from 'element-plus'

const props = defineProps({
  model: { type: Object, required: true },
})
const emit = defineEmits(['dirty'])

const { factors } = props.model
const COLORS = ['#2962ff', '#ff9800', '#26c6da', '#787b86', '#8b5cf6', '#f97316', '#26a69a', '#ef5350']

const active = computed(() => factors.filter((f) => f.enabled))
const sum = computed(() => active.value.reduce((a, f) => a + f.weight, 0))

function setWeight(f, v) {
  f.weight = Number(v)
  emit('dirty')
}
function toggleDir(f) {
  f.dir = -f.dir
  emit('dirty')
}
function icWeight() {
  const has = active.value.filter((f) => f.ic != null && f.ic !== 0)
  if (!has.length) {
    ElMessage.warning('当前无 IC 数据，请先在「回测验证」运行因子IC回测并应用结果。')
    return
  }
  has.forEach((f) => (f.weight = Math.abs(f.ic)))
  emit('dirty')
  ElMessage.success(`已按 IC 绝对值加权 ${has.length} 个因子`)
}
function normalizeWeight() {
  const s = sum.value
  if (s > 0) active.value.forEach((f) => (f.weight /= s))
  emit('dirty')
}
function equalWeight() {
  if (active.value.length) active.value.forEach((f) => (f.weight = 1 / active.value.length))
  emit('dirty')
}
</script>

<template>
  <div class="pane">
    <div class="bar">
      <el-button size="small" @click="icWeight">按IC加权</el-button>
      <el-button size="small" @click="normalizeWeight">归一化 (和=1.0)</el-button>
      <el-button size="small" @click="equalWeight">等权分配</el-button>
      <span class="wsum">权重和：<b class="accent">{{ sum.toFixed(3) }}</b></span>
    </div>

    <div class="dist">
      <i
        v-for="(f, i) in active"
        :key="f.name"
        :style="{ width: `${(sum ? Math.abs(f.weight) / active.reduce((a, x) => a + Math.abs(x.weight), 0) : 0) * 100}%`, background: COLORS[i % COLORS.length] }"
      />
    </div>

    <el-empty v-if="!active.length" description="没有启用任何因子，请先在「因子管理」中勾选。" :image-size="60" />
    <div v-else class="weight-head">
      <span>因子</span><span>IC</span><span>IC_IR</span><span>权重</span><span>滑块</span><span>方向</span>
    </div>
    <el-table v-if="active.length" :data="active" size="small" border class="wtable">
      <el-table-column label="因子" prop="label" min-width="130" />
      <el-table-column label="IC" width="80">
        <template #default="{ row }">{{ row.ic != null ? row.ic.toFixed(4) : '—' }}</template>
      </el-table-column>
      <el-table-column label="IC_IR" width="84">
        <template #default="{ row }">{{ row.ic_ir != null ? row.ic_ir.toFixed(3) : '—' }}</template>
      </el-table-column>
      <el-table-column label="权重" width="84">
        <template #default="{ row }"><span class="accent mono">{{ row.weight.toFixed(3) }}</span></template>
      </el-table-column>
      <el-table-column label="滑块" min-width="160">
        <template #default="{ row }">
          <el-slider :min="0" :max="0.5" :step="0.001" :model-value="row.weight" style="margin:0 8px"
            @input="setWeight(row, $event)" />
        </template>
      </el-table-column>
      <el-table-column label="方向" width="90">
        <template #default="{ row }">
          <el-button size="small" :type="row.dir > 0 ? 'danger' : 'info'" plain @click="toggleDir(row)">
            {{ row.dir > 0 ? '看涨 +1' : '看跌 -1' }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.pane { display: flex; flex-direction: column; gap: 10px; }
.bar { display: flex; align-items: center; gap: 8px; }
.wsum { margin-left: auto; font-size: 12px; }
.accent { color: var(--mbull-accent, #e0522c); }
.mono { font-family: var(--mbull-mono, 'SFMono-Regular', Consolas, monospace); }
.dist { display: flex; height: 8px; border-radius: 4px; overflow: hidden; background: rgba(255, 255, 255, 0.06); }
.dist i { display: block; height: 100%; opacity: 0.85; }
.weight-head { display: none; }
</style>