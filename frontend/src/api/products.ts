import { apiClient } from './client'
import type { Asset, AssetType, ImportResult, Page, Product } from './types'

export interface ProductQuery {
  search?: string
  status?: string
  garment_type?: string
  /** 服务端排序字段。白名单在后端 `product_service.SORTABLE`,越界后端报 400 */
  sort?: string
  order?: 'asc' | 'desc'
  page?: number
  page_size?: number
}

export const productsApi = {
  list: async (params: ProductQuery) =>
    (await apiClient.get<Page<Product>>('/products', { params })).data,

  get: async (id: string) => (await apiClient.get<Product>(`/products/${id}`)).data,

  create: async (payload: Partial<Product>) =>
    (await apiClient.post<Product>('/products', payload)).data,

  update: async (id: string, payload: Partial<Product>) =>
    (await apiClient.patch<Product>(`/products/${id}`, payload)).data,

  importCsv: async (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return (await apiClient.post<ImportResult>('/products/import', form)).data
  },

  assets: async (id: string) => (await apiClient.get<Asset[]>(`/products/${id}/assets`)).data,

  uploadAsset: async (id: string, file: File, assetType: AssetType) => {
    const form = new FormData()
    form.append('file', file)
    form.append('asset_type', assetType)
    return (await apiClient.post<{ asset: Asset; deduplicated: boolean }>(
      `/products/${id}/assets`,
      form,
    )).data
  },
}
