// F-401~404 诊断 API 客户端单测：mock fetch + EventSource，校验契约。
import { describe, it, expect, beforeEach, vi } from 'vitest'
import {
  startDiagnosis,
  getDiagnosis,
  cancelDiagnosis,
  openDiagnosisEvents,
} from './diagnosis'

// ---- Mock EventSource ----
class MockEventSource {
  constructor(url) {
    this.url = url
    this.listeners = {}
    this.closed = false
  }
  addEventListener(type, cb) {
    ;(this.listeners[type] ||= []).push(cb)
  }
  close() {
    this.closed = true
  }
  // 测试辅助：触发事件
  _emit(type, data) {
    ;(this.listeners[type] || []).forEach((cb) => cb({ data: JSON.stringify(data) }))
  }
}

let fetchCalls = []
global.EventSource = MockEventSource
global.fetch = vi.fn(async (url, opts) => {
  fetchCalls.push({ url, opts })
  if (url === '/api/diagnosis' && opts?.method === 'POST') {
    return { ok: true, json: async () => ({ task_id: 'T1' }) }
  }
  if (url === '/api/diagnosis/T1/cancel' && opts?.method === 'POST') {
    return { ok: true, json: async () => ({ task_id: 'T1', status: 'cancelling' }) }
  }
  if (url === '/api/diagnosis/T1') {
    return {
      ok: true,
      json: async () => ({
        status: 'completed',
        total: 2,
        current: 2,
        summary: { buy: 1, watch: 1, avoid: 0, position: 0, error: 0 },
        results: [
          { code: 'sh600000', name: '测试A', category: 'buy' },
          { code: 'sh600001', name: '测试B', category: 'watch' },
        ],
      }),
    }
  }
  return { ok: false, status: 500, json: async () => ({}) }
})

beforeEach(() => {
  fetchCalls = []
})

describe('startDiagnosis', () => {
  it('POST /api/diagnosis 且回传 task_id', async () => {
    const id = await startDiagnosis({ text: 'sh600000', market: 'stock', k_type: '日K' })
    expect(id).toBe('T1')
    const call = fetchCalls.find((c) => c.url === '/api/diagnosis')
    expect(call.opts.method).toBe('POST')
    const body = JSON.parse(call.opts.body)
    expect(body.text).toBe('sh600000')
    expect(body.market).toBe('stock')
    expect(body.k_type).toBe('日K')
  })

  it('非 2xx 抛出错误', async () => {
    global.fetch.mockImplementationOnce(async () => ({ ok: false, status: 422, json: async () => ({ detail: '空文本' }) }))
    await expect(startDiagnosis({ text: '', market: 'stock' })).rejects.toThrow('空文本')
  })
})

describe('getDiagnosis', () => {
  it('GET 返回 summary 与 results', async () => {
    const data = await getDiagnosis('T1')
    expect(data.status).toBe('completed')
    expect(data.results).toHaveLength(2)
    expect(data.results[0]).toMatchObject({ code: 'sh600000', category: 'buy' })
  })
})

describe('cancelDiagnosis', () => {
  it('POST /api/diagnosis/{id}/cancel', async () => {
    const res = await cancelDiagnosis('T1')
    expect(res.status).toBe('cancelling')
    const call = fetchCalls.find((c) => c.url === '/api/diagnosis/T1/cancel')
    expect(call.opts.method).toBe('POST')
  })
})

describe('openDiagnosisEvents', () => {
  it('progress→done 链路：回调触发且 done 后关闭', () => {
    const onProgress = vi.fn()
    const onDone = vi.fn()
    const es = openDiagnosisEvents('T1', { onProgress, onDone })
    es._emit('progress', { current: 1, total: 3, pct: 33, message: '第1只' })
    es._emit('done', { status: 'completed', summary: {} })
    expect(onProgress).toHaveBeenCalledWith(
      expect.objectContaining({ current: 1, total: 3, pct: 33 }),
    )
    expect(onDone).toHaveBeenCalled()
    expect(es.closed).toBe(true)
  })

  it('error 事件关闭连接', () => {
    const onError = vi.fn()
    const es = openDiagnosisEvents('T1', { onError })
    es._emit('error', {})
    expect(onError).toHaveBeenCalled()
    expect(es.closed).toBe(true)
  })
})
