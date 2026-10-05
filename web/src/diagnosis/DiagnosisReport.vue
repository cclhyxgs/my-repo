<script setup>
// F-404 诊断报告导出组件（批量诊断页「报告导出」子项）。
// 数据由诊断页传入：summary（五组计数）+ 可选 results（个股明细）。
// 主路径：Blob 下载 txt；iOS Safari 对 a[download] 支持不完整 → 备「复制剪贴板」兜底。
import { ElMessage } from 'element-plus'
import {
  exportDiagnosisReport,
  copyToClipboard,
  buildReportContent,
} from './exportReport'

const props = defineProps({
  summary: { type: Object, default: () => ({}) },
  results: { type: Array, default: () => [] },
  generatedAt: { type: [Date, String], default: () => new Date() },
  disabled: { type: Boolean, default: false },
})

function onDownload() {
  const res = exportDiagnosisReport({
    summary: props.summary,
    results: props.results,
    generatedAt: props.generatedAt,
  })
  ElMessage.success(`已导出 ${res.filename}`)
}

function onCopy() {
  const text = buildReportContent({
    summary: props.summary,
    results: props.results,
    generatedAt: props.generatedAt,
  })
  const ok = copyToClipboard(text)
  if (ok) ElMessage.success('报告已复制到剪贴板')
  else ElMessage.warning('复制失败，请改用下载')
}
</script>

<template>
  <div class="diag-export">
    <el-button type="primary" :disabled="disabled" @click="onDownload">下载诊断报告</el-button>
    <el-button :disabled="disabled" @click="onCopy">复制报告</el-button>
  </div>
</template>

<style scoped>
.diag-export {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}
</style>
