// F-1401 账号授权 API 客户端单测（mock fetch）。
// 仅验证客户端契约封装正确（路径/方法/参数/解析）；后端行为由 tests/h5_skeleton/test_f1401_auth.py 覆盖。
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  register, login, refreshTokens, logout, getAppInfo, getLicenseInfo,
  getQuota, listDevices, revokeDevice,
} from './auth.js'

function mockFetch(handler) {
  const fn = vi.fn(async (url, opts) => handler(url, opts || {}))
  global.fetch = fn
  return fn
}

function jsonRes(obj, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => obj, text: async () => JSON.stringify(obj) }
}

describe('auth.js API 客户端', () => {
  let fetchMock
  beforeEach(() => { fetchMock = mockFetch(() => jsonRes({})) })
  afterEach(() => { vi.restoreAllMocks() })

  it('login POST /api/auth/login，identifier/pwd 透传', async () => {
    fetchMock.mockImplementationOnce(async () => jsonRes({ access_token: 'A', refresh_token: 'R', device_cookie: 'mbull_device' }))
    const r = await login('u@e.com', 'secret1')
    expect(r.access_token).toBe('A')
    const [url, opts] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/auth/login')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({ identifier: 'u@e.com', pwd: 'secret1' })
  })

  it('register POST /api/auth/register', async () => {
    fetchMock.mockImplementationOnce(async () => jsonRes({ access_token: 'A', refresh_token: 'R' }))
    const r = await register('u@e.com', 'secret1')
    expect(r.access_token).toBe('A')
    const [, opts] = fetchMock.mock.calls[0]
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body).identifier).toBe('u@e.com')
  })

  it('受保护端点 401 → 抛出 status=401（前端据此引导重新登录）', async () => {
    fetchMock.mockImplementation(async () => ({ ok: false, status: 401, json: async () => ({ error: { code: 'AUTH_REQUIRED', message: '未登录' } }) }))
    const promise = getLicenseInfo()
    await expect(promise).rejects.toThrow('401')
    await expect(promise).rejects.toMatchObject({ status: 401 })
  })

  it('getAppInfo 无参 GET /api/app-info', async () => {
    fetchMock.mockImplementationOnce(async () => jsonRes({ version: '0.1.0', account: null, license: null, device_id: null }))
    const r = await getAppInfo()
    expect(r.version).toBe('0.1.0')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/app-info')
  })

  it('getLicenseInfo 返回授权状态字段', async () => {
    fetchMock.mockImplementationOnce(async () => jsonRes({ status: 'trial', tier: 'trial', expires_at: null, trial_first_use_at: '2026-09-16T08:00:00+08:00', trial_days_left: 7 }))
    const r = await getLicenseInfo()
    expect(r.status).toBe('trial')
    expect(r.trial_days_left).toBe(7)
  })

  it('getQuota 返回订阅制 {unlimited:true}（F#4）', async () => {
    fetchMock.mockImplementationOnce(async () => jsonRes({ tier: 'trial', status: 'trial', expires_at: null, unlimited: true }))
    const r = await getQuota()
    expect(r.unlimited).toBe(true)
  })

  it('revokeDevice DELETE /api/account/devices/{id}', async () => {
    fetchMock.mockImplementationOnce(async () => jsonRes({}, 204))
    await revokeDevice(7)
    const [url, opts] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/account/devices/7')
    expect(opts.method).toBe('DELETE')
  })

  it('logout POST /api/auth/logout', async () => {
    fetchMock.mockImplementationOnce(async () => jsonRes({ ok: true }))
    const r = await logout()
    expect(r.ok).toBe(true)
    expect(fetchMock.mock.calls[0][1].method).toBe('POST')
  })
})