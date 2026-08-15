import { apiClient } from './client'
import type { Asset, AssetType, ImportResult, Page, Product } from './types'
import type { ColorVariant } from './spus'

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

  /**
   * 归档一件商品。**这是这套系统里「删除商品」的全部含义,没有 DELETE。**
   *
   * `products.id` 被九张表引着:`channel_listings` 是 RESTRICT(平台上还挂着的
   * 商品硬删会 500),其余是 CASCADE(删得掉,但会连带清空素材、任务、属性、
   * 评估 —— 整条证据链没了,而运营点的那个按钮只写着「删除」)。所以做的是
   * 状态迁移:行还在,只是不再出现在工作台列表里。
   *
   * 理由必填。归档的痕迹只留在审计表里,一条没有理由的记录在三个月后
   * 复盘「这件当初为什么不做了」时等于没有。
   */
  archive: async (id: string, reason: string) =>
    (await apiClient.post<Product>(`/products/${id}/archive`, { reason })).data,

  /** 放回生产动线。恢复到 DRAFT 而不是归档前那一档,理由在后端服务层 */
  restore: async (id: string) =>
    (await apiClient.post<Product>(`/products/${id}/restore`)).data,

  importCsv: async (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return (await apiClient.post<ImportResult>('/products/import', form)).data
  },

  assets: async (id: string) => (await apiClient.get<Asset[]>(`/products/${id}/assets`)).data,

  /**
   * 这件商品能选的颜色(§4.3)。
   *
   * **取数与上传接口的白名单同源**(后端 `product_service.colours_for`)。
   * 前端自己走 product -> spu -> colours 的话,少列一个颜色的表现是
   * 那个颜色永远传不了图、因此永远缺正面图,而门禁只会一直说「缺图」。
   *
   * 老建档路径的商品拿到空表(它没有 SPU),不是错误 —— 素材页据此
   * 隐藏颜色选择器,而不是整页报错。
   */
  colorVariants: async (id: string) =>
    (await apiClient.get<ColorVariant[]>(`/products/${id}/color-variants`)).data,

  /**
   * 上传素材。`colorVariantId` 不传就是通用图,与本参数存在之前逐字相同。
   *
   * ## 这一个参数是 AC-21 在界面上能不能验的分界线
   *
   * 后端从 A45-batch14-22 起就收这个表单字段并做归属校验,而表单一直没发它
   * —— 于是界面传出来的素材 `color_variant_id` **恒为 null**,§5.3 的颜色
   * 作用域指纹只有共享那一档能触发。「给 A 色补图不 stale B 色事实」
   * 在真环境里**没有对象可验**:库里根本不存在一张归属到某个颜色的图。
   *
   * 空串不许发。`Form(None)` 收到空串会当成一个值去解 UUID 然后 422,
   * 而"不选颜色"是一个合法且常用的选择(通用图)。
   */
  uploadAsset: async (
    id: string,
    file: File,
    assetType: AssetType,
    colorVariantId?: string | null,
  ) => {
    const form = new FormData()
    form.append('file', file)
    form.append('asset_type', assetType)
    if (colorVariantId) form.append('color_variant_id', colorVariantId)
    return (await apiClient.post<{ asset: Asset; deduplicated: boolean }>(
      `/products/${id}/assets`,
      form,
    )).data
  },
}
