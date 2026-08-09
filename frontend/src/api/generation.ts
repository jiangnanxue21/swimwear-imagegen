import { adminHeaders, apiClient } from './client'
import type {
  ModelTemplate, Page, Provider, ProviderTestResult, Task, TaskDetail,
} from './types'

export interface TaskCreatePayload {
  product_id: string
  mode: string
  provider?: string
  routing_mode?: string
  input_asset_ids?: string[]
  model_template_id?: string | null
  candidate_count?: number
  max_rounds?: number
  base_seed?: number | null
  prompt?: string
  provider_params?: Record<string, unknown>
}

/** 对账结论字数上限。与后端 `Query(max_length=500)` 同源,改一处要改两处。 */
export const RECONCILIATION_NOTE_MAX = 500

export const generationApi = {
  create: async (payload: TaskCreatePayload) =>
    (await apiClient.post<{ task: Task; deduplicated: boolean }>('/generation-tasks', payload)).data,

  list: async (params: {
    product_id?: string
    status?: string
    /** 白名单在后端 `generation_service.TASK_SORTABLE` */
    sort?: string
    order?: 'asc' | 'desc'
    page?: number
    page_size?: number
  }) => (await apiClient.get<Page<Task>>('/generation-tasks', { params })).data,

  get: async (id: string) => (await apiClient.get<TaskDetail>(`/generation-tasks/${id}`)).data,

  cancel: async (id: string) => (await apiClient.post<Task>(`/generation-tasks/${id}/cancel`)).data,

  remove: async (id: string) => {
    await apiClient.delete(`/generation-tasks/${id}`)
  },

  /**
   * 重试。`force` 用来放行「提交结果未知」的任务(后端 `SUBMIT_RESULT_UNKNOWN`)。
   *
   * ## 为什么这个参数必须暴露到界面上
   *
   * 提交阶段超时的任务很可能**已经在 Provider 那边跑起来了**,所以后端对它
   * fail-closed:普通重试一律 409,并在错误里说"先去 Provider 后台核对"。
   * 上一版前端根本没有 force 这个入参 —— 于是那条错误变成一个死胡同:
   * 运营照着提示去核对完了,回来点重试,还是同一个 409,而界面上没有
   * 任何一个按钮能表达"我核对过了"。这类任务因此只能永远停在失败列表里。
   *
   * **默认不带 force。** 它是一个人工判断的出口,不是重试的默认形态:
   * 带上它意味着有人为"Provider 那边确实没有产生结果"背书,
   * 判断错了的代价是再买一次生成。
   *
   * ## `note` 是 force 的必备品,不是可选补充
   *
   * 后端(`services/generation_service.retry_task`)对 `force=true` 要求必填
   * `reconciliation_note`,填不出来就是 422 —— 理由是这是全系统**唯一**一个
   * 由人主动承担重复计费风险的动作,出了重复计费之后,审计里如果只有一条
   * 和自动重试长得一模一样的 "retry",就没人答得上"谁依据什么放的行"。
   *
   * 所以这里不给 `note` 留默认值、也不替运营编一个。调用方必须把这句话
   * 从人嘴里问出来 —— 上限 500 字,与后端 `Query(max_length=500)` 同一个数。
   */
  retry: async (id: string, options: { force?: boolean; note?: string } = {}) => {
    const params: Record<string, string | boolean> = {}
    if (options.force) {
      params.force = true
      const note = (options.note ?? '').trim()
      // 空串不发:发过去也是 422,不如让它在调用点就暴露成编程错误
      if (note) params.reconciliation_note = note.slice(0, RECONCILIATION_NOTE_MAX)
    }
    return (
      await apiClient.post<Task>(`/generation-tasks/${id}/retry`, null, {
        params: Object.keys(params).length ? params : undefined,
      })
    ).data
  },
}

/**
 * 「提交结果未知」的错误码。与后端 `core/enums.SUBMIT_UNKNOWN_CODE` 同值。
 *
 * 前端认它只做一件事:把普通重试换成需要人工背书的强制重试。
 * 状态机判定仍然在后端 —— `can_retry` 是后端给的,这里不重算。
 */
export const SUBMIT_RESULT_UNKNOWN = 'SUBMIT_RESULT_UNKNOWN'

export const providersApi = {
  list: async () => (await apiClient.get<Provider[]>('/providers')).data,

  /** 会拿真 Key 去打厂商接口,后端要口令。 */
  test: async (name: string) =>
    (await apiClient.post<ProviderTestResult>(`/providers/${name}/test`, null, {
      headers: adminHeaders(),
    })).data,
}

export const modelTemplatesApi = {
  /**
   * `forProductAudience` 是 §10.5 的硬约束筛选:给某个受众的商品挑候选时
   * 传商品受众,后端只返回受众匹配的模特。**不匹配的模特不出现在列表里,
   * 不是排在后面** —— 所以这里没有"排序偏好"参数,那个语义不存在。
   *
   * `audience` 则是管理端按模特受众精确筛选,两者别混用。
   */
  list: async (
    enabledOnly = false,
    opts: { audience?: string; forProductAudience?: string } = {},
  ) =>
    (await apiClient.get<ModelTemplate[]>('/model-templates', {
      params: {
        enabled_only: enabledOnly,
        audience: opts.audience,
        for_product_audience: opts.forProductAudience,
      },
    })).data,

  create: async (file: File, fields: Record<string, string | boolean>) => {
    const form = new FormData()
    form.append('file', file)
    Object.entries(fields).forEach(([k, v]) => form.append(k, String(v)))
    return (await apiClient.post<ModelTemplate>('/model-templates', form)).data
  },

  /**
   * 改模特模板,主要是 §11 的授权字段组。
   *
   * ## 为什么这个方法必须存在
   *
   * §11 的十二个字段后端一直能收、能存,`assert_usable` 也真的读其中四个 ——
   * 但在此之前 `modelTemplatesApi` 只有 `list / create / setEnabled`,
   * 后端也没有 PATCH 路由。于是 `license_status` 永远停在 `UNVERIFIED`,
   * 而 `assert_usable` 对非 `LICENSED` 提前 return:**那道授权闸口在产品里
   * 永远不会触发任何一次阻断**。存下来却从不生效的合规记录比没有更糟,
   * 因为它看起来像已经做过了。
   *
   * 受众不在可改字段里:模特的受众是它本人的属性,改它等于换一个人,
   * 而历史图片仍然溯源到这一行。要换受众就新建一个模板。
   */
  update: async (id: string, patch: Record<string, unknown>) =>
    (await apiClient.patch<ModelTemplate>(`/model-templates/${id}`, patch)).data,

  setEnabled: async (id: string, enabled: boolean) =>
    (await apiClient.post<ModelTemplate>(
      `/model-templates/${id}/${enabled ? 'enable' : 'disable'}`,
    )).data,
}
