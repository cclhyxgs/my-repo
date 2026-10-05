<script setup>
// q-manage · 因子管理（镜像 index.html fillFactorList L3220-3261）。
// 分类列出候选因子：启用勾选 + 名称 + IC/IC_IR + 参数输入 + 描述。
import { PARAM_LABELS } from '../quantModel'

const props = defineProps({
  model: { type: Object, required: true },
})
const emit = defineEmits(['dirty'])

const { factors, categories } = props.model

function toggle(f) {
  f.enabled = !f.enabled
  emit('dirty')
}
function setParam(f, key, val) {
  const n = Number(val)
  f.params[key] = Number.isNaN(n) ? val : n
  emit('dirty')
}
function fmtIc(v) {
  if (v === null || v === undefined) return null
  return { text: `${v > 0 ? '+' : ''}${v.toFixed(3)}`, up: v > 0 }
}
</script>

<template>
  <div class="pane">
    <div class="pane-head">
      <span class="title">因子勾选 · {{ factors.length }} 候选</span>
      <span class="meta">已启用 {{ factors.filter((f) => f.enabled).length }} / {{ factors.length }}（{{ categories.length }} 类）</span>
    </div>
    <el-alert type="warning" :closable="false" class="tip" title="提示：IC / IC_IR 由「回测验证 → 因子IC回测」写入，未运行回测时显示为空白。" />

    <div v-for="cat in categories" :key="cat.key" class="factor-cat">
      <div class="cat-title">{{ cat.label }}类</div>
      <div v-for="f in factors.filter((x) => x.cat === cat.key)" :key="f.name" class="factor-row">
        <span class="check" :class="{ on: f.enabled }" @click="toggle(f)">✓</span>
        <span class="name">{{ f.label }}</span>
        <span v-if="fmtIc(f.ic)" class="ic" :class="fmtIc(f.ic).up ? 'up' : 'down'">{{ fmtIc(f.ic).text }}</span>
        <span v-else class="ic">—</span>
        <span v-if="fmtIc(f.ic_ir)" class="ic" :class="fmtIc(f.ic_ir).up ? 'up' : 'down'">{{ fmtIc(f.ic_ir).text }}</span>
        <span v-else class="ic">—</span>
        <span class="params">
          <span v-for="(pv, pk) in f.params" :key="pk" class="param">
            <span class="plabel">{{ PARAM_LABELS[pk] || pk }}</span>
            <el-input v-model="f.params[pk]" size="small" class="param-input" @change="setParam(f, pk, f.params[pk])" />
          </span>
          <span v-if="!Object.keys(f.params).length" class="noparam">（无参数）</span>
        </span>
        <span class="desc">{{ f.desc }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pane {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.pane-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.title { font-weight: 600; }
.meta { font-size: 12px; color: var(--mbull-text-dim, #787b86); }
.tip { margin-bottom: 6px; }
.cat-title {
  margin: 10px 0 4px;
  font-size: 13px;
  font-weight: 600;
  color: var(--mbull-accent, #e0522c);
}
.factor-row {
  display: grid;
  grid-template-columns: 26px 130px 64px 56px 1fr 200px;
  gap: 8px;
  align-items: center;
  padding: 5px 4px;
  border-bottom: 1px solid var(--mbull-border, rgba(255, 255, 255, 0.07));
  font-size: 13px;
}
.check {
  width: 20px;
  height: 20px;
  border: 1px solid rgba(255, 255, 255, 0.25);
  border-radius: 4px;
  text-align: center;
  line-height: 18px;
  cursor: pointer;
  color: transparent;
  font-size: 13px;
  user-select: none;
}
.check.on { background: var(--mbull-accent, #e0522c); border-color: var(--mbull-accent, #e0522c); color: #fff; }
.name { font-weight: 600; }
.ic { font-family: var(--mbull-mono, 'SFMono-Regular', Consolas, monospace); }
.ic.up { color: #e0522c; }
.ic.down { color: #26c6da; }
.params { display: flex; gap: 6px; flex-wrap: wrap; }
.param { display: inline-flex; align-items: center; gap: 3px; font-size: 11px; }
.plabel { color: var(--mbull-text-dim, #787b86); }
.param-input { width: 60px; }
.noparam { font-size: 11px; color: var(--mbull-text-dim, #787b86); }
.desc { color: var(--mbull-text-dim, #787b86); font-size: 12px; }
</style>