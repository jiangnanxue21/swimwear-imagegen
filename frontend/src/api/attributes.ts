/**
 * 属性识别接口(FE-201~205,后端 app/api/attributes.py)。
 *
 * 两个置信度不是一回事,界面上不能混着用:
 *
 *     model_confidence    模型自己说的。**只用来展示**
 *     system_confidence   校准后的量。**唯一参与阈值判定的**,未校准时为 null
 *
 * `null` 表示「没校准过」,不是「置信度为零」——属性页要把这两种情况说成不同的话,
 * 否则运营会以为模型判错了,而实际上是校准数据还没灌(那是个运维动作)。
 */
import { apiClient } from './client'

/**
 * 这个字段是什么类型(后端 `attributes/registry.field_spec_out`)。
 *
 * 前端**不推断**它:`Array.isArray(value_json)` 在值为 null 时分不出
 * "空的列表字段"和"空的文本字段",而注册表是这件事的唯一事实源
 * (硬规则第 4 条)。少了它,列表字段会被纯文本输入框回传成字符串。
 */
export interface AttributeFieldSpec {
  /** ENUM / ENUM_LIST / TEXT / TEXT_LIST / NUMBER / BOOL */
  value_type: string
  /** true = 提交成数组 */
  multi_value: boolean
  /** 枚举字段的合法取值;非枚举字段是空数组 */
  options: string[]
}

export interface AttributeValue {
  field_name: string
  value_json: unknown
  normalized_value: string | null
  /**
   * 后端没升级时缺席 —— 那时按"不带约束的文本"处理(见 `AttributeTab`
   * 的 `specOf`)。退路只有那一处,不散在各个控件里
   */
  spec?: AttributeFieldSpec
  /** MODEL / MANUAL / RULE / IMPORT 等。MANUAL 从此不被自动流程覆盖 */
  source: string
  /** CONFIRMED / CANDIDATE / CONFLICT / REJECTED */
  status: string
  model_confidence: number | null
  system_confidence: number | null
  /** 因子分解:回答「0.71 是因为只有一张图,还是因为图太糊」 */
  confidence_breakdown: Record<string, unknown> | null
  evidence_ids: string[]
  /**
   * 这条事实依据的那批样品变了没有(§5.3 / AC-21)。
   *
   * **不要直接读它,走 `factsStale(row)`。** 后端没升级时它缺席,而
   * `undefined` 是假值 —— 直接读的表现是**全库事实显示正常**,
   * 也就是这一整条机制要防的那件事。退路只有 `factsStale` 一处。
   */
  facts_stale?: boolean
  confirmed_by: string | null
  confirmed_at: string | null
}

/**
 * 「这条事实的样品变了没有」的唯一读取点。
 *
 * ## 缺席一律判过期
 *
 * 方向和后端 `AttributeValueOut.facts_stale: bool = True` 一致,而且是同一条
 * 理由:缺席意味着**没有人算过它**,而"没算过"必须显示成"证明不了它仍然
 * 成立"。反过来默认 `false` 的话,一次接口降级的表现是全库事实集体显示正常
 * —— 运营照着一批可能已经不成立的事实去上架,而界面从头到尾没说过一句话。
 *
 * ## 为什么是一个函数而不是 `row.facts_stale ?? true` 散在各处
 *
 * 这个默认值是一个**决定**,不是一个写法。散开写的话,下一个消费点
 * (导出、批量、看板)照着 `row.facts_stale` 抄一行就把方向反过来了,
 * 而两个方向在界面上都"能用"。
 */
export function factsStale(row: Pick<AttributeValue, 'facts_stale'>): boolean {
  return row.facts_stale ?? true
}

export const ATTRIBUTE_STATUS_LABEL: Record<string, { text: string; color: string }> = {
  CONFIRMED: { text: '已确认', color: 'success' },
  CANDIDATE: { text: '待确认', color: 'warning' },
  CONFLICT: { text: '有冲突', color: 'error' },
  REJECTED: { text: '已否决', color: 'default' },
}

export const ATTRIBUTE_SOURCE_LABEL: Record<string, string> = {
  MANUAL: '人工',
  MODEL: '模型',
  RULE: '规则',
  IMPORT: '导入',
}

/**
 * 一次识别 run 的状态。后端 `ExtractionRunStatus` 的镜像(§4.6)。
 *
 * **这一档由后端派生,前端只展示**(硬规则 4)。在它之前这里是
 * `succeeded_count === 0 ? 失败 : failed_count > 0 ? 部分 : 完成` ——
 * 三档全在前端算,而且漏了一种:后端的循环有可能在跑完之前停下
 * (worker 被回收、将来的 cancel),那时失败数是 0 而图没跑完,
 * 上面那串三元运算落在「识别完成」上,于是**一次跑了一半的识别
 * 显示成功**,缺的那几张再没有人回来补。
 */
export type ExtractionRunStatus =
  | 'QUEUED'
  | 'RUNNING'
  | 'COMPLETED'
  | 'PARTIAL_SUCCESS'
  | 'FAILED'
  | 'CANCELLED'

export interface Extraction {
  id: string
  product_id: string
  extractor: string
  model_name: string | null
  prompt_version: string | null
  target_fields: string[]
  image_count: number
  succeeded_count: number
  failed_count: number
  failed_scopes: string[]
  status: ExtractionRunStatus
  cancel_requested: boolean
  can_cancel: boolean
  duration_ms: number | null
  created_at: string | null
}

/**
 * 每一档配一种提示语气与一句**下一步动作**。
 *
 * 语气不是装饰:`error` 会留在屏幕上等人关掉,`success` 两秒就消失。
 * 一次全部失败的识别用 success 弹一下,运营的结论是「识别过了」。
 *
 * `QUEUED` / `RUNNING` 是异步 worker 的真实中间态，是否继续轮询由后端
 * `can_cancel` 给出，前端不维护第二份终态清单。
 */
export const RUN_STATUS_LABEL: Record<ExtractionRunStatus, string> = {
  QUEUED: '已排队',
  RUNNING: '识别中',
  COMPLETED: '识别完成',
  PARTIAL_SUCCESS: '部分识别成功',
  FAILED: '识别失败',
  CANCELLED: '已中止',
}

export const RUN_STATUS_NOTICE: Record<
  ExtractionRunStatus,
  { tone: 'success' | 'warning' | 'error' | 'info'; next: string }
> = {
  QUEUED: { tone: 'info', next: '已排队,稍后自动开始' },
  RUNNING: { tone: 'info', next: '正在识别,可以先做别的' },
  COMPLETED: { tone: 'success', next: '' },
  PARTIAL_SUCCESS: { tone: 'warning', next: '可再次识别只补失败的图片' },
  FAILED: {
    tone: 'error',
    next: '请重试;连续失败请把请求编号提供给管理员',
  },
  CANCELLED: { tone: 'warning', next: '这次识别被中止,证据不会进事实' },
}

export interface Evidence {
  id: string
  media_asset_id: string
  field_name: string
  raw_value_json: unknown
  /** null = 模型自造了一个枚举值,被归一化拦下了。**要显示出来**,它是提示词要改的信号 */
  normalized_value: string | null
  model_confidence: number | null
  evidence_text: string | null
  /**
   * 非空 = 这是一条"没有值"的证据(§4.5),取值见后端 `core.enums.MissingReason`。
   *
   * A45-batch14-2 / F2:这一列在后端从模型一路铺到了 `EvidenceOut`,注释写着
   * 「**界面**按它区分『模型说看不清』和『模型给了个归一不了的值』——
   * 两者的 normalized_value 都是 None,少了这一列它们长得一模一样」,
   * 而前端连类型都没有它。指定了消费者、消费者不存在,和 batch13-3 的 R4
   * 是同一条:看不见等于没有。
   */
  missing_reason: string | null
  model_name: string
}

/**
 * 「没有值」的三个原因,各指向一个**不同的动作**(后端 `MissingReason` 的原话)。
 *
 * 不要合并成一句"模型没识别出来" —— 合并之后运营唯一能做的是重跑一遍识别,
 * 而这三种情况里重跑只对第一种有意义。
 */
export const MISSING_REASON_ACTION: Record<string, { label: string; action: string }> = {
  INSUFFICIENT_EVIDENCE: {
    label: '这张图上看不清',
    action: '换一张更清楚的图再识别',
  },
  NOT_VISUALLY_DETERMINABLE: {
    label: '这个角度判断不了',
    action: '补一张对应角度的图(背面 / 细节 / 吊牌)',
  },
  AWAITING_SUPPLIER_DATA: {
    label: '看图永远判断不了',
    action: '去要供应商结构化数据,或人工填 —— 再识别多少次都不会有值',
  },
}

export interface ExtractionDetail extends Extraction {
  evidence: Evidence[]
}

export const attributesApi = {
  list: async (productId: string) =>
    (await apiClient.get<AttributeValue[]>(`/products/${productId}/attributes`)).data,

  /**
   * 触发识别。`colorVariantIds` 用于**按颜色重试**(§11 第一行)。
   *
   * 上一次 run 的 `retry_scope` 已经算出该重跑哪几个颜色,在此之前
   * 那个答案回到界面上没有任何入口能用,运营只能人工挑图 id ——
   * 而挑漏一张的表现是该颜色重试完仍然缺证据,于是再付一次钱。
   *
   * 识别是**付费链路**,所以这里不给默认值:不传 = 整件商品重跑,
   * 那是一个更贵的选择,必须由调用点显式做出来。
   */
  extract: async (
    productId: string,
    fields?: string[],
    colorVariantIds?: string[],
    spuId?: string | null,
  ) =>
    (
      await apiClient.post<Extraction>(
        spuId
          ? `/spus/${spuId}/attribute-extraction-runs`
          : `/products/${productId}/extract-attributes`, {
        fields: fields ?? null,
        media_asset_ids: null,
        color_variant_ids: colorVariantIds ?? null,
      })
    ).data,

  cancel: async (extractionId: string) =>
    (
      await apiClient.post<Extraction>(
        `/attribute-extraction-runs/${extractionId}/cancel`,
      )
    ).data,

  /** 人工确认 / 改值 / 裁决冲突。写入 MANUAL,留痕在后端(BE-206) */
  confirm: async (productId: string, items: { field_name: string; value: unknown }[]) =>
    (
      await apiClient.post<AttributeValue[]>(`/products/${productId}/attributes/confirm`, {
        items,
      })
    ).data,

  extraction: async (extractionId: string) =>
    (
      await apiClient.get<ExtractionDetail>(
        `/attribute-extraction-runs/${extractionId}`,
      )
    ).data,
}
