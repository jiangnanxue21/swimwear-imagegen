/**
 * 运营工作台接口(任务书阶段 1+2)。
 *
 * ## 前端不推算状态
 *
 * §3.2.1 写得很直接:「前端不自行推算子状态,一律取后端聚合接口返回值」。
 * 所以这个文件里**没有一处**从素材数、属性数、草稿字段反推状态的代码 ——
 * 五个子状态、完成度、阻断数、唯一下一步全部来自
 * `GET /workbench/products` 与 `/flow`,而这两条路径在后端共用
 * `workbench.service.collect`。列表页与详情页因此不可能显示两个数字(§3.4)。
 *
 * 下面几张文案表是**唯一**的例外,它们只做「后端枚举 -> 中文」的翻译,
 * 不含任何判定。后端加一个枚举值而这里不加,
 * `tests/pure/test_frontend_contract.py` 会立刻失败 —— 那条测试的存在
 * 就是为了让「界面上冒出一串英文常量」不可能悄悄发生。
 */
import axios from 'axios'
import { apiClient, describeError, isResultUnknown, LONG_TIMEOUT_MS } from './client'
import { decodeExportName } from './batch'
import type { BrandToken } from '../theme'
import type { Audience } from './types'

// ==========================================================
// 枚举与文案(对齐 app/workbench/flow.py)
// ==========================================================

export type FlowStep =
  | 'SETUP'
  | 'MATERIAL'
  | 'ATTRIBUTE'
  | 'PLAN'
  | 'IMAGE_SET'
  | 'COPY'
  | 'DRAFT'

/**
 * 流程顺序。与后端 `STEP_ORDER` 同序 —— 列表页每步一列、详情页进度区都按它排。
 *
 * 七步(A45-batch27):`SETUP` 与 `PLAN` 是阶段 6 加的。列数从五变七,
 * 列表页那一行会变窄 —— 这是**预期**的,不是布局出了问题。
 */
export const FLOW_STEP_ORDER: FlowStep[] = [
  'SETUP',
  'MATERIAL',
  'ATTRIBUTE',
  'PLAN',
  'IMAGE_SET',
  'COPY',
  'DRAFT',
]

export const FLOW_STEP_LABEL: Record<FlowStep, string> = {
  SETUP: '建档',
  MATERIAL: '素材',
  ATTRIBUTE: '属性',
  PLAN: '方案',
  IMAGE_SET: '图片集',
  COPY: '文案',
  DRAFT: '草稿',
}

export type StepState =
  | 'BLOCKED'
  | 'TODO'
  | 'IN_PROGRESS'
  | 'NEEDS_CONFIRM'
  | 'STALE'
  | 'DONE'

/**
 * 状态上色。
 *
 * `BLOCKED` 与 `STALE` 都是红系但不同色:前者要回头补东西,后者重新生成一次即可,
 * 运营要做的事完全不同。`BLOCKED` 还有一处刻意的歧义(见 flow.py 注释)——
 * 素材步的 BLOCKED 是「缺图」,其余步的 BLOCKED 是「等上游」,
 * 所以 `hint` 分开写,鼠标悬停时给的是当前这一步真正的含义。
 */
/**
 * 五个子状态的文案与配色。
 *
 * ## `tone` 是走查 M-1 之后加的,`color` 保留给尚未迁移的调用方
 *
 * 原来这里写的是 antd 预设名,其中 `volcano`(已过期)**不受任何令牌控制** ——
 * 它来自 antd 自己那套调色板,`theme.ts` 改一百遍也动不了它。六个状态里
 * 五个跟着品牌走、一个不跟,暗色模式下那一个会格外扎眼。
 *
 * 现在六个状态都由 `brandVars` 派生(经 `BrandTag`),漂移面收敛为零。
 * 选色的约束是**六个必须两两可分**,而不是「好看」:
 *
 *     阻断      danger      走不到导出,唯一该用红的地方
 *     未开始    textMuted   没开始不是问题,不能有颜色
 *     进行中    accent      系统在动,和「待确认」(等人)必须分开
 *     待确认    sand        等人做决定 —— 这套界面里 sand 专门表示「该你了」
 *     已过期    warning     做完了但上游变了。比 sand 重、比 danger 轻,位置正确
 *     已完成    success
 */
export const STEP_STATE_LABEL: Record<
  StepState,
  { text: string; color: string; tone: BrandToken }
> = {
  BLOCKED: { text: '阻断', color: 'error', tone: 'danger' },
  TODO: { text: '未开始', color: 'default', tone: 'textMuted' },
  IN_PROGRESS: { text: '进行中', color: 'processing', tone: 'accent' },
  NEEDS_CONFIRM: { text: '待确认', color: 'warning', tone: 'sand' },
  STALE: { text: '已过期', color: 'volcano', tone: 'warning' },
  DONE: { text: '已完成', color: 'success', tone: 'success' },
}

export type IssueLevel = 'BLOCKING' | 'NEEDS_CONFIRM' | 'REMINDER'

/**
 * 问题分级。
 *
 * §3.3.2:阻断数与待确认数进徽标,提醒级**只在详情内展示**。
 * `badge` 就是这条规则的开关,列表页据此决定要不要计数。
 */
export const ISSUE_LEVEL_LABEL: Record<
  IssueLevel,
  { text: string; color: string; badge: boolean }
> = {
  BLOCKING: { text: '阻断', color: 'error', badge: true },
  NEEDS_CONFIRM: { text: '待确认', color: 'warning', badge: true },
  REMINDER: { text: '提醒', color: 'default', badge: false },
}

export type NextActionCode =
  | 'COMPLETE_SETUP'
  | 'CHOOSE_PLAN'
  | 'UPLOAD_MATERIAL'
  | 'RELEASE_QUARANTINE'
  | 'CONFIRM_ASSET_ROLE'
  | 'CONFIRM_AUDIENCE'
  | 'RUN_EXTRACTION'
  | 'RESOLVE_CONFLICT'
  | 'CONFIRM_ATTRIBUTES'
  | 'FILL_ATTRIBUTES'
  | 'BUILD_IMAGE_SET'
  | 'FIX_IMAGE_SET'
  | 'APPROVE_IMAGE_SET'
  | 'GENERATE_COPY'
  | 'FIX_COPY'
  | 'APPROVE_COPY'
  | 'BUILD_DRAFT'
  | 'FIX_DRAFT'
  | 'REFRESH_DRAFT'
  | 'EXPORT'
  | 'RESOLVE_REJECTION'
  | 'DONE'

/**
 * 动作文案。
 *
 * 后端 `next_action.label` 已经带了中文,这张表**不是**为了替换它,而是为了两件事:
 * 筛选下拉要能在没拉到数据前就列出全部动作;以及后端加了新动作码时,
 * 契约测试能立刻指出这里漏了翻译(界面上会退化成英文常量)。
 *
 * 一处已知缺口:§3.2.2 第 4 条要求「识别中 / 生成中」时按钮置灰并显示进度,
 * 但后端目前没有对应的等待动作码 —— M3 的属性识别是同步的,几毫秒返回。
 * 真接异步模型后后端要新增一个码,那时这张表的覆盖测试会先失败,
 * 这正是我们要的提醒方式,所以现在不预留一个没人产出的 WAIT。
 */
export const NEXT_ACTION_LABEL: Record<NextActionCode, string> = {
  COMPLETE_SETUP: '补全建档归属',
  CHOOSE_PLAN: '选择模特与生成方案',
  UPLOAD_MATERIAL: '补充素材',
  RELEASE_QUARANTINE: '处理隔离素材',
  CONFIRM_ASSET_ROLE: '确认素材角色',
  CONFIRM_AUDIENCE: '确认受众与品类',
  RUN_EXTRACTION: '启动属性识别',
  RESOLVE_CONFLICT: '处理属性冲突',
  CONFIRM_ATTRIBUTES: '确认属性',
  FILL_ATTRIBUTES: '人工填写属性',
  BUILD_IMAGE_SET: '编排图片集',
  FIX_IMAGE_SET: '修复图片集',
  APPROVE_IMAGE_SET: '批准图片集',
  GENERATE_COPY: '生成文案',
  FIX_COPY: '修复文案',
  APPROVE_COPY: '批准文案',
  BUILD_DRAFT: '生成上架草稿',
  FIX_DRAFT: '修复草稿字段',
  REFRESH_DRAFT: '重新生成草稿',
  EXPORT: '导出上架文件',
  RESOLVE_REJECTION: '处理平台驳回',
  DONE: '已完成',
}

/** 详情页八个标签(FE-105)。tab 值进 URL,刷新后停在原处。 */
export type WorkbenchTab =
  | 'overview'
  | 'material'
  | 'attribute'
  | 'tasks'
  | 'imageset'
  | 'copy'
  | 'draft'
  | 'export'

/**
 * 动作码 -> 落到哪个标签页(§3.2.2 的「跳转目标」列)。
 *
 * 不按 `next_action.step` 直接映射:草稿步有两个标签(草稿、导出),
 * 「导出 Excel」要落在导出页,「修复草稿字段」要落在草稿页,
 * 而它们的 step 都是 DRAFT。
 */
export const ACTION_TAB: Record<NextActionCode, WorkbenchTab> = {
  // 建档归属与方案都还没有自己的标签页(方案页在 6-4 的向导里建)。
  // 落总览而不是落素材页:去素材页会让运营开始传图,而归属没挂好之前
  // 传的图会绑到错的作用域上 —— 传得越多错得越多
  COMPLETE_SETUP: 'overview',
  CHOOSE_PLAN: 'overview',
  UPLOAD_MATERIAL: 'material',

  RELEASE_QUARANTINE: 'material',
  // §6.2:素材有了,但角色不是人工确认的。落在素材页 —— 运营要做的是
  // 打开那几张图看一眼再点确认,不是去补图(补图会落在同一页,但按钮
  // 文案不同,而说错话比不说话更难查)
  CONFIRM_ASSET_ROLE: 'material',
  // §9.4:受众与品类**同屏确认,不拆两步**。落在属性页 ——
  // 受众决定必填属性集本身(§18.3),两件事在同一块屏幕上做才对得上
  CONFIRM_AUDIENCE: 'attribute',
  RUN_EXTRACTION: 'attribute',
  RESOLVE_CONFLICT: 'attribute',
  CONFIRM_ATTRIBUTES: 'attribute',
  // §11:识别跑过了,但结论按 fail-closed 一律不采信。落在属性页 ——
  // 和「确认属性」同一页,但按钮文案不同:那边有东西可点,这边没有,
  // 运营要做的是自己填一个值(说错话比不说话更难查)
  FILL_ATTRIBUTES: 'attribute',
  BUILD_IMAGE_SET: 'imageset',
  FIX_IMAGE_SET: 'imageset',
  APPROVE_IMAGE_SET: 'imageset',
  GENERATE_COPY: 'copy',
  FIX_COPY: 'copy',
  APPROVE_COPY: 'copy',
  BUILD_DRAFT: 'draft',
  FIX_DRAFT: 'draft',
  REFRESH_DRAFT: 'draft',
  EXPORT: 'export',
  RESOLVE_REJECTION: 'export',
  DONE: 'overview',
}

/** 问题的 `target_step` -> 标签页。徽标点击后按这个跳(§3.3.2)。 */
export const STEP_TAB: Record<FlowStep, WorkbenchTab> = {
  // 建档归属改在 SPU 页,而工作台没有那个标签页 —— 落到总览,
  // 由总览上那条问题带链接跳过去。硬塞一个不存在的标签页会白屏
  SETUP: 'overview',
  MATERIAL: 'material',
  // 方案页今天还没有(6-4 的向导才建)。落到总览,与 SETUP 同一个理由
  PLAN: 'overview',
  ATTRIBUTE: 'attribute',
  IMAGE_SET: 'imageset',
  COPY: 'copy',
  DRAFT: 'draft',
}

// ==========================================================
// 出参类型
// ==========================================================

export interface FlowIssue {
  level: IssueLevel
  code: string
  message: string
  target_step: FlowStep
  hint: string | null
  /** 具体到字段 / 素材 / 校验码。没有就是空 */
  ref: string | null
}

export interface FlowStepResult {
  step: FlowStep
  label: string
  state: StepState
  summary: string
  issues: FlowIssue[]
}

export interface NextAction {
  code: NextActionCode
  label: string
  step: FlowStep
  reason: string
}

export interface ProductFlow {
  completion: number
  blocking_count: number
  pending_count: number
  reminder_count: number
  current_step: FlowStep
  current_step_label: string
  next_action: NextAction
  steps: FlowStepResult[]
}

export interface WorkbenchProduct {
  id: string
  spu: string
  /** §4.4 的归属外键。`null` = 这一行还没挂到 SPU 上(向导的方案步据此关着)。
   *  **不要用 `spu` 那个字符串码去反查** —— 它会因改名而断开(§3.39) */
  spu_id: string | null
  sku: string
  name: string
  /** §13.2:受众常驻显示。`null` = 待确认,不是 UNISEX */
  audience: Audience | null
  /** 后端算好的界面文字(§21.3 文字标签)。前端不自己映射 —— 待确认那一档
   *  在后端是 `null`,在界面上是"待确认",这个转换只该有一处 */
  audience_label: string
  /** §19:本受众的重点检查项,**从规则包下发**。
   *  前端不许自己维护一份受众到检查项的映射 —— 维护两份的后果是规则包
   *  改了检查项、界面还在提示旧的那几项,而且不会报错 */
  review_focus: string[]
  /** §9.4:与受众同屏确认,所以和受众一起下发 */
  garment_type?: string
  category: string | null
  size: string | null
  status: string
  updated_at: string | null
}

export interface WorkbenchListItem {
  product: WorkbenchProduct
  thumbnail_url: string | null
  flow: ProductFlow
}

export interface WorkbenchSummary {
  total: number
  blocked: number
  needs_confirm: number
  exportable: number
  done: number
  /**
   * 按唯一下一步分组的件数(A9 首页的七张卡片)。
   *
   * 后端 `flow.summarize` 十七个动作码全部零填,所以这里可以直接下标取值,
   * 不需要 `?? 0` —— 而如果哪天后端漏了一个码,契约测试会先失败,
   * 不会等到首页上冒出一个 `undefined`。
   *
   * 注意它统计的是**筛选之后的全部商品**,不是当页那 20 条:
   * 首页要说的是"一共有几件"。前端不再自己数(§3.2.1)。
   */
  by_next_action: Record<NextActionCode, number>
}

export interface WorkbenchListResponse {
  summary: WorkbenchSummary
  total: number
  page: number
  page_size: number
  items: WorkbenchListItem[]
}

export interface CopyDiagnosticViolation {
  code: string
  message: string
  location: string | null
  blocking: boolean
}

export interface CopyDiagnosticResult {
  success: boolean
  billable: boolean
  generator: string
  prompt_version: string
  message: string
  error_code: string | null
  copy: {
    title: string
    bullet_points: string[]
    description: string
    keywords: string[]
  } | null
  notes: string[]
  trace: {
    model?: string | null
    response_id?: string | null
    http_status?: number | null
    duration_ms?: number | null
    provider_attempts?: number | null
    prompt_tokens?: number | null
    completion_tokens?: number | null
    total_tokens?: number | null
  }
  violations: CopyDiagnosticViolation[]
}

export interface CopyViolation {
  code: string
  message: string
  location: string | null
  /** 'error' 阻断批准;'warning' 只提醒 */
  level: 'error' | 'warning' | string
}

/** 模型对自己文本的标注(FE-224 溯源的落点)。 */
export interface CopyClaim {
  field_name: string
  value: string
  /** 必须在 location 指的那段文本里逐字出现 */
  text_span: string
  location: string
}

export interface ListingCopy {
  id: string
  locale: string
  version: number
  status: string
  title: string | null
  bullet_points: string[]
  description: string | null
  keywords: string[]
  claims: CopyClaim[]
  violations: CopyViolation[]
  model_name: string | null
  approved_by: string | null
  approved_at: string | null
  /**
   * 快审退回(A10)。注意 `status === 'REJECTED'` **不等于**被人退回过:
   * 规则硬失败也置这个状态。判据是这一项有没有值,后端同一份口径
   * (见 `flow._evaluate_copy`)。
   */
  reject_reason: string | null
  reject_note: string | null
  rejected_at: string | null
}

/**
 * 一个可选的退回原因(A10)。**整条都来自后端** —— 中文、适用范围、
 * 以及退回之后的下一步。
 *
 * 前端抄一份的后果不是显示错字:运营选了「商品结构改变」,界面说
 * "接下来去重排图片",而系统实际把他送去改属性。两个说法都出自本系统,
 * 他会信界面那个。§3.2.1 的纪律是唯一下一步由后端算、前端只翻译。
 */
export interface RejectReasonOption {
  code: string
  label: string
  /** 选了它必须填说明(目前只有「其他」)。后端也会挡,这里只是提前告诉人 */
  note_required: boolean
  next_action: NextActionCode
  next_action_label: string
}

export const COPY_STATUS_LABEL: Record<string, { text: string; color: string }> = {
  DRAFT: { text: '草稿', color: 'default' },
  VALIDATED: { text: '校验通过', color: 'processing' },
  REJECTED: { text: '校验失败', color: 'error' },
  APPROVED: { text: '已批准', color: 'success' },
  ARCHIVED: { text: '已归档', color: 'default' },
}

export const DRAFT_STATUS_LABEL: Record<string, { text: string; color: string }> = {
  BUILDING: { text: '构建中', color: 'processing' },
  VALIDATED: { text: '校验通过', color: 'processing' },
  INVALID: { text: '校验失败', color: 'error' },
  STALE: { text: '已过期', color: 'volcano' },
  APPROVED: { text: '已批准', color: 'success' },
  ARCHIVED: { text: '已归档', color: 'default' },
}

export const IMAGE_SET_STATUS_LABEL: Record<string, { text: string; color: string }> = {
  DRAFT: { text: '草稿', color: 'default' },
  PENDING_REVIEW: { text: '待批准', color: 'warning' },
  /** A10:快审退回。判定层会据此给出下一步,不再回到"等待批准" */
  REJECTED: { text: '快审退回', color: 'error' },
  APPROVED: { text: '已批准', color: 'success' },
  ARCHIVED: { text: '已归档', color: 'default' },
}

/** 草稿里一个平台字段(FE-232:值、来源、状态、问题)。 */
export interface DraftField {
  key: string
  label: string
  value: string | null
  required: boolean
  /** 来源表达式,如 `manual.price` / `copy.en.title`。草稿页据此认出手填字段 */
  source: string | null
  source_label: string
  max_length: number | null
  status: 'OK' | 'WARNING' | 'ERROR'
  problems: { code: string; message: string; level: string }[]
}

/**
 * 导出预览里一行 SKU 的图片(AC-19,阶段 5 批次 5-5)。
 *
 * `primary` 为 null 时 `status` 是 MISSING —— 那一行**留在表里**,不是被跳过。
 * 跳过的话运营会读成"这个尺码不在这次导出里",而它在,只是没有图。
 */
export interface DraftImageRow {

  sku: string
  primary: string | null
  extras: string[]
  status: 'OK' | 'MISSING'
}

export interface DraftImageColor {
  variant_id: string
  /** 显示用。后端已经处理过"没有颜色码就退回短 id",前端不要再兜一次 */
  variant_code: string
  rows: DraftImageRow[]
  missing_count: number
}

/**
 * 颜色→SKU→图片预览。**读的是草稿落库的那一份映射,不是当前上游。**
 *
 * 三档状态必须分开显示(后端 `export_preview` 的同一条理由):
 * UNPROVEN 重新生成草稿就好;INCOMPATIBLE 是有人换过快照形状,
 * 重新生成一百次也不会变好。合并成一句"没有图片信息"会让运营做错动作。
 */
export interface DraftImagePreview {
  status: 'READY' | 'UNPROVEN' | 'INCOMPATIBLE'
  /** 非 READY 时的一句可执行的话;READY 时是空串 */
  note: string
  colors: DraftImageColor[]
}

export interface DraftPreview {
  header: DraftField[]
  rows: { row_index: number; sku: string | null; fields: DraftField[] }[]
  /** 可选:存量后端(未接 5-5)不返回它 */
  images?: DraftImagePreview
}

/** 一条上游变化(BE-205 / §4.5.1:哪个上游变了、变了哪些字段、该做什么)。 */
export interface StaleChange {
  component: string
  message: string
  fields: string[]
  old_value: string | null
  new_value: string | null
  action: string
}

export interface StaleReason {
  stale: boolean
  status: string
  changes: StaleChange[]
}

export const STALE_COMPONENT_LABEL: Record<string, string> = {
  attributes: '已确认属性',
  image_set: '图片集',
  copy: '文案',
  manual: '手填字段',
  spec: '导出模板',
  mapping: '字段映射',
  missing: '上游对象',
  upstream_snapshot: '颜色维快照',
  shared_facts: 'SPU 已确认事实',
  shared_sample: '共享样品',
  color_set: '在售颜色集合',
  color_facts: '颜色已确认事实',
  color_sample: '颜色样品',
  color_plan: '颜色生成方案',
}

export interface DraftViolation {
  field_key: string
  code: string
  message: string
  level: string
  row_index: number | null
}

export interface ListingDraft {
  id: string
  status: string
  manual_payload: Record<string, unknown>
  validation_errors: DraftViolation[]
  validation_warnings: DraftViolation[]
  spec_version: string | null
  mapping_version: string | null
  fingerprint: string | null
  exported_at: string | null
  exported_by: string | null
  export_count: number
  updated_at: string | null
  /** 详情接口才带,单独建草稿的返回里也带 */
  preview?: DraftPreview
  stale?: StaleReason
  /**
   * 受众闸口的**警告档**(§17 兼容缝)。
   *
   * `draft_audience_gate` 一直会算出 `warnings`(存量商品"受众未确认"
   * 那一档),但 `export_gate` 只读了 `problems` —— 于是这半边结论
   * **从来没有出口**。它的唯一消费者就是界面:那句话要说给准备点导出
   * 的人听,告诉他这份文件是按受众前时代的规则包导的。
   *
   * 与 `validation_warnings` 分开:那是字段级校验,这是受众一致性。
   */
  audience_warnings?: string[]
}

/** 一个颜色在某一步上的状态(后端 `color_flow.ColorSubState`)。 */
export type ColorSubState = 'UNKNOWN' | 'TODO' | 'BLOCKED' | 'NEEDS_CONFIRM' | 'DONE'

export const COLOR_SUBSTATE_LABEL: Record<ColorSubState, string> = {
  // UNKNOWN 与 TODO **分开显示**:前者是"没算过"(重新生成一次草稿就有了),
  // 后者是"还没做"。合并的表现是运营去重做一批其实已经做完的东西
  UNKNOWN: '未查',
  TODO: '待办',
  BLOCKED: '受阻',
  NEEDS_CONFIRM: '待确认',
  DONE: '完成',
}

export interface ColorStepState {
  step: FlowStep
  state: ColorSubState
  detail: string
}

export interface ColorFlowRow {
  variant_id: string
  variant_code: string
  is_active: boolean
  current_step: FlowStep | null
  is_blocking: boolean
  steps: ColorStepState[]
}

/**
 * 颜色维汇总(阶段 6 / AC-15)。
 *
 * `null` 的含义是**没查过**(没有 SPU 归属的存量行),不是"颜色都齐了" ——
 * 所以这一段在 null 时整块不显示,而不是显示一个空表。
 */
export interface ColorRollup {
  active_count: number
  blocking_codes: string[]
  /** 后端拼好的一句话。三个地方要显示同一句,前端不各拼一次 */
  blocking_summary: string
  worst_by_step: Record<string, ColorSubState>
  colors: ColorFlowRow[]
}

// ==========================================================
// 一体化向导(阶段 6 / AC-01 / AC-05 / AC-15 / AC-16 / AC-17)
// ==========================================================

/**
 * 向导轨道上的一格。
 *
 * `is_open` 与 `gate_reason` **都由后端给**(硬规则 4)。前端不许自己按
 * `state !== 'BLOCKED'` 推:素材步的 BLOCKED 是「这一步有活要干」,
 * 别的步的 BLOCKED 是「还没轮到」—— 自己推的那一版会让一件刚建档的商品
 * 七步全灰,而运营要做的第一件事恰恰在那个灰格子里。
 */
export interface WizardStep {
  step: FlowStep
  label: string
  intro: string
  state: StepState
  is_open: boolean
  /** 进不去的原因。`is_open` 为真时是空串 */
  gate_reason: string
  is_current: boolean
  actions: { code: NextActionCode; label: string }[]
  /** 这一步上还没完成的在售颜色码 */
  pending_colors: string[]
  /** 这一步有没有颜色维。**空 `pending_colors` 与"没有颜色维"是两回事** */
  has_color_axis: boolean
}

/** 费用预估的一行。`cost_micros === null` 是**未配价**,不是免费。 */
export interface CostLine {
  operation: string
  label: string
  provider: string
  units: number
  basis: string
  unit_price_micros: number | null
  currency: string | null
  cost_micros: number | null
  cost_display: string | null
}

export interface CostEstimate {
  is_estimate: true
  /** 算全了没有。false 时界面不许只显示一个总数 */
  is_complete: boolean
  totals: { currency: string; cost_micros: number; cost_display: string }[]
  unpriced_operations: { operation: string; label: string }[]
  unknown: { operation: string; label: string; reason: string; refs: string[] }[]
  notes: string[]
  lines: CostLine[]
  disclaimer: string
}

export interface WizardView {
  current_step: FlowStep
  /** 当前这一步"先补哪个颜色"。null = 这一步没有颜色维待办 */
  focus_color: string | null
  allowed_actions: NextActionCode[]
  blocking: { code: string; message: string; target_step: FlowStep }[]
  steps: WizardStep[]
  /** `null` 的含义是**没算**(没有颜色维),不是"不要钱" */
  cost: CostEstimate | null
}

/** 变更源(AC-17 的"我要改什么")。取值由后端下发,前端不维护第二份 */
export interface ChangeSourceOption {
  source: string
  label: string
  trigger: string
  affects: string[]
}

export interface ImpactRow {
  target: string
  target_label: string
  /** 效应的中文直接来自后端(`stale_matrix.Effect` 的成员值就是中文) */
  effect: string
  affected: boolean
  mechanism: string
  note: string
  action: NextActionCode | null
  action_label: string | null
}

export interface ImpactPreview {
  source: string
  source_label: string
  trigger: string
  rows: ImpactRow[]
  /** 具体对象。`null` = 今天还没有这个对象,**不是"不受影响"** */
  objects: {
    image_set: { id: string; version: number; status: string } | null
    copy: { id: string; version: number; status: string } | null
    draft: { id: string; status: string } | null
  }
}

export interface ProductFlowDetail {
  product: WorkbenchProduct
  thumbnail_url: string | null
  flow: ProductFlow
  colors: ColorRollup | null
  wizard: WizardView
  /** 当前颜色对应的真实操作作用域。默认值由后端 focus_color 决定。 */
  color_selection: {
    variant_id: string
    variant_code: string
    /** 建了颜色但还没建 SKU 时为 null,颜色级写面板必须停住而不是借别色 SKU */
    product_id: string | null
  } | null
  image_set: { id: string; version: number; status: string } | null
  copy: ListingCopy | null
  draft: ListingDraft | null
}


export interface ExportHistoryEntry {
  actor: string | null
  action: string
  at: string | null
  payload: Record<string, unknown>
}

// ==========================================================
// 非法状态检测(§3.2.3)
// ==========================================================

export interface FlowAnomaly {
  reason: string
  detail: string
}

/**
 * 检测「状态组合不该出现」(§3.2.3)。
 *
 * 后端 `_decide_next` 按 `STEP_ORDER` 逐条判定,结构上保证推荐动作合法 ——
 * 所以这里查的不是「下一步对不对」,而是**返回的状态组合本身是否自相矛盾**。
 * 判据直接照抄后端的依赖关系,不另发明一套:
 *
 *     属性  上游是素材   素材 ∈ {DONE, NEEDS_CONFIRM} 才允许开工
 *     图片集/文案       属性 = DONE 才允许开工
 *     草稿             图片集 = DONE 且 文案 = DONE 才允许开工
 *
 * 上游没就绪时,后端给下游的状态**一定**是 `BLOCKED`。于是「草稿已生成但
 * 图片集未批准」(任务书举的例子)表现为草稿状态不是 BLOCKED 而图片集不是
 * DONE —— 命中这里,页面显示「状态异常」而不是硬凑一个看起来合理的动作。
 *
 * 注意不能用朴素的单调性判断:图片集与文案是**同级兄弟**,两者都只依赖属性,
 * 「文案已批准而图片集还没编排」完全合法。按流程顺序要求前面的一律先 DONE,
 * 会把这种正常情况误报成异常。
 */
export function detectFlowAnomaly(flow: ProductFlow): FlowAnomaly | null {
  const state = new Map<FlowStep, StepState>()
  //: 异常详情里也说中文。这段话最后是显示给运营看的(工作台那个「状态异常」
  //: 的浮层),而 `BLOCKED` / `IMAGE_SET` 这种词摆在那里,他既看不懂也
  //: 转述不出去 —— 而这条提示的全部用处就是被转述给开发
  const stateText = (step: FlowStep) => {
    const v = state.get(step)
    return v ? STEP_STATE_LABEL[v]?.text ?? v : '缺失'
  }
  for (const step of flow.steps) {
    if (!(step.state in STEP_STATE_LABEL)) {
      return {
        reason: `未知的子状态 ${step.state}`,
        detail: `${FLOW_STEP_LABEL[step.step] ?? step.step} = ${step.state};前端文案表里没有这个取值,可能是后端枚举先上了线`,
      }
    }
    state.set(step.step, step.state)
  }

  const missing = FLOW_STEP_ORDER.filter((s) => !state.has(s))
  if (missing.length) {
    return {
      reason: '聚合接口少返回了子状态',
      detail: `缺少:${missing.map((s) => FLOW_STEP_LABEL[s] ?? s).join('、')}`,
    }
  }

  if (!(flow.next_action.code in NEXT_ACTION_LABEL)) {
    return {
      reason: `未知的下一步动作 ${flow.next_action.code}`,
      detail: '前端不猜测这个动作该跳到哪个标签页',
    }
  }

  const started = (step: FlowStep) => state.get(step) !== 'BLOCKED'
  const ready = (step: FlowStep) => state.get(step) === 'DONE'
  const materialReady =
    state.get('MATERIAL') === 'DONE' || state.get('MATERIAL') === 'NEEDS_CONFIRM'

  if (started('ATTRIBUTE') && !materialReady) {
    return {
      reason: '素材未就绪,属性步却已开工',
      detail: `素材 = ${stateText('MATERIAL')},属性 = ${stateText('ATTRIBUTE')}`,
    }
  }
  for (const step of ['IMAGE_SET', 'COPY'] as FlowStep[]) {
    if (started(step) && !ready('ATTRIBUTE')) {
      return {
        reason: `属性未确认,${FLOW_STEP_LABEL[step]}步却已开工`,
        detail: `属性 = ${stateText('ATTRIBUTE')},${FLOW_STEP_LABEL[step]} = ${stateText(step)}`,
      }
    }
  }
  if (started('DRAFT') && !(ready('IMAGE_SET') && ready('COPY'))) {
    return {
      reason: '图片集或文案未批准,草稿却已开工',
      detail: `图片集 = ${stateText('IMAGE_SET')},文案 = ${stateText('COPY')},草稿 = ${stateText('DRAFT')}`,
    }
  }

  // 「这一步显示完成,而它自己带着一条阻断问题」——
  // 后端有同名的结构不变量(`flow._no_done_while_blocking`),这里是它的
  // 前端对应物。**两处都要有**:后端那条保证出参不会出现这种组合,
  // 这一条保证万一出现了,界面说"状态异常"而不是照着那个绿勾往下走。
  //
  // 它是审阅 F-1 那条缺陷的形状:已批准的图片集少了一个颜色的配图时,
  // 上一版会同时给出「图片集 ✅」和一条 MISSING_IMAGE_FOR_VARIANT 阻断。
  for (const s of flow.steps) {
    if (s.state !== 'DONE') continue
    const blocking = s.issues.filter((i) => i.level === 'BLOCKING')
    if (blocking.length) {
      return {
        reason: `${FLOW_STEP_LABEL[s.step]}显示完成,却带着 ${blocking.length} 条阻断`,
        detail: `${blocking.map((i) => i.code).join('、')};完成与阻断不能同时成立`,
      }
    }
  }

  return null
}

/** 七步子状态的扁平映射。列表页一行七列直接用它(6-2 增维前是五列)。 */
export function stepStates(flow: ProductFlow): Record<string, StepState> {
  const out: Record<string, StepState> = {}
  for (const s of flow.steps) out[s.step] = s.state
  return out
}

/** 某一级问题的全部条目,按流程顺序。徽标点击后定位到第一条。 */
export function issuesOf(flow: ProductFlow, level: IssueLevel): FlowIssue[] {
  return flow.steps.flatMap((s) => s.issues.filter((i) => i.level === level))
}

// ==========================================================
// 请求
// ==========================================================

export interface WorkbenchQuery {
  search?: string
  step?: FlowStep
  state?: StepState
  only_blocked?: boolean
  /**
   * 带上已归档的商品。**默认不带,而且这是归档有没有意义的分界线。**
   *
   * 后端这条列表原来不带任何状态过滤,所以归档一件商品之后它照样出现在
   * 工作台、照样计进顶部那五个计数格 —— 那个动作在这一页上净效果是零。
   */
  include_archived?: boolean
  next_action?: NextActionCode
  /**
   * 按商品受众筛选。`UNCONFIRMED` 是"待确认"那一档 —— 它不是 `Audience`
   * 的第四个取值(§3.4 规则 3),只是这个参数的一个哨兵值。
   *
   * **它是批量导出闸口的配套动作,不是装饰。** 批次内规则包不一致时后端
   * 会拒绝并让运营"按受众分成两个批次分别导出";在这个参数之前,界面上
   * 没有任何手段能按受众把批次拆开 —— §9.5 要求"错误必须包含行动",
   * 那条错误给了指令却没有行动。
   */
  audience?: Audience | 'UNCONFIRMED'
  /**
   * 服务端排序。这一页的排序在**全量筛选结果上**做然后才切页(后端 `_LIST_SORT_KEYS`),
   * 所以「完成度」「阻断数」这两个现算出来的维度也能排 —— 它们不是数据库列。
   */
  sort?: string
  order?: 'asc' | 'desc'
  page?: number
  page_size?: number
}

export interface ExportedFile {
  blob: Blob
  filename: string
}

export const workbenchApi = {
  list: async (params: WorkbenchQuery) =>
    (await apiClient.get<WorkbenchListResponse>('/workbench/products', { params })).data,

  flow: async (productId: string, color?: string) =>
    (
      await apiClient.get<ProductFlowDetail>(`/workbench/products/${productId}/flow`, {
        params: color ? { color } : undefined,
      })
    ).data,

  /** 整篇生成;`onlyFields` 给了就只重生那几个字段(FE-222,其余保留上一版)。 */
  generateCopy: async (productId: string, onlyFields?: string[]) =>
    (
      await apiClient.post<{ copy: ListingCopy }>(
        `/workbench/products/${productId}/copy/generate`,
        { only_fields: onlyFields ?? null },
      )
    ).data.copy,

  testCopy: async (productId: string, confirmBillable: boolean) =>
    (
      await apiClient.post<CopyDiagnosticResult>(
        `/workbench/products/${productId}/copy/test`,
        { confirm_billable: confirmBillable },
        { timeout: LONG_TIMEOUT_MS },
      )
    ).data,

  saveCopy: async (
    productId: string,
    fields: Partial<Pick<ListingCopy, 'title' | 'bullet_points' | 'description' | 'keywords'>>,
  ) =>
    (
      await apiClient.post<{ copy: ListingCopy }>(
        `/workbench/products/${productId}/copy/save`,
        fields,
      )
    ).data.copy,

  approveCopy: async (productId: string, copyId: string) =>
    (
      await apiClient.post<{ copy: ListingCopy }>(
        `/workbench/products/${productId}/copy/${copyId}/approve`,
        {},
      )
    ).data.copy,

  rejectCopy: async (
    productId: string,
    copyId: string,
    payload: { reason: string; note?: string | null },
  ) =>
    (
      await apiClient.post<{ copy: ListingCopy }>(
        `/workbench/products/${productId}/copy/${copyId}/reject`,
        payload,
      )
    ).data.copy,

  /**
   * 变更源清单(AC-17)。**前端不硬编码一份** —— 少一项的表现是运营改
   * 那一样东西之前看不到提示,而 AC-17 要的正是"修改之前"。
   */
  changeSources: async () =>
    (
      await apiClient.get<{ sources: ChangeSourceOption[] }>(
        '/workbench/change-sources',
      )
    ).data.sources,

  /** 这次修改会让哪些对象失效(AC-17)。只读,看一眼不改任何状态 */
  changeImpact: async (productId: string, source: string) =>
    (
      await apiClient.get<ImpactPreview>(
        `/workbench/products/${productId}/impact`,
        { params: { source } },
      )
    ).data,

  /** 退回原因清单。`target` 决定给哪一组 —— 用「葡语不自然」退图片集是点错了行 */
  rejectReasons: async (target: 'image_set' | 'copy') =>
    (
      await apiClient.get<{ target: string; reasons: RejectReasonOption[] }>(
        '/workbench/reject-reasons',
        { params: { target } },
      )
    ).data.reasons,

  buildDraft: async (productId: string, manual?: Record<string, unknown>) =>
    (
      await apiClient.post<{ draft: ListingDraft }>(
        `/workbench/products/${productId}/draft`,
        { manual: manual ?? null },
      )
    ).data.draft,

  staleReason: async (productId: string) =>
    (
      await apiClient.get<StaleReason>(
        `/workbench/products/${productId}/draft/stale-reason`,
      )
    ).data,

  exportHistory: async (productId: string) =>
    (
      await apiClient.get<{ items: ExportHistoryEntry[] }>(
        `/workbench/products/${productId}/exports`,
      )
    ).data.items,

  /**
   * 导出上架文件。
   *
   * 不能做成 `<a href>` 直接下载:每个请求都要带 `X-Operator-Token`,
   * 而 `<a>` 标签带不了自定义请求头(和私有素材代发那里同一个约束)。
   * 所以走 axios 取 blob,文件名优先读后端专门给的 `X-Export-Filename` ——
   * `Content-Disposition` 里的中文与引号解析起来更容易出错。
   */
  exportFile: async (productId: string, fmt: 'xlsx' | 'csv' = 'xlsx'): Promise<ExportedFile> => {
    const response = await apiClient.post<Blob>(
      `/workbench/products/${productId}/export`,
      null,
      { params: { fmt }, responseType: 'blob' },
    )
    const header = response.headers as Record<string, string | undefined>
    const filename =
      // A45-#27:大写回退已删。axios 统一把响应头小写化,`header['X-Export-Filename']`
      // 恒为 undefined —— 它看起来像一层防御,实际是一行永远不执行的代码,
      // 而"看起来有防御"比没有防御更容易让人不去查真正的原因
      decodeExportName(header['x-export-filename']) ??
      `listing.${fmt}`
    return { blob: response.data, filename }
  },
}

/**
 * 读出导出失败的原因。
 *
 * 导出请求用 `responseType: 'blob'`,于是后端 409 返回的那个 JSON 错误体
 * 也被当成二进制收下 —— `readError` 拿到的是一个 Blob,只能给出
 * 「发生未知错误」。而这条错误恰恰是最需要显示原文的一条:
 * 「草稿已过期,禁止导出:已确认属性 primary_color 变了…请重新生成草稿」
 * 是 §4.5.1 要求的三句话(哪个上游变了、变了哪些字段、该做什么),
 * 把它压成一句「未知错误」等于这条验收项没做。
 *
 * (这段说明原本孤零零挂在下面这个 section header 上方,而它描述的函数在
 *  本文件末尾。注释和它描述的东西分家之后,读的人会以为下面那段代码
 *  在做错误处理 —— 已挪到 `readExportError` 的定义处。)
 */

// ==========================================================
// 平台侧闭环类型与接口(OPS-REVIEW P1)
// ==========================================================

export interface RejectionComponentSnapshot {
  image_set: { id: string; version: number } | null
  copies: Record<string, { id: string; version: number }>
  spec_version: string | null
}

/**
 * A4:关闭这条驳回之前,后端观察到"改过并重导过"了吗。
 *
 * `satisfied=false` 不代表不能关闭 —— 平台侧全手工录入,系统判定不了
 * "已重新上传"。它只是要求运营显式表态一次(勾选或写说明)。
 * `missing_labels` 就是界面上"还缺哪一步"那行字。
 */
export interface ResolveGate {
  satisfied: boolean
  has_new_export: boolean
  /**
   * A-13:驳回之后出现过一次**成功的**提交尝试。
   *
   * 与 `has_new_export` 互为等价证据 —— API 自动上架的商品根本不走导出,
   * 只认导出的话那条路径来的驳回永远关不掉。两者满足其一即可,但指纹那半边
   * 不放松:没改草稿的重复提交仍然卡在 `fingerprint_unchanged`。
   *
   * `draft_id` 为空的老数据取不到这条证据链,后端会给 false —— 那类驳回
   * 仍然只能人工关闭。
   */
  has_new_attempt: boolean
  fingerprint_changed: boolean
  missing: string[]
  missing_labels: string[]
  missing_advice: string[]
}

export interface PlatformRejection {
  id: string
  draft_id: string
  spu: string
  sku: string
  reason_code: string
  reason_label: string
  platform_note: string | null
  export_fingerprint: string | null
  export_at: string | null
  component_snapshot: RejectionComponentSnapshot | null
  located_by: string
  located_by_label: string
  status: string
  status_label: string
  resolved_at: string | null
  resolved_by: string | null
  resolved_note: string | null
  recorded_by: string | null
  created_at: string | null
  /** 仅 OPEN 记录带闸门;已解决的为 null */
  resolve_gate: ResolveGate | null
}

export const PLATFORM_STATUS_LABEL: Record<string, { text: string; color: string }> = {
  NONE:      { text: '未上传平台', color: 'default' },
  SUBMITTED: { text: '已上传平台', color: 'processing' },
  LIVE:      { text: '平台已上架', color: 'success' },
  REJECTED:  { text: '平台驳回',   color: 'error' },
}

export const REJECTION_REASON_LABEL: Record<string, string> = {
  IMAGE_ISSUE:           '图片不合规',
  COPY_VIOLATION:        '文案违规',
  ATTRIBUTE_MISMATCH:    '属性与图文不符',
  QUALIFICATION_MISSING: '资质/检测报告缺失',
  CATEGORY_ERROR:        '类目挂错',
  PRICE_ISSUE:           '价格异常',
  OTHER:                 '其它(见平台原话)',
}

export const LOCATED_BY_LABEL: Record<string, string> = {
  audit:         '导出留痕带版本',
  current_draft: '当前草稿快照(其后未重建)',
  unlocated:     '版本不可考(老导出且草稿已重建)',
}

// workbenchApi 扩展方法 — 平台侧

export const platformApi = {
  /** 更新平台侧状态(NONE/SUBMITTED/LIVE) */
  setStatus: async (productId: string, status: string): Promise<{ platform_status: string }> =>
    (await apiClient.post(`/workbench/products/${productId}/platform-status`, { status })).data,

  /** 录入一条平台驳回 */
  recordRejection: async (
    productId: string,
    payload: { reason_code: string; platform_note?: string | null; export_fingerprint?: string | null },
  ): Promise<{ rejection: PlatformRejection }> =>
    (await apiClient.post(`/workbench/products/${productId}/rejections`, payload)).data,

  /** 列出全部驳回记录 */
  listRejections: async (
    productId: string,
  ): Promise<{ items: PlatformRejection[]; open_count: number; platform_status: string }> =>
    (await apiClient.get(`/workbench/products/${productId}/rejections`)).data,

  /**
   * 确认「修复后的新文件已经重新传上平台」(评审第 7 条)。
   *
   * 独立动作,不再由"关闭最后一条驳回"自动触发:系统看不见平台侧发生了
   * 什么,这句话只能由人明确说一次。
   */
  markResubmitted: async (productId: string): Promise<{ platform_status: string }> =>
    (await apiClient.post(`/workbench/products/${productId}/platform-resubmitted`)).data,

  /**
   * 推进一条驳回。**「已解决」不再是一次点击能到达的状态**(评审第 7 条)。
   *
   * 后端按闸门分流:有新导出且指纹变了 -> RESOLVED;闸门没过但运营声明
   * 修过了 -> FIXED_PENDING_EXPORT(**仍然阻断**);什么都没说 -> 409。
   */
  resolveRejection: async (
    productId: string,
    rejectionId: string,
    opts?: { note?: string | null; confirmed?: boolean },
  ): Promise<{ rejection: PlatformRejection }> =>
    (
      await apiClient.post(
        `/workbench/products/${productId}/rejections/${rejectionId}/resolve`,
        { note: opts?.note ?? null, confirmed: opts?.confirmed ?? false },
      )
    ).data,
}

/**
 * 导出失败的文案。**Blob 解析 + 写语义**(BLOCK-06)。
 *
 * ## 两件事,原来只做了一件
 *
 * Blob 那一半是对的:导出用 `responseType: 'blob'`,后端 409 的 JSON 错误体
 * 也被当成二进制收下,不解开就只能显示"发生未知错误",而那条错误恰恰是
 * 最需要原文的一条(哪个上游变了、变了哪些字段、该做什么)。
 *
 * 漏掉的是另一半:**导出是 POST 写操作。** 它生成文件、`export_count + 1`、
 * 写审计、commit,然后才把文件流回浏览器。浏览器超时或连接中断时,
 * 后端很可能已经提交了 —— 而 `readError()` 是**读**语义,它会说
 * "请重试"。再点一次的后果是第二条导出审计、第二次计数、第二个文件名,
 * 于是平台驳回追溯时会看到两次逻辑上相同的导出,谁也说不清用的是哪一份。
 *
 * 改用 `describeError(err, 'write')` 之后,超时会落到 `UNKNOWN` 分支,
 * 文案变成"结果未知,请先刷新查看" —— 而调用方据此先刷历史,
 * 记录本身就是答案。
 */
export async function readExportError(err: unknown): Promise<string> {
  const blobText = await blobErrorMessage(err)
  if (blobText) return blobText
  return describeError(err, 'write').text
}

/** 是不是"写请求结果未知"。导出调用点据此决定先刷新还是给重试按钮。 */
export function isExportResultUnknown(err: unknown): boolean {
  return isResultUnknown(err)
}

async function blobErrorMessage(err: unknown): Promise<string | null> {
  if (!axios.isAxiosError(err) || !(err.response?.data instanceof Blob)) return null
  try {
    const body = JSON.parse(await err.response.data.text())
    if (body?.error?.message) return String(body.error.message)
  } catch {
    /* 不是 JSON 错误体(网关返回的 HTML 之类),退回通用文案 */
  }
  return null
}
