<script setup>
// q-penalty · 冲突惩罚（镜像 index.html fillPenaltyList L3353-3363）。全部留空 = 关闭。
const props = defineProps({
  model: { type: Object, required: true },
})
const emit = defineEmits(['dirty'])

const { penalty } = props.model

function setVal(p) {
  p.val = p.val
  emit('dirty')
}
</script>

<template>
  <div class="pane">
    <el-alert :closable="false" class="tip" title="冲突惩罚为可选项，不配置即不施加。全部留空表示关闭。" />
    <div class="list">
      <div v-for="p in penalty" :key="p.key" class="row">
        <span class="label">{{ p.label }}</span>
        <el-input v-model="p.val" size="small" class="input" placeholder="留空" @change="setVal(p)" />
        <span class="desc">{{ p.desc }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pane { display: flex; flex-direction: column; gap: 10px; }
.tip { margin-bottom: 4px; }
.list { display: flex; flex-direction: column; }
.row { display: grid; grid-template-columns: 200px 110px 1fr; gap: 12px; align-items: center; padding: 7px 4px; border-bottom: 1px solid var(--mbull-border, rgba(255, 255, 255, 0.07)); font-size: 13px; }
.label { font-size: 12.5px; }
.input { width: 110px; }
.desc { color: var(--mbull-text-dim, #787b86); font-size: 12px; }
</style>