/**
 * 图片集接口(FE-211~214,后端 app/api/image_sets.py)。
 *
 * 改一版已批准的图片集只有一条路:`derive` 派生新版。原地改的话,
 * 「平台驳回的是哪一版图」就没法回答,而驳回回流第一件要做的事
 * 正是定位到具体那一版的具体那一项。界面上因此不提供「编辑当前版本」——
 * 已批准的集只给「基于这一版新建」。
 */
import { apiClient } from './client'
import type { MediaRole } from './media'

export interface ImageSetItem {
  id: string
  media_asset_id: string
  role: MediaRole
  sort_order: number
  variant_id: string | null
  is_primary: boolean
  enabled: boolean
  derivative_purpose: string | null
}

/** 提交用的一项。没有 id —— 保存是整批替换,不做逐项 diff */
export interface ImageSetItemInput {
  media_asset_id: string
  role: MediaRole
  sort_order: number
  variant_id?: string | null
  is_primary?: boolean
  enabled?: boolean
  derivative_purpose?: string | null
}

export interface ImageSetViolation {
  code: string
  message: string
  variant_id: string | null
  media_asset_id: string | null
}

export interface ImageSet {
  id: string
  spu: string
  channel: string | null
  site: string | null
  version: number
  status: string
  derived_from: string | null
  approved_by: string | null
  approved_at: string | null
  created_at: string | null
  /** 快审退回(A10)。status 是 REJECTED 时才有值 */
  reject_reason: string | null
  reject_note: string | null
  rejected_at: string | null
}

/**
 * 变体覆盖诊断(后端 `image_set_service.variant_coverage`)。
 *
 * **不是校验结果,是现状描述** —— 它不阻断批准。今天绝大多数 SPU 的图
 * 全是通用图,把它改成阻断等于让每一个多色 SPU 立刻无法批准。
 */
export interface VariantCoverage {
  /** 这个 SPU 有哪几个变体(UUID 身份,不是显示名) */
  required: string[]
  /**
   * UUID → **当前**颜色名(A44)。
   *
   * 界面一律显示这个,不要显示 UUID。
   */
  labels: Record<string, string>
  /**
   * 五类兼容字段。0046 后 `renamed` 与 `identity_shadowed` 恒为空;
   * 当前需要处理的是 `label_collisions` 与 `key_label_conflicts`。
   *
   * **这个联合必须和后端 `variant_key.drift()` 的返回键一一对应**,
   * 由 `tests/pure/test_a45_batch13_2_fixes.py` 里那条守卫钉着:后端加一个键
   * 而这里不加,TS 不会报错,那一类异常就在界面上彻底消失。
   *
   * `Partial` 不是保守写法而是**事实**:后端这个字段的默认值是 `{}`,
   * 于是任何一个键在运行期都可能是 undefined。写成 `Record<string, string[]>`
   * 的话 TS 会保证它们存在,而 `.length` 在真实响应上炸 —— 类型撒谎比没有类型糟。
   */
  drift: Partial<
    Record<
      /** key 与当前颜色对不上 —— 正常,说明改过名 */
      | 'renamed'
      /** 两个变体的当前颜色一样了。**这是真问题** */
      | 'label_collisions'
      /**
       * 身份只剩种子表达式(`primary_color or sku`)的行 —— 既没有颜色变体
       * 外键也没有 `variant_key`,仍然会被改一次颜色名带偏。
       *
       * A45-batch13-2:判据从"没有 variant_key"改成这个。新建档路径**不写**
       * `variant_key`(退役方向),按旧判据它建的每一行都会永远挂在这里,
       * 而它们恰恰是唯一带真外键的那批 —— 恒报等于不报。
       */
      | 'unassigned'
      /**
       * 同一个身份下出现了两个不同的颜色(A-28)。
       *
       * `refs_of()` 遇到它会悄悄挑第一个当目录名 —— 于是这个变体在界面上
       * 叫什么,取决于商品行的顺序。
       */
      | 'key_label_conflicts'
      /**
       * 这一行同时有颜色变体外键和 `variant_key`,外键赢了 —— 它的身份
       * **已经不是**属性 owner_id 与图片标签当初写下的那个值。
       *
       * 今天应当恒为空:新路径写外键不写 key,老路径写 key 不写外键。
       * 一旦非空,说明有人给存量行回填了外键**而没有同时改写属性与标签**,
       * 那一批颜色事实和图片绑定已经指向不存在的变体了。
       */
      | 'identity_shadowed',
      string[]
    >
  >
  tagged: string[]
  has_generic_images: boolean
  missing: string[]
  unknown_tags: string[]
  variant_binding_missing: boolean
}

export interface ImageSetDetail extends ImageSet {
  /** 按 sort_order 升序,后端排好的 */
  items: ImageSetItem[]
  violations: ImageSetViolation[]
  /** 校验通过**且**已批准。已批准但素材后来被隔离的集在这里是 false */
  publishable: boolean
  /** 变体覆盖现状。后端一直在算,A44 之前前端一个字都没显示 */
  variant_coverage: VariantCoverage | null
}

export const imageSetsApi = {
  list: async (spu: string) =>
    (await apiClient.get<ImageSet[]>('/image-sets', { params: { spu } })).data,

  get: async (id: string) => (await apiClient.get<ImageSetDetail>(`/image-sets/${id}`)).data,

  create: async (payload: {
    spu: string
    items: ImageSetItemInput[]
    channel?: string | null
    site?: string | null
  }) => (await apiClient.post<ImageSetDetail>('/image-sets', payload)).data,

  derive: async (id: string, items: ImageSetItemInput[]) =>
    (await apiClient.post<ImageSetDetail>(`/image-sets/${id}/derive`, { items })).data,

  approve: async (id: string) =>
    (await apiClient.post<ImageSetDetail>(`/image-sets/${id}/approve`, {})).data,

  /**
   * 快审退回(A10)。`reason` 的取值来自 `workbenchApi.rejectReasons('image_set')`,
   * 前端不写死 —— 哪些原因适用于图片集、每个原因退回后去哪一步,都在后端。
   */
  reject: async (id: string, payload: { reason: string; note?: string | null }) =>
    (await apiClient.post<ImageSetDetail>(`/image-sets/${id}/reject`, payload)).data,

  rollback: async (id: string) =>
    (await apiClient.post<ImageSetDetail>(`/image-sets/${id}/rollback`, {})).data,
}
