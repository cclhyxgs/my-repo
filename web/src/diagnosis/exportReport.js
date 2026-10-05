// F-404 诊断报告导出（纯函数 + 编排层，便于单元测试）
//
// 验收（feature-matrix.md:30 F-404）：
//   导出触发 Blob 下载 自选股诊断报告_{ts}.txt（UTF-8，含 5 组表头）
// 方案（app-architecture 判 A 纯前端）：
//   纯字符串拼接（UTF-8）→ Blob + <a download>；
//   iOS Safari 对 a[download] 支持不完整 → 备「复制剪贴板」兜底。
// 数据合同：summary 五组计数 {buy,watch,avoid,position,error}（与 F-402 worker 一致）。
import { downloadBlob } from '../decision-card/exportCard'

// 五组分类（键名与 F-402 worker 的 summary 一致）
export const GROUP_KEYS = ['buy', 'watch', 'avoid', 'position', 'error']
export const GROUP_LABELS = {
  buy: '买入',
  watch: '关注',
  avoid: '回避',
  position: '持仓',
  error: '错误/异常',
}

function pad2(n) {
  return String(n).padStart(2, '0')
}

/** 文件名：`自选股诊断报告_{ts}.txt`（验收逐字：自选股诊断报告_{ts}.txt）。 */
export function buildReportFilename(date = new Date()) {
  const d = date instanceof Date ? date : new Date(date)
  const ts =
    `${d.getFullYear()}${pad2(d.getMonth() + 1)}${pad2(d.getDate())}_` +
    `${pad2(d.getHours())}${pad2(d.getMinutes())}${pad2(d.getSeconds())}`
  return `自选股诊断报告_${ts}.txt`
}

function formatTs(d) {
  const dt = d instanceof Date ? d : new Date(d)
  return (
    `${dt.getFullYear()}-${pad2(dt.getMonth() + 1)}-${pad2(dt.getDate())} ` +
    `${pad2(dt.getHours())}:${pad2(dt.getMinutes())}:${pad2(dt.getSeconds())}`
  )
}

/**
 * 生成报告文本（UTF-8 明文，不含 BOM；BOM 仅加在 Blob 层）。
 * @param {{summary?:object, results?:Array, generatedAt?:Date|string}} diagnosis
 *   summary：五组计数 {buy,watch,avoid,position,error}（缺省按 0）
 *   results：可选个股明细 [{code, name, category}]
 */
export function buildReportContent(diagnosis = {}) {
  const { summary = {}, results = [], generatedAt = new Date() } = diagnosis
  const lines = []
  lines.push('M-Bull 自选股诊断报告')
  lines.push(`生成时间：${formatTs(generatedAt)}`)
  lines.push('')

  // 验收核心：含 5 组表头
  lines.push('=== 五组分类汇总 ===')
  let total = 0
  for (const key of GROUP_KEYS) {
    const count = Number(summary[key] || 0)
    total += count
    lines.push(`${GROUP_LABELS[key]}：${count}`)
  }
  lines.push(`合计：${total}`)
  lines.push('')

  const safeResults = Array.isArray(results) ? results : []
  if (safeResults.length) {
    lines.push('=== 个股明细 ===')
    lines.push('代码\t名称\t分类')
    for (const r of safeResults) {
      const label = GROUP_LABELS[r.category] || r.category || '-'
      lines.push(`${r.code || ''}\t${r.name || ''}\t${label}`)
    }
    lines.push('')
  }

  lines.push('（本报告由 M-Bull 技术分析工具生成，仅供参考，不构成投资建议。）')
  return lines.join('\n')
}

/**
 * 复制到剪贴板（iOS 兜底：navigator.clipboard 不可用时降级 textarea+execCommand）。
 * @returns {boolean} 是否成功
 */
export function copyToClipboard(text) {
  try {
    if (
      typeof navigator !== 'undefined' &&
      navigator.clipboard &&
      navigator.clipboard.writeText
    ) {
      navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // 降级到 execCommand
  }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch {
    return false
  }
}

/**
 * 导出诊断报告为 txt 并触发浏览器下载（F-404 验收核心）。
 * @param {object} diagnosis {summary, results, generatedAt}
 * @param {object} opts {filename, date}
 * @returns {{filename:string, mime:string, blob:Blob}}
 */
export function exportDiagnosisReport(diagnosis = {}, opts = {}) {
  const content = buildReportContent(diagnosis)
  // 加 BOM 保证 Windows 记事本中文不乱码（仍为 UTF-8 编码）
  const blob = new Blob(['\uFEFF' + content], { type: 'text/plain;charset=utf-8' })
  const filename = opts.filename || buildReportFilename(opts.date)
  downloadBlob(blob, filename)
  return { filename, mime: 'text/plain;charset=utf-8', blob }
}
