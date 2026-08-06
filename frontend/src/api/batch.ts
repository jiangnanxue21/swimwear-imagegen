/**
 * 运营工作台阶段 3 接口(批量试运营,任务书 §6)。
 *
 * ## 同一条纪律
 *
 * 和 `workbench.ts` 一样:**这个文件里没有一处判定**。可执行/不可执行、
 * 成功率、可解释比例、能不能重试,全部来自后端 —— 前端只做「枚举 -> 中文」。
 *
 * 理由在阶段 3 更硬:§6.3.2 的签字门槛是「不可解释失败数为 0」。这个数字
 * 如果前端也能算一份,验收会上就会出现两个数,然后花半小时争论该信哪个。
 *
 * 下面几张文案表由 `tests/pure/test_frontend_contract.py` 静态对齐后端枚举,
 * 后端加一个异常码而这里不加,测试立刻失败。
 */
import { adminHeaders, apiClient, LONG_TIMEOUT_MS } from './client'
import type { FlowStep } from './workbench'

// ==========================================================
// 枚举与文案(对齐 app/workbench/batch.py)
// ==========================================================

export type BatchAction = 'EXTRACT' | 'GENERATE_COPY' | 'VALIDATE_DRAFT' | 'EXPORT'

export const BATCH_ACTION_LABEL: Record<BatchAction, string> = {
  EXTRACT: '批量属性识别',
  GENERATE_COPY: '批量生成文案',
  VALIDATE_DRAFT: '批量校验草稿',
  EXPORT: '批量导出',
}

/**
 * 动作说明。挂在批量按钮的 Tooltip 上。
 *
 * 「不批准」那句话必须写出来:运营看到「批量生成文案」很容易以为文案就此完成,
 * 而它的正确终态是「等人批准」。批量批准等于取消审核这一步,系统刻意不提供。
 */
export const BATCH_ACTION_HINT: Record<BatchAction, string> = {
  EXTRACT: '对选中商品逐图跑属性识别。识别结果仍需人工确认',
  GENERATE_COPY: '生成一版文案,不批准。已有人工编辑过的文案不会被覆盖',
  VALIDATE_DRAFT: '生成或重建上架草稿并校验。手填字段不会丢',
  EXPORT: '把校验通过的草稿导出成一份上架文件 + 批次清单',
}

/** 付费动作。界面在确认框里对这两个多问一句。 */
export const PAID_BATCH_ACTIONS: BatchAction[] = ['EXTRACT', 'GENERATE_COPY']

export type BatchItemStatus =
  | 'PENDING'
  | 'RUNNING'
  | 'SUCCEEDED'
  | 'FAILED'
  | 'NEEDS_HUMAN'
  | 'SKIPPED'

/**
 * 条目状态上色。
 *
 * `NEEDS_HUMAN` 用暖色不用红色:它不是错误,是系统正确地停下来等人做决定。
 * 上成红色的后果是运营对着一屏"失败"反复点重试,而重试对它永远无效。
 */
export const BATCH_ITEM_STATUS_LABEL: Record<
  BatchItemStatus,
  { text: string; color: string }
> = {
  PENDING: { text: '待处理', color: 'default' },
  RUNNING: { text: '处理中', color: 'processing' },
  SUCCEEDED: { text: '成功', color: 'success' },
  FAILED: { text: '失败', color: 'error' },
  NEEDS_HUMAN: { text: '需人工', color: 'warning' },
  SKIPPED: { text: '已跳过', color: 'default' },
}

export type BatchJobStatus = 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'COMPLETED_WITH_ERRORS'

/** 批次状态。**没有 FAILED** —— 单件失败不导致整个批次失败(§6.3)。 */
export const BATCH_JOB_STATUS_LABEL: Record<
  BatchJobStatus,
  { text: string; color: string }
> = {
  QUEUED: { text: '排队中', color: 'default' },
  RUNNING: { text: '执行中', color: 'processing' },
  COMPLETED: { text: '已完成', color: 'success' },
  COMPLETED_WITH_ERRORS: { text: '完成(有失败)', color: 'warning' },
}

/**
 * 异常码文案。键与 `app/workbench/batch.py` 的 `BatchErrorCode` 一一对应。
 *
 * 后端每个码都带了 `error_label` 与 `advice` 在出参里,这张表是为了两件事:
 * 计划预览的分组统计要在拿到明细前就能显示中文;以及后端加了新码而这里没加时,
 * 契约测试能立刻指出来 —— 那种情况下界面会显示英文常量,而没人会立刻发现。
 */
export const BATCH_ERROR_LABEL: Record<string, string> = {
  PRECHECK_MISMATCH: '当前不该做这个动作',
  SAME_SPU_DEDUPED: '同 SPU 已有一件在执行',
  ALREADY_EXPORTED: '已导出且无过期',
  NO_USABLE_MATERIAL: '没有可用素材',
  ASSET_ROLE_UNCONFIRMED: '素材角色未确认',
  EXTRACTION_FAILED: '属性识别失败',
  ATTRIBUTE_CONFLICT: '属性有冲突',
  AUDIENCE_UNCONFIRMED: '受众未确认',
  NO_CONFIRMED_ATTRIBUTES: '没有已确认属性',
  ATTRIBUTE_NOT_CREDIBLE: '属性证据不采信',
  IMAGE_SET_NOT_APPROVED: '图片集未批准',
  COPY_GENERATION_FAILED: '文案生成失败',
  COPY_RULE_VIOLATION: '文案校验不通过',
  COPY_NOT_APPROVED: '文案未批准',
  DRAFT_PRECONDITION: '草稿前置条件不满足',
  DRAFT_FIELD_INVALID: '草稿字段不合格',
  DRAFT_STALE: '草稿已过期',
  DRAFT_NOT_EXPORTABLE: '草稿状态不允许导出',
  SPEC_VERSION_MIXED: '批次内导出模板版本不一致',
  EXPORT_WRITE_FAILED: '导出文件写入失败',
  CONCURRENT_IN_FLIGHT: '同一商品正被另一个请求执行',
  WORKER_LOST: '执行中断,已用完自动重试',
  LEASE_LOST: '执行权已交接,本次未执行',
  BILLED_RESULT_UNKNOWN: '费用不明,需人工对账',
  UNEXPLAINED: '未归类的失败',
}

/** 审计对象类型。对齐 `app/workbench/audit_view.py` 的 `ENTITY_LABELS`。 */
export const AUDIT_ENTITY_LABEL: Record<string, string> = {
  Product: '商品',
  ProductAsset: '商品素材',
  MediaAsset: '素材',
  ProductAttributeValue: '属性值',
  ListingImageSet: '图片集',
  ListingCopy: '文案',
  ListingDraft: '上架草稿',
  ContentPlan: '内容计划',
  GenerationTask: '生成任务',
  ReviewItem: '审核项',
  OutputAsset: '成品图',
  ModelTemplate: '模特模板',
  BatchJob: '批量任务',
  PlatformRejection: '平台驳回记录',
  app_setting: '系统设置',
}

/** 业务动作(payload.action)。对齐 `audit_view.PAYLOAD_ACTION_LABELS`。 */
export const AUDIT_ACTION_LABEL: Record<string, string> = {
  /** A45-#35:比对用导出。与 `export` 分开——它不构成"交付给平台"的证据 */
  export_preview: '导出比对文件',
  approve: '批准',
  reject: '驳回',
  build: '生成草稿',
  export: '导出上架文件',
  // 评审第 6 条:生成与下载拆成两个动作。旧的 download 保留给历史记录
  download: '下载批量文件(旧)',
  generate_export_file: '生成批量上架文件',
  download_export_file: '下载批量上架文件',
  // A-21:作废重生成。与"生成"分开一个词,审计里才数得出这份文件被换过几版
  regenerate_export_file: '重新生成批量上架文件',
  retry: '重试',
  cancel: '取消',
  regenerate: '重新生成',
  platform_status: '更新平台状态',
  record_rejection: '记录平台驳回',
  resolve_rejection: '解决平台驳回',
  // 评审第 7 条:声明修复 ≠ 已解决;重新上传是独立动作
  declare_rejection_fixed: '声明已修复,待重新导出',
  resubmit_after_rejection: '确认修复后重新上传',
}

/** 导入行的处理计划。对齐 `import_plan.RowPlan`。 */
export const IMPORT_ROW_PLAN_LABEL: Record<string, { text: string; color: string }> = {
  CREATE: { text: '新增', color: 'success' },
  SKIP_EXISTING: { text: '已存在,跳过', color: 'default' },
  ERROR: { text: '有错,不导入', color: 'error' },
}

// ==========================================================
// 出参类型
// ==========================================================

export interface BatchPlanBlocked {
  product_id: string
  sku: string
  spu: string
  code: string
  code_label: string
  reason: string
  advice: string
  target_step: FlowStep | null
  target_ref: string | null
}

export interface BatchPlanResponse {
  action: BatchAction
  action_label: string
  executable_count: number
  blocked_count: number
  by_code: Record<string, number>
  executable: { product_id: string; sku: string; spu: string }[]
  blocked: BatchPlanBlocked[]
}

/** FE-302 的那一行数字 + §6.3 的两条比率。**全部由后端给出。** */
export interface BatchCounts {
  total: number
  pending: number
  running: number
  succeeded: number
  failed: number
  needs_human: number
  skipped: number
  /** §6.3.2 的签字门槛:必须为 0 */
  unexplained: number
  attempted: number
  paid_calls: number
  reused: number
  /** 分母为 0 时是 null,不是 0 —— 0% 会被读成"全失败" */
  success_rate: number | null
  explainable_rate: number | null
}

export interface BatchItem {
  id: string
  batch_id: string
  product_id: string
  sku: string
  spu: string
  scope_key: string
  status: BatchItemStatus
  status_label: string
  error_code: string | null
  error_label: string | null
  error_message: string | null
  /** §6.3.1 第三条:运营无需开发协助即可判断下一步 */
  advice: string | null
  explainable: boolean
  retryable: boolean
  target_step: FlowStep | null
  target_ref: string | null
  attempts: number
  paid_calls: number
  reused: boolean
  result: Record<string, unknown>
  finished_at: string | null
}

/** 批量导出文件的身份(评审第 6 条:生成一次、之后不可变)。 */
export interface BatchExportFile {
  generated: boolean
  /** 本次调用是否真的产出了新字节。false = 复用已生成的那一份 */
  regenerated: boolean
  file_name: string | null
  file_sha256: string | null
  file_size: number | null
  generated_at: string | null
  generated_by: string | null
  mime: string
  /** 只有 `regenerate` 会回这两个:被换掉的那一版 sha,以及字节是否真的变了 */
  previous_sha256?: string | null
  bytes_changed?: boolean
}

export interface BatchJob {
  id: string
  action: BatchAction
  action_label: string
  status: BatchJobStatus
  status_label: string
  label: string | null
  created_by: string
  created_at: string | null
  started_at: string | null
  finished_at: string | null
  /** 计划阶段的快照:执行前显示的可执行/不可执行数量(FE-301 验收项) */
  params: Record<string, unknown>
  counts: BatchCounts
  /** 有成功条目、**可以去生成**上架文件。不等于已经生成过 */
  downloadable: boolean
  /** A-21:已经生成过了。只有它为真才谈得上「重新生成」 */
  file_generated: boolean
  file_sha256: string | null
  file_name: string | null
  file_generated_at: string | null
  file_generated_by: string | null
  retryable_count: number
  items?: BatchItem[]
  /**
   * 这个批次是不是**投给后台 worker 了**(后端 `BATCH_EXECUTION_MODE=celery`)。
   *
   * ## 上一版漏掉它的后果
   *
   * 后端从建批次那次请求起就返回这个字段,前端类型里没有,于是三件事同时发生:
   *
   *     `dispatched=true`   批次刚排队、一件没跑,而界面照着 counts 说"已执行 N 件"
   *     `dispatched=false`  在 celery 模式下意味着 **broker 投递失败**,
   *                         批次会永远停在 PENDING —— 而界面同样报"已执行"
   *     两种模式无法区分     inline 模式返回的 false 是"已经跑完了",
   *                         celery 模式返回的 false 是"没人会来跑"
   *
   * 所以它不是一个可选的展示字段,是判断"接下来该轮询还是该看结果"的依据。
   * 判定本身仍在后端:前端只读这一位 + `status`,不自己推断执行模式。
   *
   * 可选,因为列表接口不返回它(它描述的是**那一次调用**的投递结果,
   * 不是批次的持久状态)。读到 undefined 时按 `status` 走。
   *
   * **优先读 `dispatch_state`。** 这个布尔留着只是为了兼容还没升级的后端,
   * 见下面那个字段的说明。
   */
  dispatched?: boolean
  /**
   * 那一次调用的投递结果。**这是后端算好的判定,前端只读不推**
   * (`CLAUDE.md` 硬规则第 4 条)。
   *
   *     INLINE_DONE      同步模式,已经跑完了 —— 直接展示统计
   *     QUEUED           异步模式,已排队 —— 开始轮询,别展示统计
   *     DISPATCH_FAILED  异步模式但投递失败 —— 没人会来跑,要报警
   *     NOT_REQUESTED    只建不跑(`?run=false`)—— 既没跑也没排队
   *
   * ## 为什么不能继续用 `dispatched` 自己拼
   *
   * 一个布尔背着三种意思(后端整改报告 6.18「字段过载」),前端只能靠
   * `dispatched === false && isLiveBatchStatus(status)` 去猜是哪一种。
   * 这个猜法在 `NOT_REQUESTED` 上直接猜错:批次停在 QUEUED、`dispatched=false`,
   * 于是界面报 error 级的「broker 投递失败」,而实际上只是没让它跑。
   * 眼下 UI 不发 `run=false` 所以够不到,但这份镜像本身就是硬规则禁止的东西 ——
   * 后端已经把答案算好了,前端再算一遍就是等着两边分叉。
   *
   * 可选:与 `dispatched` 同理,只有那一次写请求的响应里有。
   */
  dispatch_state?: BatchDispatchState
  /**
   * 这一次创建请求**什么都没做**,返回的是同一把 `Idempotency-Key` 上次
   * 建好的那个批次(A45-batch12-3 / EX-06)。
   *
   * 与 `dispatch_state` 同理,只在那一次写请求的响应里。读到 true 时
   * `counts` 描述的是**上一次**的执行结果,不是这一次 —— 界面必须说清楚,
   * 否则运营会以为自己刚刚又跑了一批。
   */
  reused_request?: boolean
  /**
   * 这个批次还会不会自己往前走。**后端算的,前端只读**(硬规则第 4 条)。
   *
   * 和上面那个 `dispatch_state` 的区别是它**每次读取都在**:
   * `dispatch_state` 描述的是"那一次写请求投出去了没有",只在创建/重试的
   * 响应里;`liveness` 是从 `status` / `created_at` / `started_at` 和条目的
   * `finished_at` 现算的,列表和详情都带。
   *
   * 这一位存在的理由就是修掉「Broker 挂着时批次列表永远转圈」:投递失败
   * **不落库**,刷新之后前端只看得到一个 QUEUED 批次,而"非终态就轮询"
   * 会让它转到天荒地老。
   *
   * 可选:老后端不返回。读到 undefined 时退回只按 `status` 判断(= 旧行为)。
   */
  liveness?: BatchLiveness
  liveness_label?: string
  /** STALLED 时后端给的一句可执行的话。前端**不改写它** */
  liveness_advice?: string | null
  stalled_seconds?: number | null
}

/** 与后端 `workbench/batch.py` 的 `BatchLiveness` 同源。 */
export type BatchLiveness = 'SETTLED' | 'LIVE' | 'STALLED'

/** 与后端 `api/workbench_batch.py` 顶部那四个常量同源。 */
export type BatchDispatchState = 'INLINE_DONE' | 'QUEUED' | 'DISPATCH_FAILED' | 'NOT_REQUESTED'

/**
 * 这一次调用之后该怎么办 —— 轮询、看结果,还是报警。
 *
 * 优先用后端给的 `dispatch_state`;字段缺席(后端尚未升级)时退回旧的
 * `dispatched` + `status` 推断,保持两种后端都不说错话。**退路只有这一条,
 * 而且只在这一个函数里** —— 散在各页面里就又变成两份判定了。
 */
export function readDispatchOutcome(job: BatchJob): 'queued' | 'stuck' | 'done' {
  switch (job.dispatch_state) {
    case 'QUEUED':
      return 'queued'
    case 'DISPATCH_FAILED':
      return 'stuck'
    case 'INLINE_DONE':
      return 'done'
    case 'NOT_REQUESTED':
      // 没派也没跑。当作"看结果"处理:它确实不会自己动,而报警是错的
      return 'done'
    default:
      break
  }
  if (job.dispatched === true) return 'queued'
  if (job.dispatched === false && isLiveBatchStatus(job.status)) return 'stuck'
  return 'done'
}

/**
 * 这个批次还会不会自己变(FE-BATCH-01)。
 *
 * 与任务列表同一条纪律(`taskLiveness`):用**终态反推**,而不是维护一份
 * "进行中"白名单。后端加一个中间状态时,漏进白名单的后果是页面停止刷新、
 * 批次看着像卡住了,而且没有人会立刻发现。
 */
const TERMINAL_BATCH_STATUSES: BatchJobStatus[] = ['COMPLETED', 'COMPLETED_WITH_ERRORS']

export function isLiveBatchStatus(status: BatchJobStatus): boolean {
  return !TERMINAL_BATCH_STATUSES.includes(status)
}

/** 卡住的批次隔多久再问一次。慢,但不是停 —— 人点了重试之后它还会动。 */
export const STALLED_BATCH_POLL_MS = 30_000

/**
 * 这个批次该隔多久问一次,`false` = 别问了。
 *
 * 优先读后端的 `liveness`;字段缺席(后端未升级)时退回 `isLiveBatchStatus`,
 * 也就是旧行为。**退路只有这一条,和 `readDispatchOutcome` 同样的纪律** ——
 * 散到各页面里就又是两份判定。
 *
 * STALLED 不停轮询而是放慢:批次卡住的原因(Broker 挂了、worker 没了)
 * 都是会自己恢复的,恢复之后它会继续跑。停掉的话运营要手动刷新才知道,
 * 而页面没有任何地方告诉他需要这么做 —— 那正是 FE-BATCH-01 修过的坑。
 */
export function batchLivenessOf(job: BatchJob): BatchLiveness {
  if (job.liveness) return job.liveness
  // 老后端没有这个字段。退回旧行为(只按状态判终态),此时永远不会是
  // STALLED —— 那是对的:没有依据就不该说"卡住了"
  return isLiveBatchStatus(job.status) ? 'LIVE' : 'SETTLED'
}

export function batchPollInterval(job: BatchJob, liveMs: number): number | false {
  switch (batchLivenessOf(job)) {
    case 'SETTLED':
      return false
    case 'STALLED':
      return STALLED_BATCH_POLL_MS
    case 'LIVE':
      return liveMs
  }
}

/** 一批里最急的那个节拍。`false` = 这一批都不会再自己变。 */
export function batchListPollInterval(jobs: BatchJob[], liveMs: number): number | false {
  const intervals = jobs
    .map((job) => batchPollInterval(job, liveMs))
    .filter((v): v is number => v !== false)
  return intervals.length ? Math.min(...intervals) : false
}

/**
 * 历史批次文件是否还能传平台(OPS-REVIEW P4,对齐 `batch.FileState`)。
 *
 * 判定全在后端 —— 前端连"指纹一样不一样"都不比:过期口径只有
 * `service.refresh_draft` 一处,批次页自己比一次就会出现第二个数字。
 */
export type FileState =
  | 'DRAFT_MISSING'
  | 'UPSTREAM_STALE'
  | 'SUPERSEDED'
  | 'UNKNOWN'
  | 'FRESH'

export const FILE_STATE_LABEL: Record<FileState, string> = {
  DRAFT_MISSING: '草稿已不存在',
  UPSTREAM_STALE: '草稿已过期',
  SUPERSEDED: '已被新版取代',
  UNKNOWN: '无法比对',
  FRESH: '仍然有效',
}

/** 上色。`SUPERSEDED` 用暖色不用红:重导一次就好,不是坏事。 */
export const FILE_STATE_COLOR: Record<FileState, string> = {
  DRAFT_MISSING: 'error',
  UPSTREAM_STALE: 'error',
  SUPERSEDED: 'warning',
  UNKNOWN: 'default',
  FRESH: 'success',
}

export interface FileAuditItem {
  product_id: string
  sku: string
  spu: string
  state: FileState
  state_label: string
  /** 后端给的一句可执行的话。前端不改写它 */
  advice: string
  exported_fingerprint: string | null
  current_fingerprint: string | null
}

export interface FileAudit {
  /** 非导出批次为 false,此时整个结论不显示 */
  applicable: boolean
  /** 一切正常时为 null —— 那时候不该有横幅 */
  banner: string | null
  total: number
  stale_count: number
  unknown_count: number
  usable: boolean
  /** 过期草稿会让「重新下载」被导出闸 409。为 true 时提前把话说了 */
  blocks_redownload: boolean
  items: FileAuditItem[]
}

export interface ReceiptRow {
  action: string
  action_label: string
  scope_key: string
  /** 幂等键前 16 位。**这是这张表唯一可用的行标识**(A45-#5)——
      `(action, scope_key)` 在上游指纹变化后会有多行 */
  idempotency_key: string
  /** 一共花了多少次付费调用。**逐图计费**:一件 4 图商品首次识别就是 4 */
  paid_calls: number
  /**
   * A-17:这条作用域被**真的开跑**过几次。
   *
   * 原来"重复付费"判的是 `paid_calls > 1`,而第 4 条把识别改成逐图计费之后,
   * 多图商品首次执行就会超过 1 —— 每一件都被标成 P0,真的重复付费反而看不见。
   * 次数归次数、钱归钱,异常判据现在看这一列。
   */
  executions: number
  reuse_count: number
  last_actor: string | null
  last_used_at: string | null
  /** 同一动作同一上游被真的执行了两次**且花过钱** —— §7.3 里那个 P0 */
  paid_call_anomaly: boolean
}

export interface ExceptionEntry {
  product_id: string
  sku: string
  spu: string
  step: FlowStep
  level: 'BLOCKING' | 'NEEDS_CONFIRM' | 'REMINDER'
  code: string
  message: string
  hint: string | null
  ref: string | null
  source: 'flow' | 'batch'
  batch_id: string | null
}

export interface ExceptionGroup {
  code: string
  level: 'BLOCKING' | 'NEEDS_CONFIRM' | 'REMINDER'
  message: string
  hint: string | null
  count: number
  entries: ExceptionEntry[]
}

export interface ExceptionSection {
  step: FlowStep
  label: string
  count: number
  blocking_count: number
  groups: ExceptionGroup[]
}

export interface ExceptionResponse {
  summary: Record<string, number>
  sections: ExceptionSection[]
}

export interface SpuSkuRow {
  product_id: string
  sku: string
  name: string
  color: string | null
  size: string | null
  completion: number
  blocking_count: number
  next_action_label: string
}

export interface SpuGroup {
  spu: string
  name: string
  sku_count: number
  /** 各 SKU 的最小值,不是平均值 —— 上架文件是整个 SPU 一起导的 */
  completion: number
  blocking_count: number
  variants: Record<string, string[]>
  common: { field_name: string; value: string | null }[]
  /** 本该一致却不一致的公共属性。**这是要人处理的那一列** */
  inconsistent: { field_name: string; values: { value: string; skus: string[] }[] }[]
  skus: SpuSkuRow[]
}

export interface SpuResponse {
  /** **全量** SPU 数,不是本页条数(A-02:改之前它恒 <= limit) */
  total: number
  page: number
  page_size: number
  /** **全量**口径的告警数,与当前页无关(A-03) */
  inconsistent_spus: number
  items: SpuGroup[]
}

export interface AuditEntry {
  id: string
  at: string | null
  actor: string | null
  action: string
  business_action: string
  action_label: string
  entity_type: string
  entity_label: string
  entity_id: string | null
  request_id: string | null
  summary: string
  payload: Record<string, unknown>
}

export interface AuditResponse {
  total: number
  page: number
  page_size: number
  items: AuditEntry[]
}

export interface ImportProblem {
  field: string
  message: string
}

export interface ImportPreviewRow {
  row_number: number
  plan: string
  plan_label: string
  sku: string
  spu: string
  name: string
  problems: ImportProblem[]
}

export interface ImportPreviewResponse {
  blocked: boolean
  counts: { total: number; will_create: number; will_skip: number; error_rows: number }
  unknown_columns: string[]
  header_problems: { row_number: number; field: string; message: string }[]
  rows: ImportPreviewRow[]
  /** 错误行文件,原始列 + 诊断列。前端存成 Blob 让运营下载 */
  error_csv: string
  committed?: boolean
  created?: number
  skipped_existing?: number
  failed?: number
}

export interface ImportColumn {
  name: string
  label: string
  required: boolean
  enum: string[] | null
}

// ==========================================================
// 请求
// ==========================================================

export const batchApi = {
  /** 只算不做。运营可以反复点,后端这条路径不写库 */
  plan: async (action: BatchAction, productIds: string[], includeExported = false) =>
    (
      await apiClient.post<BatchPlanResponse>('/workbench/batches/plan', {
        action,
        product_ids: productIds,
        include_exported: includeExported,
      })
    ).data,

  /**
   * 建批次。`requestKey` 走 `Idempotency-Key` 头(A45-batch12-3 / EX-06)。
   *
   * 后端从 batch12-2 起就认这个头了,但一直没有人发 —— 于是 0032 那条迁移
   * 的 docstring 里点名的场景(运营双击 -> 两个 BatchJob、两套条目、两份进度)
   * 在界面上照样能复现。这里把它接上。
   *
   * 键由调用方**每次点击**生成一把(见 `utils/requestKey`),不在这一层生成:
   * 这一层每被调用一次就是一次新的 HTTP 请求,而"同一次提交的重发"要复用
   * 同一把键 —— 那个生命周期只有调用方知道。
   *
   * 不传的话行为与从前一字不差。
   */
  create: async (
    action: BatchAction,
    productIds: string[],
    options: {
      label?: string
      includeExported?: boolean
      requestKey?: string
    } = {},
  ) =>
    (
      await apiClient.post<BatchJob>(
        '/workbench/batches',
        {
          action,
          product_ids: productIds,
          label: options.label ?? null,
          include_exported: options.includeExported ?? false,
        },
        options.requestKey
          ? { headers: { 'Idempotency-Key': options.requestKey } }
          : undefined,
      )
    ).data,

  list: async (params: { action?: BatchAction; limit?: number } = {}) =>
    (await apiClient.get<{ items: BatchJob[] }>('/workbench/batches', { params })).data
      .items,

  get: async (batchId: string) =>
    (await apiClient.get<BatchJob>(`/workbench/batches/${batchId}`)).data,

  /**
   * 不传 itemIds = 重试全部可重试项。成功项与「需人工」项后端一律不动。
   *
   * 返回值按 `dispatch_state` 读,和 `create` 一致:排队时 `counts` 是
   * **重置之后的排队快照**而不是执行结果。
   *
   * 前端交付说明里给后端的需求 B-1(重试也按 `BATCH_EXECUTION_MODE` 分模式)
   * 已经在 a28 后端整改的 BLOCK-03 落地,所以 celery 模式下这里现在会拿到
   * `QUEUED` 或 `DISPATCH_FAILED`,不再是同步跑完。字段缺席时
   * `readDispatchOutcome` 仍退回旧推断 —— 老后端也不会显示错话。
   */
  retry: async (batchId: string, itemIds?: string[]) =>
    (
      await apiClient.post<BatchJob>(`/workbench/batches/${batchId}/retry`, {
        item_ids: itemIds ?? null,
      })
    ).data,

  /**
   * 生成批量上架文件(评审第 6 条)。**幂等:已生成则原样返回旧的那一份。**
   *
   * 生成是写动作,所以是 POST。原来生成藏在下载的 GET 里,浏览器预取和
   * 重复点击都会重新产出字节、累加 `export_count` —— 而那个数字是
   * 「这份文件导出过几次」的答案,被改花之后就不可信了。
   */
  generateFile: async (batchId: string): Promise<BatchExportFile> =>
    (
      await apiClient.post<BatchExportFile>(
        `/workbench/batches/${batchId}/file`,
        undefined,
        // A-33:生成要跑完整批的导出闸 + 打包 zip,30s 默认值在几十件的
        // 批次上会先超时。超时之后运营会再点一次,而那正是 A-16 那条竞态
        // 最现实的触发路径(行锁已经挡住了写坏,但"不知道成没成"还在)
        { timeout: LONG_TIMEOUT_MS },
      )
    ).data,

  /**
   * 作废旧的上架文件、重新生成一份(A-21)。**需要管理员口令。**
   *
   * `read_batch_file` 的两处 409 都写着"请重新生成",而在这条接口之前
   * 全仓没有任何再生成入口 —— 运营照着提示做只会撞回同一个 409。
   *
   * 它换掉 `file_sha256`,而那是平台驳回时「我当时传的是这一版」的物证,
   * 所以 `reason` 必填,后端会把 `旧sha → 新sha` 一起写进审计。
   */
  regenerateFile: async (
    batchId: string,
    reason: string,
  ): Promise<BatchExportFile> =>
    (
      await apiClient.post<BatchExportFile>(
        `/workbench/batches/${batchId}/file/regenerate`,
        { reason },
        // A45-#1:**这一行原来不在。**
        //
        // 后端这条路由挂 `require_admin`,而请求拦截器只会带
        // `X-Operator-Token` —— 于是任何存过操作口令的浏览器(也就是全部
        // 正常用户)点"重新生成"都是 403 `AUTH_FORBIDDEN`。而那个错误码被
        // `isAuthError` 刻意排除(A5 的正确设计:它不是"口令错了"),
        // 冷启动横幅也不会亮,运营只看到一句红字。
        //
        // 结果是 A-21 这条**专门为解开 `read_batch_file` 那两处 409 死循环
        // 而加的唯一出口,在最需要它的人手里点不动** —— 修复本身等于没交付。
        //
        // 同类路由(settings / prompts / generation)一直是带的,只有这条漏了。
        { timeout: LONG_TIMEOUT_MS, headers: adminHeaders() },
      )
    ).data,

  /**
   * 下载已生成的 zip(上架 Excel + 批次清单)。**只读,不生成。**
   *
   * 没生成过时后端 409 并提示先点生成 —— 前端不该替它顺手生成一份,
   * 那等于把副作用又搬回 GET 上。
   *
   * 和单件导出同一个约束:`<a href>` 带不了 `X-Operator-Token`,
   * 所以走 axios 取 blob,文件名读后端专门给的 `X-Export-Filename`。
   */
  file: async (batchId: string): Promise<{ blob: Blob; filename: string }> => {
    const response = await apiClient.get<Blob>(
      `/workbench/batches/${batchId}/file`,
      // 大 zip 的下载本身也可能超过默认超时。这一条是只读的,超时了重来无害
      { responseType: 'blob', timeout: LONG_TIMEOUT_MS },
    )
    const header = response.headers as Record<string, string | undefined>
    return {
      blob: response.data,
      filename:
        decodeExportName(header['x-export-filename']) ?? `batch-${batchId.slice(0, 8)}.zip`,
    }
  },

  /**
   * 「上周导的压缩包还能用吗」(P4)。
   *
   * 和 `file` 分开两条请求:这一条只读结论、不生成文件,运营打开批次详情
   * 就该看到答案,而不是先下载 90 MB 再发现里面一半是旧内容。
   */
  fileAudit: async (batchId: string) =>
    (await apiClient.get<FileAudit>(`/workbench/batches/${batchId}/file-audit`)).data,

  receipts: async (limit = 50) =>
    (await apiClient.get<{ items: ReceiptRow[] }>('/workbench/receipts', { params: { limit } }))
      .data.items,

  exceptions: async (params: { include_batch?: boolean; search?: string } = {}) =>
    (await apiClient.get<ExceptionResponse>('/workbench/exceptions', { params })).data,

  /** 翻页在服务端。`limit` 已从参数表里去掉:留着它,调用方一写
      `limit: 100` 就又把\"第 101 个 SPU 起看不见\"带回来了(A-01) */
  spus: async (params: { search?: string; page?: number; page_size?: number } = {}) =>
    (await apiClient.get<SpuResponse>('/workbench/spus', { params })).data,

  audit: async (params: {
    product_id?: string
    actor?: string
    entity_type?: string
    action?: string
    days?: number
    workbench_only?: boolean
    page?: number
    page_size?: number
  }) => (await apiClient.get<AuditResponse>('/workbench/audit', { params })).data,

  importColumns: async () =>
    (await apiClient.get<{ columns: ImportColumn[] }>('/workbench/import/columns')).data
      .columns,

  importTemplate: async (): Promise<{ blob: Blob; filename: string }> => {
    const response = await apiClient.get<Blob>('/workbench/import/template', {
      responseType: 'blob',
    })
    const header = response.headers as Record<string, string | undefined>
    return {
      blob: response.data,
      filename:
        decodeExportName(header['x-export-filename']) ?? 'product-import-template.csv',
    }
  },

  importPreview: async (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return (
      await apiClient.post<ImportPreviewResponse>('/workbench/import/preview', form)
    ).data
  },

  importCommit: async (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return (
      await apiClient.post<ImportPreviewResponse>('/workbench/import/commit', form)
    ).data
  },
}

/**
 * 读后端给的导出文件名。**它是百分号编码的。**
 *
 * HTTP 头按 latin-1 编码,而 SKU / SPU 可以是中文(后端 schema 上只限长度)。
 * 后端 `core/http_headers.py` 因此把原名 `encodeURIComponent` 之后再放进
 * `X-Export-Filename`,这里解回来。
 *
 * 解不开就当没有:`decodeURIComponent` 对残缺的 `%` 会抛 URIError,
 * 而一个文件名解析失败不该让整次下载失败 —— 退回调用方的默认名即可。
 */
export function decodeExportName(raw: string | undefined): string | undefined {
  if (!raw) return undefined
  try {
    return decodeURIComponent(raw) || undefined
  } catch {
    return undefined
  }
}

/** 浏览器另存。文件都要带口令请求头,所以统一走 blob 而不是 `<a href>`。 */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  /**
   * **revoke 不能在同一个 tick 里做。**
   *
   * Chrome 里下载是 `click()` 同步启动的,所以立刻 revoke 也能work ——
   * 这就是它一直没被发现的原因。Firefox 与 Safari 是异步取字节的,
   * URL 在它们读之前被吊销,下载会被静默取消:界面上是「点了没反应」,
   * 控制台里什么都没有。批量导出的压缩包可能几十 MB,正是最容易撞上的那种。
   *
   * 不 revoke 也不行 —— blob 会一直占着内存直到页面关闭,而运营一天要导十几次。
   * 推到下一个宏任务即可:那时浏览器已经接手了这次下载。
   */
  // A45-#22:0ms 只保证"下一个宏任务",而几十 MB 的 zip 在 Safari 上开始读
  // 之前可能还要更久。放宽到 60 秒 —— 比任何一次"开始读"都长得多,又不至于
  // 把 blob 一直挂在内存里;页面在这之前卸载的话整份文档的 object URL 会一起释放。
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000)
}

/** 百分比显示。**null 显示成「—」而不是 0%**(理由见 `BatchCounts.success_rate`)。 */
export function percent(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `${Math.round(value * 1000) / 10}%`
}
