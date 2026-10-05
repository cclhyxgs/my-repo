import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  GROUP_KEYS,
  GROUP_LABELS,
  buildReportFilename,
  buildReportContent,
  copyToClipboard,
  exportDiagnosisReport,
} from './exportReport'

// jsdom 未实现 URL.createObjectURL / revokeObjectURL，测试里 mock。
function mockObjectURL() {
  const created = []
  vi.stubGlobal('URL', {
    ...URL,
    createObjectURL: vi.fn((blob) => {
      const url = `blob:mock-${created.length}`
      created.push({ blob, url })
      return url
    }),
    revokeObjectURL: vi.fn(),
  })
  return created
}

// jsdom 的 Blob 无 .text()，用 FileReader 读内容。
function blobText(blob) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader()
    fr.onload = () => resolve(fr.result)
    fr.onerror = reject
    fr.readAsText(blob)
  })
}

describe('F-404 诊断报告导出', () => {
  let clickSpy

  beforeEach(() => {
    clickSpy = vi.fn()
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(clickSpy)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  describe('buildReportFilename（验收：自选股诊断报告_{ts}.txt）', () => {
    it('格式逐字：自选股诊断报告_YYYYMMDD_HHMMSS.txt', () => {
      expect(buildReportFilename(new Date(2026, 8, 13, 18, 35, 52))).toBe(
        '自选股诊断报告_20260913_183552.txt',
      )
    })

    it('月份/日期/时分秒补零', () => {
      expect(buildReportFilename(new Date(2026, 0, 5, 3, 4, 7))).toBe(
        '自选股诊断报告_20260105_030407.txt',
      )
    })

    it('缺省 date 用当前时间（仅断言格式，不与具体时间耦合）', () => {
      expect(buildReportFilename()).toMatch(/^自选股诊断报告_\d{8}_\d{6}\.txt$/)
    })
  })

  describe('buildReportContent（验收：UTF-8，含 5 组表头）', () => {
    it('含全部 5 组表头且计数正确', () => {
      const text = buildReportContent({
        summary: { buy: 1, watch: 2, avoid: 0, position: 0, error: 1 },
      })
      for (const key of GROUP_KEYS) {
        expect(text).toContain(GROUP_LABELS[key])
      }
      expect(text).toContain('买入：1')
      expect(text).toContain('关注：2')
      expect(text).toContain('错误/异常：1')
      expect(text).toContain('合计：4')
    })

    it('summary 缺字段按 0 计，不抛错', () => {
      const text = buildReportContent({ summary: {} })
      expect(text).toContain('买入：0')
      expect(text).toContain('合计：0')
    })

    it('含 results 时输出个股明细（category → 中文标签）', () => {
      const text = buildReportContent({
        summary: { buy: 1, watch: 1, avoid: 0, position: 0, error: 0 },
        results: [
          { code: 'sh600519', name: '贵州茅台', category: 'buy' },
          { code: 'sz000001', name: '平安银行', category: 'watch' },
        ],
      })
      expect(text).toContain('=== 个股明细 ===')
      expect(text).toContain('sh600519')
      expect(text).toContain('贵州茅台')
      expect(text).toContain('买入')
      expect(text).toContain('sz000001')
    })

    it('results 缺省不输出明细区块', () => {
      const text = buildReportContent({ summary: { buy: 1, watch: 0, avoid: 0, position: 0, error: 0 } })
      expect(text).not.toContain('=== 个股明细 ===')
    })

    it('结语含合规免责文案', () => {
      expect(buildReportContent({ summary: {} })).toContain('不构成投资建议')
    })
  })

  describe('copyToClipboard（iOS 复制兜底）', () => {
    // jsdom 无 navigator.clipboard / document.execCommand，用 defineProperty 注入模拟降级路径。
    function disableClipboard() {
      try {
        Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
      } catch {
        /* jsdom 本来就没有 clipboard，直接降级 */
      }
    }

    it('navigator.clipboard 不可用时降级 textarea+execCommand 并返回 true', () => {
      Object.defineProperty(document, 'execCommand', { value: vi.fn(() => true), configurable: true })
      disableClipboard()
      const ok = copyToClipboard('hello 报告')
      expect(document.execCommand).toHaveBeenCalledWith('copy')
      expect(ok).toBe(true)
    })

    it('execCommand 失败时返回 false（不抛）', () => {
      Object.defineProperty(document, 'execCommand', {
        value: () => {
          throw new Error('blocked')
        },
        configurable: true,
      })
      disableClipboard()
      expect(copyToClipboard('x')).toBe(false)
    })
  })

  describe('exportDiagnosisReport（编排层，验收核心）', () => {
    it('文件名 + MIME(UTF-8) + Blob 下载三要素齐全', async () => {
      mockObjectURL()
      const summary = { buy: 1, watch: 2, avoid: 0, position: 0, error: 0 }
      const res = exportDiagnosisReport({ summary }, { date: new Date(2026, 8, 13, 18, 35, 52) })

      expect(res.filename).toBe('自选股诊断报告_20260913_183552.txt')
      expect(res.mime).toBe('text/plain;charset=utf-8')
      expect(res.blob).toBeInstanceOf(Blob)
      expect(URL.createObjectURL).toHaveBeenCalledWith(res.blob)
      expect(clickSpy).toHaveBeenCalledTimes(1)
      expect(clickSpy.mock.instances[0].download).toBe(res.filename)

      // 字节级验证 UTF-8 BOM（EF BB BF）：readAsText 解码会剥离 BOM，故用 ArrayBuffer 校验
      const buf = await new Promise((resolve, reject) => {
        const fr = new FileReader()
        fr.onload = () => resolve(fr.result)
        fr.onerror = reject
        fr.readAsArrayBuffer(res.blob)
      })
      const head = Array.from(new Uint8Array(buf).slice(0, 3))
      expect(head).toEqual([0xef, 0xbb, 0xbf])

      // 解码后正文含 5 组表头
      const text = await blobText(res.blob)
      expect(text).toContain('买入：1')
      expect(text).toContain('关注：2')
    })

    it('未传 date 时文件名带当前时间戳格式', () => {
      mockObjectURL()
      const res = exportDiagnosisReport({ summary: { buy: 0, watch: 0, avoid: 0, position: 0, error: 0 } })
      expect(res.filename).toMatch(/^自选股诊断报告_\d{8}_\d{6}\.txt$/)
    })
  })
})
