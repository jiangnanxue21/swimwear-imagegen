/**
 * 失败提示（A12）与冷启动引导（OPS-REVIEW P5）的行为用例。
 *
 * ## 它取代了什么
 *
 * `test_frontend_contract.py` 里这一组：
 *
 *     test_operations_never_see_a_command          搜 TSX 里有没有 "docker"
 *     test_technical_details_are_collapsed_by_default  搜有没有写 defaultActiveKey
 *     test_error_notice_reads_the_shared_judgement_and_invents_nothing
 *     test_cold_start_banner_points_at_the_settings_page   搜有没有 "/settings"
 *     test_cold_start_probe_uses_a_path_that_needs_no_token
 *
 * 「运营看不到 docker 命令」这一条特别值得说：它当初的实现是在 TSX 源码里
 * 搜 `docker` 这个词。但源码里**必须**有这个词——管理员那一支要显示它。
 * 于是那条测试真正断言的是「这个词出现在某个 isAdmin 分支附近」，
 * 靠的是文本距离，不是逻辑。分支条件写反了它照样绿。
 *
 * 下面是把同一件事渲染出来看：以运营身份渲染，然后断言整棵 DOM 里没有 docker。
 *
 * ## 为什么 mock 掉 useIdentity
 *
 * 它内部是两个 `useQuery`，真跑要拖进 QueryClientProvider 和一套 HTTP 桩。
 * 而这两个组件要验的东西**全部**只取决于 useIdentity 的返回值——
 * 身份是怎么探出来的是另一件事（那件事由 `client.test.ts` 的 17 条覆盖）。
 * 在这里连真实探测，等于让「横幅说哪句话」的用例在后端桩改了以后跟着红。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'

const identity = vi.hoisted(() => ({
  value: {
    who: null as unknown,
    isAdmin: false,
    backendDown: false,
    loading: false,
    authFailed: false,
  },
}))

vi.mock('../../src/hooks/useIdentity', () => ({
  useIdentity: () => identity.value,
}))

const { default: ErrorNotice } = await import('../../src/components/ErrorNotice')
const { default: ColdStartBanner } = await import('../../src/components/ColdStartBanner')
const { writeAdminToken, writeOperatorToken } = await import('../../src/api/client')

function setIdentity(patch: Partial<typeof identity.value>) {
  identity.value = { ...identity.value, ...patch }
}

/** 造一个后端返回的统一错误体 */
function apiError(status: number, code: string, message: string, requestId?: string) {
  return {
    isAxiosError: true,
    message: 'boom',
    response: {
      status,
      data: { error: { code, message, fields: [] } },
      headers: requestId ? { 'x-request-id': requestId } : {},
    },
  }
}

beforeEach(() => {
  setIdentity({ isAdmin: false, backendDown: false, loading: false, authFailed: false })
  window.localStorage.clear()
})

// ================================================================ ErrorNotice

describe('ErrorNotice：运营一句话，管理员能展开', () => {
  it('运营看到的是后端那句业务话术，不套「请重试」', () => {
    render(<ErrorNotice error={apiError(400, 'DRAFT_STALE', '草稿已过期，禁止导出')} />)

    expect(screen.getByText(/草稿已过期/)).toBeInTheDocument()
    expect(screen.queryByText(/请重试/)).not.toBeInTheDocument()
  })

  it('运营视野里不出现任何命令 —— 整棵 DOM 里搜，不是搜源码', () => {
    // 这一条是本文件的核心。原来的 Python 版搜的是 TSX 文本，
    // 而那个词在源码里**本来就该有**（管理员分支要用它）
    setIdentity({ isAdmin: false })
    const { container } = render(<ErrorNotice error={{ isAxiosError: true, code: 'ERR_NETWORK' }} />)

    expect(container.textContent).not.toContain('docker')
    expect(container.textContent).not.toContain('compose')
    expect(screen.getByText(/不是你操作错了/)).toBeInTheDocument()
  })

  it('管理员多一块技术详情，而且默认是收起的', async () => {
    const user = userEvent.setup()
    setIdentity({ isAdmin: true })
    render(<ErrorNotice error={apiError(500, 'INTERNAL', '服务器开小差', 'req-abc123')} />)

    // 折叠面板的标题在，内容不在 —— 默认收起
    expect(screen.getByText(/技术详情/)).toBeInTheDocument()
    expect(screen.queryByText('错误码')).not.toBeInTheDocument()

    await user.click(screen.getByText(/技术详情/))

    expect(await screen.findByText('错误码')).toBeInTheDocument()
    expect(screen.getByText('INTERNAL')).toBeInTheDocument()
  })

  it('请求编号对运营也可见：上一句刚要求他把它转述给管理员', () => {
    setIdentity({ isAdmin: false })
    render(<ErrorNotice error={apiError(500, 'INTERNAL', '服务器开小差', 'req-abc123')} />)

    // 5xx 的编号会同时出现在话术句和那行等宽文本里，两处都是刻意的
    expect(screen.getAllByText(/req-abc123/).length).toBeGreaterThan(0)
  })

  it('不给 onRetry 就不渲染重试按钮 —— 有些失败重试没有意义', () => {
    const { rerender } = render(<ErrorNotice error={apiError(403, 'UNAUTHORIZED', '口令不对')} />)
    expect(screen.queryByRole('button', { name: /重\s*试/ })).not.toBeInTheDocument()

    rerender(<ErrorNotice error={apiError(500, 'INTERNAL', 'x')} onRetry={() => {}} />)
    // antd 会给两字中文按钮插一个空格（「重 试」），所以用正则而不是全等
    expect(screen.getByRole('button', { name: /重\s*试/ })).toBeInTheDocument()
  })
})

// ================================================================ ColdStartBanner

function renderBanner(path = '/today') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <ColdStartBanner />
    </MemoryRouter>,
  )
}

describe('ColdStartBanner：三种状态说三句不同的话', () => {
  it('后端连不上时，运营那一支不出现命令', () => {
    setIdentity({ backendDown: true, isAdmin: false })
    const { container } = renderBanner()

    expect(screen.getByText('连不上后端服务')).toBeInTheDocument()
    expect(container.textContent).not.toContain('docker')
    expect(container.textContent).toContain('联系管理员')
  })

  it('后端连不上时，管理员那一支给出那一行命令', () => {
    setIdentity({ backendDown: true, isAdmin: true })
    const { container } = renderBanner()

    expect(container.textContent).toContain('docker compose up -d')
  })

  it('后端活着但一把口令都没填：指到设置页', () => {
    setIdentity({ backendDown: false, isAdmin: false })
    const { container } = renderBanner()

    const link = container.querySelector('a[href="/settings"]')
    expect(link).not.toBeNull()
  })

  it('已经在设置页上就不显示 —— 那一页自己会说该填什么', () => {
    setIdentity({ backendDown: false })
    const { container } = renderBanner('/settings')

    expect(container.textContent).toBe('')
  })

  it('口令填了也没被拒时不打扰：横幅让位给页面', () => {
    writeOperatorToken('op-1')
    writeAdminToken('')
    setIdentity({ backendDown: false, authFailed: false, who: { name: 'alice' } })
    const { container } = renderBanner()

    expect(container.textContent).toBe('')
  })
})
