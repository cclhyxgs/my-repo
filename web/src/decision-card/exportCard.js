// F-304 决策卡导出链路（纯函数 + 编排层，便于单元测试）
//
// 验收（feature-matrix.md:25 F-304）：
//   点击导出触发 Blob 下载，文件名 `M-Bull_{code}_{YYYYMMDD}.png`，MIME `image/png`。
// 方案（app-architecture §2 数据迁移表，判 A 纯前端）：
//   html2canvas 本地依赖（去 CDN）→ canvas.toBlob() → <a download>；
//   iOS Safari 老版本不支持 toBlob（R-04）→ toDataURL 另存降级。
import html2canvas from 'html2canvas'

const CARD_MIME = 'image/png'

/** 生成决策卡文件名：`M-Bull_{code}_{YYYYMMDD}.png`（验收逐字）。 */
export function buildCardFilename(code, date = new Date()) {
  const d = date instanceof Date ? date : new Date(date)
  const ymd =
    String(d.getFullYear()) +
    String(d.getMonth() + 1).padStart(2, '0') +
    String(d.getDate()).padStart(2, '0')
  return `M-Bull_${code}_${ymd}.png`
}

/** canvas → Blob。优先 toBlob；无 toBlob（iOS 老版本）降级 toDataURL。 */
export function canvasToBlob(canvas, type = CARD_MIME, quality) {
  return new Promise((resolve) => {
    if (typeof canvas.toBlob === 'function') {
      canvas.toBlob((blob) => resolve(blob), type, quality)
      return
    }
    // iOS Safari 降级（R-04）
    const dataUrl = canvas.toDataURL(type, quality)
    resolve(dataUrlToBlob(dataUrl))
  })
}

/** dataURL → Blob（MIME 从 dataURL 头解析）。 */
export function dataUrlToBlob(dataUrl) {
  const comma = dataUrl.indexOf(',')
  const meta = dataUrl.slice(0, comma)
  const mime = (meta.match(/data:(.*?)(;|$)/) || [])[1] || CARD_MIME
  const bin = atob(dataUrl.slice(comma + 1))
  const arr = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i += 1) arr[i] = bin.charCodeAt(i)
  return new Blob([arr], { type: mime })
}

/** 触发浏览器下载（Blob → <a download> → click）。 */
export function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

/**
 * 导出决策卡为 PNG 并触发下载。
 * @param {HTMLElement} cardEl 决策卡 DOM 元素（html2canvas 截图目标）
 * @param {object} opts { code, date, scale, renderer }
 *   renderer 可注入（默认本地 html2canvas），测试传 mock。
 * @returns {Promise<{filename:string, mime:string, blob:Blob}>}
 */
export async function exportDecisionCard(cardEl, opts = {}) {
  const {
    code = 'decision',
    date = new Date(),
    scale = 2,
    renderer = html2canvas,
  } = opts

  const canvas = await renderer(cardEl, {
    scale,
    useCORS: true,
    backgroundColor: '#fff',
    logging: false,
  })
  const blob = await canvasToBlob(canvas, CARD_MIME, 1.0)
  const filename = buildCardFilename(code, date)
  downloadBlob(blob, filename)
  return { filename, mime: CARD_MIME, blob }
}
