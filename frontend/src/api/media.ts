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
  migrationStatus: async (): Promise<MigrationStatus> =>
    (await apiClient.get<MigrationStatus>('/media/migration/status')).data,
}
