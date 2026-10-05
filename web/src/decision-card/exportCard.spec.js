import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  buildCardFilename,
  canvasToBlob,
  dataUrlToBlob,
  downloadBlob,
  exportDecisionCard,
} from './exportCard'

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

describe('F-304 决策卡导出', () => {
  let clickSpy

  beforeEach(() => {
    clickSpy = vi.fn()
    // jsdom 不支持 <a> 真实导航，统一 mock 掉 click（记录调用，避免 navigation 警告）。
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(clickSpy)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  describe('buildCardFilename', () => {
    it('文件名 = M-Bull_{code}_{YYYYMMDD}.png（验收逐字）', () => {
      expect(buildCardFilename('sh600519', new Date(2026, 8, 12))).toBe(
        'M-Bull_sh600519_20260912.png',
      )
    })

    it('月份/日期补零', () => {
      expect(buildCardFilename('rb0', new Date(2026, 0, 5))).toBe('M-Bull_rb0_20260105.png')
    })

    it('缺省 date 用当前时间（仅断言格式，不与具体日期耦合）', () => {
      expect(buildCardFilename('sh600519')).toMatch(/^M-Bull_sh600519_\d{8}\.png$/)
    })
  })

  describe('canvasToBlob', () => {
    it('优先走 toBlob，且 MIME = image/png', async () => {
      const blob = { type: 'image/png' }
      const canvas = {
        toBlob: vi.fn((cb, type) => cb(blob, type)),
      }
      const out = await canvasToBlob(canvas, 'image/png')
      expect(out).toBe(blob)
      expect(canvas.toBlob).toHaveBeenCalledWith(expect.any(Function), 'image/png', undefined)
    })

    it('iOS 老版本无 toBlob → toDataURL 降级', async () => {
      const canvas = {
        toDataURL: vi.fn(() => 'data:image/png;base64,QUJD'), // "ABC" base64
      }
      const blob = await canvasToBlob(canvas, 'image/png')
      expect(canvas.toDataURL).toHaveBeenCalledWith('image/png', undefined)
      expect(blob).toBeInstanceOf(Blob)
      expect(blob.type).toBe('image/png')
    })
  })

  describe('dataUrlToBlob', () => {
    it('MIME 从 dataURL 头解析，内容正确', async () => {
      const blob = dataUrlToBlob('data:image/png;base64,QUJD')
      expect(blob.type).toBe('image/png')
      const text = await new Promise((resolve) => {
        const fr = new FileReader()
        fr.onload = () => resolve(fr.result)
        fr.readAsText(blob)
      })
      expect(text).toBe('ABC')
    })
  })

  describe('downloadBlob', () => {
    it('触发 <a download> + click，且 revoke URL', () => {
      mockObjectURL()

      const blob = new Blob(['x'], { type: 'image/png' })
      downloadBlob(blob, 'M-Bull_sh600519_20260912.png')

      expect(URL.createObjectURL).toHaveBeenCalledWith(blob)
      expect(clickSpy).toHaveBeenCalledTimes(1)
      const anchor = clickSpy.mock.instances[0]
      expect(anchor.download).toBe('M-Bull_sh600519_20260912.png')
      expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:mock-0')
    })
  })

  describe('exportDecisionCard（编排层，验收核心）', () => {
    it('文件名 + MIME + Blob 下载三要素齐全', async () => {
      mockObjectURL()

      // mock html2canvas 渲染器 → 返回带 toBlob 的假 canvas
      const renderer = vi.fn(async () => ({
        toBlob: (cb) => cb(new Blob(['png'], { type: 'image/png' })),
      }))

      const res = await exportDecisionCard({ id: 'card' }, {
        code: 'sh600519',
        date: new Date(2026, 8, 12),
        renderer,
      })

      expect(renderer).toHaveBeenCalledWith(
        { id: 'card' },
        expect.objectContaining({ scale: 2, backgroundColor: '#fff' }),
      )
      expect(res.filename).toBe('M-Bull_sh600519_20260912.png')
      expect(res.mime).toBe('image/png')
      expect(res.blob).toBeInstanceOf(Blob)
      expect(clickSpy).toHaveBeenCalledTimes(1)
      expect(clickSpy.mock.instances[0].download).toBe('M-Bull_sh600519_20260912.png')
      expect(URL.createObjectURL).toHaveBeenCalled()
    })

    it('toBlob 不可用时经降级链仍产出 image/png', async () => {
      mockObjectURL()
      const renderer = vi.fn(async () => ({
        toDataURL: () => 'data:image/png;base64,QUJD',
      }))
      const res = await exportDecisionCard({}, { code: 'rb0', renderer })
      expect(res.mime).toBe('image/png')
      expect(res.blob.type).toBe('image/png')
    })
  })
})
