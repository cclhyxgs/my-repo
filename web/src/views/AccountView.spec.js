// F-1406 授权面板 + F-1403 登录/设备联动 —— 组件单测（mock ../api/auth.js）。
// 后端行为由 tests/h5_skeleton/test_f1401_auth.py + test_license_auth.py 覆盖。
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ElementPlus, { ElMessageBox } from 'element-plus'

const authApi = vi.hoisted(() => ({
  getAppInfo: vi.fn(),
  listDevices: vi.fn(),
  revokeDevice: vi.fn(),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
}))
vi.mock('../api/auth.js', () => authApi)

import AccountView from './AccountView.vue'

function mountView() {
  return mount(AccountView, { global: { plugins: [ElementPlus] } })
}

const anonInfo = {
  version: '0.1.0', account: null, license: null, device_id: null,
}
const authedInfo = {
  version: '0.1.0',
  account: { id: 1, identifier: 'u@e.com' },
  license: { status: 'trial', tier: 'trial', expires_at: null, trial_first_use_at: '2026-09-16T08:00:00+08:00', trial_days_left: 7 },
  device_id: 100,
}

const devices = [
  { device_id: 100, ua: '本机浏览器', bound_at: '2026-09-16T08:00:00+08:00', last_seen_at: '2026-09-16T09:00:00+08:00', current: true },
  { device_id: 200, ua: '手机 Chrome', bound_at: '2026-09-15T08:00:00+08:00', last_seen_at: '2026-09-15T09:00:00+08:00', current: false },
]

describe('AccountView（授权面板）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    authApi.getAppInfo.mockResolvedValue(anonInfo)
    authApi.listDevices.mockResolvedValue([])
    ElMessageBox.confirm = vi.fn().mockResolvedValue('confirm')
  })

  it('未登录 → 显示登录表单，提交调用 login', async () => {
    const w = mountView()
    await flushPromises()
    expect(w.text()).toContain('账号')
    w.vm.tab = 'login'
    w.vm.identifier = 'u@e.com'
    w.vm.pwd = 'secret1'
    await w.find('.submit-btn').trigger('click')
    await flushPromises()
    expect(authApi.login).toHaveBeenCalledWith('u@e.com', 'secret1')
  })

  it('已登录 → 显示授权状态与设备列表，解绑非当前设备调用 revokeDevice', async () => {
    authApi.getAppInfo.mockResolvedValue(authedInfo)
    authApi.listDevices.mockResolvedValue(devices)
    const w = mountView()
    await flushPromises()
    expect(w.text()).toContain('u@e.com')
    expect(w.text()).toContain('免费试用')
    expect(w.text()).toContain('当前')

    // 当前设备解绑按钮应禁用
    const rows = w.findAll('.device-row')
    expect(rows.length).toBe(2)
    const currentBtn = rows[0].find('button')
    expect(currentBtn.attributes('disabled')).toBeDefined()

    // 非当前设备可解绑
    await rows[1].find('button').trigger('click')
    await flushPromises()
    expect(authApi.revokeDevice).toHaveBeenCalledWith(200)
  })

  it('已登录 → 退出登录调用 logout 并清空状态', async () => {
    authApi.getAppInfo.mockResolvedValue(authedInfo)
    authApi.listDevices.mockResolvedValue([])
    const w = mountView()
    await flushPromises()
    await w.find('.logout').trigger('click')
    await flushPromises()
    expect(authApi.logout).toHaveBeenCalled()
  })

  it('试用到期 → 显示过期警告', async () => {
    authApi.getAppInfo.mockResolvedValue({
      ...authedInfo,
      license: { status: 'trial', tier: 'trial', expires_at: null, trial_first_use_at: '2026-09-01T08:00:00+08:00', trial_days_left: 0 },
    })
    const w = mountView()
    await flushPromises()
    expect(w.text()).toContain('授权已过期')
  })
})