import { apiClient} from './client'
import type { SettingsOverview, SettingsWriteResult } from './types'

/** 三个接口都要口令 —— 读接口返回的密钥末位与内网白名单同样不该开放。 */
export const settingsApi = {
  read: async () =>
    (await apiClient.get<SettingsOverview>('/settings')).data,

  /** 只送改动过的项。空字符串 = 删掉覆盖,回到环境变量/默认值。 */
  update: async (values: Record<string, string>) =>
    (await apiClient.put<SettingsWriteResult>('/settings', { values }))
      .data,

  reset: async (keys: string[]) =>
    (await apiClient.post<SettingsWriteResult>('/settings/reset', { keys })).data,
}
