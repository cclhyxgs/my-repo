// F-1401 账号授权 / 状态 API 客户端（H5 前端 ↔ FastAPI 后端契约）。
// 契约见 server/api/auth.py：鉴权载体 = HttpOnly device cookie（fetch 自动携带，
// 前端不接触 cookie 明文）；JWT 由后端下发、当前端点不校验 access token，
// 后端以 cookie 解析会话。受保护端点 401 → 客户端应引导回登录。
const BASE = ''

async function _send(url, opts, failMsg) {
  const res = await fetch(url, opts)
  if (!res.ok) {
    let detail = ''
    try {
      const body = await res.json()
      detail = body?.error?.message || body?.detail || ''
    } catch { /* ignore */ }
    const err = new Error(`${failMsg}（${res.status}）${detail}`)
    err.status = res.status
    throw err
  }
  return res.json()
}

/** 注册并首次登录。返回 {access_token, refresh_token, device_cookie}（cookie 由浏览器持有） */
export async function register(identifier, pwd) {
  return _send(`${BASE}/api/auth/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ identifier, pwd }),
  }, '注册失败')
}

/** 登录（设备绑定/续触）。返回同上。 */
export async function login(identifier, pwd) {
  return _send(`${BASE}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ identifier, pwd }),
  }, '登录失败')
}

/** 刷新令牌。401 时前端引导重新登录。 */
export async function refreshTokens() {
  return _send(`${BASE}/api/auth/refresh`, { method: 'POST' }, '刷新会话失败')
}

/** 注销当前设备并清 cookie。 */
export async function logout() {
  const res = await fetch(`${BASE}/api/auth/logout`, { method: 'POST' })
  if (!res.ok) throw new Error(`退出登录失败（${res.status}）`)
  return res.json()
}

/** 应用信息（F-104）。未登录返回 account/license/device_id 为 null。 */
export async function getAppInfo() {
  return _send(`${BASE}/api/app-info`, {}, '加载应用信息失败')
}

/** 授权状态（F-1401/1402）。返回 {status, tier, expires_at, trial_first_use_at, trial_days_left} */
export async function getLicenseInfo() {
  return _send(`${BASE}/api/license/info`, {}, '加载授权状态失败')
}

/** 订阅配额（F#4）。返回 {tier, status, expires_at, unlimited} */
export async function getQuota() {
  return _send(`${BASE}/api/quota`, {}, '加载配额失败')
}

/** 已绑定设备列表（F-1403）。返回 [{device_id, ua, bound_at, last_seen_at, current}] */
export async function listDevices() {
  return _send(`${BASE}/api/account/devices`, {}, '加载设备列表失败')
}

/** 解绑指定设备（F-1403）。 */
export async function revokeDevice(deviceId) {
  const res = await fetch(`${BASE}/api/account/devices/${encodeURIComponent(deviceId)}`,
    { method: 'DELETE' })
  if (!res.ok) {
    let detail = ''
    try {
      const body = await res.json()
      detail = body?.error?.message || body?.detail || ''
    } catch { /* ignore */ }
    throw new Error(`解绑设备失败（${res.status}）${detail}`)
  }
}