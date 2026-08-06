/**
 * 生成方案接口(阶段 4,后端 app/api/generation_plans.py)。
 *
 * ## 界面上不提供「直接修改已启用的方案」
 *
 * 后端的唯一索引是 `(spu_id, COALESCE(color_variant_id,''))` 且排除 ARCHIVED,
 * 所以每层当前只有一份生效方案。改一份正在用的方案会**当场改掉幂等键**,
 * 于是编辑器里改到一半的参数会立刻影响下一次创建任务。
 *
 * 界面因此是两步:先存 DRAFT,再启用。与图片集那条「已批准的集只给
 * 『基于这一版新建』」是同一条道理。
 *
 * ## 启用之前先看代价
 *
 * `previewActivation` 回答的是 §7.5「上游变化前展示失效影响」:启用它会让
 * 哪些颜色的图片集过期。**必须在按钮点下去之前展示** —— 一个九色 SPU 改一次
 * 默认角度可能要重出八个颜色的全部候选,每一张都是真实付费调用。
 */
import { apiClient } from './client'

/** 与后端 `core/enums.ImageAngle` 逐值对应。前端不许自己扩这张表 */
export type ImageAngle = 'FRONT' | 'BACK' | 'SIDE' | 'THREE_QUARTER' | 'DETAIL' | 'FLAT_LAY'

export const IMAGE_ANGLE_LABEL: Record<ImageAngle, string> = {
  FRONT: '正面',
  BACK: '背面',
  SIDE: '侧面',
  THREE_QUARTER: '四分之三侧',
  DETAIL: '细节',
  FLAT_LAY: '平铺',
}

export type GenerationPlanStatus = 'DRAFT' | 'ACTIVE' | 'ARCHIVED'

export const PLAN_STATUS_LABEL: Record<GenerationPlanStatus, { text: string; color: string }> = {
  DRAFT: { text: '草稿', color: 'default' },
  ACTIVE: { text: '生效中', color: 'green' },
  // 归档不是删除:图片集过期判定要拿它的指纹比对,所以它会一直留在列表里
  ARCHIVED: { text: '已归档', color: 'default' },
}

export interface PlanAngle {
  angle: ImageAngle
  count: number
}

export interface GenerationPlan {
  id: string
  spu_id: string
  /** null = SPU 默认方案;非空 = 该颜色的覆盖 */
  color_variant_id: string | null
  model_template_id: string | null
  provider: string
  scene: string
  pose: string
  angles_json: PlanAngle[]
  budget_cap: string | null
  /**
   * 派生值,**只读**。唯一写入点在后端服务层 —— 回传它等于给那个值
   * 开第二个入口,而两个入口算出的指纹漂移时没有任何地方会报错。
   */
  plan_fingerprint: string
  status: GenerationPlanStatus
  row_version: number
  note: string | null
}

export interface GenerationPlanInput {
  spu_id: string
  color_variant_id?: string | null
  model_template_id?: string | null
  provider?: string
  scene?: string
  pose?: string
  angles?: PlanAngle[]
  budget_cap?: string | null
  note?: string | null
}

export interface PlanActivationEffect {
  plan: GenerationPlan
  /** 启用之后这些颜色的图片集会过期(§8.1 第 7 行) */
  stale_color_variant_ids: string[]
}

export async function listGenerationPlans(spuId: string): Promise<GenerationPlan[]> {
  const { data } = await apiClient.get<GenerationPlan[]>('/generation-plans', {
    params: { spu_id: spuId },
  })
  return data
}

export async function createGenerationPlan(
  payload: GenerationPlanInput,
): Promise<GenerationPlan> {
  const { data } = await apiClient.post<GenerationPlan>('/generation-plans', payload)
  return data
}

export async function previewActivation(planId: string): Promise<PlanActivationEffect> {
  const { data } = await apiClient.post<PlanActivationEffect>(
    `/generation-plans/${planId}/preview`,
  )
  return data
}

export async function activateGenerationPlan(planId: string): Promise<PlanActivationEffect> {
  const { data } = await apiClient.post<PlanActivationEffect>(
    `/generation-plans/${planId}/activate`,
  )
  return data
}
