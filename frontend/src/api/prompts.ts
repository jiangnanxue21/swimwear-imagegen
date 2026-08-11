import { apiClient} from './client'
import type { PromptOverview, PromptPreview } from './types'

/** 提示词决定评分口径,读写都要口令 —— 能改它的人等于能改「什么图算合格」。 */
export const promptsApi = {
  read: async (key: string) =>
    (await apiClient.get<PromptOverview>(`/prompts/${key}`)).data,

  /** 体检,不写库。保存和「看看这版有没有问题」是两件事。 */
  preview: async (key: string, content: string) =>
    (
      await apiClient.post<PromptPreview>(
        `/prompts/${key}/preview`,
        { content },
      )
    ).data,

  /**
   * 保存一版。
   *
   * **没有 `updatedBy` 入参**:后端在 6.11 那轮把 `updated_by` 从请求体里
   * 移掉了,署名改用 `require_admin` 解析出的 actor —— 否则任何持管理口令的人
   * 都能把一次改动签成别人的名字。
   *
   * 这个入参在 a30 之后又留了一轮:pydantic 默认 ignore 多余字段,所以传了
   * 也不报错,只是**静默无效**。一个不报错的假契约比一个报错的坏契约更贵:
   * 下一个人会照着它去传,然后花半天查"为什么署名没生效"。
   */
  save: async (key: string, content: string, note?: string) =>
    (
      await apiClient.put<PromptOverview>(
        `/prompts/${key}`,
        { content, note: note || null },
      )
    ).data,

  activate: async (key: string, version: number) =>
    (
      await apiClient.post<PromptOverview>(
        `/prompts/${key}/activate`,
        { version },
      )
    ).data,

  reset: async (key: string) =>
    (await apiClient.post<PromptOverview>(`/prompts/${key}/reset`, null)).data,
}

export const VISION_SYSTEM_PROMPT = 'vision_system_prompt'
