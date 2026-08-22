/**
 * 时间显示的唯一入口。
 *
 * ## 为什么要有这个文件
 *
 * 后端的时间列全是 `DateTime(timezone=True)`(`app/db/base.py` 的 `TimestampMixin`),
 * 序列化出来带 `+00:00` 后缀。而前端原本有两种写法并存:
 *
 *     row.created_at?.replace('T', ' ').slice(0, 19)      // 把后缀切掉
 *     new Date(v).toLocaleString('zh-CN', { hour12: false })
 *
 * 第一种是错的:`slice(0, 19)` 正好切在秒的位置,把时区后缀连同它携带的信息一起
 * 丢掉,于是 UTC 的数字被当成本地时间显示 —— 新加坡/国内运营看到的每一个时间都
 * **慢 8 小时**。「上周导的那个压缩包是几点跑的」这类判断全建立在这个数字上。
 *
 * 第二种是对的,但四个页面四种格式,而且 `toLocaleString` 的输出随浏览器区域设置
 * 变化,截图对不上。
 *
 * ## 一个防御
 *
 * 有些写库路径是 `datetime.now(timezone.utc).replace(tzinfo=None)`(批次那几张表),
 * 万一哪天某条链路把裸串透出来,这里按 **UTC** 解释而不是按本地 —— 存进去的本来
 * 就是 UTC,按本地读会二次偏移。判据是尾部有没有 `Z` 或 `±HH:MM`。
 */
import dayjs from 'dayjs'
import utc from 'dayjs/plugin/utc'
import relativeTime from 'dayjs/plugin/relativeTime'
import 'dayjs/locale/zh-cn'

dayjs.extend(utc)
dayjs.extend(relativeTime)
dayjs.locale('zh-cn')

export type DateInput = string | number | Date | null | undefined

/** 空值统一显示成这个,不要在调用点各写各的 */
export const EMPTY_TIME = '—'

/** ISO 串尾部的时区标记:`Z` 或 `+08:00` / `+0800` */
const HAS_TZ = /(?:Z|z|[+-]\d{2}:?\d{2})$/

function parse(value: DateInput) {
  if (value === null || value === undefined || value === '') return null
  if (typeof value === 'string' && !HAS_TZ.test(value)) {
    const naive = dayjs.utc(value)
    return naive.isValid() ? naive.local() : null
  }
  const parsed = dayjs(value)
  return parsed.isValid() ? parsed : null
}

/** `2026-07-30 14:32:05`。表格、详情页的默认写法。 */
export function formatDateTime(value: DateInput, fallback = EMPTY_TIME): string {
  return parse(value)?.format('YYYY-MM-DD HH:mm:ss') ?? fallback
}

/** `07-30 14:32`。列窄的时候用,省掉年和秒。 */
export function formatDateTimeShort(value: DateInput, fallback = EMPTY_TIME): string {
  return parse(value)?.format('MM-DD HH:mm') ?? fallback
}

/** `2026-07-30` */
export function formatDate(value: DateInput, fallback = EMPTY_TIME): string {
  return parse(value)?.format('YYYY-MM-DD') ?? fallback
}

/**
 * `14:32:05`。只给「这一屏的数据取回来的时刻」用。
 *
 * 不省成相对时间(「1 分钟前」):相对时间要么定时重渲染,要么它自己会过期 ——
 * 一个用来回答\"我看的数还新不新\"的标签,过期之后说的正好是反话。
 * 也不带日期:它旁边就有刷新按钮,跨天的陈旧数据在这一页不存在。
 */
export function formatClock(value: DateInput, fallback = EMPTY_TIME): string {
  return parse(value)?.format('HH:mm:ss') ?? fallback
}

/** `3 小时前`。只用来做补充信息,不单独出现 —— 相对时间没法用来对账。 */
export function formatRelative(value: DateInput, fallback = EMPTY_TIME): string {
  return parse(value)?.fromNow() ?? fallback
}

/**
 * 绝对时间 + 相对时间。列表里「什么时候的事」和「多久以前」经常同时要,
 * 分成两列太占地方。
 */
export function formatDateTimeWithRelative(value: DateInput, fallback = EMPTY_TIME): string {
  const parsed = parse(value)
  if (!parsed) return fallback
  return `${parsed.format('YYYY-MM-DD HH:mm:ss')}(${parsed.fromNow()})`
}

/**
 * 当前浏览器时区,例如 `Asia/Singapore`。
 * 用在页面角落的一句说明上 —— 时间显示出过一次错之后,写明按谁的时区渲染
 * 比多解释一遍便宜。
 */
export function localTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || '本机时区'
  } catch {
    return '本机时区'
  }
}

/**
 * 排序用比较器。空值在升序里排到末尾 —— 「没有时间」不该被读成「很久以前」。
 * (降序时 antd 会整体取反,空值会翻到最前,这是纯比较器绕不开的,
 *  真要固定空值位置得靠后端排序。)
 */
export function compareTime(a: DateInput, b: DateInput): number {
  const left = parse(a)
  const right = parse(b)
  if (!left && !right) return 0
  if (!left) return 1
  if (!right) return -1
  return left.valueOf() - right.valueOf()
}
