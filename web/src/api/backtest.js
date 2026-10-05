// F-701~705 回测页 API 客户端（H5 前端 ↔ FastAPI 后端契约）。
// 契约见 server/api/backtest.py：
//   POST  /api/backtest                          → {task_id}（非法 mode 4xx / 初级 403）
//   GET   /api/backtest/modes                     → {ok, modes:[{mode,label,pool,hold,scan}]}
//   GET   /api/backtest/{id}/events               → SSE（stage/log/progress/done/error）
//   POST  /api/backtest/{id}/cancel               → {task_id, status:'cancelling'}
//   GET   /api/backtest/{id}/artifacts            → {task_id, artifacts:[{name,size,url}]}
//   GET   /api/backtest/{id}/artifacts/{name}     → 产物文件流（在线预览 / 下载）
//   POST  /api/backtest/ic-apply                  → {ok, updated, version}（409 版本冲突）
const BASE = ''

/** 提交回测。payload: {mode, runParams:{pool,hold,scan,prefix,...}} */
export async function submitBacktest(mode, runParams = {}) {
  const res = await fetch(`${BASE}/api/backtest`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode, run_params: runParams }),
  })
  if (!res.ok) {
    let detail = ''
    try {
      const body = await res.json()
      detail = body?.error?.message || body?.detail || ''
    } catch { /* ignore */ }
    throw new Error(`提交回测失败（${res.status}）${detail}`)
  }
  return res.json()
}

/** 回测模式与参数约束（F-702 消费面）。返回 {ok, modes:[...]} */
export async function getBacktestModes() {
  const res = await fetch(`${BASE}/api/backtest/modes`)
  if (!res.ok) throw new Error(`加载回测模式失败（${res.status}）`)
  return res.json()
}

/** 取消回测。返回 {task_id, status} */
export async function cancelBacktest(taskId) {
  const res = await fetch(`${BASE}/api/backtest/${encodeURIComponent(taskId)}/cancel`, { method: 'POST' })
  if (!res.ok) throw new Error(`取消回测失败（${res.status}）`)
  return res.json()
}

/** 产物清单。返回 {task_id, artifacts:[{name,size,url}]} */
export async function getBacktestArtifacts(taskId) {
  const res = await fetch(`${BASE}/api/backtest/${encodeURIComponent(taskId)}/artifacts`)
  if (!res.ok) throw new Error(`加载回测产物失败（${res.status}）`)
  return res.json()
}

/** 产物 URL（在线预览 / a[download] 下载）。 */
export function artifactUrl(taskId, name) {
  return `${BASE}/api/backtest/${encodeURIComponent(taskId)}/artifacts/${encodeURIComponent(name)}`
}

/** 触发产物下载（F-705：弃用 os.startfile，改 a[download] 触发浏览器下载）。 */
export function downloadArtifact(taskId, name) {
  const a = document.createElement('a')
  a.href = artifactUrl(taskId, name)
  a.download = name
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
}

/** 应用因子 IC 结果（F-704，version 为乐观锁凭据）。 */
export async function applyFactorIC({ profile, version, factors }) {
  const res = await fetch(`${BASE}/api/backtest/ic-apply`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ profile, version, factors }),
  })
  if (!res.ok) {
    let detail = ''
    try {
      const body = await res.json()
      detail = body?.error?.message || body?.detail || ''
    } catch { /* ignore */ }
    throw new Error(`应用IC结果失败（${res.status}）${detail}`)
  }
  return res.json()
}

/**
 * 打开回测 SSE 流（GET /api/backtest/{id}/events）。
 * 事件：stage / log / progress / done / error；done 或 error 后自动关闭。
 * @returns {EventSource}
 */
export function openBacktestEvents(taskId, handlers = {}) {
  const url = `${BASE}/api/backtest/${encodeURIComponent(taskId)}/events`
  const es = new EventSource(url)
  const parse = (e) => {
    try { return JSON.parse(e.data) } catch { return null }
  }
  if (handlers.onStage) es.addEventListener('stage', (e) => handlers.onStage(parse(e)))
  if (handlers.onLog) es.addEventListener('log', (e) => handlers.onLog(parse(e)))
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