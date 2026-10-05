<script setup>
// q-stats · z-score 标准化 + 评分缩放（镜像 index.html fillScaleList L3331-3341 + fillStatsList L3343-3351）。
import { computed } from 'vue'
const props = defineProps({
  model: { type: Object, required: true },
})
const emit = defineEmits(['dirty'])

const { factors, scale } = props.model
const active = computed(() => factors.filter((f) => f.enabled))

function setScale(s, v) {
  s.val = Number(v) || 0
  emit('dirty')
}
function setStat(f, key, v) {
  f[key] = Number(v) || 0
  emit('dirty')
}
</script>

<template>
  <div class="pane">
    <el-alert type="warning" :closable="false" class="tip" title="修改会直接改变因子预期分布。建议先跑「回测验证 → 因子IC回测」，再点「应用因子IC回测结果」刷新均值/标准差/IC。" />

    <div class="card">
      <div class="qh">评分缩放</div>
      <div class="qb">
        <div v-for="s in scale" :key="s.key" class="row">
          <span class="label">{{ s.label }}</span>
          <el-input v-model="s.val" size="small" class="input" @change="setScale(s, s.val)" />
          <span class="desc">{{ s.desc }}</span>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="qh">已启用因子统计</div>
      <div class="qb">
        <el-table :data="active" size="small" border>
          <el-table-column label="因子" prop="label" min-width="140" />
          <el-table-column label="均值（历史平均水平）" width="160">
            <template #default="{ row }">
              <el-input v-model="row.mean" size="small" class="input" @change="setStat(row, 'mean', row.mean)" />
            </template>
          </el-table-column>
          <el-table-column label="标准差（波动范围）" width="160">
            <template #default="{ row }">
              <el-input v-model="row.std" size="small" class="input" @change="setStat(row, 'std', row.std)" />
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-if="!active.length" description="没有启用任何因子" :image-size="56" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.pane { display: flex; flex-direction: column; gap: 10px; }
.tip { margin-bottom: 4px; }
.card { border: 1px solid var(--mbull-border, rgba(255, 255, 255, 0.08)); border-radius: 6px; overflow: hidden; }
.qh { padding: 8px 12px; font-weight: 600; font-size: 13px; background: rgba(255, 255, 255, 0.04); }
.qb { padding: 10px 12px; }
.row { display: grid; grid-template-columns: 150px 90px 1fr; gap: 10px; align-items: center; margin-bottom: 8px; font-size: 13px; }
.label { font-size: 12.5px; }
.input { width: 90px; }
.desc { color: var(--mbull-text-dim, #787b86); font-size: 12px; }
</style>