// F-501~506 扫描页 API 客户端（H5 前端 ↔ FastAPI 后端契约）。
// 相对路径 /api/* 由 vite dev 代理转发到 FastAPI(8000)；生产由托管层负责同源。
// 契约见 server/api/scan.py 文档字符串：
//   POST /api/scan                 → {task_id, reused}
//   GET  /api/scan/{id}            → {status, total, current, pct, market, reused}
//   GET  /api/scan/{id}/results    → {total, offset, limit, items}（不含 tech_snapshot）
//   GET  /api/scan/{id}/events     → SSE（stage/progress/done/error）
//   POST /api/scan/{id}/cancel      → {status:'cancelling'}
//   POST /api/scan/{id}/pause       → {status:'paused'}
//   POST /api/scan/{id}/resume      → {status:'running'}
//   GET  /api/scan/{id}/export      → text/csv（utf-8-sig）
//   GET  /api/sectors?market=&task_id= → {sectors:[...]}
//   GET  /api/scan/cache?market=    → {exists, stale, saved_at}
const BASE = ''

/** 启动扫描。payload: {market, direction?, k_type, scheme_name?, use_cache} */
export async function startScan(payload) {
  const res = await fetch(`${BASE}/api/scan`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!res.ok) {
    let detail = ''
    try {
      // 后端统一错误体 = {error:{code,message}, ts}（server/core/errors.py）
      const body = await res.json()
      detail = body?.error?.message || body?.detail || ''
    } catch {
      // 解析失败忽略
    }
    throw new Error(`启动扫描失败（${res.status}）${detail}`)
  }
  return res.json()
}

/** 查询扫描进度。返回 {status, total, current, pct, market, reused, message} */
export async function getScanProgress(taskId) {
  const res = await fetch(`${BASE}/api/scan/${encodeURIComponent(taskId)}`)
  if (!res.ok) throw new Error(`查询扫描失败（${res.status}）`)
  return res.json()
}

/** 查询扫描结果（分页/筛选/排序）。返回 {total, filtered_total, offset, limit, items} */
export async function getScanResults(taskId, { offset = 0, limit = 200, rating = null, sort = 'final_score', order = 'desc' } = {}) {
  const params = new URLSearchParams({ offset, limit, sort, order })
  if (rating != null) params.set('rating', String(rating))
  const res = await fetch(`${BASE}/api/scan/${encodeURIComponent(taskId)}/results?${params.toString()}`)
  if (!res.ok) throw new Error(`查询扫描结果失败（${res.status}）`)
  return res.json()
}

/** 板块强度聚合。返回 {market, sectors:[...]} */
export async function getSectors({ market = 'stock', taskId = null } = {}) {
  const params = new URLSearchParams({ market })
  if (taskId) params.set('task_id', taskId)
  const res = await fetch(`${BASE}/api/sectors?${params.toString()}`)
  if (!res.ok) throw new Error(`查询板块失败（${res.status}）`)
  return res.json()
}

/** 扫描缓存状态。返回 {market, exists, stale, saved_at} */
export async function getScanCache(market = 'stock') {
  const res = await fetch(`${BASE}/api/scan/cache?market=${encodeURIComponent(market)}`)
  if (!res.ok) throw new Error(`查询缓存失败（${res.status}）`)
  return res.json()
}

/** 取消扫描。返回 {task_id, status:'cancelling'} */
export async function cancelScan(taskId) {
  const res = await fetch(`${BASE}/api/scan/${encodeURIComponent(taskId)}/cancel`, { method: 'POST' })
  if (!res.ok) throw new Error(`取消扫描失败（${res.status}）`)
  return res.json()
}

/** 暂停扫描。返回 {task_id, status:'paused'} */
export async function pauseScan(taskId) {
  const res = await fetch(`${BASE}/api/scan/${encodeURIComponent(taskId)}/pause`, { method: 'POST' })
  if (!res.ok) throw new Error(`暂停扫描失败（${res.status}）`)
  return res.json()
}

/** 继续扫描。返回 {task_id, status:'running'} */
export async function resumeScan(taskId) {
  const res = await fetch(`${BASE}/api/scan/${encodeURIComponent(taskId)}/resume`, { method: 'POST' })
  if (!res.ok) throw new Error(`继续扫描失败（${res.status}）`)
  return res.json()
}

/** 导出 CSV 的下载 URL（GET 返回 utf-8-sig CSV，Content-Disposition 触发下载）。 */
export function exportScanUrl(taskId) {
  return `${BASE}/api/scan/${encodeURIComponent(taskId)}/export`
}

/**
 * 打开 SSE 进度流（GET /api/scan/{id}/events）。
 * 事件：stage / progress / done / error；done 或 error 后自动关闭连接。
 * @returns {EventSource}
 */
export function openScanEvents(taskId, handlers = {}) {
  const url = `${BASE}/api/scan/${encodeURIComponent(taskId)}/events`
  const es = new EventSource(url)
  const parse = (e) => {
    try {
      return JSON.parse(e.data)
    } catch {
      return null
    }
  }
  if (handlers.onStage) es.addEventListener('stage', (e) => handlers.onStage(parse(e)))
  if (handlers.onProgress) es.addEventListener('progress', (e) => handlers.onProgress(parse(e)))
  if (handlers.onDone) {
    es.addEventListener('done', (e) => {
      handlers.onDone(parse(e))
      es.close()
    })
  }
  if (handlers.onError) {
    es.addEventListener('error', (e) => {
      handlers.onError(e)
      es.close()
    })
  }
  return es
}
