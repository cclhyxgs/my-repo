// F-601 方案管理 API 客户端单测（mock fetch）。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  createScheme,
  deleteScheme,
  exportSchemeUrl,
  fetchSchemeExport,
  filenameFromDisposition,
  getScheme,
  importScheme,
  listSchemes,
  renameScheme,
  updateScheme,
} from './scheme'

const jsonRes = (body, status = 200, headers = {}) => ({
  ok: status >= 200 && status < 300,
  status,
  headers: { get: (k) => headers[k.toLowerCase()] ?? null },
  json: async () => body,
})

describe('scheme api client', () => {
  let fetchMock

  beforeEach(() => {
    fetchMock = vi.fn()
    global.fetch = fetchMock
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('listSchemes GET /api/scheme', async () => {
    fetchMock.mockImplementationOnce(async () => jsonRes([{ name: '策略A' }]))
    const rows = await listSchemes()
    expect(rows).toEqual([{ name: '策略A' }])
    expect(fetchMock.mock.calls[0][0]).toBe('/api/scheme')
  })

  it('getScheme 对中文名做 URL 编码', async () => {
    fetchMock.mockImplementationOnce(async () => jsonRes({ name: '策略四_轮动均值回归' }))
    await getScheme('策略四_轮动均值回归')
    expect(fetchMock.mock.calls[0][0]).toBe(`/api/scheme/${encodeURIComponent('策略四_轮动均值回归')}`)
  })

  it('createScheme POST 且 409 错误体 message 上抛', async () => {
    fetchMock.mockImplementationOnce(async () => jsonRes({ name: '策略A' }, 201))
    const res = await createScheme({ name: '策略A' })
    expect(res).toEqual({ name: '策略A' })
    const [url, opts] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/scheme')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({ name: '策略A' })

    fetchMock.mockImplementationOnce(async () =>
      jsonRes({ error: { code: 'SCHEME_EXISTS', message: 'scheme 已存在：策略A' } }, 409),
    )
    await expect(createScheme({ name: '策略A' })).rejects.toThrow(/scheme 已存在：策略A/)
  })

  it('updateScheme PUT 带 version；409 时提示含最新版本', async () => {
    fetchMock.mockImplementationOnce(async () => jsonRes({ ok: true, version: 2, updated_factors: [] }))
    const res = await updateScheme('A', { version: 1, desc: 'x' })
    expect(res.version).toBe(2)
    const [url, opts] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/scheme/A')
    expect(opts.method).toBe('PUT')
    expect(JSON.parse(opts.body)).toEqual({ version: 1, desc: 'x' })

    fetchMock.mockImplementationOnce(async () =>
      jsonRes({ error: { code: 'VERSION_CONFLICT', message: '版本冲突（最新版本=2）' } }, 409),
    )
    await expect(updateScheme('A', { version: 1 })).rejects.toThrow(/最新版本=2/)
  })

  it('deleteScheme 204 返回 true；404 抛错', async () => {
    fetchMock.mockImplementationOnce(async () => jsonRes(null, 204))
    expect(await deleteScheme('A')).toBe(true)
    expect(fetchMock.mock.calls[0][1].method).toBe('DELETE')

    fetchMock.mockImplementationOnce(async () =>
      jsonRes({ error: { code: 'SCHEME_NOT_FOUND', message: '方案不存在：A' } }, 404),
    )
    await expect(deleteScheme('A')).rejects.toThrow(/方案不存在：A/)
  })

  it('importScheme 用 FormData 且带上可选表单字段', async () => {
    fetchMock.mockImplementationOnce(async () => jsonRes({ ok: true, name: '导入_1' }))
    const file = new File(['{}'], 'a.json', { type: 'application/json' })
    await importScheme(file, { scheme_name: '我的方案', market: 'stock', empty: '' })
    const [url, opts] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/scheme/import')
    expect(opts.method).toBe('POST')
    expect(opts.body).toBeInstanceOf(FormData)
    expect(opts.body.get('scheme_name')).toBe('我的方案')
    expect(opts.body.get('market')).toBe('stock')
    expect(opts.body.get('empty')).toBeNull()
  })

  it('exportSchemeUrl 编码名称', () => {
    expect(exportSchemeUrl('策略A')).toBe(`/api/scheme/${encodeURIComponent('策略A')}/export`)
  })

  it('filenameFromDisposition 解析 RFC 5987 与普通 filename', () => {
    expect(filenameFromDisposition(`attachment; filename="scheme.json"; filename*=UTF-8''${encodeURIComponent('方案_策略A.json')}`))
      .toBe('方案_策略A.json')
    expect(filenameFromDisposition('attachment; filename="plain.json"')).toBe('plain.json')
    expect(filenameFromDisposition('')).toBe('')
  })

  it('fetchSchemeExport 返回 blob + 文件名', async () => {
    // 覆盖 blob()（jsdom 的 Blob 无 text()，这里用假对象）
    fetchMock.mockImplementationOnce(async () => ({
      ok: true,
      status: 200,
      headers: { get: (k) => (k.toLowerCase() === 'content-disposition'
        ? `attachment; filename="scheme.json"; filename*=UTF-8''${encodeURIComponent('方案_策略A.json')}`
        : null) },
      blob: async () => ({ text: async () => '{"name":"策略A"}' }),
    }))
    const { filename, text } = await fetchSchemeExport('策略A')
    expect(filename).toBe('方案_策略A.json')
    expect(text).toBe('{"name":"策略A"}')
  })

  it('renameScheme = 建新 → 删旧', async () => {
    fetchMock
      .mockImplementationOnce(async () => jsonRes({ name: '旧', market: 'stock', config: {}, period_configs: {}, version: 1 })) // GET
      .mockImplementationOnce(async () => jsonRes({ name: '新' }, 201)) // POST
      .mockImplementationOnce(async () => jsonRes(null, 204)) // DELETE 旧
    const res = await renameScheme('旧', '新')
    expect(res).toEqual({ name: '新' })
    expect(fetchMock.mock.calls.map((c) => c[1]?.method || 'GET')).toEqual(['GET', 'POST', 'DELETE'])
  })

  it('renameScheme 删旧失败 → 补偿删新并抛错', async () => {
    fetchMock
      .mockImplementationOnce(async () => jsonRes({ name: '旧', config: {}, version: 1 })) // GET
      .mockImplementationOnce(async () => jsonRes({ name: '新' }, 201)) // POST
      .mockImplementationOnce(async () => jsonRes({ error: { message: '删除失败' } }, 500)) // DELETE 旧
      .mockImplementationOnce(async () => jsonRes(null, 204)) // 补偿 DELETE 新
    await expect(renameScheme('旧', '新')).rejects.toThrow(/已回滚新方案/)
    expect(fetchMock.mock.calls.map((c) => c[1]?.method || 'GET')).toEqual(['GET', 'POST', 'DELETE', 'DELETE'])
  })
})
