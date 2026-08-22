/**
 * 发布(上架)接口(方案 v4.1 任务 18 / 6.4 节)。**台账里的 B-02 / A45-#8。**
 *
 * 后端这六个端点从任务 18 起就在,而在这个文件之前**前端一个字都没消费** ——
 * 全仓 grep `publish` 只能命中两处无关的注释。后果不是"少一个页面":
 * 上架是这条流水线的**终点**,没有入口意味着人工评测只能走到导出为止,
 * 再往后要拿 curl 打接口,而运营不会。
 *
 * ## 这一层刻意很薄
 *
 * CLAUDE.md 硬规则 4:**前端不许推测状态。** 发布链路是这条规则最容易破的
 * 地方 —— "现在到底怎么样了"散在四张表里(listing / attempt / outbox /
 * rejection),而最贵的那种拼错已经写在后端 `publish_view.py` 顶部:
 * 一次退避耗尽落进 DEAD 的提交,listing 仍写着 SUBMITTING、attempt 仍写着
 * IN_FLIGHT,只看这两样的界面会说「正在处理」,而实际上什么都不会再发生。
 *
 * 所以后端已经把结论算好了,每一行都带 `display_status` / `next_action` /
 * `blocking_reasons` / `allowed_actions`。**这里不做任何派生**,连"能不能点
 * 下架"都读 `allowed_actions` —— 页面按钮的可用性来自后端那一个数组。
 *
 * 于是这个文件里没有一张文案表:状态名、动作名、阻断原因、修复建议全部由
 * 后端给 `*_label` / `advice`。少了这张表就少了一处会和后端漂移的东西。
 */
import { apiClient, LONG_TIMEOUT_MS } from './client'

/** 界面上真的存在的按钮。每一个都对应一个端点(后端 `ActionCode`) */
export type PublishAction =
  | 'SUBMIT'
  | 'DRY_RUN'
  | 'POLL'
  | 'DELIST'
  /**
   * A45-batch12-2 / EX-04:两条异常恢复动作。
   *
   * 它们不是"重试"。两者都让后端**复用原来那条 PublishAttempt 的幂等键**,
   * 所以平台上不会多出一件商品 —— 而这正是它们敢出现在界面上的前提。
   *
   *   RECONCILE  上次提交超时,不知道到没到 -> 带原键去问一次
   *   REDELIVER  退避耗尽落 DEAD,明确知道没到 -> 带原键重新排队
   *
   * 少了这两个按钮,运营要把这件商品弄上架只能改草稿换输入,而那会算出
   * 一把**新的**幂等键 —— 也就是真正会重复上架的那条路。
   */
  | 'RECONCILE'
  | 'REDELIVER'

/**
 * 一条阻断原因。
 *
 * `label` 是"哪里不对",`advice` 是"去哪儿改" —— 两个都由后端给。
 * 前端只做展示:把 advice 改写成自己的话,就等于在前端复制了一份业务规则。
 */
export interface PublishBlocker {
  code: string
  label: string
  advice: string
}

/**
 * 发布清单的一行 / 详情的公共部分。
 *
 * `display_status` 与 `status` 是**两个不同的东西**,不要合并显示:
 * 后者是 `ChannelListing.status`(状态机的 24 个取值之一),前者是给人看的
 * 派生结论,而且多出一个状态机里根本不存在的 `STALLED`(「已停住,不会自动
 * 继续」)。多出来的那一个正是这一层存在的理由。
 */
export interface ChannelListingRow {
  listing_id: string
  product_id: string
  channel: string
  site: string
  shop_id: string
  /** `ChannelListing.status` 原值。诊断用,界面主位显示 `display_status_label` */
  status: string
  external_spu_id: string | null
  /** 平台侧 SKU ID 映射 `{ 本地 sku: 平台 sku_id }`。
   *  后端是 JSONB 字典(`ChannelListing.external_sku_ids`),不是数组 ——
   *  声明成 `string[]` 时 `.map()` 编译得过、运行期拿到的是对象 */
  external_sku_ids: Record<string, string> | null
  /**
   * 这个外部 ID 是 Simulator 造的 —— **真实平台上并不存在这个商品**。
   *
   * 4.1 节 F 要求「真实渠道 / Simulator」在关键页面可见,所以后端给的是一个
   * 布尔而不是一句话:可见的前提是它能被前端拿来做样式。
   */
  simulated: boolean
  last_platform_status: string | null
  last_polled_at: string | null
  next_poll_at: string | null
  poll_attempt_count: number
  test_batch_tag: string | null
  created_at: string | null
  display_status: string
  display_status_label: string
  next_action: string
  next_action_label: string
  blocking_reasons: PublishBlocker[]
  allowed_actions: PublishAction[]
}

export interface PublishAttemptOut {
  id: string
  operation: string
  status: string
  idempotency_key: string | null
  request_fingerprint: string | null
  provider_request_id: string | null
  started_at: string | null
  finished_at: string | null
  error_code: string | null
  error_message: string | null
  /** 已脱敏(后端 `publish_service._safe_snapshot`)。不脱敏的原文不离开后端 */
  request_snapshot: unknown
  response_snapshot: unknown
}

export interface PublishOutboxOut {
  status: string
  attempt_count: number
  next_attempt_at: string | null
  last_error: string | null
}

export interface PublishRejectionOut {
  id: string
  reason_code: string
  platform_note: string | null
  status: string
  located_by: string | null
  created_at: string | null
}

/* ------------------------------------------------------------------
 * 诊断字段的中文名。
 *
 * **这几张表不违反本文件顶部那句「这一层没有文案表」。** 那句话说的是
 * *派生结论* —— 发布到哪一步了、能点什么、为什么不能提交 —— 那些必须
 * 由后端 `publish_view.py` 算完给 `*_label`,前端复述一遍就等于有了
 * 第二份业务规则。
 *
 * 下面这四张表翻译的是**已经写死的历史事实**:这次尝试是创建还是更新、
 * 结局是什么、投递行排到哪了。它们是枚举值,不是结论,后端不会为它们
 * 派生标签,而抽屉里原来直接把 `SUCCEEDED` / `DEAD` 这类词摆给运营看。
 *
 * 查不到时一律回落成原值:后端加了新取值,界面显示的是那个英文词,
 * 而不是空白 —— 空白会让人以为这一格没有数据。
 * ------------------------------------------------------------------ */

/** `PublishOperation`。说「首次上架」而不是「创建」——运营眼里创建的是草稿。 */
export const PUBLISH_OPERATION_LABEL: Record<string, string> = {
  CREATE: '首次上架',
  UPDATE: '更新商品',
  REPUBLISH: '重新上架',
  DELIST: '下架',
}

/** `PublishStatus`。仅用于诊断表格；主状态仍以服务端派生标签为准。 */
export const PUBLISH_STATUS_LABEL: Record<string, string> = {
  DRAFT: '草稿',
  MAPPING: '映射中',
  VALIDATING: '校验中',
  VALIDATED: '校验通过',
  VALIDATION_FAILED: '校验失败',
  REVISING: '修订中',
  DRY_RUN_VALIDATED: '预览校验通过',
  DRY_RUN_COMPLETED: '预览完成',
  UPLOADING_IMAGES: '上传图片中',
  SUBMITTING: '提交中',
  SUBMITTED: '已提交',
  SUBMIT_RESULT_UNKNOWN: '提交结果未知',
  SUBMIT_FAILED: '提交失败',
  PLATFORM_REVIEWING: '平台审核中',
  PLATFORM_REJECTED: '平台驳回',
  LISTED: '已上架',
  UPDATING: '更新中',
  UPDATE_FAILED: '更新失败',
  PAUSED: '已暂停',
  DELISTING: '下架中',
  DELISTED: '已下架',
  REPUBLISHING: '重新上架中',
  CANCELLED: '已取消',
  ARCHIVED: '已归档',
}

/**
 * `PublishAttemptStatus`。
 *
 * `UNKNOWN` 的措辞是这张表里最要紧的一格:它**不是**「失败」。请求发出去了,
 * 平台可能已经收到 —— 说成失败会让运营直接重发,而那正是 7.4 节要防的。
 */
export const PUBLISH_ATTEMPT_STATUS_LABEL: Record<string, string> = {
  PENDING: '待发出',
  IN_FLIGHT: '发送中',
  SUCCEEDED: '平台已接收',
  UNKNOWN: '结果未知',
  FAILED: '平台拒绝',
  ABORTED: '本地放弃(未发出)',
}

/**
 * `PublishOutboxStatus`。
 *
 * `DEAD` 说「重试已用尽,待人工处理」而不是「已死亡」—— 它是一句待办,
 * 不是一个结局:处理完还能重新投递(REDELIVER)。
 */
export const PUBLISH_OUTBOX_STATUS_LABEL: Record<string, string> = {
  PENDING: '排队中',
  LEASED: '投递中',
  DONE: '已投递',
  DEAD: '重试已用尽,待人工处理',
}

/** `RejectionStatus`。前两档都仍然阻断,措辞不能让人误以为已经了结。 */
export const PUBLISH_REJECTION_STATUS_LABEL: Record<string, string> = {
  OPEN: '未处理',
  FIXED_PENDING_EXPORT: '已声明修复,待新导出佐证',
  RESOLVED: '已解决',
}

/** 6.4 的「发布前 / 发布中 / 发布后」三段,后端一次给全 —— 不用拼第二个请求 */
export interface ChannelListingDetail extends ChannelListingRow {
  draft: {
    id: string
    status: string
    locale: string | null
    version: number
    submittable: boolean
  } | null
  latest_attempt: PublishAttemptOut | null
  outbox: PublishOutboxOut | null
  unresolved_rejections: PublishRejectionOut[]
  pollable_statuses: string[]
}

/**
 * 提交的结果通知。**这是提交返回里最重要的东西。**
 *
 * 四种结局里有两种看起来都是"已提交":沿用了在途的那一条,和沿用了
 * **已经死掉**的那一条。后者再点多少次都不会有变化 —— 所以不要只看
 * `reused`,要看 `will_deliver`:那是"接下来会不会真的发一次"的唯一答案。
 */
export interface EnqueueNotice {
  code: string
  message: string
  will_deliver: boolean
}

export interface SubmitResult {
  listing: ChannelListingRow
  operation: string
  idempotency_key: string | null
  dry_run: boolean
  reused: boolean
  reused_terminal: boolean
  notice: EnqueueNotice
  /** 干跑的全部产出就是这份报文。非干跑也给 —— 出事后第一个要问的是"我们发了什么" */
  prepared_request: unknown
}

export interface CleanupRow {
  listing_id: string
  channel: string
  shop_id: string
  site: string
  spu: string
  sku: string
  external_spu_id: string
  status: string
  last_platform_status: string | null
  test_batch_tag: string | null
  created_at: string | null
  simulated: boolean
  /** 还占着平台上的位置 */
  occupying: boolean
  delistable: boolean
  reason: string | null
  /** **必须由人去平台后台核对的一行**:提交结果未知且没有外部 ID。
   *  自动下架处理不了它,但它绝不能从清单上消失 */
  needs_reconcile: boolean
  /** 人拿着去平台后台找这件商品的线索。没有外部 ID 时它是唯一入口 */
  locator: Record<string, unknown>
}

export interface CleanupReport {
  total: number
  simulated: number
  real: number
  occupying: number
  not_delistable: number
  /** 提交结果未知且没有外部 ID 的行数。`clean` 为真要求它也是 0 */
  unreconciled: number
  /** **这次动手真的会排掉几行**(含模拟行)。和 `occupying` 不是一个口径 */
  to_queue: number
  to_skip: number
  clean: boolean
  probe: Record<string, number>
  rows: CleanupRow[]
}

export interface SubmitPayload {
  shop_id: string
  /** 不给由后端按有没有外部 ID 推。DELIST / REPUBLISH 推不出来,必须显式给 */
  operation?: string | null
  dry_run?: boolean
  /**
   * 人工测试期间**务必带上**。
   *
   * 不带的话商品仍然进得了清理清单(靠 shop_id 过滤),但分不出是哪一轮
   * 测试建的,而 4.1 节 H 要的恰恰是"本轮建了哪些"。
   */
  test_batch_tag?: string | null
}

export const publishApi = {
  listings: async (
    params: {
      channel?: string
      shop_id?: string
      status?: string
      test_batch_tag?: string
      page?: number
      page_size?: number
    } = {},
  ) =>
    (
      await apiClient.get<{
        items: ChannelListingRow[]
        total: number
        page: number
        page_size: number
      }>('/publish/listings', { params })
    ).data,

  get: async (listingId: string) =>
    (await apiClient.get<ChannelListingDetail>(`/publish/listings/${listingId}`)).data,

  /**
   * 提交上架。`dry_run: true` 时只构造报文不发送。
   *
   * 用长超时:提交要跑一次渠道映射 + 校验,而超时之后运营会再点一次 ——
   * 幂等键挡得住重复投递,但"不知道成没成"仍然是最难受的状态。
   */
  submit: async (productId: string, payload: SubmitPayload) =>
    (
      await apiClient.post<SubmitResult>(
        `/publish/products/${productId}/submit`,
        payload,
        { timeout: LONG_TIMEOUT_MS },
      )
    ).data,

  /**
   * 立刻问一次平台。
   *
   * 后端走 `poll_one` 而不是 `poll_due`:人点刷新时问的是"平台上现在到底还有
   * 没有这个商品",这个问题对已经 LISTED、甚至 DELISTED 的行同样成立。
   * 没有外部 ID 时后端回 409 而不是静默成功 —— 静默成功会让界面显示
   * "已刷新",而实际上一个字都没问出去。
   */
  poll: async (listingId: string) =>
    (
      await apiClient.post<{ answer: unknown; listing: ChannelListingRow }>(
        `/publish/listings/${listingId}/poll`,
        undefined,
        { timeout: LONG_TIMEOUT_MS },
      )
    ).data,

  /**
   * 带原幂等键去平台确认那次结果未知的提交(EX-04)。
   *
   * `note` 必填,后端 422 挡住空值:这是由人决定"再对外发一次请求"的动作,
   * 事后要能回答是谁、依据什么。前端不重复实现这条校验,只把输入交上去。
   */
  reconcile: async (listingId: string, note: string) =>
    (
      await apiClient.post<{ answer: unknown; listing: ChannelListingRow }>(
        `/publish/listings/${listingId}/reconcile`,
        { note },
        { timeout: LONG_TIMEOUT_MS },
      )
    ).data,

  /** 把已放弃自动投递(DEAD)的记录重新排队。同样复用原键(EX-04) */
  redeliver: async (listingId: string, note: string) =>
    (
      await apiClient.post<{ listing: ChannelListingRow }>(
        `/publish/listings/${listingId}/redeliver`,
        { note },
        { timeout: LONG_TIMEOUT_MS },
      )
    ).data,

  /** 下架。人工测试期间清理测试商品的唯一入口(4.1 节 H) */
  delist: async (listingId: string) =>
    (
      await apiClient.post<{ listing: ChannelListingRow; notice: EnqueueNotice }>(
        `/publish/listings/${listingId}/delist`,
        undefined,
        { timeout: LONG_TIMEOUT_MS },
      )
    ).data,

  /**
   * 本轮测试在平台上建了哪些商品(4.1 节 H 第二条)。
   *
   * **必须给至少一个作用域参数**,否则后端 400。不限定的话这份清单会覆盖
   * 全部渠道的全部商品 —— 而运营会拿着它去平台后台逐条核对,一份混进了
   * 别人商品的清单核不上,然后就没人信它了。
   */
  cleanupInventory: async (params: {
    test_batch_tag?: string
    shop_id?: string
    channel?: string
    include_simulated?: boolean
  }) => (await apiClient.get<CleanupReport>('/publish/cleanup/inventory', { params })).data,
}
