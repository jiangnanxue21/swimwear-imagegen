/**
 * 提示词调用统计的展示判定(a70,FE-301 / FE-306)。
 *
 * ## 这一批钉的是「一句假话」
 *
 * 统计类的展示缺陷不会白屏、不会报错,只会给一句读起来完全正常的话,而运营
 * 正是拿它判断"要不要回滚这一版"。三句最容易说错的:
 *
 *     「调用 0 次」    统计有起点,更早的调用查不出来 —— 一个跑过 500 次的
 *                      老版本会拿到全零统计,原样画出来就是假话
 *     「失败率 0%」    没有分母时后端给 null。前端 `?? 0` 一下,后端那份克制作废
 *     「近 7 天」      窗口天数由后端给。写死的话后端改了窗口而文案不跟
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  isEmpty,
  percentLabel,
  recentColumnTitle,
  recentLabel,
  unattributedNote,
  versionLabel,
} from '../../src/utils/promptStats.ts'

const zero = {
  calls: 0,
  failures: 0,
  parse_failed: 0,
  provider_error: 0,
  unavailable: 0,
  diagnostic: 0,
  parse_failure_rate: null,
  failure_rate: null,
}

const busy = {
  calls: 100,
  failures: 7,
  parse_failed: 5,
  provider_error: 2,
  unavailable: 0,
  diagnostic: 3,
  parse_failure_rate: 0.05,
  failure_rate: 0.07,
}

// ------------------------------------------------ 「0 次」与「没统计到」分得开

test('统计还没开始时,零调用说的是「统计尚未开始」而不是「0 次」', () => {
  const display = versionLabel(zero, null)
  assert.equal(display.text, '未统计')
  assert.match(display.hint ?? '', /还没有任何带归属/)
})

test('统计已开始时,零调用才说「0 次」,并说明起点', () => {
  const display = versionLabel(zero, '2026-08-19 10:00')
  assert.equal(display.text, '0 次')
  assert.match(display.hint ?? '', /2026-08-19 10:00/)
  assert.match(display.hint ?? '', /在那之前的调用不在其中/)
})

test('列表页:没有 stats 的项说「不统计」,不说「0 次」', () => {
  // 不可编辑的那 6 处链路不读库,调用根本不经过评分留痕。
  // 说「0 次」会被读成"没人用",而实际是"这里压根不记这个数"
  const display = recentLabel(undefined, '2026-08-19 10:00')
  assert.equal(display.text, '不统计')
  assert.equal(display.tone, 'muted')
})

test('列表页:零调用给「暂无调用」并带上起点', () => {
  const display = recentLabel(zero, '2026-08-19 10:00')
  assert.equal(display.text, '暂无调用')
  assert.match(display.hint ?? '', /统计自 2026-08-19 10:00 起/)
})

// ------------------------------------------------ 空分母不许兜底成 0%

test('失败率为 null 时不画成 0%', () => {
  assert.equal(percentLabel(null), null)
  assert.equal(percentLabel(undefined), null)
})

test('失败率保留一位小数 —— 2.0% 到 2.4% 是值得看见的变化', () => {
  assert.equal(percentLabel(0.024), '2.4%')
  assert.equal(percentLabel(0.05), '5.0%')
  assert.equal(percentLabel(0), '0.0%')
})

test('真的是 0% 与没有分母,两者说的话不同', () => {
  // 有 100 次调用、一次解析失败都没有 —— 这时 0.0% 是**真的**,该显示
  const perfect = { ...busy, parse_failed: 0, failures: 2, parse_failure_rate: 0 }
  const display = recentLabel(perfect, '2026-08-19 10:00')
  assert.match(display.text, /0\.0%/)
  // 而零调用那一支根本不画比率
  assert.doesNotMatch(recentLabel(zero, '2026-08-19 10:00').text, /%/)
})

test('非有限数不被画成百分比', () => {
  assert.equal(percentLabel(Number.NaN), null)
  assert.equal(percentLabel(Number.POSITIVE_INFINITY), null)
})

// ------------------------------------------------ 正常路径与悬停明细

test('列表页画出调用次数与解析失败率', () => {
  const display = recentLabel(busy, '2026-08-19 10:00')
  assert.equal(display.text, '100 次 · 解析失败 5.0%')
  assert.equal(display.tone, 'normal')
})

test('版本表画出「调用 N 次 · 失败 M 次」', () => {
  assert.equal(versionLabel(busy, '2026-08-19 10:00').text, '100 次 · 失败 7 次')
})

test('悬停把三类失败拆开,并单独点出手工测试的次数', () => {
  const hint = versionLabel(busy, '2026-08-19 10:00').hint ?? ''
  assert.match(hint, /解析失败 5/)
  assert.match(hint, /调用出错 2/)
  assert.doesNotMatch(hint, /评分器不可用/) // 为 0 的那类不摆出来
  assert.match(hint, /其中手工测试 3 次/)
})

test('诊断调用含在总数里,不是另一批', () => {
  // 扣掉的话「调用 0 次」会出现在一个刚刚被测过的版本上
  const onlyDiagnostic = { ...zero, calls: 3, diagnostic: 3 }
  assert.equal(versionLabel(onlyDiagnostic, '2026-08-19 10:00').text, '3 次 · 失败 0 次')
})

// ------------------------------------------------ 窗口天数与未归属

test('列头的 N 由后端给', () => {
  assert.equal(recentColumnTitle(7), '近 7 天调用')
  assert.equal(recentColumnTitle(30), '近 30 天调用')
})

test('拿不到窗口天数时退成不带数字的说法,不猜一个 7', () => {
  assert.equal(recentColumnTitle(undefined), '近期调用')
  assert.equal(recentColumnTitle(0), '近期调用')
})

test('没有未归属调用时不摆一句「另有 0 次」的废话', () => {
  assert.equal(unattributedNote(0), null)
  assert.equal(unattributedNote(undefined), null)
})

test('有未归属调用时说清它们没有被计入上表', () => {
  const note = unattributedNote(297) ?? ''
  assert.match(note, /297/)
  assert.match(note, /未计入上表/)
})

test('isEmpty 把 null 与零调用一视同仁,语义交给调用方', () => {
  assert.equal(isEmpty(null), true)
  assert.equal(isEmpty(undefined), true)
  assert.equal(isEmpty(zero), true)
  assert.equal(isEmpty(busy), false)
})
