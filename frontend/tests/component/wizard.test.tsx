/**
 * 一体化向导的行为用例(阶段 6 批次 6-4 / 6-5,A45-batch29 / batch30)。
 *
 * ## 它挡的是什么
 *
 * 后端这两批把七步判定、颜色维聚焦、AC-05 的门禁与费用预估都算好了,
 * 而**界面上有没有这些是另一跳**。batch24 与 batch28 各为同一个形状付过
 * 一次账(§3.43 那一族:后端算好、前端零渲染点,两侧测试全绿)。
 *
 * 所以这份文件渲染整页,从运营看得到的 DOM 上断言。
 *
 * ## 为什么 mock 掉七个面板组件
 *
 * 每一步的操作面板都是详情页那几个既有 Tab(向导刻意不另写一套),
 * 而它们各自连着素材、属性、图片集三条独立的取数链路。连真的等于让
 * 「向导轨道上有几步」这条用例在任何一个标签页改了 HTTP 桩之后跟着红。
 * 面板自己的行为由它们各自的用例负责,这里验的是**编排**。
 *
 * `@tanstack/react-query` 保持真实:AC-16 的后半句「不因刷新重复提交」
 * 只有在真的跑一遍挂载流程时才验得了。
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

import type {
  ChangeSourceOption,
  ImpactPreview as ImpactPreviewData,
  ProductFlowDetail,
} from '../../src/api/workbench'

const api = vi.hoisted(() => ({
  flow: vi.fn(),
  changeSources: vi.fn(),
  changeImpact: vi.fn(),
  generateCopy: vi.fn(),
  buildDraft: vi.fn(),
  exportDraft: vi.fn(),
}))

vi.mock('../../src/api/workbench', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../src/api/workbench')>()),
  workbenchApi: api,
}))

const stub = (name: string) => ({
  default: () => <div data-testid={`panel-${name}`}>{name}</div>,
})

vi.mock('../../src/components/workbench/MaterialTab', () => stub('material'))
vi.mock('../../src/components/workbench/AttributeTab', () => stub('attribute'))
vi.mock('../../src/components/workbench/ImageSetTab', () => stub('imageset'))
vi.mock('../../src/components/workbench/CopyTab', () => stub('copy'))
vi.mock('../../src/components/workbench/DraftTab', () => stub('draft'))
vi.mock('../../src/components/workbench/ExportTab', () => stub('export'))
vi.mock('../../src/components/GenerationPlanPanel', () => stub('plan'))

const { default: WizardPage } = await import('../../src/pages/WizardPage')

// ---------------------------------------------------------------- 夹具

/** 一份形状完整的向导响应。每条用例只改它关心的那一处。 */
function detail(overrides: Partial<ProductFlowDetail['wizard']> = {}): ProductFlowDetail {
  const steps = [
    ['SETUP', '建档', 'DONE', true],
    ['MATERIAL', '素材', 'DONE', true],
    ['ATTRIBUTE', '属性', 'NEEDS_CONFIRM', true],
    ['PLAN', '方案', 'BLOCKED', false],
    ['IMAGE_SET', '图片集', 'BLOCKED', false],
    ['COPY', '文案', 'BLOCKED', false],
    ['DRAFT', '草稿', 'BLOCKED', false],
  ] as const

  return {
    product: {
      id: 'p-1',
      spu: 'SW-001',
      spu_id: 'spu-1',
      sku: 'SW-001-M',
      name: '连体泳衣',
      audience: null,
      audience_label: '待确认',
      review_focus: [],
      category: null,
      size: 'M',
      status: 'ACTIVE',
      updated_at: null,
    },
    thumbnail_url: null,
    flow: {
      completion: 45,
      blocking_count: 0,
      pending_count: 1,
      reminder_count: 0,
      current_step: 'ATTRIBUTE',
      current_step_label: '属性',
      next_action: {
        code: 'CONFIRM_ATTRIBUTES',
        label: '确认属性',
        step: 'ATTRIBUTE',
        reason: '',
      },
      steps: [],
    },
    colors: null,
    wizard: {
      current_step: 'ATTRIBUTE',
      focus_color: 'BLU',
      allowed_actions: ['CONFIRM_ATTRIBUTES'],
      blocking: [],
      steps: steps.map(([step, label, state, open]) => ({
        step,
        label,
        intro: `${label}这一步要做的事`,
        state,
        is_open: open,
        gate_reason: open ? '' : '等「属性」:已确认 1/3',
        is_current: step === 'ATTRIBUTE',
        actions: open ? [{ code: 'CONFIRM_ATTRIBUTES', label: '确认属性' }] : [],
        pending_colors: step === 'IMAGE_SET' ? ['BLU'] : [],
        has_color_axis: step === 'IMAGE_SET',
      })),
      cost: null,
      ...overrides,
    },
    image_set: null,
    copy: null,
    draft: null,
  } as ProductFlowDetail
}

const SOURCES: ChangeSourceOption[] = [
  {
    source: 'CONFIRMED_ATTRIBUTE_MODIFIED',
    label: '修改一个已确认的属性',
    trigger: '保存修改时立即',
    affects: ['文案', '上架草稿'],
  },
]

const IMPACT: ImpactPreviewData = {
  source: 'CONFIRMED_ATTRIBUTE_MODIFIED',
  source_label: '修改一个已确认的属性',
  trigger: '保存修改时立即',
  rows: [
    {
      target: 'COPY',
      target_label: '文案',
      effect: '过期',
      affected: true,
      mechanism: 'app.listings.copy_service:approve_copy',
      note: '',
      action: 'GENERATE_COPY',
      action_label: '生成文案',
    },
  ],
  objects: { image_set: null, copy: null, draft: null },
}

function renderWizard(url = '/wizard/p-1'): void {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  const wrap = (children: ReactNode) => (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[url]}>
        <Routes>
          <Route path="/wizard/:id" element={children} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
  render(wrap(<WizardPage />))
}

beforeEach(() => {
  vi.clearAllMocks()
  api.flow.mockResolvedValue(detail())
  api.changeSources.mockResolvedValue(SOURCES)
  api.changeImpact.mockResolvedValue(IMPACT)
})

// ================================================================ AC-01

describe('七步向导', () => {
  it('七步都在轨道上,而且关着的那几步也看得见', async () => {
    renderWizard()
    await waitFor(() => expect(screen.getByTestId('wizard-step-SETUP')).toBeTruthy())

    for (const step of ['SETUP', 'MATERIAL', 'ATTRIBUTE', 'PLAN', 'IMAGE_SET', 'COPY', 'DRAFT']) {
      // 关着的步骤压暗而不是隐藏:运营要看得见这条路一共几步、自己在第几步
      expect(screen.getByTestId(`wizard-step-${step}`)).toBeTruthy()
    }
    expect(screen.getByTestId('wizard-step-PLAN').getAttribute('data-open')).toBe('0')
    expect(screen.getByTestId('wizard-step-MATERIAL').getAttribute('data-open')).toBe('1')
  })

  it('没带 step 参数时落在后端算出来的当前步,不是第一步', async () => {
    renderWizard()
    await waitFor(() => expect(screen.getByTestId('panel-attribute')).toBeTruthy())
    expect(
      screen.getByTestId('wizard-step-ATTRIBUTE').getAttribute('data-active'),
    ).toBe('1')
  })

  it('关着的步骤显示后端给的挡路理由,而且不渲染那一步的面板', async () => {
    renderWizard('/wizard/p-1?step=IMAGE_SET')
    await waitFor(() => expect(screen.getByTestId('wizard-gate-reason')).toBeTruthy())

    // 理由由后端拼。前端拼一句"等上一步"的话,它与服务端 409 里那句会分叉。
    // 轨道上那几格与右侧提示各显示一次,所以是 getAllByText —— 两处显示的
    // 必须是同一句(都读 `gate_reason`),而不是各拼一句
    expect(screen.getAllByText('等「属性」:已确认 1/3').length).toBeGreaterThan(0)
    // 面板不渲染:让人在一个做不了的步骤上填半天,然后被 AC-05 那道闸拒掉
    expect(screen.queryByTestId('panel-imageset')).toBeNull()
  })

  it('「先补哪个颜色」来自后端,前端不自己挑第一个', async () => {
    renderWizard()
    await waitFor(() =>
      expect(screen.getByTestId('wizard-focus-color').textContent).toContain('BLU'),
    )
  })
})

// ================================================================ AC-16

describe('刷新恢复', () => {
  it('URL 上的那一步优先于后端的当前步', async () => {
    // 刷新前停在素材步 -> 地址栏带着它 -> 刷新后仍然是素材步,
    // 而不是被后端算出来的 ATTRIBUTE 顶掉
    renderWizard('/wizard/p-1?step=MATERIAL')
    await waitFor(() => expect(screen.getByTestId('panel-material')).toBeTruthy())
    expect(screen.queryByTestId('panel-attribute')).toBeNull()
  })

  it('手敲一个不存在的步骤退回当前步,不白屏', async () => {
    renderWizard('/wizard/p-1?step=TELEPORT')
    await waitFor(() => expect(screen.getByTestId('panel-attribute')).toBeTruthy())
  })

  it('挂载**不发起任何写请求** —— 刷新不等于重新提交一次', async () => {
    renderWizard()
    await waitFor(() => expect(api.flow).toHaveBeenCalled())

    // 一个写在 useEffect 里的"自动继续"会让每次刷新变成一次重复付费调用。
    // 这三个是向导能触发的全部写动作
    expect(api.generateCopy).not.toHaveBeenCalled()
    expect(api.buildDraft).not.toHaveBeenCalled()
    expect(api.exportDraft).not.toHaveBeenCalled()
  })
})

// ================================================================ AC-15 费用

describe('费用预估', () => {
  it('未配价显示「未配价」,不显示 ¥0', async () => {
    api.flow.mockResolvedValue(
      detail({
        cost: {
          is_estimate: true,
          is_complete: false,
          totals: [],
          unpriced_operations: [{ operation: 'submit', label: '出图' }],
          unknown: [],
          notes: [],
          lines: [
            {
              operation: 'submit',
              label: '出图',
              provider: 'mock',
              units: 6,
              basis: '2 个在售颜色合计还缺 6 个角度',
              unit_price_micros: null,
              currency: null,
              cost_micros: null,
              cost_display: null,
            },
          ],
          disclaimer: '以上为预计花费',
        },
      }),
    )
    renderWizard()
    await waitFor(() => expect(screen.getByText('未配价')).toBeTruthy())
    // 「预计 ¥0」是一句有具体数值的假话
    expect(screen.queryByText('¥0.00')).toBeNull()
    expect(screen.getByText('2 个在售颜色合计还缺 6 个角度')).toBeTruthy()
  })

  it('算不全的时候明说,不只给一个偏小的总数', async () => {
    api.flow.mockResolvedValue(
      detail({
        cost: {
          is_estimate: true,
          is_complete: false,
          totals: [{ currency: 'USD', cost_micros: 120000, cost_display: '$0.12' }],
          unpriced_operations: [],
          unknown: [
            {
              operation: 'submit',
              label: '出图',
              reason: '这些颜色还没有生成方案',
              refs: ['BLU'],
            },
          ],
          notes: ['文案生成:目前不写付费流水,未计入预估'],
          lines: [],
          disclaimer: '以上为预计花费',
        },
      }),
    )
    renderWizard()
    await waitFor(() => expect(screen.getByTestId('wizard-cost-incomplete')).toBeTruthy())
    expect(screen.getByText(/这些颜色还没有生成方案/)).toBeTruthy()
    // 「这一项为什么不在清单里」要有答案
    expect(screen.getByText(/未计入预估/)).toBeTruthy()
  })

  it('没有颜色维时说"算不出",不显示一个合计 0 的空卡', async () => {
    renderWizard()
    await waitFor(() =>
      expect(screen.getByText(/算不出颜色维,因此也算不出预估/)).toBeTruthy(),
    )
  })
})

// ================================================================ AC-17

describe('上游变化影响提示', () => {
  it('变更源清单来自后端,页面自己不维护一份', async () => {
    renderWizard()
    await waitFor(() => expect(api.changeSources).toHaveBeenCalled())
    expect(screen.getByText(/选一项,看它会让什么失效/)).toBeTruthy()
  })

  it('没选变更源之前不查影响 —— 看一眼不该先改一次状态', async () => {
    renderWizard()
    await waitFor(() => expect(api.changeSources).toHaveBeenCalled())
    expect(api.changeImpact).not.toHaveBeenCalled()
  })
})

// ================================================================ 阻塞清单

describe('阻塞清单', () => {
  it('阻断项由后端给,前端不做二次分级', async () => {
    api.flow.mockResolvedValue(
      detail({
        blocking: [
          { code: 'PLAN_MISSING', message: '这个颜色还没有生成方案', target_step: 'PLAN' },
        ],
      }),
    )
    renderWizard()
    await waitFor(() => expect(screen.getByTestId('wizard-blocking')).toBeTruthy())
    expect(screen.getByText('这个颜色还没有生成方案')).toBeTruthy()
  })
})
