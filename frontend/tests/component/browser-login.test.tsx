/**
 * 浏览器登录的前端行为(a46-phase2)。
 *
 * ## 这一批用例钉的是「最后一跳」
 *
 * phase1 把后端做完了:`/auth/login` 发 HttpOnly Cookie、`resolve_identity`
 * Session 优先、配置不全直接拒绝启动,那一侧有自己的用例。而链路真正会断的
 * 地方在这一侧,而且断了**不会有任何报错**:
 *
 *     withCredentials 没开     Cookie 不出浏览器。同源部署下看不出来,
 *                              跨源部署下"登录成功然后立刻又要登录"
 *     enabled 还挂在 hasToken   whoami 一次都不发,顶栏永远"未登录",
 *                              而没有任何请求失败
 *     登录页挂进 AppLayout      AppLayout 要跳登录页,而登录页在它里面
 *
 * 三种的共同点是:两侧的单元测试都绿,缺陷只在浏览器里可见 —— 也就是本仓
 * §3.43 那一族。所以下面每一条都尽量读**真实的对象**(axios 实例的 defaults、
 * 真实的路由组件树),而不是断言源码里有某个词。
 *
 * ## 为什么 mock 掉 useIdentity 和三条横幅 —— 以及这么做**看不见什么**
 *
 * 与 `error-and-cold-start.test.tsx` 同一条理由:这几条用例要验的东西**全部**
 * 只取决于 `useIdentity` 的返回值,而它内部是两个 `useQuery`。真跑要拖进一整套
 * HTTP 桩,那些桩会变成被测对象 —— 桩塌了的表现是这几条红,而读的人会以为
 * 登录坏了。`AppLayout` 里那三条横幅同理:它们和登录判定没有一分钱关系。
 *
 * **代价要写在这里,因为它已经咬过一次。** 把 `useIdentity` 换成一个常量之后,
 * 这个文件验的是"拿到 `needsLogin` 之后怎么办",而验不到"`needsLogin` 会不会
 * 变成真"。a46-phase2 第一版恰好断在后半句:401 信号算出来了、没有人订阅,
 * 于是会话在用着的过程中过期时 `needsLogin` 永远是假 —— 而这个文件全绿。
 *
 * 补在两处:`tests/unit/client.test.ts` 那一组不 mock 任何东西,直接让真实的
 * 响应拦截器跑;纯层 `test_a46_phase2_browser_login_seam.py` 钉"信号必须有
 * 消费端"。往这个文件里加用例之前,先想一遍要验的东西在不在 mock 的另一侧。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App as AntApp } from 'antd'
import type { ReactNode } from 'react'

const identity = vi.hoisted(() => ({
  value: {
    who: null as unknown,
    isAdmin: false,
    backendDown: false,
    loading: false,
    authFailed: false,
    sessionAuth: true,
    needsLogin: false,
  },
}))

const auth = vi.hoisted(() => ({
  login: vi.fn(),
  logout: vi.fn(),
  whoami: vi.fn(),
}))

vi.mock('../../src/hooks/useIdentity', () => ({
  useIdentity: () => identity.value,
}))

vi.mock('../../src/api/auth', () => ({
  authApi: auth,
}))

// AppLayout 顶部那三条横幅各自要一套 HTTP 桩,而它们和登录判定无关
vi.mock('../../src/components/ColdStartBanner', () => ({ default: () => null }))
vi.mock('../../src/components/EnvironmentBanner', () => ({ default: () => null }))
vi.mock('../../src/components/SpendAlertBanner', () => ({ default: () => null }))

const { default: LoginPage, safeNext } = await import('../../src/pages/LoginPage')
const { default: AppLayout } = await import('../../src/components/AppLayout')
const { apiClient } = await import('../../src/api/client')

function setIdentity(patch: Partial<typeof identity.value>) {
  identity.value = { ...identity.value, ...patch }
}

function Harness({ initial, children }: { initial: string; children: ReactNode }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={queryClient}>
      <AntApp>
        <MemoryRouter initialEntries={[initial]}>{children}</MemoryRouter>
      </AntApp>
    </QueryClientProvider>
  )
}

beforeEach(() => {
  auth.login.mockReset()
  auth.logout.mockReset()
  setIdentity({
    who: null,
    isAdmin: false,
    backendDown: false,
    loading: false,
    authFailed: false,
    sessionAuth: true,
    needsLogin: false,
  })
})

// ================================================================ 最后一跳

describe('请求要带上 Session Cookie', () => {
  it('apiClient 真的开着 withCredentials —— 少了它 Cookie 不出浏览器', () => {
    // 读的是**实例的 defaults**,不是源码里有没有那个词。跨源部署下少了它的
    // 表现是"登录接口 200,下一个请求 401",而同源开发环境完全看不出来
    expect(apiClient.defaults.withCredentials).toBe(true)
  })
})

// ================================================================ 登录页

describe('登录页', () => {
  it('提交后调登录接口,并把用户名两端的空格去掉', async () => {
    auth.login.mockResolvedValue({ name: 'operator', role: 'operator', is_admin: false })

    render(
      <Harness initial="/login">
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/today" element={<div>今日工作页</div>} />
        </Routes>
      </Harness>,
    )

    await userEvent.type(screen.getByLabelText('用户名'), '  operator  ')
    await userEvent.type(screen.getByLabelText('密码'), 'pw-123456')
    await userEvent.click(screen.getByRole('button', { name: '登录' }))

    await waitFor(() => expect(auth.login).toHaveBeenCalledTimes(1))
    // 密码**不 trim**:两端的空格可能是密码的一部分,而用户名不可能
    expect(auth.login.mock.calls[0][0]).toEqual({
      username: 'operator',
      password: 'pw-123456',
    })
  })

  it('登录成功后回到 `?next=` 上那一页,而不是一律回首页', async () => {
    auth.login.mockResolvedValue({ name: 'admin', role: 'admin', is_admin: true })

    render(
      <Harness initial="/login?next=%2Fwizard%2Fp-1">
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/today" element={<div>今日工作页</div>} />
          <Route path="/wizard/:id" element={<div>向导页</div>} />
        </Routes>
      </Harness>,
    )

    await userEvent.type(screen.getByLabelText('用户名'), 'admin')
    await userEvent.type(screen.getByLabelText('密码'), 'pw')
    await userEvent.click(screen.getByRole('button', { name: '登录' }))

    // 运营常态是从聊天窗点一条深链进来,会话恰好过期。只把他丢回首页的话,
    // 那条链接就白点了
    await waitFor(() => expect(screen.getByText('向导页')).toBeInTheDocument())
  })

  it('登录失败时把后端那句话原样显示出来,不自己编一句', async () => {
    auth.login.mockRejectedValue({
      isAxiosError: true,
      message: 'boom',
      response: {
        status: 401,
        headers: {},
        data: { error: { code: 'AUTH_FAILED', message: '用户名或密码错误' } },
      },
    })

    render(
      <Harness initial="/login">
        <Routes>
          <Route path="/login" element={<LoginPage />} />
        </Routes>
      </Harness>,
    )

    await userEvent.type(screen.getByLabelText('用户名'), 'admin')
    await userEvent.type(screen.getByLabelText('密码'), 'wrong')
    await userEvent.click(screen.getByRole('button', { name: '登录' }))

    await waitFor(() =>
      expect(screen.getByText(/用户名或密码错误/)).toBeInTheDocument(),
    )
    // 后端把"用户名不存在 / 密码错 / 该账号没配密码"合并成同一句话,
    // 前端不许在这里替它拆开
    expect(screen.queryByText(/用户不存在/)).toBeNull()
  })

  it('口令模式的部署不画登录表单,直说这里没有这条路', () => {
    setIdentity({ sessionAuth: false })

    render(
      <Harness initial="/login">
        <Routes>
          <Route path="/login" element={<LoginPage />} />
        </Routes>
      </Harness>,
    )

    // 画一个永远登不进去的表单,人会一直试密码
    expect(screen.queryByRole('button', { name: '登录' })).toBeNull()
    expect(screen.getByText('这个部署没有开启浏览器登录')).toBeInTheDocument()
  })
})

describe('safeNext:`?next=` 只接受本站绝对路径', () => {
  it('本站路径原样返回', () => {
    expect(safeNext('/wizard/p-1?step=COPY')).toBe('/wizard/p-1?step=COPY')
  })

  it('协议相对与绝对 URL 一律落回首页 —— 否则这是一个开放重定向', () => {
    expect(safeNext('//evil.example/x')).toBe('/today')
    expect(safeNext('https://evil.example/x')).toBe('/today')
    expect(safeNext('javascript:alert(1)')).toBe('/today')
  })

  it('登录页自己不能当落点,否则登录成功后停在原地', () => {
    expect(safeNext('/login')).toBe('/today')
    expect(safeNext('/login?next=%2Ftoday')).toBe('/today')
  })

  it('没给就回首页', () => {
    expect(safeNext(null)).toBe('/today')
  })
})

// ================================================================ 外框

describe('AppLayout 的登录闸', () => {
  it('会话模式下没有登录态时把人送到登录页,并带上原来那一页', () => {
    setIdentity({ needsLogin: true })

    render(
      <Harness initial="/tasks?status=RUNNING">
        <Routes>
          <Route element={<AppLayout groups={[]} />}>
            <Route path="/tasks" element={<div>生成任务页</div>} />
          </Route>
          <Route path="/login" element={<div>登录页占位</div>} />
        </Routes>
      </Harness>,
    )

    expect(screen.getByText('登录页占位')).toBeInTheDocument()
    expect(screen.queryByText('生成任务页')).toBeNull()
  })

  it('探测还没有结论时**不**跳转 —— 否则已登录的人每次刷新都闪一下登录页', () => {
    setIdentity({ needsLogin: false, loading: true })

    render(
      <Harness initial="/tasks">
        <Routes>
          <Route element={<AppLayout groups={[]} />}>
            <Route path="/tasks" element={<div>生成任务页</div>} />
          </Route>
          <Route path="/login" element={<div>登录页占位</div>} />
        </Routes>
      </Harness>,
    )

    expect(screen.getByText('生成任务页')).toBeInTheDocument()
  })

  it('会话模式下顶栏给「退出登录」,点它会调后端并回到登录页', async () => {
    auth.logout.mockResolvedValue(undefined)
    setIdentity({ who: { name: 'operator', role: 'operator', is_admin: false } })

    render(
      <Harness initial="/tasks">
        <Routes>
          <Route element={<AppLayout groups={[]} />}>
            <Route path="/tasks" element={<div>生成任务页</div>} />
          </Route>
          <Route path="/login" element={<div>登录页占位</div>} />
        </Routes>
      </Harness>,
    )

    await userEvent.click(screen.getByRole('button', { name: /operator/ }))
    await userEvent.click(await screen.findByText('退出登录'))

    await waitFor(() => expect(auth.logout).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByText('登录页占位')).toBeInTheDocument())
  })

  it('口令模式下没有「退出登录」—— 那里没有可退的东西', async () => {
    setIdentity({
      sessionAuth: false,
      who: { name: 'operator', role: 'operator', is_admin: false },
    })

    render(
      <Harness initial="/tasks">
        <Routes>
          <Route element={<AppLayout groups={[]} />}>
            <Route path="/tasks" element={<div>生成任务页</div>} />
          </Route>
        </Routes>
      </Harness>,
    )

    await userEvent.click(screen.getByRole('button', { name: /operator/ }))
    // 给一个点了什么都不会发生的菜单项,比不给更糟
    expect(await screen.findByText('更换口令')).toBeInTheDocument()
    expect(screen.queryByText('退出登录')).toBeNull()
  })
})
