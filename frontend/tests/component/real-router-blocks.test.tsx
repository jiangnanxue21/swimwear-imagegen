/**
 * 真实 router 对象上的离开保护（A45-batch14-4 §五之一）。
 *
 * ## 这里补的是哪一条缝
 *
 * 纯层的 `test_router_is_a_data_router_so_blocker_works` 断言的是
 * **`App.tsx` 源码里出现过 `createBrowserRouter` 这串字**。
 * batch14-4 §五之一自己记了账：那证明不了机制在运行时成立。
 *
 * 而 `unsaved-guard.test.tsx` 那 6 条虽然是真跑 react-router，却是
 * **自己 `createMemoryRouter` 搭的壳**。两边合起来仍然有一条缝：
 *
 *     源码扫描      看得见 `createBrowserRouter` 这串字，看不见 router 对象
 *     UnsavedGuard  证明了「给它一个数据路由，它会拦」
 *     没有人证明    「**这个应用真正挂上去的那个 router** 是数据路由」
 *
 * 缝的宽度正好是一次 `<BrowserRouter>` 回退：那 6 条照样全绿（它们自带壳），
 * 纯层那条也照样绿（只要 `App.tsx` 里还留着那个词），而线上离开保护
 * 整条静默失效。§五之一说的「拦不住『这套机制从来就没生效过』」就是它。
 *
 * ## 为什么不渲染整个应用
 *
 * 最"完整"的做法是 `<RouterProvider router={router} />` 挂起来、走到一个
 * 真实编辑页、改一下、点菜单。但那需要 QueryClientProvider、antd 上下文、
 * 以及给几十个查询兜底的 MSW——**这些垫片本身会变成被测对象**，
 * 而它们和离开保护没有一分钱关系。垫塌了的表现是这条用例红，
 * 读的人会以为离开保护坏了。
 *
 * `useBlocker` 内部调的就是 `router.getBlocker(key, fn)`。直接调它，
 * 验的是同一条通路，而且没有一行垫片。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, useBlocker } from 'react-router-dom'
import { router } from '../../src/App'

const PROBE = 'batch14-4-real-router-probe'

// 刻意**不**调 `router.initialize()`。
//
// 这条路走过一次：`initialize()` 会 `history.listen(...)`，而这个 router
// 是模块级单例，第二次就撞上「A history only accepts one active listener」。
// 更要紧的是根本不需要它——本应用所有路由都只有 element、没有 loader，
// 于是 `createRouter` 当场就把 `state.initialized` 置真了；`initialize()`
// 只负责接管浏览器的前进/后退（POP），而下面验的是 `navigate()` 这条路。
afterEach(() => {
  // router 是模块级单例，留下的 blocker 会挡住同文件后面的用例
  try {
    router.deleteBlocker(PROBE)
  } catch {
    /* 没挂上过 */
  }
})

describe('真实 router 对象', () => {
  it('导出的 router 带着 useBlocker 要用的那套 API', () => {
    // 源码扫描看得见 `createBrowserRouter` 这串字；这条看的是**对象**。
    // 它比字符串多抓一类：react-router 升级把 blocker API 改名或拿掉时，
    // `App.tsx` 里那个词一个字都不会变，而离开保护已经没了
    expect(typeof router.getBlocker).toBe('function')
    expect(typeof router.deleteBlocker).toBe('function')
    expect(router.state).toBeTypeOf('object')
  })

  it('在真实 router 上挂一个 blocker，导航真的走不动', async () => {
    // 先确认这次导航在没有 blocker 时是走得通的，否则下面的"没动"没有意义
    await router.navigate('/tasks')
    expect(router.state.location.pathname).toBe('/tasks')

    router.getBlocker(PROBE, () => true)
    await router.navigate('/products')

    // 关键的一句：真实 router 上，导航**停在原地**
    expect(router.state.location.pathname).toBe('/tasks')
    expect(router.getBlocker(PROBE, () => true).state).toBe('blocked')
  })

  it('blocker 放行之后，那次被挂起的导航走得掉', async () => {
    await router.navigate('/tasks')

    router.getBlocker(PROBE, () => true)
    await router.navigate('/products')
    expect(router.state.location.pathname).toBe('/tasks')

    // 对应 UnsavedGuard 里「保存后放行」那一句。不验这条的话，
    // 一个"永远拦、从不放行"的实现也能让上一条变绿——
    // 而它的表现是页面看起来正常但再也走不动
    router.getBlocker(PROBE, () => true).proceed?.()
    await Promise.resolve()
    expect(router.state.location.pathname).toBe('/products')
  })
})

describe('非数据路由', () => {
  it('useBlocker 在非数据路由下是**大声**失败，不是静默失效', () => {
    // 这条钉的不是我们的代码，是 react-router 的失败方式。
    //
    // §五之一担心的是"这套机制从来就没生效过"。只要 `useBlocker` 在
    // `<BrowserRouter>` 下会抛，那么真有人换回去，页面当场白屏——刺眼、
    // 当天就会被发现。哪天 react-router 把它改成静默 no-op，
    // 这条会红，而那时候上面那三条和纯层那条就是仅剩的防线了
    function Probe() {
      useBlocker(() => true)
      return <span>不该渲染出来</span>
    }

    // React 会把这次抛错原样打进 console.error（一整屏组件栈）。这里是
    // **预期内**的抛错，不静音的话每次跑测试都多一屏噪声——而 setup.ts
    // 顶部为 jsdom 垫片写的理由正是这条：真正的失败信息会混在那一屏里
    const quiet = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      expect(() =>
        render(
          <MemoryRouter>
            <Probe />
          </MemoryRouter>,
        ),
      ).toThrow()
    } finally {
      quiet.mockRestore()
    }
    expect(screen.queryByText('不该渲染出来')).not.toBeInTheDocument()
  })
})
