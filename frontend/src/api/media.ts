import { apiClient } from './client'

export type MediaSource =
  | 'MANUAL_UPLOAD' | 'IMPORTED_URL' | 'SUPPLIER_FEED' | 'AI_GENERATED' | 'PLATFORM_SYNC'

export type MediaRole =
  | 'PRODUCT_FRONT' | 'PRODUCT_BACK' | 'MODEL_FRONT' | 'MODEL_BACK'
  | 'DETAIL' | 'SIZE_CHART' | 'FLAT_LAY' | 'PACKAGING' | 'OTHER'

export type MediaStatus = 'PENDING' | 'READY' | 'QUARANTINED' | 'FAILED' | 'DELETED'

export interface MediaAsset {
  id: string
  product_id: string
  spu: string | null
  source: MediaSource
  /** 角色可空:AI 生成图刻意留 UNSET —— 正面还是背面取决于任务参数,猜一个等于用假依据填空 */
  role: MediaRole | null
  role_source: 'HUMAN' | 'RULE' | 'MODEL' | 'UNSET'
  role_confidence: number | null
  variant_hint: string | null
  /**
   * 这张图归属到哪个颜色(§4.8 的归属外键)。空 = 通用图。
   *
   * **不许和 `variant_hint` 合并。** 那一列是模型猜的「识别建议位」,
   * 这一列是人在上传时指定的归属,而 §6.2 颜色完整度门禁、§5.3 颜色指纹
   * 读的都是这一列。拿 hint 顶上的表现是:界面按 A 色显示的一张图,
   * 在门禁和指纹眼里属于共享作用域 —— 同一张图有两个答案,而两边都不报错。
   */
  color_variant_id: string | null
  storage_path: string
  mime_type: string
  width: number
  height: number
  bytes: number
  sha256: string
  status: MediaStatus
  quarantine_reason: string | null
  /** 从旧表带过来的。角色映射可能有损,页面据此提示复核 */
  legacy_kind: string | null
  created_at: string | null
  /** 短期签名地址,不是永久公开地址 */
  url: string
}

export interface MediaPage {
  items: MediaAsset[]
  total: number
  page: number
  page_size: number
}

export interface MigrationStatus {
  coverage: {
    product_assets: number
    product_assets_shadowed: number
    candidates_with_image: number
    candidates_linked: number
    media_assets: number
  }
  mismatches: Record<string, number>
  window_days: number
}

export interface MediaQuery {
  product_id?: string
  spu?: string
  source?: MediaSource
  role?: MediaRole
  status?: MediaStatus
  /** 带上已删除的素材。默认不带 —— 「删除」的意思就是从眼前拿走 */
  include_deleted?: boolean
  /** 白名单在后端 `media/service.SORTABLE` */
  sort?: string
  order?: 'asc' | 'desc'
  page?: number
  page_size?: number
}

export const mediaApi = {
  list: async (params: MediaQuery = {}): Promise<MediaPage> =>
    (await apiClient.get<MediaPage>('/media', { params })).data,
  get: async (id: string): Promise<MediaAsset> =>
    (await apiClient.get<MediaAsset>(`/media/${id}`)).data,
  setRole: async (id: string, role: MediaRole): Promise<MediaAsset> =>
    (await apiClient.post<MediaAsset>(`/media/${id}/role`, { role })).data,
  quarantine: async (id: string, reason: string): Promise<MediaAsset> =>
    (await apiClient.post<MediaAsset>(`/media/${id}/quarantine`, { reason })).data,
  release: async (id: string): Promise<MediaAsset> =>
    (await apiClient.post<MediaAsset>(`/media/${id}/release`)).data,

  /**
   * 删除一条素材(生成出来的废图、传错的图)。**软删除,且不可撤销。**
   *
   * 与隔离的分工:隔离是「疑似不合规,待复核,可放行」,删除是「确认不要」。
   * 后端把 `status` 迁到 `DELETED`,行与字节都留着 —— 候选图是计费产物,
   * 删掉行之后「这一轮出了几张图、花了多少钱」在台账上会对不上。
   *
   * 是 POST 不是 DELETE,正因为它不删行。界面上必须先确认再调:
   * 没有 restore 接口,点错了只能重新上传或重新出图。
   */
  remove: async (id: string, reason: string): Promise<MediaAsset> =>
    (await apiClient.post<MediaAsset>(`/media/${id}/delete`, { reason })).data,
  migrationStatus: async (): Promise<MigrationStatus> =>
    (await apiClient.get<MigrationStatus>('/media/migration/status')).data,
}
