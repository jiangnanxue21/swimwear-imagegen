/**
 * 路由级代码分割不许被悄悄退回去(a51)。
 *
 * ## 要防的回归
 *
 * 分割之前首屏是一个 1,933 KB 的 chunk —— 28 个页面全部静态 import,
 * 一个只想看今日待办的人要先下完发布页、审核台、批次页、设置页的代码。
 * 拆完之后首屏 986 KB(gzip 597 -> 320 KB)。
 *
 * 而退回去只需要**一行**:新加一个页面时顺手写成
 * `import FooPage from './pages/FooPage'`。那一行不会报错、不会变红,
 * 只会让首屏悄悄涨回去 —— 体积没有任何门禁在看,构建的 500 kB 警告
 * 在拆分之后仍然常亮(共享的 antd 块本来就超),所以它也提示不了什么。
 *
 * ## 判据取真实 router 对象上的元素类型,不是扫源码
 *
 * `React.lazy()` 产出的组件带 `$$typeof === Symbol.for('react.lazy')`。
 * 这是**运行期事实**:写成 lazy 但忘了用、或者用了别的包装,都会当场露出来。
 * 扫源码只能看见 `lazy(` 这几个字符,看不见它有没有真的落到路由上。
 * (frontend/CLAUDE.md 的第 3 条也在说同一件事:能用运行期断言就别扫文本。)
 */
import { describe, it, expect } from 'vitest'
import type { ComponentType, ReactElement } from 'react'
import { router } from '../../src/App'

/**
 * 刻意保持同步导入的页面。**改这张表要连着改 `App.tsx` 上那段说明。**
 *
 * 三个都有各自的理由,不是"暂时没拆":登录页是未登录时的第一眼,
 * 今日待办是 `/` 的默认落点(首屏必到),404 要在什么都不对的时候
 * 还能渲染出来 —— 而那正是最不该依赖"再取一个 chunk"的时刻。
 */
const EAGER_BY_DESIGN = new Set(['LoginPage', 'TodayPage', 'NotFoundPage'])

const LAZY = Symbol.for('react.lazy')

interface RouteLike {
  path?: string
  element?: unknown
  children?: readonly RouteLike[]
}

function pageElements(): { path: string; type: unknown }[] {
  const out: { path: string; type: unknown }[] = []
  const walk = (routes: readonly RouteLike[], parent: string) => {
    for (const r of routes) {
      const path = r.path ? `${parent}${r.path}` : parent
      if (r.element) out.push({ path: path || '/', type: (r.element as ReactElement).type })
      if (r.children) walk(r.children, path)
    }
  }
  walk(router.routes as readonly RouteLike[], '')
  return out
}

function nameOf(type: unknown): string {
  const t = type as ComponentType & { displayName?: string; name?: string }
  return t?.displayName ?? t?.name ?? ''
}

function isLazy(type: unknown): boolean {
  return (type as { $$typeof?: symbol })?.$$typeof === LAZY
}

describe('路由级代码分割', () => {
  it('页面组件默认是懒加载的,同步导入的只有名单上那三个', () => {
    // 布局路由(AppLayout)和重定向(Navigate)不是页面,它们必须同步 ——
    // 前者是壳子,后者根本不渲染内容。按"不是 lazy 且不在白名单"筛出来之后,
    // 剩下的应当只有这两类 + 三个署名的页面。
    const eager = pageElements()
      .filter((e) => !isLazy(e.type))
      .map((e) => nameOf(e.type))
      .filter((n) => n.endsWith('Page'))

    const unexpected = eager.filter((n) => !EAGER_BY_DESIGN.has(n))
    expect(unexpected).toEqual([])
  })

  it('绝大多数页面确实被拆出去了', () => {
    // 反向的那一半。上面那条在**所有**页面都变成同步导入时也会红,
    // 但在另一个方向上是空的:如果哪天路由表被改成一条都不剩,
    // 它同样绿。这一条要求真的有一批 lazy 页面在。
    const lazyCount = pageElements().filter((e) => isLazy(e.type)).length
    expect(lazyCount).toBeGreaterThan(20)
  })

  it('名单上的三个页面确实还在路由表里', () => {
    // 防的是白名单变成一份只增不减、指向已经不存在的页面的名单。
    const names = new Set(pageElements().map((e) => nameOf(e.type)))
    for (const eagerName of EAGER_BY_DESIGN) {
      expect(names.has(eagerName), `${eagerName} 不在路由表里了,白名单该更新`).toBe(true)
    }
  })
})
