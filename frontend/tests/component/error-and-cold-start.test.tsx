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
    // 显式写出来,而不是靠 `undefined` 恰好是假 —— 靠巧合成立的默认值,
    // 下一个人往这个对象里加字段时不会知道自己动了什么。
    // (phase2 时这行注释还说"默认按口令模式渲染,下面那几条说的都是口令的事";
    // phase6 把口令那几条删了,横幅在后端活着时一律沉默,模式只影响
    // backendDown 那句话的措辞)
    sessionAuth: false,
  },
}))

vi.mock('../../src/hooks/useIdentity', () => ({
  useIdentity: () => identity.value,
}))

const { default: ErrorNotice } = await import('../../src/components/ErrorNotice')
const { default: ColdStartBanner } = await import('../../src/components/ColdStartBanner')

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
  setIdentity({ isAdmin: false, backendDown: false, loading: false, authFailed: false, sessionAuth: false })
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

// ======================================================= ErrorNotice 重试矩阵

/**
 * A67-ISSUE-002 的行为回归。
 *
 * ## 上面那条为什么没抓到
 *
 * 它把**错误类型**和**调用点给不给 onRetry** 绑在了一起测：403 那次不传
 * onRetry，500 那次才传。于是它证明的是「onRetry 缺席时没按钮」，而真正
 * 出问题的组合 —— **403 且传了 onRetry** —— 一次都没渲染过。
 *
 * 而六个 a67 迁移页面恰好全是那个组合：它们无条件把 `onRetry` 传下去。
 * 于是测试全绿，页面照样对权限错误画重试按钮，同一张卡片的技术详情里还
 * 写着「重试不会改变结果」。
 *
 * 下面这张表**固定传 onRetry**，只让错误类型变化 —— 按钮出不出现由组件
 * 自己按 `technical.retriable` 决定，这才是组件的 action policy。
 */
describe('ErrorNotice：传了 onRetry 之后，按钮仍由 retriable 决定', () => {
  const noop = () => {}
  const retryButton = () => screen.queryByRole('button', { name: /重\s*试/ })

  /** 请求没到后端那一支：超时与连不上。`kind` 决定它是 FAILED 还是 UNKNOWN */
  function transportError(code: 'ECONNABORTED' | 'ERR_NETWORK') {
    return { isAxiosError: true, message: 'boom', code }
  }

  it.each([
    ['403 权限不足', apiError(403, 'AUTH_FORBIDDEN', '这一步需要管理员账号')],
    ['404 找不到', apiError(404, 'NOT_FOUND', '这条记录不在了')],
    ['422 参数不合法', apiError(422, 'VALIDATION_ERROR', '字段填得不对')],
    ['401 登录失效', apiError(401, 'AUTH_REQUIRED', '登录状态已失效')],
  ])('%s：不画重试按钮 —— 再点一次仍然是同一个结果', (_label, error) => {
    render(<ErrorNotice error={error} onRetry={noop} />)
    expect(retryButton()).not.toBeInTheDocument()
  })

  it.each([
    ['429 太频繁', apiError(429, 'RATE_LIMITED', '请求太频繁，请稍后再试')],
    ['500 服务端失败', apiError(500, 'INTERNAL', '服务器开小差')],
    ['503 服务不可用', apiError(503, 'UNAVAILABLE', '服务暂时不可用')],
  ])('%s：画重试按钮 —— 等一会儿再来结果可能不同', (_label, error) => {
    render(<ErrorNotice error={error} onRetry={noop} />)
    expect(retryButton()).toBeInTheDocument()
  })

  it('连不上服务：画重试按钮', () => {
    render(<ErrorNotice error={transportError('ERR_NETWORK')} onRetry={noop} />)
    expect(retryButton()).toBeInTheDocument()
  })

  it('读请求超时：画重试按钮', () => {
    render(<ErrorNotice error={transportError('ECONNABORTED')} onRetry={noop} kind="read" />)
    expect(retryButton()).toBeInTheDocument()
  })

  it('写请求超时（UNKNOWN）：不画 —— 那一次可能已经生效了', () => {
    render(<ErrorNotice error={transportError('ECONNABORTED')} onRetry={noop} kind="write" />)
    expect(retryButton()).not.toBeInTheDocument()
  })

  it('按钮与技术详情不许各说各话', async () => {
    // 管理员身份下，「可否重试」那一行和按钮出自同一个 `technical.retriable`。
    // 这一条就是 ISSUE-002 的原始症状：详情说「不会改变结果」，底下摆着按钮
    setIdentity({ isAdmin: true })
    const user = userEvent.setup()
    render(<ErrorNotice error={apiError(403, 'AUTH_FORBIDDEN', '权限不足')} onRetry={noop} />)

    await user.click(screen.getByText(/技术详情/))
    expect(await screen.findByText('重试不会改变结果')).toBeInTheDocument()
    expect(retryButton()).not.toBeInTheDocument()
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

  it('后端活着时横幅一句话都不说 —— 未登录由 /login 处理,不由横幅', () => {
    // 这里原来有三条:「还差一步:填入操作口令」「口令填了也没被拒时不打扰」
    // 「会话模式下整条让位给登录页」。三条都建立在"浏览器手里有一把口令"上,
    // 随 localStorage 口令链一起退役(PRD §32)。剩下的不变量只有一句:
    // **后端活着的时候,横幅不出声**
    setIdentity({ backendDown: false, isAdmin: false, sessionAuth: false })
    const { container } = renderBanner()

    expect(container.textContent).toBe('')
  })

  it('已经在设置页上就不显示 —— 那一页自己会说该填什么', () => {
    setIdentity({ backendDown: false })
    const { container } = renderBanner('/settings')

    expect(container.textContent).toBe('')
  })

  it('会话模式下后端连不上,仍然要说那一句 —— 那时登录页也进不去', () => {
    setIdentity({ backendDown: true, sessionAuth: true, isAdmin: false })
    renderBanner()

    expect(screen.getByText('连不上后端服务')).toBeInTheDocument()
  })
})
