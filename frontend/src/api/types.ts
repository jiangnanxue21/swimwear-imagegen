export type ProductStatus =
  | 'DRAFT' | 'READY' | 'GENERATING' | 'NEEDS_REVIEW' | 'COMPLETED' | 'ARCHIVED'

export type AssetType =
  | 'GARMENT_FRONT' | 'GARMENT_BACK' | 'GARMENT_DETAIL'
  | 'GARMENT_CUTOUT' | 'MODEL_REFERENCE' | 'BACKGROUND_REFERENCE'

export interface Product {
  id: string
  spu: string
  sku: string
  name: string
  category: string
  /**
   * 受众(PRD v2 §3.4)。**`null` = 待确认,不是 UNISEX。**
   *
   * 后端 `ProductOut extends ProductBase`,这个字段一直是实打实下发的 ——
   * 之前前端类型里没有它,后果不是"少一个字段":`ProductFormModal` 因此
   * 没有受众输入项,于是**界面上无法给商品设置或修改受众**,受众只能
   * 靠 CSV 导入一次性写对。导入错了、或走界面新建的商品,没有任何补救路径。
   */
  audience: Audience | null
  primary_color: string
  secondary_colors: string[]
  pattern_type: string
  garment_type: string
  material: string
  strap_type: string
  neckline_type: string
  back_style: string
  complexity: string
  status: ProductStatus
  notes: string | null
  created_at: string
  updated_at: string
  asset_count: number
}

export interface Asset {
  id: string
  product_id: string
  asset_type: AssetType
  file_hash: string
  mime_type: string
  file_size: number
  width: number
  height: number
  storage_path: string
  original_filename: string
  check_passed: boolean
  check_message: string | null
  created_at: string
  url: string
}

export interface Page<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

export interface ImportResult {
  created: number
  skipped_existing: number
  failed: number
  errors: { row_number: number; field: string; message: string }[]
  created_ids: string[]
}

export const ASSET_TYPE_LABEL: Record<AssetType, string> = {
  GARMENT_FRONT: '商品正面',
  GARMENT_BACK: '商品背面',
  GARMENT_DETAIL: '商品细节',
  GARMENT_CUTOUT: '透明底图',
  MODEL_REFERENCE: '模特参考',
  BACKGROUND_REFERENCE: '背景参考',
}

export const STATUS_LABEL: Record<ProductStatus, { text: string; color: string }> = {
  DRAFT: { text: '素材待补', color: 'default' },
  READY: { text: '可生成', color: 'blue' },
  GENERATING: { text: '生成中', color: 'processing' },
  NEEDS_REVIEW: { text: '待人工审核', color: 'warning' },
  COMPLETED: { text: '已完成', color: 'success' },
  ARCHIVED: { text: '已归档', color: 'default' },
}

/** 选项来自后端枚举,前端不重复定义业务规则,只做展示。 */
export const GARMENT_TYPES = [
  // 女装(§2.2)
  'ONE_PIECE', 'BIKINI_TOP', 'BIKINI_BOTTOM', 'BIKINI_SET',
  'TANKINI', 'COVER_UP', 'DRESS', 'TOP',
  // 男装(§2.2)
  'SWIM_SHORTS', 'BOARDSHORTS', 'SWIM_BRIEFS', 'JAMMERS', 'SWIM_TRUNKS_SET',
  // 中性(§2.2)
  'RASH_GUARD',
  'OTHER',
] as const

/**
 * 受众(PRD v2 §2.3)。**取值和文案都来自后端**,前端只展示。
 *
 * `null` = 待确认,不是 UNISEX(§3.4 规则 3)—— UNISEX 是一个明确的
 * 业务判断,"没填"必须和它区分开。
 *
 * §21.3:受众用**文字标签**,不用性别符号或颜色编码。符号跨站点含义
 * 不稳定,粉/蓝在部分市场是负面信号,而这个系统的输出最终面向多个国家。
 * 所以这张表只有文字,没有 color 字段 —— 别顺手加一个。
 */
export type Audience = 'WOMEN' | 'MEN' | 'UNISEX'
export const AUDIENCES = ['WOMEN', 'MEN', 'UNISEX'] as const
export const AUDIENCE_LABEL: Record<Audience, string> = {
  WOMEN: '女装',
  MEN: '男装',
  UNISEX: '中性',
}

/**
 * 模特受众:**只有两个取值**(§10.5)。
 * UNISEX 是商品的取值,模特本人总有受众 —— 后端 create 会拒绝 UNISEX。
 */
export const MODEL_AUDIENCES = ['WOMEN', 'MEN'] as const

/**
 * 受众 -> 该受众下有意义的服装类型(§2.2)。
 *
 * `GARMENT_TYPES` 是**全集**(与后端枚举逐值一致,由契约测试守着);
 * 这张表只管**展示时按受众收窄** —— 与体型词表(§10.4)同一个道理:
 * 给一件男装商品选到 `BIKINI_SET` 不应该是一个可以做到的操作。
 *
 * 中性款取两侧的交集加中性专属项:一件中性款可能是防晒衣,
 * 也可能按实际穿着形态归到任一侧(§2.2 / §14.3)。
 *
 * **这不是业务规则的第二份定义。** 后端仍按 `GarmentType` 枚举全集
 * 接收,这里收窄的只是下拉框里能点到什么;受众未确认时退回全集,
 * 因为那时"该出哪一组"这个问题本身还没有答案。
 */
export const GARMENT_TYPES_BY_AUDIENCE: Record<Audience, readonly string[]> = {
  WOMEN: [
    'ONE_PIECE', 'BIKINI_TOP', 'BIKINI_BOTTOM', 'BIKINI_SET',
    'TANKINI', 'COVER_UP', 'DRESS', 'TOP', 'RASH_GUARD', 'OTHER',
  ],
  MEN: [
    'SWIM_SHORTS', 'BOARDSHORTS', 'SWIM_BRIEFS', 'JAMMERS',
    'SWIM_TRUNKS_SET', 'RASH_GUARD', 'OTHER',
  ],
  UNISEX: ['RASH_GUARD', 'COVER_UP', 'TOP', 'OTHER'],
}

/**
 * 受众 -> 可选体型(§10.4)。与后端 `BODY_TYPES_FOR_AUDIENCE` 一一对应,
 * 由 `tests/pure/test_audience_rules.py` 的跨语言契约测试守着。
 *
 * "给男装模特打上 CURVY 不应该是一个可以做到的操作" —— 这张表让那个
 * 选项在下拉框里就不存在;后端仍有一道同样的校验(前端只是就近报错)。
 */
export const BODY_TYPES_BY_AUDIENCE: Record<'WOMEN' | 'MEN', readonly string[]> = {
  WOMEN: ['SLIM', 'STRAIGHT', 'CURVY', 'ATHLETIC', 'PLUS', 'UNKNOWN'],
  MEN: ['SLIM', 'REGULAR', 'ATHLETIC', 'MUSCULAR', 'PLUS', 'UNKNOWN'],
}

/** 模特授权状态(§11)。UNVERIFIED = 未核实,不阻断新任务但要补。 */
export const MODEL_LICENSE_STATUS_LABEL: Record<string, string> = {
  UNVERIFIED: '未核实',
  LICENSED: '已授权',
  EXPIRED: '已过期',
  REVOKED: '已撤销',
}

export const PATTERN_TYPES = [
  'SOLID', 'STRIPE', 'FLORAL', 'ANIMAL', 'GEOMETRIC',
  'TIE_DYE', 'COLOR_BLOCK', 'PRINT_LOGO', 'OTHER',
] as const

export const COMPLEXITIES = ['SIMPLE', 'MEDIUM', 'COMPLEX', 'UNKNOWN'] as const

/**
 * 商品字段长度上限。与后端 `app/core/field_limits.py` 的 PRODUCT_FIELD_LIMITS
 * 一一对应,由 `tests/pure/test_frontend_contract.py` 静态比对守住 ——
 * 两边改任何一个数字,契约测试都会红。
 *
 * 前端限长只是把错误说在输入框旁边(评审第 5 条);后端仍按同一份限额
 * 逐字段校验,这里**不发明**后端没有的约束(比如 SPU 字符集)——
 * 前端比后端严,合法值会在界面被拦;前端比后端松,错误要到提交才暴露。
 */
export const PRODUCT_FIELD_LIMITS = {
  spu: 64,
  sku: 64,
  name: 255,
  category: 64,
  primary_color: 64,
  material: 128,
} as const

/** 辅助颜色:最多几个、每个多长。同样对齐后端 field_limits。 */
export const MAX_SECONDARY_COLORS = 10
export const MAX_SECONDARY_COLOR_LENGTH = 64

// ===== 阶段 3:生成任务 =====

export type TaskStatus =
  | 'CREATED' | 'QUEUED' | 'PREPROCESSING' | 'SUBMITTING' | 'PROVIDER_RUNNING'
  | 'DOWNLOADING' | 'SCORING' | 'REGENERATING' | 'AUTO_APPROVED' | 'AUTO_REJECTED'
  | 'MANUAL_REVIEW' | 'MANUALLY_APPROVED' | 'MANUALLY_REJECTED' | 'FORMATTING'
  | 'COMPLETED' | 'FAILED' | 'CANCELLED'

/**
 * 终态:任务到了这里就不会再变,页面可以彻底停止轮询。
 *
 * **这份清单必须和后端 `state_machine.TERMINAL_STATES` 一字不差**,
 * 由 `tests/pure/test_frontend_contract.py` 逐值比对钉住。
 *
 * 它曾经多出 `FAILED` 与 `MANUAL_REVIEW` 两个值,而后端这两个都还有出边:
 *
 *     FAILED        -> QUEUED / FORMATTING / SCORING   (重试与两条续跑边)
 *     MANUAL_REVIEW -> MANUALLY_APPROVED / MANUALLY_REJECTED / REGENERATING
 *
 * 后果不是"多刷几次",是**页面永远停在旧状态**:另一个人(或批量重试)
 * 把任务推进之后,这一边的界面再也不会知道 —— 而这正是"同一个任务、
 * 两个页面两种事实"那一类缺陷,只是这次两个事实分别在两个人的屏幕上。
 *
 * 刻意仍用「终态」反推「要轮询的状态」:后端加一个中间状态时,漏进白名单的
 * 后果是页面停止刷新、任务看着像卡住;反过来写,最坏后果只是多刷新几次。
 */
export const TERMINAL_TASK_STATUSES: TaskStatus[] = [
  'COMPLETED', 'CANCELLED', 'AUTO_REJECTED', 'MANUALLY_REJECTED',
]

/**
 * 等人动:机器已经不再写这个任务了,但它**不是终态** —— 有人来点一下就会继续走。
 *
 * 这一档存在的理由是"停止轮询"和"降低频率"是两个结论。这两个状态下
 * 每 2.5 秒问一次纯属浪费(没有任何后台进程会改它),但一次都不问是错的
 * (审核台批准、列表页重试、批量重试都会改它,而且往往是另一个人在改)。
 *
 * **这份清单必须和后端 `state_machine.AWAITING_HUMAN_STATES` 一字不差**,
 * 由 `tests/pure/test_frontend_contract.py` 逐值比对钉住 —— 和上面那份终态
 * 清单同样的纪律。
 *
 * 它曾经只是一份写在这里的前端清单,契约测试只做负向约束(断言它不含终态、
 * 不含后端 `STALLABLE_STATUSES`)。那挡得住"放错值",挡不住**后端新增一个
 * 等人动的状态而这里不知道**:新状态既不是终态也不在 worker 持有清单里,
 * 负向约束一条都不会红,而它会掉进 LIVE 档被每 2.5 秒问一次 ——
 * 而没有任何后台进程会改它。清单钉到后端之后,那种情况直接是一条红测试。
 */
export const AWAITING_HUMAN_TASK_STATUSES: TaskStatus[] = ['MANUAL_REVIEW', 'FAILED']

/** 等人动那一档的轮询节拍。慢,但不是停。 */
export const AWAITING_HUMAN_POLL_MS = 30_000

export type TaskLiveness = 'LIVE' | 'AWAITING_HUMAN' | 'TERMINAL'

export function taskLiveness(status: TaskStatus): TaskLiveness {
  if (TERMINAL_TASK_STATUSES.includes(status)) return 'TERMINAL'
  if (AWAITING_HUMAN_TASK_STATUSES.includes(status)) return 'AWAITING_HUMAN'
  return 'LIVE'
}

/**
 * 这个状态下该隔多久问一次,`false` = 别问了。
 *
 * `liveMs` 由调用方给:列表页 3 秒、详情页 2.5 秒、评分 4 秒,各页正常节拍不同。
 * 慢档是共享常量 —— 它由"人的反应速度"决定,和是哪个页面无关。
 */
export function taskPollInterval(status: TaskStatus, liveMs: number): number | false {
  switch (taskLiveness(status)) {
    case 'LIVE':
      return liveMs
    case 'AWAITING_HUMAN':
      return AWAITING_HUMAN_POLL_MS
    case 'TERMINAL':
      return false
  }
}

/**
 * 一批任务里最急的那个节拍。`false` = 这一批都不会再自己变。
 *
 * 取最小值而不是"有一个活的就用快档":一屏里 19 个待审核 + 1 个正在跑的时候,
 * 该跟的是那个正在跑的。
 */
export function listPollInterval(statuses: TaskStatus[], liveMs: number): number | false {
  const intervals = statuses
    .map((s) => taskPollInterval(s, liveMs))
    .filter((v): v is number => v !== false)
  return intervals.length ? Math.min(...intervals) : false
}

export type GenerationMode = 'virtual_try_on' | 'product_to_model'
export type RoutingMode = 'MANUAL' | 'PRIORITY' | 'FALLBACK'

export interface Task {
  id: string
  product_id: string
  model_template_id: string | null
  mode: GenerationMode
  provider: string
  routing_mode: RoutingMode
  candidate_count: number
  current_round: number
  max_rounds: number
  status: TaskStatus
  error_code: string | null
  error_message: string | null
  external_task_id: string | null
  idempotency_key: string
  base_seed: number | null
  prompt: string | null
  prompt_version: string
  created_at: string
  started_at: string | null
  finished_at: string | null
  can_cancel: boolean
  can_retry: boolean
  can_delete: boolean
}

export interface Attempt {
  id: string
  round_number: number
  attempt_number: number
  provider: string
  external_task_id: string | null
  seed: number | null
  status: string
  error_code: string | null
  error_message: string | null
  regeneration_reason: string | null
  candidate_count: number
  duration_ms: number | null
  created_at: string
}

export interface Candidate {
  id: string
  round_number: number
  candidate_index: number
  external_id: string | null
  storage_path: string | null
  width: number | null
  height: number | null
  file_size: number | null
  seed: number | null
  status: string
  overall_score: number | null
  grade: string | null
  error_message: string | null
  created_at: string
  url: string
}

export interface TaskDetail extends Task {
  attempts: Attempt[]
  candidates: Candidate[]
  dispatch_status: string | null
  dispatch_attempts: number
  dispatch_error: string | null
}

export interface Provider {
  name: string
  display_name: string
  configured: boolean
  implemented: boolean
  capabilities: {
    supported_modes: string[]
    supports_multiple_candidates: boolean
    supports_seed: boolean
    supports_cancel: boolean
    supports_webhook: boolean
    max_candidates_per_call: number
  }
}

export interface ProviderTestResult {
  provider: string
  configured: boolean
  reachable: boolean | null
  message: string
  base_url?: string | null
  workflow_file?: string | null
  /** FASHN:当前使用的试穿模型(tryon-max / tryon-v1.6) */
  tryon_model?: string | null
}

export interface ModelTemplate {
  id: string
  name: string
  storage_path: string
  /** §10.3:模特卡片必须展示。后端 NOT NULL,这里也不给可选 */
  audience: Audience
  pose: string
  body_type: string
  background: string
  tags: string[]
  notes: string | null
  enabled: boolean
  width: number
  height: number
  created_at: string
  /** ---- §11 授权字段(管理端展示;普通运营端只看"可用/暂不可用")---- */
  source?: string
  license_status?: string
  commercial_scope?: string | null
  allowed_platforms?: string[]
  allowed_regions?: string[]
  license_start_at?: string | null
  license_expires_at?: string | null
  allow_ai_dressing?: boolean
  allow_derivative_images?: boolean
  age_verified?: boolean
  prohibited_categories?: string[]
  disabled_reason?: string | null
  url: string
}

/** 状态展示文案。用运营看得懂的说法,不直接抛英文枚举。 */
export const TASK_STATUS_LABEL: Record<TaskStatus, { text: string; color: string }> = {
  CREATED: { text: '已创建', color: 'default' },
  QUEUED: { text: '排队中', color: 'default' },
  PREPROCESSING: { text: '准备素材', color: 'processing' },
  SUBMITTING: { text: '提交中', color: 'processing' },
  PROVIDER_RUNNING: { text: '生成中', color: 'processing' },
  DOWNLOADING: { text: '下载结果', color: 'processing' },
  SCORING: { text: '等待评分', color: 'warning' },
  REGENERATING: { text: '待重新生成', color: 'warning' },
  AUTO_APPROVED: { text: '自动通过', color: 'success' },
  AUTO_REJECTED: { text: '自动淘汰', color: 'error' },
  MANUAL_REVIEW: { text: '待人工审核', color: 'warning' },
  MANUALLY_APPROVED: { text: '人工通过', color: 'success' },
  MANUALLY_REJECTED: { text: '人工驳回', color: 'error' },
  FORMATTING: { text: '生成多尺寸', color: 'processing' },
  COMPLETED: { text: '已完成', color: 'success' },
  FAILED: { text: '失败', color: 'error' },
  CANCELLED: { text: '已取消', color: 'default' },
}

export const MODE_LABEL: Record<GenerationMode, string> = {
  virtual_try_on: '虚拟试穿',
  product_to_model: '商品转模特图',
}

export const MOCK_OUTCOMES = [
  { value: 'success', label: '成功(4 张候选)' },
  { value: 'fail', label: '生成失败' },
  { value: 'timeout', label: '超时' },
  { value: 'empty', label: '无候选返回' },
  { value: 'rate_limit', label: '限流' },
  { value: 'safety', label: '内容安全拦截' },
]

export const POSES = ['STANDING_FRONT', 'STANDING_BACK', 'THREE_QUARTER', 'SITTING', 'WALKING', 'UNKNOWN'] as const
/**
 * 体型全集。**展示与筛选请用 `BODY_TYPES_BY_AUDIENCE`**(§10.4):
 * 这张全集表里 CURVY 只对女装有意义、REGULAR/MUSCULAR 只对男装有意义。
 */
export const BODY_TYPES = [
  'SLIM', 'STRAIGHT', 'CURVY', 'PLUS', 'ATHLETIC', 'REGULAR', 'MUSCULAR', 'UNKNOWN',
] as const

// ===== 阶段 4:评分与人工审核 =====

export type Grade = 'A' | 'B' | 'C' | 'D'
export type EvaluationDepth = 'FULL' | 'QUICK'
export type ProblemSeverity = 'MINOR' | 'MAJOR' | 'CRITICAL'
export type RecommendedAction =
  | 'AUTO_APPROVE' | 'REGENERATE_TUNED' | 'REGENERATE_RESEED' | 'REJECT' | 'MANUAL_REVIEW'

export type ReviewStatus =
  | 'PENDING' | 'APPROVED' | 'REJECTED' | 'REGENERATION_REQUESTED' | 'CANCELLED'

export type ReviewReason =
  | 'ROUNDS_EXHAUSTED' | 'PROVIDER_EXHAUSTED' | 'REQUIRES_HUMAN_ERROR'
  | 'SPOT_CHECK' | 'MANUAL_REQUEST'

export interface EvaluationProblem {
  code: string
  severity: ProblemSeverity
  dimension: string | null
  message: string
  hard_fail: boolean
}

export interface Evaluation {
  id: string
  candidate_id: string
  round_number: number
  evaluator: string
  model_name: string | null
  depth: EvaluationDepth
  hard_fail: boolean
  hard_fail_codes: string[]
  scores: Record<string, number>
  overall_score: number
  /** 评分器自报的总分。与 overall_score 的差值用来监控大模型打分漂移。 */
  model_reported_overall: number | null
  grade: Grade
  recommended_action: RecommendedAction
  grade_reasons: string[]
  uncertain_items: string[]
  rank_index: number | null
  similarity_score: number | null
  realism_score: number | null
  duration_ms: number | null
  created_at: string
  problems: EvaluationProblem[]
}

export type EvaluationOutcome =
  | 'SUCCEEDED'
  | 'PARSE_FAILED'
  | 'PROVIDER_ERROR'
  | 'UNAVAILABLE'
  | 'ROUND_RETRY_SCHEDULED'

export const EVALUATION_OUTCOME_LABEL: Record<EvaluationOutcome, string> = {
  SUCCEEDED: '评分成功',
  PARSE_FAILED: '返回解析失败',
  PROVIDER_ERROR: '模型调用失败',
  UNAVAILABLE: '评分器不可用',
  ROUND_RETRY_SCHEDULED: '已安排评分重试',
}

export interface EvaluationAttempt {
  id: string
  candidate_id: string | null
  evaluation_id: string | null
  round_number: number
  outcome: EvaluationOutcome
  evaluator: string
  model_name: string | null
  depth: EvaluationDepth
  response_id: string | null
  http_status: number | null
  finish_reason: string | null
  prompt_version: number | null
  prompt_tokens: number | null
  completion_tokens: number | null
  total_tokens: number | null
  duration_ms: number | null
  error_code: string | null
  error_message: string | null
  diagnostic: boolean
  created_at: string
}

export type DiagnosticEvaluation = Omit<
  Evaluation,
  'id' | 'candidate_id' | 'round_number' | 'rank_index' | 'similarity_score'
  | 'realism_score' | 'created_at'
>

export interface EvaluationDiagnosticResult {
  success: boolean
  candidate_id: string
  billable: boolean
  message: string
  evaluation: DiagnosticEvaluation | null
  attempt: EvaluationAttempt
}

export interface Review {
  id: string
  task_id: string
  product_id: string
  best_candidate_id: string | null
  reason: ReviewReason
  status: ReviewStatus
  blocking: boolean
  provider: string
  round_count: number
  attempt_count: number
  best_score: number | null
  best_grade: Grade | null
  hard_fail_codes: string[]
  summary: string | null
  decided_by: string | null
  decision_note: string | null
  manual_tags: string[]
  decided_at: string | null
  created_at: string
  product_sku: string
  product_name: string
  /** ---- 受众三件套(§13.2 / §19)。语义与 `WorkbenchProduct` 完全一致 ----
   *  审核页也是审阅页:§13.4 第 5 条要在这里逐张执行,而它需要受众在场 */
  audience: Audience | null
  audience_label: string
  review_focus: string[]
  task_status: TaskStatus | ''
}

export interface ReviewCandidate {
  id: string
  round_number: number
  candidate_index: number
  url: string
  width: number | null
  height: number | null
  seed: number | null
  status: string
  grade: Grade | null
  overall_score: number | null
  evaluation: Evaluation | null
}

export interface ReviewProductAsset {
  id: string
  asset_type: AssetType
  url: string
  width: number | null
  height: number | null
}

export interface ReviewDetail extends Review {
  product_assets: ReviewProductAsset[]
  round_best_candidates: ReviewCandidate[]
  all_candidates: ReviewCandidate[]
  task_mode: GenerationMode | ''
  max_rounds: number
}

export interface RuleSet {
  id: string
  name: string
  version: number
  description: string | null
  is_active: boolean
  weights: Record<string, number>
  thresholds: Record<string, number | boolean>
  evaluator_options: Record<string, unknown>
  created_at: string
}

export interface Evaluator {
  name: string
  display_name: string
  configured: boolean
  implemented: boolean
  active: boolean
}

/** 分档展示。颜色语义固定:绿=可用,蓝=可修复,橙=需重生,红=淘汰。 */
export const GRADE_LABEL: Record<Grade, { text: string; color: string; hint: string }> = {
  A: { text: 'A 自动通过', color: 'success', hint: '总分与四项底线全部达标,且无硬错误' },
  B: { text: 'B 定向重生', color: 'blue', hint: '轻度问题,换 seed / 提示词后有机会达标' },
  C: { text: 'C 淘汰重生', color: 'orange', hint: '问题明显,需换模特模板甚至换 Provider' },
  D: { text: 'D 直接淘汰', color: 'error', hint: '低于 55 分或存在硬错误,不进人工审核' },
}

/** 与后端 app/evaluators/base.py DIMENSION_LABELS 一一对应,由纯测试守护不漂移。 */
export const DIMENSION_LABEL: Record<string, string> = {
  garment_identity: '商品身份一致性',
  structural_fidelity: '结构一致性',
  color_fidelity: '颜色一致性',
  pattern_fidelity: '图案一致性',
  anatomy_realism: '人体真实性',
  face_realism: '面部真实性',
  skin_realism: '皮肤真实性',
  lighting_realism: '光照真实性',
  background_realism: '背景真实性',
  composition: '构图',
  website_usability: '网站可用性',
}

/** A 档的四条底线。审核页要让人一眼看出是哪条没过。 */
export const GATE_DIMENSIONS = [
  'garment_identity', 'structural_fidelity', 'anatomy_realism', 'website_usability',
] as const

/** 与后端 app/core/enums.py HardFailCode 一一对应,由纯测试守护不漂移。 */
export const HARD_FAIL_LABEL: Record<string, string> = {
  GARMENT_WRONG: '穿的不是这件商品',
  GARMENT_PART_MISSING: '商品部件缺失',
  GARMENT_PART_ADDED: '商品被多加了部件',
  STRAP_CHANGED: '肩带样式被改',
  NECKLINE_CHANGED: '领口样式被改',
  BACK_STYLE_CHANGED: '背部样式被改',
  COVERAGE_CHANGED: '覆盖面积被改',
  COLOR_VARIANT_WRONG: '颜色款错误',
  PATTERN_DISTORTED: '图案畸变',
  LOGO_OR_TEXT_CHANGED: 'Logo 或文字被改',
  SKU_IMAGE_MISMATCH: '图与 SKU 不符',
  ANATOMY_HARD_ERROR: '人体结构硬错误',
  FACE_HARD_ERROR: '面部硬错误',
  IMAGE_CORRUPTED: '图片损坏',
  PRODUCT_SEVERELY_OCCLUDED: '商品被严重遮挡',
  // ---- 男装结构码 + 受众错配(PRD v2 §14.4,译文口径 §20.4) ----
  WAISTBAND_CHANGED: '生成图的腰头结构与原商品不一致',
  INSEAM_CHANGED: '生成图的裤长与原商品不一致',
  LINER_CHANGED: '生成图的内衬与原商品不一致',
  AUDIENCE_MISMATCH: '图片中模特的受众与商品不符',
  // ---- 渲染缺陷(PRD v3.1 §6.5 事实一致性清单末两项)----
  // 措辞刻意不说"商品错了" —— 这两条里商品是对的,坏的是渲染。
  // 说成"商品不一致"会让运营去查商品录入,而该做的是换模板重生。
  ASYMMETRY_INTRODUCED: '本该对称的部件被生成成左右不对称',
  FUSION_DEFECT: '衣物边缘与皮肤或背景粘连、融合异常',
}

export const SEVERITY_LABEL: Record<ProblemSeverity, { text: string; color: string }> = {
  MINOR: { text: '轻度', color: 'default' },
  MAJOR: { text: '明显', color: 'orange' },
  CRITICAL: { text: '严重', color: 'error' },
}

export const RECOMMENDED_ACTION_LABEL: Record<RecommendedAction, string> = {
  AUTO_APPROVE: '自动通过',
  REGENERATE_TUNED: '定向重生',
  REGENERATE_RESEED: '换 seed 重生',
  REJECT: '淘汰该候选',
  MANUAL_REVIEW: '转人工审核',
}

export const REVIEW_STATUS_LABEL: Record<ReviewStatus, { text: string; color: string }> = {
  PENDING: { text: '待处理', color: 'warning' },
  APPROVED: { text: '已通过', color: 'success' },
  REJECTED: { text: '已驳回', color: 'error' },
  REGENERATION_REQUESTED: { text: '已转重生', color: 'processing' },
  CANCELLED: { text: '已取消', color: 'default' },
}

export const REVIEW_REASON_LABEL: Record<ReviewReason, { text: string; hint: string }> = {
  ROUNDS_EXHAUSTED: { text: '轮次耗尽', hint: '跑满最大轮次仍没有 A 档候选' },
  PROVIDER_EXHAUSTED: { text: 'Provider 耗尽', hint: '可用 Provider 都试过了' },
  REQUIRES_HUMAN_ERROR: { text: '需人工处理的错误', hint: '鉴权或内容安全类错误,重试无意义' },
  SPOT_CHECK: { text: '随机抽检', hint: 'A 档抽检,任务已通过,只需事后复核' },
  MANUAL_REQUEST: { text: '人工发起', hint: '由运营主动挂起' },
}

/** Mock 评分器的演示档位,写进 provider_params.mock_evaluator.outcome。 */
export const MOCK_EVALUATOR_OUTCOMES = [
  { value: 'auto', label: '自动(按图片指纹判定)' },
  { value: 'a', label: 'A 档 · 首轮即自动通过' },
  { value: 'b', label: 'B 档 · 定向重生' },
  { value: 'c', label: 'C 档 · 淘汰重生' },
  { value: 'd', label: 'D 档 · 直接淘汰' },
  { value: 'hard_fail', label: '硬错误 · 不论总分都不通过' },
  { value: 'improving', label: '逐轮变好 · 第 3 轮通过' },
  { value: 'stuck', label: '始终不达标 · 轮次耗尽转人工' },
]

// ===== 阶段 6:网站图片输出 =====

export type OutputPurpose = 'ORIGINAL' | 'DETAIL' | 'THUMBNAIL' | 'MOBILE' | 'SQUARE'

export interface OutputImage {
  url: string
  width: number
  height: number
  format: string
  file_size: number
  hash: string
}

export interface ProductExport {
  product: {
    id: string
    spu: string
    sku: string
    name: string
    category: string
    garment_type: string
    primary_color: string
    pattern_type: string
    status: ProductStatus
  }
  images: Partial<Record<OutputPurpose, OutputImage>>
  image_count: number
  /** 五个用途齐全才算这个 SKU 的图做完了 */
  complete: boolean
}

export interface DashboardSummary {
  products: { total: number; by_status: Record<string, number> }
  tasks: {
    total: number
    by_status: Record<string, number>
    auto_approved: number
    auto_rejected: number
    manual_review: number
    failed: number
    /** 还在等外部 Provider 出图的件数。清单由后端状态机的 IN_FLIGHT_STATES 定义 */
    in_flight: number
    average_rounds: number | null
  }
  grades: Record<Grade, number>
  reviews: { total: number; pending: number; by_status: Record<string, number> }
  providers: {
    calls_by_provider: Record<string, number>
    total_calls: number
    attempts: number
    failed_attempts: number
  }
  outputs: {
    total: number
    by_purpose: Record<string, number>
    products_with_outputs: number
  }
  recent_failures: {
    id: string
    product_id: string
    provider: string
    status: string
    error_code: string | null
    error_message: string | null
    created_at: string | null
  }[]
}

/** 用途而不是尺寸做标签:网站改版会改像素,但"详情页要用哪张"不会变。 */
export const OUTPUT_PURPOSE_LABEL: Record<OutputPurpose, { text: string; hint: string }> = {
  ORIGINAL: { text: '原始高清', hint: '不缩放,仅重新编码。留档与再加工用' },
  DETAIL: { text: '详情页', hint: '商品详情页主图,最长边 1200×1600 内' },
  THUMBNAIL: { text: '列表缩略图', hint: '商品列表卡片,400×533 内' },
  MOBILE: { text: '移动端', hint: '移动端详情,750×1000 内' },
  SQUARE: { text: '正方形', hint: '社媒与网格位,1000×1000 补边而非裁剪' },
}

export const OUTPUT_PURPOSE_ORDER: OutputPurpose[] = [
  'ORIGINAL', 'DETAIL', 'THUMBNAIL', 'MOBILE', 'SQUARE',
]

// ---------------------------------------------------------------- 阶段 8:设置

/**
 * 设置项控件类型。与后端 settings_schema 的 TYPE_* 常量同集合。
 *
 * `integer` 曾经漏在这里:页面代码一直正确地区分了它(整数项禁小数),
 * 但类型声明少了这个取值,于是 `field.type === 'integer'` 被 tsc 判成
 * 「两个类型没有交集」——`npm run build` 因此是失败的,而
 * `tools/syntax-check.mjs` 只做语法诊断,看不出这类问题。
 */
export type SettingFieldType =
  | 'text' | 'password' | 'number' | 'integer' | 'bool' | 'select'

/** 值是谁给的。后端 settings_service 里的三个 SOURCE_* 常量。 */
export type SettingSource = 'db' | 'env' | 'default'

export const SETTING_SOURCE_LABEL: Record<SettingSource, string> = {
  db: '后台设置',
  env: '环境变量',
  default: '默认值',
}

export interface SettingOption {
  value: string
  label: string
}

export interface SettingField {
  key: string
  label: string
  type: SettingFieldType
  help: string
  placeholder: string
  secret: boolean
  advanced: boolean
  minimum: number | null
  maximum: number | null
  options: SettingOption[]
  /** 密钥字段这里是末位打码串,永远不是明文 */
  value: string
  has_value: boolean
  source: SettingSource
  env_present: boolean
  /** SETTINGS_ENV_LOCK 打开且环境变量给过值,后台改不了 */
  locked: boolean
  updated_at: string | null
  updated_by: string | null
}

export interface SettingGroup {
  key: string
  title: string
  description: string
  badge: string
  fields: SettingField[]
}

export interface EncryptionStatus {
  available: boolean
  source: string
  message: string
}

export interface SettingsOverview {
  groups: SettingGroup[]
  env_lock: boolean
  encryption: EncryptionStatus
}

export interface SettingsWriteResult {
  updated: string[]
  message: string
}

/** Provider 下拉里这一项能不能选:已配置且已实现,缺一不可。 */
export function isProviderSelectable(p: Provider): boolean {
  return p.configured && p.implemented
}

/** 下拉里显示的名字,把不可选的原因直接写在标签上。 */
export function providerOptionLabel(p: Provider): string {
  if (!p.implemented) return `${p.display_name}(尚未实现)`
  if (!p.configured) return `${p.display_name}(未配置)`
  return p.display_name
}

/** 分组 key -> Provider 名。有对应 Provider 的分组才显示「测试连接」。 */
export const SETTING_GROUP_PROVIDER: Record<string, string> = {
  fashn: 'fashn',
  fal: 'fal',
  comfyui: 'comfyui',
}

// ---------------------------------------------------------------- 提示词模板

export interface PromptWarning {
  code: string
  message: string
}

export interface PromptVersion {
  version: number
  is_active: boolean
  note: string | null
  updated_by: string | null
  chars: number
  created_at: string | null
}

export interface PromptOverview {
  key: string
  content: string
  /** null 表示当前用的是代码内置默认,库里还没有任何生效版本 */
  version: number | null
  is_default: boolean
  default_content: string
  warnings: PromptWarning[]
  versions: PromptVersion[]
  saved_version?: number
}

export interface PromptPreview {
  key: string
  chars: number
  warnings: PromptWarning[]
}
