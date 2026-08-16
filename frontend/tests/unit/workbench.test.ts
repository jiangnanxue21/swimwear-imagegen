/**
 * `api/workbench.ts` 里两个纯函数的用例(前端评审 T-4)。
 *
 * 放在 `frontend/tests/` 的理由见 `client.test.ts` 文件头 —— 同一条约束。
 *
 * `detectFlowAnomaly` 是这一层分支最多的函数,而它的职责恰好是「发现不该出现的
 * 状态组合」。一个用来发现异常的东西自己出错时,表现是**安静地什么都不报** ——
 * 页面看起来一切正常,而正在被漏掉的正是数据出问题的那些商品。
 */
import { describe, expect, it } from 'vitest'
import {
  FLOW_STEP_LABEL,
  detectFlowAnomaly,
  stepStates,
  type FlowStep,
  type FlowStepResult,
  type NextActionCode,
  type ProductFlow,
  type StepState,
} from '../../src/api/workbench'

//: 七步(A45-batch27 的增维)。这里原来是五步,而 `detectFlowAnomaly` 检查
//: 响应有没有覆盖 `FLOW_STEP_ORDER` 的每一步 —— 于是增维当天这份夹具
//: 让 8 条用例一起红。红的是夹具,不是被测逻辑:那条检查正是要防
//: 「聚合接口少返回了子状态」,它做对了自己的事。
const ORDER: FlowStep[] = [
  'SETUP',
  'MATERIAL',
  'ATTRIBUTE',
  'PLAN',
  'IMAGE_SET',
  'COPY',
  'DRAFT',
]


function step(name: FlowStep, state: StepState): FlowStepResult {
  return { step: name, label: name, state, summary: '', issues: [] }
}

/** 七步全 DONE、下一步 DONE 的健康流程。每条用例只改它关心的那一处。 */

function flow(
  overrides: Partial<Record<FlowStep, StepState>> = {},
  nextCode: NextActionCode = 'DONE',
  steps?: FlowStepResult[],
): ProductFlow {
  return {
    completion: 1,
    blocking_count: 0,
    pending_count: 0,
    reminder_count: 0,
    current_step: 'DRAFT',
    current_step_label: '草稿',
    next_action: { code: nextCode, label: '', step: 'DRAFT', reason: '' },
    steps: steps ?? ORDER.map((s) => step(s, overrides[s] ?? 'DONE')),
  }
}

describe('detectFlowAnomaly', () => {
  it('五步齐全且顺序合法时不报', () => {
    expect(detectFlowAnomaly(flow())).toBeNull()
  })

  it('后端枚举先上线:未知子状态要点名说出来', () => {
    // 这一支的价值在于**它说了实话**:前端文案表里没有这个取值。
    // 不报的话页面会渲染一个空标签,看起来像数据缺失而不是版本不齐
    const bad = flow({}, 'DONE', [
      ...ORDER.slice(1).map((s) => step(s, 'DONE')),
      { step: 'MATERIAL', label: '素材', state: 'WAT' as StepState, summary: '', issues: [] },
    ])
    const anomaly = detectFlowAnomaly(bad)
    expect(anomaly?.reason).toContain('WAT')
    expect(anomaly?.detail).toContain('后端枚举')
  })

  it('聚合接口少返回子状态:列出缺哪些', () => {
    // 列的是**中文步骤名**。这段 detail 最终显示在工作台的「状态异常」浮层里,
    // 而 COPY / DRAFT 这种词运营既看不懂也转述不出去 —— 转述给开发正是它
    // 唯一的用处。要断言的是"缺哪几步说得出来",不是"用哪种写法说"。
    const short = flow({}, 'DONE', ORDER.slice(0, 3).map((s) => step(s, 'DONE')))
    const anomaly = detectFlowAnomaly(short)
    expect(anomaly?.reason).toContain('少返回')
    expect(anomaly?.detail).toContain(FLOW_STEP_LABEL.COPY)
    expect(anomaly?.detail).toContain(FLOW_STEP_LABEL.DRAFT)
  })

  it('未知的下一步动作:明说前端不猜跳转目标', () => {
    // 猜错的后果是把人送到一个和他要做的事无关的标签页,
    // 比不跳更糟 —— 他会以为自己点错了
    const anomaly = detectFlowAnomaly(flow({}, 'TELEPORT' as NextActionCode))
    expect(anomaly?.reason).toContain('TELEPORT')
    expect(anomaly?.detail).toContain('不猜测')
  })

  describe('顺序约束', () => {
    it('素材未就绪,属性却已开工', () => {
      const anomaly = detectFlowAnomaly(flow({ MATERIAL: 'BLOCKED' }))
      expect(anomaly?.reason).toContain('素材未就绪')
    })

    it('素材是 NEEDS_CONFIRM 时属性可以开工 —— 这是正常的,不许误报', () => {
      // 「等人确认」不等于「没有素材」。误报的代价是页面上常年挂着一条
      // 不该出现的异常提示,而运营很快就会学会无视它
      expect(detectFlowAnomaly(flow({ MATERIAL: 'NEEDS_CONFIRM' }))).toBeNull()
    })

    it('属性未确认,图片集却已开工', () => {
      const anomaly = detectFlowAnomaly(flow({ ATTRIBUTE: 'IN_PROGRESS', COPY: 'BLOCKED' }))
      expect(anomaly?.reason).toContain('图片集')
    })

    it('属性未确认,文案却已开工', () => {
      const anomaly = detectFlowAnomaly(flow({ ATTRIBUTE: 'IN_PROGRESS', IMAGE_SET: 'BLOCKED' }))
      expect(anomaly?.reason).toContain('文案')
    })

    it('图片集或文案未批准,草稿却已开工', () => {
      const anomaly = detectFlowAnomaly(flow({ COPY: 'NEEDS_CONFIRM' }))
      expect(anomaly?.reason).toContain('草稿')
    })

    it('全部 BLOCKED 是全新商品的正常形态,不报', () => {
      const fresh = flow({
        MATERIAL: 'TODO',
        ATTRIBUTE: 'BLOCKED',
        IMAGE_SET: 'BLOCKED',
        COPY: 'BLOCKED',
        DRAFT: 'BLOCKED',
      })
      expect(detectFlowAnomaly(fresh)).toBeNull()
    })
  })
})

describe('stepStates', () => {
  it('摊平成 step -> state', () => {
    expect(stepStates(flow({ COPY: 'TODO' }))).toEqual({
      SETUP: 'DONE',
      MATERIAL: 'DONE',
      ATTRIBUTE: 'DONE',
      PLAN: 'DONE',
      IMAGE_SET: 'DONE',
      COPY: 'TODO',
      DRAFT: 'DONE',
    })

  })

  it('接口少给了子状态时,键就少 —— 不补默认值', () => {
    // 列表页那一行五列靠它渲染。这里补一个 DONE 会让缺失的步骤显示成「完成」,
    // 那是拿假依据填空,比留空更糟
    const out = stepStates(flow({}, 'DONE', [step('MATERIAL', 'DONE')]))
    expect(Object.keys(out)).toEqual(['MATERIAL'])
  })
})
