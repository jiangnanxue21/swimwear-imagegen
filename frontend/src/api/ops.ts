/**
 * 运行日志控制台(docs/LOG-CONSOLE.md)。
 *
 * ## 这一层**不许**出现任何事件码或域名的字面量
 *
 * 硬规则第 4 条在这一页的具体形状:下拉取值、中文标签、例行判定,全部来自
 * `/ops/logs/meta` 与每一条日志自带的 `domain_label` / `event_label` /
 * `routine`。前端持有一份分类表的代价不是"多写几行",是它会和后端注册表分叉,
 * 而分叉的表现是筛选框里列着一个后端已经改名的码 —— 筛出来永远是空的,
 * 而运营会读成「这段时间没发生」。
 *
 * `frontend/tests/component/ops-log-page.test.tsx` 反向钉着这一点:
 * 这个文件与 `pages/OpsLogPage.tsx` 的源码里不许出现形如 `xxx.yyy` 的事件码。
 *
 * ## 与 `api/batch.ts` 里那两张 AUDIT_* 表的区别
 *
 * 那两张表是"拉到数据之前就要能列出取值"的历史妥协。这一页没有这个问题:
 * `/meta` 是一次独立的、不带筛选条件的请求,它**就是**"拉到数据之前"能拿到的
 * 那份东西。所以这里不重复那个妥协。
 */
import { apiClient } from './client'

/** 一条运行日志。判定字段全部由后端给,前端只展示。 */
export interface LogEntry {
  /** 进程内单调计数 + 进程标识。只为去重与稳定排序,**不承诺全局连续** */
  seq: string | null
  ts: string | null
  level: string | null
  logger: string | null
  domain: string
  domain_label: string
  event: string | null
  /** 没迁移的调用点没有这个 —— 界面此时显示 message 原文 */
  event_label: string | null
  /** 例行事件:流视角里折叠。**ERROR 永远是 false**,后端已经判过 */
  routine: boolean
  /** 折叠计数条上那个中文分组名(「租约让位」「幂等复用」……) */
  routine_group: string | null
  message: string | null
  request_id: string | null
  fields: Record<string, unknown>
  /** stdout 上逐字的那一行。分类法是索引,不是转述 —— 原文必须一键可得 */
  raw: string
}

/**
 * 环形窗口的边界。**界面必须把它说出来。**
 *
 * 查不到早于窗口的记录不是「没发生」,是「滚出窗口了」。
 */
export interface RingMeta {
  cap: number
  held: number | null
  enabled: boolean
  /** 掉了多少条。日志写失败是静默吞掉的,但**不瞎** —— 数字在这里 */
  dropped_since_boot: number
  last_error: string | null
  /** 有值 = 环形读不到。此时列表为空**不是**"这段时间没有日志" */
  unavailable_reason?: string
}

export interface LogPage {
  items: LogEntry[]
  ring: RingMeta
  /** 全窗最老的一条的时间,与当前筛选无关 */
  oldest_ts: string | null
}

export interface LogMeta {
  domains: Array<{ key: string; label: string }>
  events: Array<{
    key: string
    label: string
    domain: string
    routine: boolean
    routine_group: string | null
  }>
  routine_groups: Array<{ key: string; label: string }>
  levels: string[]
  ring: RingMeta
  payload_capture: { enabled: boolean; ttl_seconds: number }
}

/** 一次尝试收回来的东西。多次尝试各存各的 —— 覆盖会让第一次的现场消失 */
export interface LlmAttempt {
  attempt: number
  http_status: number | null
  duration_ms: number | null
  content_type: string | null
  upstream_request_id: string | null
  /** JSON 对象或一整段文本(网关 HTML 错误页那一类) */
  body: unknown
}

export interface LlmPayload {
  llm_call_id: string
  provider: string | null
  model: string | null
  request: {
    endpoint: string
    headers: Record<string, unknown>
    body: unknown
    images: Array<Record<string, unknown>>
    /** 发出去的**原始字节**的摘要。脱敏视图对不上它是正常的,见界面上那行小字 */
    sha256_16: string
    body_bytes: number
  } | null
  attempts: LlmAttempt[]
  /** 被截断的字段路径。界面要画出来,不能让人以为看到的是全文 */
  truncated: string[]
  ttl_seconds: number
}

export interface LogQuery {
  domain?: string
  event?: string
  level?: string
  request_id?: string
  task_id?: string
  q?: string
  limit?: number
}

export const opsApi = {
  logs: async (params: LogQuery): Promise<LogPage> =>
    (await apiClient.get<LogPage>('/ops/logs', { params })).data,
  meta: async (): Promise<LogMeta> => (await apiClient.get<LogMeta>('/ops/logs/meta')).data,
  llmPayload: async (callId: string): Promise<LlmPayload> =>
    (await apiClient.get<LlmPayload>(`/ops/llm/${callId}`)).data,
}

/**
 * 级别 -> antd 语义色。**这不是分类表** —— 级别是 logging 的固有档位,
 * 不是本仓的业务分类,后端也没有一张"级别注册表"可读。
 */
export const LEVEL_TONE: Record<string, string> = {
  DEBUG: 'default',
  INFO: 'default',
  WARNING: 'warning',
  ERROR: 'error',
  CRITICAL: 'error',
}
