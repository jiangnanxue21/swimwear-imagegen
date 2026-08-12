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
  /*
   * ## 这条用例的期望在 2026-08-11 订正过一次
   *
   * 原来它还断言 `provider: 'mock'`,而**那一直是红的**:`provider` 那个
   * Form.Item 长在「高级选项(仅管理员)」的 Collapse 里,而这里没有 mock
   * `useIdentity`,`isAdmin` 是 false —— 未渲染的 Form.Item 不属于
   * `validateFields()` 的返回值,所以请求体里根本没有 provider。
   *
   * **不是缺陷,是设计**:运营不选 Provider,由方案(§5.2)或后端的
   * `DEFAULT_PROVIDER` 决定。所以这里改成断言真正的不变量 —— 两个必填项
   * 一定在,而 provider 对非管理员一定**不**在(它一旦冒出来,说明那个
   * 高级区的管理员闸漏了)。
   *
   * 这条红灯活了下来,是因为 `make check-offline` 不跑 Vitest。
   */
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
    const body = submit.mock.calls[0][0]
    expect(body).toMatchObject({
      product_id: 'product-123',
      mode: 'virtual_try_on',
      // 非管理员也必须显式表达"这一次没有绕过方案",而不是让它缺席
      override_plan: false,
    })
    // 非管理员看不到高级区,所以 provider 不该出现在请求体里 —— 它一旦冒出来,
    // 说明那个"仅管理员"的闸漏了
    expect(body).not.toHaveProperty('provider')
  })
})
