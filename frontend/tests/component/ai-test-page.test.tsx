import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App } from 'antd'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import AITestPage from '../../src/pages/AITestPage'

const generationMocks = vi.hoisted(() => ({ list: vi.fn(), get: vi.fn() }))
const evaluationMocks = vi.hoisted(() => ({ testCandidate: vi.fn() }))
const workbenchMocks = vi.hoisted(() => ({ list: vi.fn(), testCopy: vi.fn() }))
const aiTestMocks = vi.hoisted(() => ({ list: vi.fn() }))

vi.mock('../../src/api/generation', () => ({ generationApi: generationMocks }))
vi.mock('../../src/api/reviews', () => ({ evaluationApi: evaluationMocks }))
vi.mock('../../src/api/workbench', () => ({ workbenchApi: workbenchMocks }))
vi.mock('../../src/api/aiTests', () => ({ aiTestsApi: aiTestMocks }))
vi.mock('antd', async (importOriginal) => {
  const actual = await importOriginal<typeof import('antd')>()
  return {
    ...actual,
    Image: ({ alt }: { alt?: string }) => <span role="img" aria-label={alt ?? '候选图'} />,
  }
})

const taskId = '11111111-1111-4111-8111-111111111111'
const candidateId = '22222222-2222-4222-8222-222222222222'
const productId = '33333333-3333-4333-8333-333333333333'

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <App><AITestPage /></App>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('AI 能力测试页', () => {
  beforeEach(() => {
    generationMocks.list.mockResolvedValue({
      items: [{ id: taskId, provider: 'mock', status: 'COMPLETED' }], total: 1, page: 1, page_size: 100,
    })
    generationMocks.get.mockResolvedValue({
      id: taskId,
      candidates: [{
        id: candidateId, round_number: 1, candidate_index: 0, status: 'DOWNLOADED',
        width: 1024, height: 1024, grade: null,
        url: 'data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=',
      }],
    })
    workbenchMocks.list.mockResolvedValue({
      items: [{ product: { id: productId, sku: 'SKU-1', name: 'Navy Swimwear' } }],
      total: 1, page: 1, page_size: 100,
    })
    evaluationMocks.testCandidate.mockResolvedValue({
      success: false,
      candidate_id: candidateId,
      billable: true,
      message: '评分服务限流',
      evaluation: null,
      attempt: {
        id: '44444444-4444-4444-8444-444444444444', candidate_id: candidateId,
        evaluation_id: null, round_number: 1, outcome: 'PROVIDER_ERROR', evaluator: 'vision',
        model_name: 'gpt-test', depth: 'FULL', response_id: null, http_status: 429,
        finish_reason: null, prompt_version: 1, prompt_tokens: 10, completion_tokens: 0,
        total_tokens: 10, duration_ms: 500, error_code: 'RATE_LIMITED',
        error_message: '评分服务限流', diagnostic: true, created_at: '2026-08-13T00:00:00Z',
      },
    })
    workbenchMocks.testCopy.mockResolvedValue({
      success: true, billable: false, generator: 'template', prompt_version: 'template-1',
      message: '文案测试完成', error_code: null,
      copy: {
        title: 'Navy One Piece Swimsuit', bullet_points: ['Soft', 'Secure', 'Beach ready'],
        description: 'A navy one piece swimsuit.', keywords: ['navy', 'swimwear'],
      },
      notes: [], trace: {}, violations: [],
    })
    aiTestMocks.list.mockResolvedValue({
      runs: [], has_more: false, limit: 50, offset: 0, max_limit: 100,
    })
  })

  it('清楚区分评分测试和文案测试，并在确认费用前禁用执行', async () => {
    renderPage()
    expect(screen.getByRole('heading', { name: 'AI 能力测试' })).toBeInTheDocument()
    expect(screen.getByText('出图后大模型评分')).toBeInTheDocument()
    expect(screen.getByText('标题与描述生成')).toBeInTheDocument()
    expect(screen.getByText(/评分会留下一条标为「诊断」的调用记录/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /开始评分测试/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: /开始文案测试/ })).toBeDisabled()
    await waitFor(() => expect(generationMocks.list).toHaveBeenCalled())
  })

  it('评分测试明确选任务、候选图并确认费用后才调用独立接口', async () => {
    renderPage()
    fireEvent.mouseDown(await screen.findByRole('combobox', { name: '生成任务' }))
    fireEvent.click(await screen.findByText(/11111111 · mock · COMPLETED/))

    expect(await screen.findByRole('img', { name: '第 1 轮候选图 1 缩略图' })).toBeInTheDocument()
    fireEvent.click(await screen.findByRole('radio', { name: '第 1 轮候选图 1' }))
    fireEvent.click(screen.getByText(/当前评分器如果是真实模型/))
    fireEvent.click(screen.getByRole('button', { name: /开始评分测试/ }))

    await waitFor(() => expect(evaluationMocks.testCandidate).toHaveBeenCalledWith(candidateId, true))
    expect((await screen.findAllByText('评分服务限流')).length).toBeGreaterThan(0)
    expect(screen.getByText('独立测试')).toBeInTheDocument()
  })

  it('评分调用等待期间持续显示状态，而不是只剩一个没有解释的转圈', async () => {
    evaluationMocks.testCandidate.mockImplementation(() => new Promise(() => {}))
    renderPage()
    fireEvent.mouseDown(await screen.findByRole('combobox', { name: '生成任务' }))
    fireEvent.click(await screen.findByText(/11111111 · mock · COMPLETED/))
    fireEvent.click(await screen.findByRole('radio', { name: '第 1 轮候选图 1' }))
    fireEvent.click(screen.getByText(/当前评分器如果是真实模型/))
    fireEvent.click(screen.getByRole('button', { name: /开始评分测试/ }))

    expect(await screen.findByText(/评分模型正在处理 · 已等待/)).toBeInTheDocument()
    expect(screen.getByText(/可能需要 1–5 分钟/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /开始评分测试/ })).toBeDisabled()
  })

  it('文案测试只回显生成结果，不把保存动作混进页面', async () => {
    renderPage()
    fireEvent.mouseDown(await screen.findByRole('combobox', { name: '文案商品' }))
    fireEvent.click(await screen.findByText('SKU-1 · Navy Swimwear'))
    fireEvent.click(screen.getByText(/若当前文案生成器为真实模型/))
    fireEvent.click(screen.getByRole('button', { name: /开始文案测试/ }))

    await waitFor(() => expect(workbenchMocks.testCopy).toHaveBeenCalledWith(productId, true))
    expect(screen.queryByRole('button', { name: /保存/ })).not.toBeInTheDocument()
  })
})
