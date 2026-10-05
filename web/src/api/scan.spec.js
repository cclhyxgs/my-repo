// F-501~506 扫描 API 客户端单测（mock fetch + EventSource）。
// 仅验证客户端契约封装正确（路径/方法/参数/解析），后端行为由 tests/h5_skeleton 覆盖。
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  startScan, getScanProgress, getScanResults, getSectors,
  getScanCache, cancelScan, pauseScan, resumeScan, exportScanUrl, openScanEvents,
} from './scan.js'

function mockFetch(handler) {
  const fn = vi.fn(async (url, opts) => {
    const res = await handler(url, opts || {})
    return res
  })
  global.fetch = fn
  return fn
}

function jsonRes(obj, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => obj, text: async () => JSON.stringify(obj) }
}

describe('scan.js API 客户端', () => {
  let fetchMock
  beforeEach(() => { fetchMock = mockFetch(() => jsonRes({})) })
  afterEach(() => { vi.restoreAllMocks() })

  it('startScan POST /api/scan 返回 task_id', async () => {
    fetchMock.mockImplementationOnce(async () => jsonRes({ task_id: 'S1', reused: false }))
    const r = await startScan({ market: 'stock', k_type: '日K' })
    expect(r.task_id).toBe('S1')
    expect(r.reused).toBe(false)
    const [url, opts] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/scan')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toMatchObject({ market: 'stock' })
  })

  it('startScan 分钟级 k_type 由后端 422（客户端透传错误）', async () => {
    fetchMock.mockImplementationOnce(async () => ({ ok: false, status: 422, json: async () => ({ detail: '扫描不支持 1分钟' }) }))
    await expect(startScan({ market: 'stock', k_type: '1分钟' })).rejects.toThrow('422')
  })

  it('getScanProgress GET /api/scan/{id}', async () => {
    fetchMock.mockImplementationOnce(async () => jsonRes({ status: 'running', total: 100, current: 50, pct: 50 }))
    const p = await getScanProgress('S1')
    expect(p.status).toBe('running')
    expect(p.current).toBe(50)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/scan/S1')
  })

  it('getScanResults 携带分页/筛选/排序参数', async () => {
    fetchMock.mockImplementationOnce(async () => jsonRes({ total: 10, filtered_total: 10, offset: 0, limit: 200, items: [] }))
    await getScanResults('S1', { offset: 0, limit: 200, rating: 5, sort: 'final_score', order: 'desc' })
    const url = fetchMock.mock.calls[0][0]
    expect(url).toContain('/api/scan/S1/results?')
    expect(url).toContain('offset=0')
    expect(url).toContain('limit=200')
    expect(url).toContain('rating=5')
    expect(url).toContain('sort=final_score')
    expect(url).toContain('order=desc')
  })

  it('getSectors 聚合仅含 task_id 参数', async () => {
    fetchMock.mockImplementationOnce(async () => jsonRes({ market: 'stock', sectors: [] }))
    await getSectors({ market: 'stock', taskId: 'S1' })
    expect(fetchMock.mock.calls[0][0]).toContain('/api/sectors?market=stock&task_id=S1')
  })

  it('getScanCache GET /api/scan/cache', async () => {
    fetchMock.mockImplementationOnce(async () => jsonRes({ market: 'stock', exists: true, stale: false }))
    const c = await getScanCache('stock')
    expect(c.exists).toBe(true)
    expect(c.stale).toBe(false)
  })

  it('cancelScan / pauseScan / resumeScan POST 对应端点', async () => {
    fetchMock.mockImplementationOnce(async () => jsonRes({ task_id: 'S1', status: 'cancelling' }))
    expect((await cancelScan('S1')).status).toBe('cancelling')
    expect(fetchMock.mock.calls.at(-1)[0]).toBe('/api/scan/S1/cancel')

    fetchMock.mockImplementationOnce(async () => jsonRes({ task_id: 'S1', status: 'paused' }))
    expect((await pauseScan('S1')).status).toBe('paused')
    expect(fetchMock.mock.calls.at(-1)[0]).toBe('/api/scan/S1/pause')

    fetchMock.mockImplementationOnce(async () => jsonRes({ task_id: 'S1', status: 'running' }))
    expect((await resumeScan('S1')).status).toBe('running')
    expect(fetchMock.mock.calls.at(-1)[0]).toBe('/api/scan/S1/resume')
  })

  it('exportScanUrl 返回导出端点', () => {
    expect(exportScanUrl('S1')).toBe('/api/scan/S1/export')
  })

  it('openScanEvents 监听 done 后自动关闭', () => {
    const handlers = { onProgress: vi.fn(), onDone: vi.fn() }
    const fakeES = {
      _listeners: {},
      addEventListener(type, cb) { this._listeners[type] = cb },
      close: vi.fn(),
    }
    vi.stubGlobal('EventSource', class { constructor() { return fakeES } })
    const es = openScanEvents('S1', handlers)
    expect(es).toBe(fakeES)
    fakeES._listeners.progress({ data: JSON.stringify({ current: 1 }) })
    expect(handlers.onProgress).toHaveBeenCalled()
    fakeES._listeners.done({ data: JSON.stringify({ status: 'completed' }) })
    expect(handlers.onDone).toHaveBeenCalled()
    expect(fakeES.close).toHaveBeenCalled()
  })
})
