// F-601 方案管理 API 客户端（H5 前端 ↔ FastAPI 后端契约）。
// 相对路径 /api/* 由 vite dev 代理转发到 FastAPI(8000)；生产由托管层负责同源。
// 契约见 server/api/scheme.py 文档字符串：
//   GET    /api/scheme               → [{name, factor_profile, market, direction, period}]
//   POST   /api/scheme               → 201 {name} ∥ 409 SCHEME_EXISTS ∥ 422 SCHEME_NAME_INVALID
//   GET    /api/scheme/{name}        → 完整方案（含 config / version）
//   PUT    /api/scheme/{name}        → {ok, version, updated_factors[]} ∥ 409 VERSION_CONFLICT
//   DELETE /api/scheme/{name}        → 204 ∥ 404
//   POST   /api/scheme/import        → {ok, name, imported[], skipped[]}（multipart）
//   GET    /api/scheme/{name}/export → JSON 文件流（Content-Disposition，中文名 RFC 5987）
const BASE = ''

/** 统一读后端错误体 `{error:{code,message}, ts}`，拼成可读 message。 */
async function toError(res, fallback) {
  let message = ''
  try {
    const body = await res.json()
    message = body?.error?.message || body?.detail || ''
  } catch {
    // 解析失败忽略
  }
  const err = new Error(`${fallback}（${res.status}）${message ? '：' + message : ''}`)
  err.status = res.status
  return err
}

/** 方案列表。返回 [{name, factor_profile, market, direction, period}] */
export async function listSchemes() {
  const res = await fetch(`${BASE}/api/scheme`)
  if (!res.ok) throw await toError(res, '查询方案列表失败')
  return res.json()
}

/** 方案详情（含 config / version）。 */
export async function getScheme(name) {
  const res = await fetch(`${BASE}/api/scheme/${encodeURIComponent(name)}`)
  if (!res.ok) throw await toError(res, '查询方案详情失败')
  return res.json()
}

/** 新建方案。payload: {name, label?, desc?, market?, direction?, period?, mode?, factor_profile?, config?, period_configs?} */
export async function createScheme(payload) {
  const res = await fetch(`${BASE}/api/scheme`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw await toError(res, '新建方案失败')
  return res.json()
}

/** 更新方案（乐观锁）。payload 必须含 version。返回 {ok, version, updated_factors} */
export async function updateScheme(name, payload) {
  const res = await fetch(`${BASE}/api/scheme/${encodeURIComponent(name)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw await toError(res, '保存方案失败')
  return res.json()
}

/** 删除方案。成功返回 true（204）。 */
export async function deleteScheme(name) {
  const res = await fetch(`${BASE}/api/scheme/${encodeURIComponent(name)}`, { method: 'DELETE' })
  if (!res.ok) throw await toError(res, '删除方案失败')
  return true
}

/**
 * 导入方案（multipart，3 格式兼容）。
 * @param {File} file 用户选择的 .json 文件
 * @param {{market?:string, direction?:string, period?:string, scheme_name?:string}} form
 */
export async function importScheme(file, form = {}) {
  const fd = new FormData()
  fd.append('file', file, file.name || 'scheme.json')
  Object.entries(form).forEach(([k, v]) => {
    if (v != null && v !== '') fd.append(k, v)
  })
  const res = await fetch(`${BASE}/api/scheme/import`, { method: 'POST', body: fd })
  if (!res.ok) throw await toError(res, '导入方案失败')
  return res.json()
}

/** 导出下载 URL（GET 返回 JSON 流，Content-Disposition 触发下载）。 */
export function exportSchemeUrl(name) {
  return `${BASE}/api/scheme/${encodeURIComponent(name)}/export`
}

/** 解析 Content-Disposition 的 RFC 5987 文件名（`filename*=UTF-8''...`），回退普通 filename。 */
export function filenameFromDisposition(disposition) {
  if (!disposition) return ''
  const star = /filename\*=UTF-8''([^;]+)/i.exec(disposition)
  if (star) {
    try {
      return decodeURIComponent(star[1].trim())
    } catch {
      return star[1].trim()
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(disposition)
  return plain ? plain[1].trim() : ''
}

/**
 * 拉取方案导出内容（Blob + 文件名），供下载 / 剪贴板降级复用。
 * @returns {Promise<{blob: Blob, filename: string, text: string}>}
 */
export async function fetchSchemeExport(name) {
  const res = await fetch(exportSchemeUrl(name))
  if (!res.ok) throw await toError(res, '导出方案失败')
  const blob = await res.blob()
  let text = ''
  try {
    text = await blob.text()
  } catch {
    text = ''
  }
  const filename = filenameFromDisposition(res.headers.get('content-disposition')) || `方案_${name}.json`
  return { blob, filename, text }
}

/**
 * 重命名方案：契约未提供 rename 端点（C3 §3.2 六个端点内无 rename），
 * 故用「建新 → 删旧」组合实现；删旧失败则补偿删除新方案，保持原状。
 */
export async function renameScheme(oldName, newName) {
  const detail = await getScheme(oldName)
  await createScheme({
    name: newName,
    label: detail.label,
    desc: detail.desc,
    market: detail.market,
    direction: detail.direction,
    period: detail.period,
    mode: detail.mode,
    factor_profile: detail.factor_profile,
    config: detail.config,
    period_configs: detail.period_configs,
  })
  try {
    await deleteScheme(oldName)
  } catch (e) {
    try {
      await deleteScheme(newName)
    } catch {
      // 补偿删除也失败：抛出原始错误，由 UI 提示人工处理
    }
    throw new Error(`重命名失败，已回滚新方案：${e.message}`)
  }
  return { name: newName }
}
