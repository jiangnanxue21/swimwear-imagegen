import { apiClient, LONG_TIMEOUT_MS } from './client'
import type {
  Evaluation, EvaluationAttempt, EvaluationDiagnosticResult, Evaluator, Page, Review,
  ReviewDetail, RuleSet,
} from './types'

export interface ReviewListParams {
  status?: string
  reason?: string
  product_id?: string
  /** 白名单在后端 `review_service.REVIEW_SORTABLE` */
  sort?: string
  order?: 'asc' | 'desc'
  page?: number
  page_size?: number
}

export interface ReviewDecisionPayload {
  note?: string
  tags?: string[]
}

export interface ReviewApprovePayload extends ReviewDecisionPayload {
  /** 不传则采用系统挑出的最佳候选。 */
  candidate_id?: string
}

export interface ReviewRegeneratePayload extends ReviewDecisionPayload {
  /** 留空沿用原 Provider。 */
  provider?: string
  /** 任务本来就是轮次耗尽才进的队列,不追加轮次会立刻弹回来。 */
  extra_rounds?: number
}

export const reviewsApi = {
  list: async (params: ReviewListParams) =>
    (await apiClient.get<Page<Review>>('/reviews', { params })).data,

  get: async (id: string) => (await apiClient.get<ReviewDetail>(`/reviews/${id}`)).data,

  approve: async (id: string, payload: ReviewApprovePayload) =>
    (await apiClient.post<Review>(`/reviews/${id}/approve`, payload)).data,

  reject: async (id: string, payload: ReviewDecisionPayload) =>
    (await apiClient.post<Review>(`/reviews/${id}/reject`, payload)).data,

  regenerate: async (id: string, payload: ReviewRegeneratePayload) =>
    (await apiClient.post<Review>(`/reviews/${id}/regenerate`, payload)).data,
}

/** 只读:分档标准与当前评分器。运营需要能自己查「这张图为什么被判 C」。 */
export const evaluationApi = {
  ruleSets: async () => (await apiClient.get<RuleSet[]>('/rule-sets')).data,

  evaluators: async () => (await apiClient.get<Evaluator[]>('/evaluators')).data,

  forTask: async (taskId: string) =>
    (await apiClient.get<Evaluation[]>(`/generation-tasks/${taskId}/evaluations`)).data,

  attemptsForTask: async (taskId: string) =>
    (await apiClient.get<EvaluationAttempt[]>(
      `/generation-tasks/${taskId}/evaluation-attempts`,
    )).data,

  testCandidate: async (candidateId: string, confirmBillable: boolean) =>
    (
      await apiClient.post<EvaluationDiagnosticResult>(
        `/generation-candidates/${candidateId}/evaluation-test`,
        { confirm_billable: confirmBillable },
        { timeout: LONG_TIMEOUT_MS },
      )
    ).data,
}
