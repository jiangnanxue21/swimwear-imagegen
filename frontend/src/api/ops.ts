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
  /** 时间戳解析不出来。这类行排在末尾,界面单独提示 —— 不静默丢,也不假装它在某个位置 */
  ts_unparsed: boolean
  level: string | null
  logger: string | null
  /** 哪个进程写的:api / worker / beat / script。三种进程写进同一个环形 */
  service: string | null
  pid: number | null
  domain: string
  domain_label: string
  event: string | null
  /** 没迁移的调用点没有这个 —— 界面此时显示 message 原文 */
  event_label: string | null
  /** 例行事件:流视角里折叠。**ERROR 永远是 false**,后端已经判过 */
  routine: boolean
  /** 折叠计数条上那个中文分组名(「租约让位」「幂等复用」……) */
  routine_group: string | null
  /**
   * 这条事件是不是一轮的里程碑 —— 链路模式拿它当段头。
   *
   * 是个布尔而不是让前端认事件码:哪条事件算里程碑属于分类法,
   * 而这一页不持有分类表(硬规则第 4 条)。
   */
  round_summary: boolean
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
  /**
   * 访问日志进不进诊断窗口。**界面必须把它说出来。**
   *
   *     errors  只有 4xx/5xx 的访问日志在窗口里
   *     all     全部在
   *
   * 窗口可以少装东西,但不许让人把"我没收"读成"没发生"。
   */
  access_mode?: 'errors' | 'all'
}

/** 一个域在当前窗口里的条数。**不吃 domain 筛选** —— 见 LogPage.domain_counts */
export interface DomainCount {
  total: number
  warn: number
  error: number
}

export interface LogPage {
  items: LogEntry[]
  ring: RingMeta
  /** 全窗最老的一条的时间,与当前筛选无关 */
  oldest_ts: string | null
  /**
   * **当前这张列表**实际的起点。和 `oldest_ts` 是两个数,谁也不冒充谁。
   *
   * 被 `limit` 截断时它比 `oldest_ts` 晚得多 —— 上一版界面只说了 `oldest_ts`,
   * 于是那行「更早的不是没发生,是滚出窗口了」在此刻是在误导人。
   */
  shown_oldest_ts: string | null
  /** 筛选命中的总条数(**截断前**) */
  matched: number
  /** `matched > items.length`。上一版超出就静默停,响应里一个字都没有 */
  truncated: boolean
  /** 进程 -> 条数。`worker` 一条都没有,就是 worker 没在写日志 */
  services_seen: Record<string, number>
  /**
   * 域 -> 计数。**服务端按全窗算,不受当前选中的域影响。**
   *
   * 上一版是前端按已经筛过的那一屏算的,于是点进一个域之后其余十四格
   * 全变 0 —— 恰好在最需要"别处还有没有事"的时候把这个信息拿掉了。
   */
  domain_counts: Record<string, DomainCount>
}

export interface LogMeta {
  domains: Array<{ key: string; label: string }>
  /**
   * 窗口里**实际出现过**的进程,不是一张写死的表。
   *
   * 写死的表会在 worker 没起来的时候依然列出 `worker` —— 而那正是要发现的事。
   */
  services_seen: Record<string, number>
  events: Array<{
    key: string
    label: string
    domain: string
    routine: boolean
    routine_group: string | null
  }>
  routine_groups: Array<{ key: string; label: string }>
  /** 按严重度升序。级别筛选是「及以上」,乱序就没法读 */
  levels: string[]
  ring: RingMeta
  payload_capture: {
    enabled: boolean
    ttl_seconds: number
    /** 旁挂库掉了多少条。404 时用来区分「过期了」和「从来没写进去过」 */
    dropped_since_boot: number
    last_error: string | null
  }
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

/** 一张图在旁挂库里剩下的全部东西。**正文永不留存** */
export interface LlmImageChip {
  tag?: string
  mime_type?: string
  base64_chars?: number
  sha256_16?: string
  [key: string]: unknown
}

export interface LlmPayload {
  llm_call_id: string
  provider: string | null
  model: string | null
  request: {
    endpoint: string
    headers: Record<string, unknown>
    body: unknown
    images: LlmImageChip[]
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
  /** **最低级别**,不是精确值 —— 选 WARNING 的人要看得见 ERROR */
  level?: string
  /** 哪个进程写的 */
  service?: string
  request_id?: string
  task_id?: string
  q?: string
  /** ISO 8601 带时区。**进 URL 的是绝对时间戳,不是「最近 15 分钟」** */
  since?: string
  until?: string
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
