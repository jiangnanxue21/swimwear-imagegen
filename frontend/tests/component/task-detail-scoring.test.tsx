import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App } from 'antd'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import type { EvaluationAttempt, TaskDetail } from '../../src/api/types'
import TaskDetailPage from '../../src/pages/TaskDetailPage'

const generationMocks = vi.hoisted(() => ({
  get: vi.fn(),
  cancel: vi.fn(),
  retry: vi.fn(),
}))

const evaluationMocks = vi.hoisted(() => ({
  forTask: vi.fn(),
  attemptsForTask: vi.fn(),
}))

vi.mock('../../src/api/generation', () => ({
  generationApi: generationMocks,
  SUBMIT_RESULT_UNKNOWN: 'SUBMIT_RESULT_UNKNOWN',
}))

vi.mock('../../src/api/reviews', () => ({ evaluationApi: evaluationMocks }))
vi.mock('../../src/components/ForceRetryModal', () => ({ default: () => null }))
vi.mock('antd', async (importOriginal) => {
  const actual = await importOriginal<typeof import('antd')>()
  return {
    ...actual,
    // jsdom 会尝试真正读取候选图 URL，产生与本用例无关的网络噪声。
    Image: ({ alt }: { alt?: string }) => <span role="img" aria-label={alt ?? '候选图'} />,
  }
})

const task: TaskDetail = {
  id: '11111111-1111-4111-8111-111111111111',
  product_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
  model_template_id: null,
  mode: 'virtual_try_on',
  provider: 'mock',
  routing_mode: 'MANUAL',
  candidate_count: 1,
  current_round: 1,
  max_rounds: 1,
  status: 'COMPLETED',
  error_code: null,
  error_message: null,
  external_task_id: 'mock-1',
  idempotency_key: 'task-detail-scoring-test',
  base_seed: 42,
  prompt: null,
  prompt_version: 'v1',
  created_at: '2026-08-13T00:00:00Z',
  started_at: '2026-08-13T00:00:01Z',
  finished_at: '2026-08-13T00:00:05Z',
  can_cancel: false,
  can_retry: false,
  can_delete: false,
  dispatch_status: 'DELIVERED',
  dispatch_attempts: 1,
  dispatch_error: null,
  attempts: [],
  candidates: [{
    id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
    round_number: 1,
    candidate_index: 0,
    external_id: 'candidate-1',
    storage_path: 'generated/candidate-1.jpg',
    width: 1024,
    height: 1024,
    file_size: 100,
    seed: 42,
    status: 'DOWNLOADED',
    overall_score: null,
    grade: null,
    error_message: null,
    created_at: '2026-08-13T00:00:03Z',
    url: 'data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=',
  }],
}

function failedAttempt(): EvaluationAttempt {
  return {
    id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
    candidate_id: task.candidates[0].id,
    evaluation_id: null,
    round_number: 1,
    outcome: 'PROVIDER_ERROR',
    evaluator: 'vision',
    model_name: 'gpt-test',
    depth: 'FULL',
    response_id: 'resp-safe',
    http_status: 429,
    finish_reason: null,
    prompt_version: '3',
    prompt_tokens: 100,
    completion_tokens: 0,
    total_tokens: 100,
    duration_ms: 1200,
    error_code: 'PROVIDER_RATE_LIMITED',
    error_message: '评分服务限流',
    diagnostic: false,
    created_at: '2026-08-13T00:00:04Z',
  }
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <MemoryRouter initialEntries={[`/tasks/${task.id}`]}>
      <QueryClientProvider client={queryClient}>
        <App>
          <Routes>
            <Route path="/tasks/:id" element={<TaskDetailPage />} />
          </Routes>
        </App>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('生成任务详情的评分与文案入口', () => {
  it('零评分记录时不再隐藏入口，并能跳到文案步骤', async () => {
    generationMocks.get.mockResolvedValue(task)
    evaluationMocks.forTask.mockResolvedValue([])
    evaluationMocks.attemptsForTask.mockResolvedValue([])

    renderPage()

    expect(await screen.findByText('这个任务没有评分记录')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '查看评分状态' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '进入文案生成' })).toHaveAttribute(
      'href',
      `/wizard/${task.product_id}?step=COPY`,
    )
  })

  it('评分调用失败时直接显示失败留痕，而不是伪装成没有评分', async () => {
    generationMocks.get.mockResolvedValue(task)
    evaluationMocks.forTask.mockResolvedValue([])
    evaluationMocks.attemptsForTask.mockResolvedValue([failedAttempt()])

    renderPage()

    expect(await screen.findByText('评分未成功')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '查看评分失败' })).toBeInTheDocument()
    expect(screen.getByText('PROVIDER_ERROR')).toBeInTheDocument()
    expect(screen.getByText('PROVIDER_RATE_LIMITED')).toBeInTheDocument()
  })

  it('评分查询失败时错误提示在主页面可达', async () => {
    generationMocks.get.mockResolvedValue(task)
    evaluationMocks.forTask.mockRejectedValue(new Error('评分接口不可用'))
    evaluationMocks.attemptsForTask.mockResolvedValue([])

    renderPage()

    await waitFor(() => expect(evaluationMocks.forTask).toHaveBeenCalled())
    expect(await screen.findByText('拉不到成功评分记录')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '评分记录读取失败' })).toBeInTheDocument()
  })
})
