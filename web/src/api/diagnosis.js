// F-401~404 诊断页 API 客户端（H5 前端 ↔ FastAPI 后端契约）。
// 相对路径 /api/* 由 vite dev 代理转发到 FastAPI(8000)；生产由托管层负责同源。
// 契约见 server/api/diagnosis.py 文档字符串：
//   POST /api/diagnosis            → {task_id}
//   GET  /api/diagnosis/{id}       → {status, total, current, summary, results}
//   GET  /api/diagnosis/{id}/events→ SSE（stage/progress/done/error）
//   POST /api/diagnosis/{id}/cancel→ {task_id, status:'cancelling'}
const BASE = ''

/** 启动诊断。payload: {text, market, direction?, scheme_name?, k_type} */
export async function startDiagnosis(payload) {
  const res = await fetch(`${BASE}/api/diagnosis`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!res.ok) {
    let detail = ''
    try {
      detail = (await res.json()).detail || ''
    } catch {
      // 解析失败忽略
    }
    throw new Error(`启动诊断失败（${res.status}）${detail}`)
  }
  const data = await res.json()
  return data.task_id
}

/** 查询诊断进度与结果。返回 {status, total, current, summary, results} */
export async function getDiagnosis(taskId) {
  const res = await fetch(`${BASE}/api/diagnosis/${encodeURIComponent(taskId)}`)
  if (!res.ok) throw new Error(`查询诊断失败（${res.status}）`)
  return res.json()
}

/** 取消诊断。返回 {task_id, status:'cancelling'} */
export async function cancelDiagnosis(taskId) {
  const res = await fetch(`${BASE}/api/diagnosis/${encodeURIComponent(taskId)}/cancel`, {
    method: 'POST',
  })
  if (!res.ok) throw new Error(`取消诊断失败（${res.status}）`)
  return res.json()
}

/**
 * 打开 SSE 进度流（GET /api/diagnosis/{id}/events）。
 * 事件：stage / progress / done / error；done 或 error 后自动关闭连接。
 * @param {string} taskId
 * @param {{onStage?,onProgress?,onDone?,onError?}} handlers
 * @returns {EventSource}
 */
export function openDiagnosisEvents(taskId, handlers = {}) {
  const url = `${BASE}/api/diagnosis/${encodeURIComponent(taskId)}/events`
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
