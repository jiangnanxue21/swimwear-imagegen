/**
 * 付费调用花费台账(A23)。
 *
 * ## 一个必须守住的措辞
 *
 * 后端返回的是**本系统发起的调用**花了多少,不是厂商账户余额。
 * 这一层的类型名、这一页的文案,一律用「预算 / 花费」,**不许出现「余额」**——
 * 两者会不一致(共用同一把 Key 的其他系统、厂商赠送额度、失败但仍计费的调用),
 * 而一个写着「余额 12.30」的数字会让人以为可以照着它决定还能不能跑批量。
 *
 * 后端 `disclaimer` 字段就是这句话的权威版本,原样显示,不要在前端改写。
 */
import { apiClient } from './client'

/** 预算档位。顺序即严重程度。`UNSET` = 还没配预算,不是「没超支」 */
export type BudgetLevel = 'OK' | 'WARN' | 'CRITICAL' | 'EXCEEDED' | 'UNSET'

export interface ProviderSpend {
  provider: string
  currency: string
  cost_micros: number
  cost_display: string
  calls: number
  billable_units: number
  /** 没配单价的调用数。是「花费未知」,不是「免费」 */
  unpriced_calls: number
}

/**
 * 一天一个币种的花费。
 *
 * `cost_micros` 是**主币种**那一条线(与 `spent_micros`、预算判定同源),
 * `by_currency` 是那一天的全部币种,原样列出 —— 后端刻意不求和也不折算:
 * micros 是"某个币种的百万分之一单位",不同币种相加得到的数没有单位,
 * 但它长得像钱,还会被画成一条平滑的曲线。
 */
export interface DailySpend {
  date: string
  currency: string
  cost_micros: number
  by_currency: Array<{ currency: string; cost_micros: number }>
}

/**
 * 按币种汇总。**主币种那一行也在里面**。
 *
 * 这一条存在的理由是「本月已花费」只统计主币种 —— 有别的币种时那个数
 * 偏小,而在此之前界面上没有任何地方说它偏小。金额字符串由后端格式化
 * (`format_money`),前端不自己拼:两份货币格式化分叉的表现是同一笔钱
 * 在两个位置上写法不同,那比不显示更让人不敢信。
 */
export interface CurrencySpend {
  currency: string
  cost_micros: number
  cost_display: string
  is_main: boolean
  /** 这个币种下发生过几次调用 */
  calls: number
  /**
   * 其中没配单价、因而没有计入金额的有几次。
   *
   * 有了它,一个**只有未配价调用**的币种才不会从提示里消失:它的
   * `cost_micros` 是 0,而按金额非零过滤会把它整行丢掉。顶层的
   * `unpriced_calls` 会亮,但那条不说是哪个币种。
   */
  unpriced_calls: number
}

export interface SpendBudget {
  level: BudgetLevel
  budget_micros: number
  budget_display: string
  used_ratio: number
  should_alert: boolean
  /** 按目前日均速度外推的整月花费。没花钱时为 null */
  projected_month_micros: number | null
  projected_month_display: string | null
  /** 按目前速度,预算撑到本月第几天。花不完时为 null */
  projected_exhaustion_day: number | null
}

export interface SpendSummary {
  period: {
    month: string
    started_at: string
    elapsed_days: number
    days_in_month: number
  }
  currency: string
  spent_micros: number
  spent_display: string
  budget: SpendBudget
  by_provider: ProviderSpend[]
  by_currency: CurrencySpend[]
  daily: DailySpend[]
  /** 本月出现过的全部币种代码。`by_currency` 是它的带金额版本 */
  daily_currencies: string[]
  unpriced_calls: number
  pricing_version: string
  disclaimer: string
  /**
   * 价目表解析失败的原因;正常时是 null。
   *
   * 必须显示。价目表写坏之后后端会把每一次调用记成「未计价」,于是这一页
   * 显示「本月花费 0」—— 而那和「这个月真的没花钱」在界面上完全一样。
   * 后端不再为此抛异常(那会把一个 JSON 拼写错误升级成生成链路瘫痪),
   * 所以这个字段是运营唯一能看见「配置坏了」的地方。
   */
  price_book_error: string | null
}

export const usageApi = {
  spend: async (): Promise<SpendSummary> =>
    (await apiClient.get<SpendSummary>('/usage/spend')).data,
}

/** 档位 -> antd 的语义色。集中一处,横幅与进度条不会各上各的色 */
export const BUDGET_TONE: Record<BudgetLevel, 'success' | 'warning' | 'error' | 'normal'> = {
  OK: 'success',
  WARN: 'warning',
  CRITICAL: 'error',
  EXCEEDED: 'error',
  UNSET: 'normal',
}

export const BUDGET_LABEL: Record<BudgetLevel, string> = {
  OK: '预算充足',
  WARN: '已用过半,注意节奏',
  CRITICAL: '接近预算上限',
  EXCEEDED: '已超出本月预算',
  UNSET: '未设预算',
}
