import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App } from 'antd'
import { MemoryRouter } from 'react-router-dom'

import type { Task } from '../../src/api/types'
import TaskListPage from '../../src/pages/TaskListPage'

const generationMocks = vi.hoisted(() => ({
  list: vi.fn(),
  create: vi.fn(),
  cancel: vi.fn(),
  retry: vi.fn(),
  remove: vi.fn(),
}))

vi.mock('../../src/api/generation', () => ({
  generationApi: generationMocks,
  SUBMIT_RESULT_UNKNOWN: 'SUBMIT_RESULT_UNKNOWN',
}))

// 这组用例只看列表的动作编排；弹窗内部的表单行为由自己的组件用例覆盖。
vi.mock('../../src/components/TaskCreateModal', () => ({ default: () => null }))
vi.mock('../../src/components/ForceRetryModal', () => ({ default: () => null }))

function task(id: string, patch: Partial<Task>): Task {
  return {
    id,
    product_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    model_template_id: null,
    mode: 'virtual_try_on',
    provider: 'mock',
    routing_mode: 'MANUAL',
    candidate_count: 1,
    current_round: 0,
    max_rounds: 1,
    status: 'CREATED',
    error_code: null,
    error_message: null,
    external_task_id: null,
    idempotency_key: `key-${id}`,
    base_seed: null,
    prompt: null,
    prompt_version: 'v1',
    created_at: '2026-08-09T00:00:00Z',
    started_at: null,
    finished_at: null,
    can_cancel: false,
    can_retry: false,
    can_delete: false,
    ...patch,
  }
}

function rowFor(shortId: string): HTMLElement {
  const row = screen.getByText(shortId).closest('tr')
  if (!row) throw new Error(`找不到任务行 ${shortId}`)
  return row
}

describe('生成任务列表操作', () => {
  it('只展示后端允许的状态动作，并允许删除已执行后取消的任务', async () => {
    generationMocks.list.mockResolvedValue({
      items: [
        task('11111111-1111-4111-8111-111111111111', {
          status: 'CANCELLED', current_round: 3, can_delete: true,
        }),
        task('22222222-2222-4222-8222-222222222222', {
          status: 'QUEUED', can_cancel: true,
        }),
        task('33333333-3333-4333-8333-333333333333', {
          status: 'FAILED', can_retry: true,
        }),
        task('44444444-4444-4444-8444-444444444444', {
          status: 'CANCELLED', can_delete: true,
        }),
      ],
      total: 4,
      page: 1,
      page_size: 20,
    })

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <MemoryRouter>
        <QueryClientProvider client={queryClient}>
          <App>
            <TaskListPage />
          </App>
        </QueryClientProvider>
      </MemoryRouter>,
    )

    await waitFor(() => expect(generationMocks.list).toHaveBeenCalled())
    await screen.findByText('11111111')

    const executedCancelled = within(rowFor('11111111'))
    expect(executedCancelled.queryByRole('button', { name: '取消任务' })).not.toBeInTheDocument()
    expect(executedCancelled.queryByRole('button', { name: /重\s*试/ })).not.toBeInTheDocument()
    expect(executedCancelled.getByRole('button', { name: /删除/ })).toBeInTheDocument()
    expect(executedCancelled.queryByText('3 / 1')).not.toBeInTheDocument()
    expect(executedCancelled.getByText('3')).toBeInTheDocument()

    expect(within(rowFor('22222222')).getByRole('button', { name: '取消任务' }))
      .toBeInTheDocument()
    expect(within(rowFor('33333333')).getByRole('button', { name: /重\s*试/ }))
      .toBeInTheDocument()
    expect(within(rowFor('44444444')).getByRole('button', { name: /删除/ }))
      .toBeInTheDocument()
  })
})
