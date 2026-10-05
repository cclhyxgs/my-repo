// AI 解读 API 客户端（H5 前端 ↔ FastAPI 后端契约）。
// 契约见 server/api/ai.py：
//   GET  /api/ai/config        → {ok, configured, model, enabled}
//   PUT  /api/ai/config        → {ok, configured, model}
//   POST /api/ai/analyze       → {ok, text, scene}
//   POST /api/ai/diagnosis     → {ok, text, scene}
//   POST /api/ai/scan          → {ok, text, scene, covered}
//   POST /api/ai/ledger        → {ok, text, scene, covered}
const BASE = ''

async function postJson(path, payload) {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload ?? {}),
  })
  if (!res.ok) {
    let detail = ''
    try {
      const body = await res.json()
      detail = body?.error?.message || body?.detail || ''
    } catch {
      /* 解析失败忽略 */
    }
    throw new Error(detail || `AI 解读请求失败（${res.status}）`)
  }
  return res.json()
}

/** 读取 AI 配置快照（不含 key）。返回 {configured, model, enabled} */
export async function getAiConfig() {
  const res = await fetch(`${BASE}/api/ai/config`)
  if (!res.ok) throw new Error(`读取 AI 配置失败（${res.status}）`)
  return res.json()
}

/** 保存 AI 配置。payload: {api_key, model?} */
export async function saveAiConfig(payload) {
  const res = await fetch(`${BASE}/api/ai/config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!res.ok) {
    let detail = ''
    try {
      const body = await res.json()
      detail = body?.error?.message || body?.detail || ''
    } catch {
      /* 忽略 */
    }
    throw new Error(detail || `保存 AI 配置失败（${res.status}）`)
  }
  return res.json()
}

/** 个股分析 AI 解读。payload: {code, market_type, k_type, scheme} */
export function aiAnalyze(payload) {
  return postJson('/api/ai/analyze', payload)
}

/** 单只诊断 AI 解读。payload: {item} */
export function aiDiagnosis(item) {
  return postJson('/api/ai/diagnosis', { item })
}

/** 扫描概览 AI 解读。payload: {items, filters} */
export function aiScan(items, filters = null) {
  return postJson('/api/ai/scan', { items, filters })
}

/** 情绪账本 AI 复盘。payload: {market} */
export function aiLedger(market = 'all') {
  return postJson('/api/ai/ledger', { market })
}

/**
 * 自然语言 → 方案配置条件。
 * payload: {text}
 * 返回 { ok, filters, factor_tweaks, unsupported }
 *  filters: [{signal} 或 {indicator,op,value}]
 */
export function aiSchemeConditions(text) {
  return postJson('/api/ai/scheme-conditions', { text })
}