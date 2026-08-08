/**
 * 建档域(PRD v3.1 §12.1 / §6.1 步骤 1)。
 *
 * ## 这个文件在此之前**不存在**
 *
 * `POST /api/spus`、`GET /api/spus/size-templates`、`GET /api/spus` 三个端点
 * 从阶段 1 起就齐了,而 `frontend/src/api/` 下没有任何 spus 客户端 ——
 * 全仓只有 `batch.ts` 打过 `/workbench/spus`(只读聚合)。后端做完、
 * 门禁全绿、动线断在最后一跳,和 `facts_stale` / `variant_gate_roles`
 * 是同一个形状。
 *
 * ## 这里**没有**视觉属性字段,那不是遗漏
 *
 * 阶段 1 的验收之一是「不填视觉属性即可建档」。后端的做法不是把那 8 个
 * 字段设成可选,而是让它们在这个接口上根本不存在 —— 可选字段会被前端
 * 填成空串,而空串和"还没识别"在下游是两件事:前者会被当成一个确定的
 * 事实("主色是空")。前端跟着这条走,不许自己加。
 *
 * ## 尺码模板不内置
 *
 * 硬规则 4:后端给什么就显示什么。内置一份的话,加一个模板要改两个仓库,
 * 而漏改的那一侧不报错 —— 它只会少一个选项。
 */
import { apiClient } from './client'
import type { Page } from './types'

/** 受众。SPU 层**必填且没有"待确认"**(§4.2)——选不出来说明这个款还不该建档 */
export type SpuAudience = 'WOMEN' | 'MEN' | 'UNISEX'

export const SPU_AUDIENCE_LABEL: Record<SpuAudience, string> = {
  WOMEN: '女装',
  MEN: '男装',
  UNISEX: '中性',
}

export interface ColorVariant {
  id: string
  variant_code: string
  /** 供应商口中的那个名字,内部用。建档阶段能填的是它 */
  working_name: string
  /**
   * 正式颜色名称。**投影列** —— 唯一写入点是属性服务在 VARIANT 层
   * `standard_color_name` 被确认时(§4.3)。建档时它是空的,别拿它当标题
   */
  display_name: string
  supplier_color_code: string | null
  sort_order: number
  sellable_status: string
}

/** 一个颜色在表单里的样子。`display_name` 不在这里 —— 它不是人能填的 */
export interface ColorVariantDraft {
  variant_code: string
  working_name: string
  supplier_color_code?: string | null
}

export interface SizeTemplate {
  name: string
  label: string
  sizes: string[]
}

export interface Spu {
  id: string
  spu_code: string
  internal_name: string
  audience: SpuAudience
  base_category: string
  supplier_ref: string | null
  status: string
  created_by: string
  created_at: string
  updated_at: string
  color_variants: ColorVariant[]
  sku_count: number
}

export interface SpuSku {
  id: string
  sku: string
  size: string | null
  size_group: string | null
  color_variant_id: string | null
  barcode: string | null
  price: string | null
  inventory: number | null
}

export interface SpuDetail extends Spu {
  skus: SpuSku[]
}

export interface SpuCreate {
  spu_code: string
  internal_name: string
  audience: SpuAudience
  base_category: string
  supplier_ref?: string | null
  notes?: string | null
  color_variants: ColorVariantDraft[]
  size_template: string
}

export const spusApi = {
  sizeTemplates: async () =>
    (await apiClient.get<SizeTemplate[]>('/spus/size-templates')).data,

  list: async (params: { page?: number; page_size?: number } = {}) =>
    (await apiClient.get<Page<Spu>>('/spus', { params })).data,

  get: async (id: string) => (await apiClient.get<SpuDetail>(`/spus/${id}`)).data,

  create: async (payload: SpuCreate) =>
    (await apiClient.post<SpuDetail>('/spus', payload)).data,
}

/**
 * 颜色在界面上叫什么。
 *
 * 顺序是 `working_name` -> `display_name` -> `variant_code`,与后端
 * `workbench/service._material_facts` 拼 `variant_labels` 的顺序**逐字相同**。
 * 两处不同的话,同一个颜色在素材页的分组标题和门禁提示语里叫两个名字,
 * 而运营会以为那是两个颜色。
 */
export function colorVariantLabel(variant: ColorVariant): string {
  return variant.working_name || variant.display_name || variant.variant_code
}
