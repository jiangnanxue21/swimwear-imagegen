/**
 * 总览页颜色维矩阵的渲染用例(阶段 6 / AC-15,A45-batch28)。
 *
 * ## 它挡的是什么
 *
 * 6-1 交付 `color_flow` 时明说了它零调用点、要等 6-3。后端这一批把链路接
 * 通了(取数层 → 端点),而**界面上有没有这一段是另一跳** ——
 * batch24 刚刚为同一个形状付过账:`preview.images` 后端算好、类型也齐了,
 * 而没有任何组件读它,§3.43 那一族的第十三次。
 *
 * 所以这份文件渲染整个总览页,从运营看得到的 DOM 上断言,而不是单独渲染
 * `ColorMatrix`:后者在有人把它从 OverviewTab 里删掉之后仍然全绿。
 */
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'

import OverviewTab from '../../src/components/workbench/OverviewTab'
import type { ColorRollup, ProductFlow } from '../../src/api/workbench'

const FLOW: ProductFlow = {
  completion: 40,
  blocking_count: 0,
  pending_count: 0,
  reminder_count: 0,
  current_step: 'IMAGE_SET',
  current_step_label: '图片集',
  next_action: { code: 'DONE', label: '已完成', step: 'DRAFT', reason: '' },
  steps: [],
}

const COLORS: ColorRollup = {
  active_count: 2,
  blocking_codes: ['BLU'],
  blocking_summary: '1 个颜色拦着:BLU',
  worst_by_step: { MATERIAL: 'BLOCKED' },
  colors: [
    {
      variant_id: 'v-red',
      variant_code: 'RED',
      is_active: true,
      current_step: null,
      is_blocking: false,
      steps: [
        { step: 'MATERIAL', state: 'DONE', detail: '' },
        { step: 'IMAGE_SET', state: 'UNKNOWN', detail: '图片没查' },
      ],
    },
    {
      variant_id: 'v-blu',
      variant_code: 'BLU',
      is_active: true,
      current_step: 'MATERIAL',
      is_blocking: true,
      steps: [
        { step: 'MATERIAL', state: 'BLOCKED', detail: '这个颜色还没有原始样品' },
        { step: 'IMAGE_SET', state: 'TODO', detail: '等上一步' },
      ],
    },
    {
      variant_id: 'v-pln',
      variant_code: 'PLN',
      is_active: false,
      current_step: 'MATERIAL',
      is_blocking: false,
      steps: [
        { step: 'MATERIAL', state: 'BLOCKED', detail: '这个颜色还没有原始样品' },
        { step: 'IMAGE_SET', state: 'TODO', detail: '等上一步' },
      ],
    },
  ],
}

describe('总览页的颜色维', () => {
  it('每个颜色一行,拦路的那个被点名', () => {
    render(<OverviewTab flow={FLOW} colors={COLORS} onJump={() => {}} />)
    expect(screen.getByText('颜色维')).toBeTruthy()
    expect(screen.getByText('RED')).toBeTruthy()
    expect(screen.getByText('BLU')).toBeTruthy()
    // 这句话由后端拼,前端只显示 —— 三个地方要显示同一句
    expect(screen.getByText('1 个颜色拦着:BLU')).toBeTruthy()
    // BLU 与 PLN 都缺样品 —— 两行都要说出理由,所以这里断言的是**两条**。
    // 用 getByText 会因为"找到多个"而失败,而那正是期望的行为
    expect(screen.getAllByText('这个颜色还没有原始样品')).toHaveLength(2)

  })

  it('未开售的颜色如实显示,但标出来它不拦路', () => {
    // 不显示的话运营会以为这个颜色不存在;算进阻断的话,任何一个刚建档的
    // 颜色都会让整个 SPU 变红,而他对此无能为力
    render(<OverviewTab flow={FLOW} colors={COLORS} onJump={() => {}} />)
    expect(screen.getByText('PLN')).toBeTruthy()
    expect(screen.getByText('未开售')).toBeTruthy()
  })

  it('未查与待办分开显示,不合并成一句', () => {
    // UNKNOWN 重新生成一次草稿就有了;TODO 是真的还没做。
    // 合并的表现是运营去重做一批其实已经做完的东西
    render(<OverviewTab flow={FLOW} colors={COLORS} onJump={() => {}} />)
    expect(screen.getByText('未查')).toBeTruthy()
    expect(screen.getAllByText('待办').length).toBeGreaterThan(0)
  })

  it('colors 为 null 时整块不显示 —— 那是「没查过」,不是「都齐了」', () => {
    render(<OverviewTab flow={FLOW} colors={null} onJump={() => {}} />)
    expect(screen.queryByText('颜色维')).toBeNull()
  })
})
