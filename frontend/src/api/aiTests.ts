import { apiClient } from './client'
import type { AiTestRunList } from './types'

/**
 * AI 测试留档的读接口(BE-311)。
 *
 * **它存在的理由是页面上那句「请保持页面打开,不要刷新」。** 在留档之前,
 * 文案测试的输出全仓一行都没存 —— 关掉页面就没了,而那句提示正是这件事的自述。
 */
export const aiTestsApi = {
  list: async (params: {
    kind?: string
    prompt_key?: string
    prompt_version?: string
    prompt_template_version?: string | number
    limit?: number
    offset?: number
  } = {}) =>
    (await apiClient.get<AiTestRunList>('/ai-tests/runs', { params })).data,
}
