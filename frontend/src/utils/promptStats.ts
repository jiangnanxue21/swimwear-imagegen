/**
 * 提示词调用统计的展示判定(FE-301 / FE-306)。
 *
 * ## 为什么这些逻辑不留在 JSX 里
 *
 * 照 a61 那条约定、a63 那份先例:**能不带 JSX 的部分就别带**。a62 写历史表时
 * 把列渲染全内联进 `<Table columns={...}>`,一行都测不了 —— 这个文件是不重蹈
 * 那一次。判定进 `.ts`,`<Table>` 只负责摆。
 *
 * ## 这一列最容易说错的三句话
 *
 * **「0 次」不等于「没人用过」。** 统计有起点(后端 `stats_since`,来自迁移
 * 0059 补上归属那一刻)。更早的调用不带归属,查不出来。一个在那之前跑过 500 次的
 * 版本会拿到一份全零统计 —— 原样画成「调用 0 次」是**一句假话**,而它和一个
 * 真的没人用过的版本长得一模一样,正是这一列要分开的两件事。
 *
 * **失败率没有分母时不是 0%。** 后端在这种情况下返回 `null` 而不是 `0`
 * (它自己的注释写着理由)。前端要是 `?? 0` 一下,后端那份克制当场作废。
 *
 * **窗口天数不许在前端写死。** 列头那句「近 N 天」的 N 由后端
 * `stats_window_days` 给。写死的话,后端改了窗口而界面继续说「近 7 天」,
 * 数字和文案对不上,且没有任何东西会红。
 */

import type { PromptCallStats } from '../api/types'

/** 一格统计该怎么画。`tone` 给调用方决定用不用次要色。 */
export interface StatsDisplay {
  text: string
  /** 鼠标悬停的补充说明。没有额外可说的就是 null */
  hint: string | null
  tone: 'normal' | 'muted'
}

/**
 * 这份统计是不是「什么都没有」。
 *
 * 与「调用了 0 次但统计确实覆盖着这段时间」是两回事 —— 后者这里也返回 true,
 * 由调用方结合 `statsSince` 决定措辞。本函数只回答形状,不回答语义。
 */
export function isEmpty(stats: PromptCallStats | null | undefined): boolean {
  return stats == null || stats.calls <= 0
}

/**
 * 百分比文本。`null` 进 `null` 出 —— **不许兜底成 0%**。
 *
 * 保留一位小数:失败率从 2.0% 涨到 2.4% 是一个值得看见的变化,而取整会把它
 * 抹成"没变"。
 */
export function percentLabel(rate: number | null | undefined): string | null {
  if (rate == null || !Number.isFinite(rate)) return null
  return `${(rate * 100).toFixed(1)}%`
}

/**
 * FE-301 那一列:「近 N 天调用次数与解析失败率」。
 *
 * `statsSince` 为 null 表示后端一条带归属的留痕都没有 —— 那时候「0 次」
 * 一定是"还没开始统计",不是"没人用"。
 */
export function recentLabel(
  stats: PromptCallStats | null | undefined,
  statsSince: string | null,
): StatsDisplay {
  if (stats == null) {
    // 后端只给 editable 的项带 stats。其余几处的链路不读库,调用根本不经过
    // 那张表 —— 说「不统计」而不是「0 次」,后者会被读成"没人用"
    return { text: '不统计', hint: '这处提示词的消费链路不读库,调用不经过评分留痕', tone: 'muted' }
  }
  if (stats.calls <= 0) {
    return {
      text: '暂无调用',
      hint: statsSince
        ? `统计自 ${statsSince} 起。在那之前的调用不带归属,查不出来`
        : '还没有任何带归属的评分留痕 —— 统计尚未开始',
      tone: 'muted',
    }
  }
  const rate = percentLabel(stats.parse_failure_rate)
  return {
    text: `${stats.calls} 次 · 解析失败 ${rate ?? '—'}`,
    hint: hintFor(stats),
    tone: 'normal',
  }
}

/**
 * FE-306 那一列:「调用 N 次 · 失败 M 次」,逐版。
 *
 * 不设时间窗(后端同):一个老版本的全部调用可能都在窗口之前,加了窗会让
 * 每一个非当前版本都显示 0 次。
 */
export function versionLabel(
  stats: PromptCallStats | null | undefined,
  statsSince: string | null,
): StatsDisplay {
  if (isEmpty(stats)) {
    return {
      text: statsSince ? '0 次' : '未统计',
      hint: statsSince
        ? `统计自 ${statsSince} 起 —— 这一版在那之前的调用不在其中`
        : '还没有任何带归属的评分留痕',
      tone: 'muted',
    }
  }
  const s = stats as PromptCallStats
  return {
    text: `${s.calls} 次 · 失败 ${s.failures} 次`,
    hint: hintFor(s),
    tone: 'normal',
  }
}

/** 悬停时把三类失败拆开说。全零时不给 hint,免得挂一个「失败 0/0/0」的空提示。 */
function hintFor(stats: PromptCallStats): string | null {
  const parts: string[] = []
  if (stats.parse_failed > 0) parts.push(`解析失败 ${stats.parse_failed}`)
  if (stats.provider_error > 0) parts.push(`调用出错 ${stats.provider_error}`)
  if (stats.unavailable > 0) parts.push(`评分器不可用 ${stats.unavailable}`)
  if (stats.diagnostic > 0) parts.push(`其中手工测试 ${stats.diagnostic} 次`)
  return parts.length ? parts.join(' · ') : null
}

/**
 * 列头文案。N 由后端给 —— 前端写死会在后端改窗口那天变成一句假话。
 *
 * `days` 拿不到时退成不带数字的说法,而不是猜一个 7。
 */
export function recentColumnTitle(days: number | null | undefined): string {
  return days && days > 0 ? `近 ${days} 天调用` : '近期调用'
}

/**
 * 未归属调用的提示语。0 条时返回 null —— 不给一句「另有 0 次未归属」的废话。
 *
 * 这句话必须存在的理由:归不了属的调用既不能算进任何一份提示词,也不该消失。
 * 消失的话「近 7 天 3 次」会被读成「这一份只被用了 3 次」,而真相可能是
 * 另外 297 次落在统计的起点之前。
 */
export function unattributedNote(count: number | null | undefined): string | null {
  if (!count || count <= 0) return null
  return `另有 ${count} 次调用归不到具体提示词上(统计起点之前的留痕、或读取提示词失败时的降级调用),未计入上表`
}
