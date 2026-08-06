/**
 * 未保存离开保护（A11）的行为用例。
 *
 * ## 它取代了什么
 *
 * `backend/tests/pure/test_frontend_contract.py` 里原来有四条：
 *
 *     test_unsaved_guard_covers_all_four_ways_out
 *     test_unsaved_guard_releases_when_the_edit_is_saved
 *     test_every_editing_surface_is_guarded
 *     test_router_is_a_data_router_so_blocker_works
 *
 * 它们的做法是**用 Python 读 TSX 源码**，搜 `useBlocker`、搜 `beforeunload`、
 * 搜 `createBrowserRouter` 这些字符串。那能证明的只有「这几个词写在文件里」。
 *
 * 它证明不了的恰好是全部要紧的事：blocker 真的会拦下导航吗？点「留在本页」
 * 真的留得住吗？dirty 变回 false 之后那次被挂起的导航会不会永远卡住？
 * 最后一条尤其要紧——它的故障表现是**页面看起来正常但再也走不动**，
 * 而源码里那三个词一个不少。
 *
 * 下面这五条把同样的四件事放进真实的 react-router 里跑一遍。
 */
import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { Link, RouterProvider, createMemoryRouter, createRoutesFromElements, Route } from 'react-router-dom'
import UnsavedGuard from '../../src/components/UnsavedGuard'

/**
 * 一个最小的编辑页：一个「改一下」按钮把 dirty 置真，一个「保存」置假，
 * 外加一条通往别处的链接。要验的四条路径里有三条都是站内导航，
 * 这个外壳就够了。
 */
function EditPage({ initiallyDirty = false }: { initiallyDirty?: boolean }) {
  const [dirty, setDirty] = useState(initiallyDirty)
  return (
    <>
      <UnsavedGuard dirty={dirty} what="文案" />
      <button onClick={() => setDirty(true)}>改一下</button>
      <button onClick={() => setDirty(false)}>保存</button>
      <Link to="/elsewhere">去别处</Link>
      <span>编辑页</span>
    </>
  )
}

function renderApp(initiallyDirty = false) {
  const router = createMemoryRouter(
    createRoutesFromElements(
      <>
        <Route path="/" element={<EditPage initiallyDirty={initiallyDirty} />} />
        <Route path="/elsewhere" element={<span>别处</span>} />
      </>,
    ),
    { initialEntries: ['/'] },
  )
  return { ...render(<RouterProvider router={router} />), router }
}

describe('UnsavedGuard', () => {
  it('没有未保存改动时，导航畅通无阻', async () => {
    const user = userEvent.setup()
    renderApp()

    await user.click(screen.getByText('去别处'))

    expect(await screen.findByText('别处')).toBeInTheDocument()
  })

  it('有未保存改动时，站内导航被拦下并弹确认框', async () => {
    const user = userEvent.setup()
    renderApp()

    await user.click(screen.getByText('改一下'))
    await user.click(screen.getByText('去别处'))

    expect(await screen.findByText(/文案还有未保存的改动/)).toBeInTheDocument()
    // 关键：还停在原地。源码扫描永远看不到这一句
    expect(screen.getByText('编辑页')).toBeInTheDocument()
  })

  it('点「留在本页」真的留得住', async () => {
    const user = userEvent.setup()
    renderApp()

    await user.click(screen.getByText('改一下'))
    await user.click(screen.getByText('去别处'))
    await screen.findByText(/文案还有未保存的改动/)
    await user.click(screen.getByText('留在本页'))

    await waitFor(() => expect(screen.queryByText('别处')).not.toBeInTheDocument())
    expect(screen.getByText('编辑页')).toBeInTheDocument()
  })

  it('点「放弃改动并离开」才放行', async () => {
    const user = userEvent.setup()
    renderApp()

    await user.click(screen.getByText('改一下'))
    await user.click(screen.getByText('去别处'))
    await screen.findByText(/文案还有未保存的改动/)
    await user.click(screen.getByText('放弃改动并离开'))

    expect(await screen.findByText('别处')).toBeInTheDocument()
  })

  it('挂起期间改动被保存了，那次导航要自动放行而不是永远卡住', async () => {
    // 这一条是整个文件里最值得写的。故障表现是「页面看起来正常但再也走不动」，
    // 而源码里 useBlocker / proceed 一个词都不少 —— 静态扫描对它完全无能为力
    const user = userEvent.setup()
    renderApp()

    await user.click(screen.getByText('改一下'))
    await user.click(screen.getByText('去别处'))
    await screen.findByText(/文案还有未保存的改动/)

    // 用户回头点了保存（真实场景里是确认框背后的那个保存按钮）
    await user.click(screen.getByText('保存'))

    expect(await screen.findByText('别处')).toBeInTheDocument()
  })

  it('刷新与关标签页这条路径靠 beforeunload，只在 dirty 时挂上', async () => {
    const user = userEvent.setup()
    const add = vi.spyOn(window, 'addEventListener')
    const remove = vi.spyOn(window, 'removeEventListener')

    renderApp()
    expect(add.mock.calls.some(([type]) => type === 'beforeunload')).toBe(false)

    await user.click(screen.getByText('改一下'))
    await waitFor(() =>
      expect(add.mock.calls.some(([type]) => type === 'beforeunload')).toBe(true),
    )

    // 保存之后要摘掉，否则改完又存好的页面刷新时还会弹浏览器原生确认框 ——
    // 那种弹窗文案改不了，运营只会觉得系统在乱拦
    await user.click(screen.getByText('保存'))
    await waitFor(() =>
      expect(remove.mock.calls.some(([type]) => type === 'beforeunload')).toBe(true),
    )

    add.mockRestore()
    remove.mockRestore()
  })
})
