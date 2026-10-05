// F-701~705 回测 API 客户端单测（mock fetch + EventSource）。
// 仅验证客户端契约封装正确（路径/方法/参数/解析），后端行为由 tests/h5_skeleton 覆盖。
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  submitBacktest, getBacktestModes, cancelBacktest, getBacktestArtifacts,
  artifactUrl, downloadArtifact, applyFactorIC, openBacktestEvents,
} from './backtest.js'

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

describe('backtest.js API 客户端', () => {
  let fetchMock
  beforeEach(() => { fetchMock = mockFetch(() => jsonRes({})) })
  afterEach(() => { vi.restoreAllMocks() })

  it('submitBacktest POST /api/backtest，run_params 透传', async () => {
    fetchMock.mockImplementationOnce(async () => jsonRes({ task_id: 'B1' }))
    const r = await submitBacktest('factoric', { pool: 'full', hold: 30, scan: 5 })
    expect(r.task_id).toBe('B1')
    const [url, opts] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/backtest')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({ mode: 'factoric', run_params: { pool: 'full', hold: 30, scan: 5 } })
  })

  it('submitBacktest 校验错误透传 status 与 detail（F-702 消费面）', async () => {
    fetchMock.mockImplementation(async () => ({ ok: false, status: 422, json: async () => ({ error: { message: 'hold 需在 5-60' } }) }))
    const promise = submitBacktest('factoric', { hold: 70 })
    await expect(promise).rejects.toThrow('422')
    await expect(promise).rejects.toThrow('hold 需在 5-60')
  })

  it('getBacktestModes GET /api/backtest/modes，返回 5 模式含约束（F-702）', async () => {
    const modes = [
      { mode: 'strategy', label: '策略回测', pool: 'full', hold: [5, 60], scan: [1, 20] },
      { mode: 'scoreic', label: '评分IC', pool: 'full', hold: [5, 60], scan: [1, 20] },
      { mode: 'factoric', label: '因子IC', pool: 'full', hold: [5, 60], scan: [1, 20] },
      { mode: 'futures', label: '期货回测', pool: 'all', hold: [5, 60], scan: [1, 20] },
      { mode: 'futuresic', label: '期货IC', pool: 'all', hold: [5, 60], scan: [1, 20] },
    ]
    fetchMock.mockImplementationOnce(async () => jsonRes({ ok: true, modes }))
    const r = await getBacktestModes()
    expect(r.modes).toHaveLength(5)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/backtest/modes')
  })

  it('cancelBacktest POST /api/backtest/{id}/cancel', async () => {
    fetchMock.mockImplementationOnce(async () => jsonRes({ task_id: 'B1', status: 'cancelling' }))
    const r = await cancelBacktest('B1')
    expect(r.status).toBe('cancelling')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/backtest/B1/cancel')
    expect(fetchMock.mock.calls[0][1].method).toBe('POST')
  })

  it('getBacktestArtifacts GET /api/backtest/{id}/artifacts', async () => {
    fetchMock.mockImplementationOnce(async () => jsonRes({ task_id: 'B1', artifacts: [{ name: 'r.csv', size: 10, url: '/api/backtest/B1/artifacts/r.csv' }] }))
    const r = await getBacktestArtifacts('B1')
    expect(r.artifacts[0].name).toBe('r.csv')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/backtest/B1/artifacts')
  })

  it('artifactUrl 正确编码 taskId 与 name（F-705 产物在线预览/下载 URL）', () => {
    expect(artifactUrl('B1', 'bt_factoric.csv')).toBe('/api/backtest/B1/artifacts/bt_factoric.csv')
    expect(artifactUrl('a b', 'c d.csv')).toBe('/api/backtest/a%20b/artifacts/c%20d.csv')
  })

  it('downloadArtifact 用 a[download] 触发下载（弃用 os.startfile，F-705）', () => {
    const click = vi.fn()
    const remove = vi.fn()
    document.createElement = vi.fn(() => ({ href: '', download: '', click, ...{ parentNode: null } }))
    const bodyAppend = vi.spyOn(document.body, 'appendChild').mockImplementation(() => {})
    const bodyRemove = vi.spyOn(document.body, 'removeChild').mockImplementation(() => {})
    downloadArtifact('B1', 'r.csv')
    const a = document.createElement.mock.results[0].value
    expect(a.href).toBe('/api/backtest/B1/artifacts/r.csv')
    expect(a.download).toBe('r.csv')
    expect(bodyAppend).toHaveBeenCalled()
    expect(click).toHaveBeenCalled()
    expect(bodyRemove).toHaveBeenCalled()
  })

  it('applyFactorIC POST /api/backtest/ic-apply 携带 version（乐观锁凭据）与 factors（F-704）', async () => {
    fetchMock.mockImplementationOnce(async () => jsonRes({ ok: true, updated: 2, version: 5 }))
    const r = await applyFactorIC({ profile: 'p1', version: 4, factors: { alpha: 0.5 } })
    expect(r.updated).toBe(2)
    const [url, opts] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/backtest/ic-apply')
    expect(JSON.parse(opts.body)).toEqual({ profile: 'p1', version: 4, factors: { alpha: 0.5 } })
  })

  it('applyFactorIC 版本冲突 409 时透传 X-Latest-Version 相关错误信息（F-704）', async () => {
    fetchMock.mockImplementation(async () => ({ ok: false, status: 409, json: async () => ({ error: { message: '版本冲突：最新版本=2' } }) }))
    const promise = applyFactorIC({ profile: 'p1', version: 1, factors: { x: 1 } })
    await expect(promise).rejects.toThrow('409')
    await expect(promise).rejects.toThrow('最新版本=2')
  })

  it('openBacktestEvents 订阅 stage/log/progress/done/error 并在 done 后 close（F-705 SSE 日志流）', () => {
    const handlers = { onStage: vi.fn(), onLog: vi.fn(), onProgress: vi.fn(), onDone: vi.fn(), onError: vi.fn() }
    const register = vi.fn()
    const close = vi.fn()
    global.EventSource = vi.fn(function (url) { this.url = url; this.addEventListener = register; this.close = close })
    const es = openBacktestEvents('B1', handlers)
    expect(es.url).toBe('/api/backtest/B1/events')
    expect(register).toHaveBeenCalledWith('stage', expect.any(Function))
    expect(register).toHaveBeenCalledWith('log', expect.any(Function))
    expect(register).toHaveBeenCalledWith('progress', expect.any(Function))
    expect(register).toHaveBeenCalledWith('done', expect.any(Function))
    expect(register).toHaveBeenCalledWith('error', expect.any(Function))
    // 触发 done -> onDone 回调 + 自动 close
    const doneCb = register.mock.calls.find(([ev]) => ev === 'done')[1]
    doneCb({ data: JSON.stringify({ status: 'completed', current: 20, total: 20, pct: 100 }) })
    expect(handlers.onDone).toHaveBeenCalled()
    expect(close).toHaveBeenCalled()
  })
})