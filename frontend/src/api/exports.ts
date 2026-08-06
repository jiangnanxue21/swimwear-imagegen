import { apiClient } from './client'
import { decodeExportName } from './batch'
import type { DashboardSummary, ProductExport } from './types'

export const dashboardApi = {
  summary: async () => (await apiClient.get<DashboardSummary>('/dashboard/summary')).data,
}

export interface ExportListParams {
  search?: string
  status?: string
  category?: string
  garment_type?: string
  only_complete?: boolean
  limit?: number
}

export const exportsApi = {
  product: async (id: string) =>
    (await apiClient.get<ProductExport>(`/exports/products/${id}`)).data,

  list: async (params: ExportListParams) =>
    (await apiClient.get<{ items: ProductExport[]; total: number }>('/exports/products', {
      params,
    })).data,

  /**
   * 单商品成品图导出,CSV。
   *
   * **不能做成 `<a href>` 直接下载。** 上一版是 `csvUrl()` 返回一个地址、
   * 由按钮的 `href` 打开,那条路在后端读接口也要口令之后就断了:
   * `exports.py` 的路由整体挂着 `require_operator`,`main.py` 的中间件是第二道,
   * 而 `<a>` 标签发起的导航**带不了自定义请求头** —— 点下去必然 401。
   *
   * 走 axios 取 blob 是全仓已有的做法(`workbench.exportFile`、
   * `batch.file`、私有素材代发那里都是同一个约束),这里只是把漏掉的一处补上。
   */
  csv: async (id: string, sku?: string): Promise<{ blob: Blob; filename: string }> => {
    const response = await apiClient.get<Blob>(`/exports/products/${id}`, {
      params: { format: 'csv' },
      responseType: 'blob',
    })
    return {
      blob: response.data,
      filename: filenameFrom(response.headers, `${sku || id}-images.csv`),
    }
  },

  /** 同上,JSON。原来是 `target="_blank"` 开新标签,同样带不了口令。 */
  json: async (id: string): Promise<{ blob: Blob; filename: string }> => {
    const payload = (await apiClient.get<ProductExport>(`/exports/products/${id}`)).data
    const sku = payload?.product?.sku || id
    return {
      blob: new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' }),
      filename: `${sku}-images.json`,
    }
  },
}

/**
 * 从响应头里取后端给的文件名,取不到就用回退名。
 *
 * 优先 `X-Export-Filename`:`Content-Disposition` 里的中文与引号解析起来
 * 更容易出错(和 `workbench.exportFile` 同一个理由)。
 * 跨域部署时这个头要靠后端 `expose_headers` 才读得到,读不到就退回回退名 ——
 * 退化成默认文件名可以接受,下载失败不行。
 */
function filenameFrom(headers: unknown, fallback: string): string {
  const map = (headers ?? {}) as Record<string, string | undefined>
  return decodeExportName(map['x-export-filename'] ?? map['X-Export-Filename']) ?? fallback
}
