import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import TaskCreateModal from '../../src/components/TaskCreateModal'

vi.mock('../../src/api/generation', () => ({
  providersApi: {
    list: vi.fn().mockResolvedValue([
      {
        name: 'mock',
        implemented: true,
        configured: true,
        is_simulator: true,
      },
    ]),
  },
  modelTemplatesApi: {
    list: vi.fn().mockResolvedValue([]),
  },
}))

vi.mock('../../src/api/products', () => ({
  productsApi: {
    get: vi.fn().mockResolvedValue({ id: 'product-123', audience: 'WOMEN' }),
    list: vi.fn().mockResolvedValue({ items: [], total: 0 }),
  },
}))

describe('创建生成任务弹窗', () => {
  it('从商品详情页打开时仍把固定的 product_id 放进请求体', async () => {
    const submit = vi.fn()
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })

    render(
      <QueryClientProvider client={queryClient}>
        <TaskCreateModal
          open
          productId="product-123"
          onCancel={() => {}}
          onSubmit={submit}
        />
      </QueryClientProvider>,
    )

    fireEvent.click(screen.getByRole('button', { name: '提交任务' }))

    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1))
    expect(submit.mock.calls[0][0]).toMatchObject({
      product_id: 'product-123',
      mode: 'virtual_try_on',
      provider: 'mock',
    })
  })
})
