<script setup>
// 可复用的「AI解读」按钮 + 结果展示组件。
// 用法：传入 fetchFn（异步，返回 {text}）+ scene（仓内注释用）；点按钮触发解读并弹窗展示。
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { aiAnalyze, aiDiagnosis, aiScan, aiLedger } from '../api/ai'

const props = defineProps({
  // 必传：执行解读的异步函数，返回 Promise<{text}>。或直接给场景字符串走内置映射。
  scene: { type: String, default: '' }, // analyze | diagnosis | scan | ledger
  // 调用解读需要的参数（按场景而异）：
  payload: { type: [Object, Array, null], default: null },
  // 覆盖标签（默认取场景中文名）
  label: { type: String, default: '' },
  // 禁用（如扫描未完成）
  disabled: { type: Boolean, default: false },
})

const loading = ref(false)
const dialogVisible = ref(false)
const output = ref('')

const SCENE_LABELS = {
  analyze: 'AI解读',
  diagnosis: 'AI解读',
  scan: 'AI概览',
  ledger: 'AI复盘',
}

const btnLabel = computed(() => props.label || SCENE_LABELS[props.scene] || 'AI解读')

// 场景 → 后端调用器
function caller(scene, payload) {
  if (scene === 'analyze') return aiAnalyze(payload)
  if (scene === 'diagnosis') return aiDiagnosis(payload)
  if (scene === 'scan') return aiScan(payload ?? [])
  if (scene === 'ledger') return aiLedger(payload?.market ?? 'all')
  return Promise.reject(new Error('未知 AI 场景：' + scene))
}

async function run() {
  if (loading.value) return
  loading.value = true
  output.value = ''
  dialogVisible.value = true
  try {
    const res = await caller(props.scene, props.payload)
    output.value = res?.text || '（AI 未返回内容）'
  } catch (e) {
    dialogVisible.value = false
    ElMessage.error(e.message || 'AI 解读失败')
  } finally {
    loading.value = false
  }
}

function formatText(text) {
  // 简单处理：把 **加粗** 转成 <b>；换行保留，其余渲染为纯文本（防 XSS）。
  const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  return esc(text)
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/\n/g, '<br/>')
}
</script>

<template>
  <span class="ai-interpret">
    <el-button
      type="primary"
      plain
      size="small"
      :loading="loading"
      :disabled="disabled"
      @click="run"
    >{{ btnLabel }}</el-button>

    <el-dialog
      v-model="dialogVisible"
      :title="btnLabel"
      width="560px"
      append-to-body
      class="ai-dialog"
    >
      <div v-loading="loading" class="ai-body">
        <!-- eslint-disable-next-line vue/no-v-html -->
        <div v-if="!loading && output" class="ai-text" v-html="formatText(output)" />
        <el-empty v-else-if="!loading && !output" description="无内容" :image-size="56" />
      </div>
    </el-dialog>
  </span>
</template>

<style scoped>
.ai-body {
  min-height: 80px;
}
.ai-text {
  font-size: 14px;
  line-height: 1.8;
  color: var(--mbull-text, #d1d4dc);
  white-space: normal;
  word-break: break-word;
}
.ai-text :deep(b) {
  color: var(--mbull-accent, #e0522c);
}
</style>