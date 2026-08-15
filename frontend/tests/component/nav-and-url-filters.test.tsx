/**
 * 菜单裁剪（A8）与 URL 筛选（A9 / GAP-033）的行为用例。
 *
 * ## 它取代了什么
 *
 * `test_frontend_contract.py` 里这一组：
 *
 *     test_nav_is_grouped_and_every_entry_is_routed
 *     test_admin_guarded_pages_are_not_in_the_operator_menu
 *     test_routes_are_not_gated_by_role
 *     test_landing_pages_accept_the_filter_from_the_url
 *     test_new_pages_are_routed_and_navigable
 *
 * 前三条是用 Python 正则去 `App.tsx` 里抠 `<Route path="...">` 和 NAV 字面量，
 * 然后比对两个字符串集合。它的失败模式很具体：**改一次装配方式就全红**。
 * 这个仓库真发生过——`<BrowserRouter>` 换成 `createBrowserRouter` 那次，
 * 正则抠不到东西，于是集合为空，而「空集是任何集合的子集」让测试变绿了。
 *
 * 现在改成从**真实 router 对象**里读路由表，从**导出的 NAV 常量**里读菜单。
 * 装配方式再怎么换，这两个东西都还在。
 *
 * ## 「路由不按角色裁剪」为什么值得单独测
 *
 * 新人第一次进来还不是管理员，他必须能顺着冷启动横幅走到 `/settings` 填口令。
 * 前端要是加了角色守卫，他会被锁在门外——而这个故障只在第一次装系统时出现，
 * 也就是最没人会去回归的那条路径上。
 */
import { describe, expect, it } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { MemoryRouter, useNavigate, useSearchParams } from 'react-router-dom'
import type { ReactNode } from 'react'
import { NAV, OPERATOR_NAV_ITEM_COUNT, router } from '../../src/App'
import { PAGE_TITLE as WORKBENCH_PAGE_TITLE } from '../../src/pages/WorkbenchListPage'
import {
  enumParam, flagParam, intParam, oneOfParam, useUrlFilters,
} from '../../src/hooks/useUrlFilters'

/** 从真实 router 对象里把所有声明过的路径摊平出来 */
function declaredPaths(): Set<string> {
  const out = new Set<string>()
  const walk = (routes: readonly { path?: string; children?: readonly unknown[] }[]) => {
    for (const r of routes) {
      if (r.path) out.add(r.path)
      if (r.children) walk(r.children as never)
    }
  }
  walk(router.routes as never)
  return out
}

// ================================================================ 菜单

describe('侧栏导航', () => {
  it('每一个菜单项都指向一条真实声明过的路由', () => {
    const paths = declaredPaths()
    const dangling = NAV.flatMap((g) => g.items.map((i) => i.key)).filter((k) => !paths.has(k))

    expect(dangling).toEqual([])
  })

  it('菜单是分组的，而且组名不重复', () => {
    const labels = NAV.map((g) => g.label)

    expect(labels.length).toBeGreaterThan(1)
    expect(new Set(labels).size).toBe(labels.length)
  })

  it('「系统管理」标了 adminOnly —— 运营看不到这一组入口', () => {
    const system = NAV.find((g) => g.label === '系统管理')

    expect(system).toBeDefined()
    expect(system?.adminOnly).toBe(true)
    // 收敛的只有这一组。日常动线那三组任何账号都必须看得见,
    // 否则运营会以为系统坏了
    for (const label of ['今日工作', '商品生产', '导出与上架']) {
      expect(NAV.find((g) => g.label === label)?.adminOnly).toBeUndefined()
    }
  })

  it('operator 的侧栏里没有管理入口,admin 的有', () => {
    const forRole = (isAdmin: boolean) =>
      NAV.filter((g) => !g.adminOnly || isAdmin).flatMap((g) => g.items.map((i) => i.key))

    // 这一条在 a46 之前是**反过来**写的:「不按账号隐藏,所有设置入口都可以被
    // 发现」,并且明令禁止 NavGroup 上出现 adminOnly。撤掉它的理由是那条约束的
    // 前提消失了 —— 密码在 .env 里之后,没有人需要"先进设置页把自己变成管理员"。
    //
    // a47 往这份清单里加了 `/tasks`:它整个移进「系统管理」组,对运营是消失
    // 而不是降级。前提是工作台详情能回答"我这件商品的任务跑到哪了",
    // 下面那条 `工作台详情答得出任务状态` 钉着这个前提。
    for (const entry of [
      '/settings', '/providers', '/prompts', '/audit', '/system', '/dashboard', '/tasks',
    ]) {
      expect(forRole(false)).not.toContain(entry)
      expect(forRole(true)).toContain(entry)
    }
    // 反向:运营该有的一个都不能少
    for (const entry of ['/today', '/workbench', '/workbench-review', '/publish']) {
      expect(forRole(false)).toContain(entry)
    }
  })

  it('运营菜单收敛到目标项数 —— 这个数只写在这里(a47 §4.4)', () => {
    const operatorKeys = NAV
      .filter((g) => !g.adminOnly)
      .flatMap((g) => g.items.map((i) => i.key))

    // 仓库规矩「数字不写死」:会变的数写进门禁做一致性断言,别抄进散文。
    // 这条断言的价值不在 7 这个数,在于**它和 NAV 是同一份事实的两次读取** ——
    // 加一项菜单而不来改这里,它会红
    expect(operatorKeys).toHaveLength(7)
    expect(OPERATOR_NAV_ITEM_COUNT).toBe(operatorKeys.length)
  })

  it('侧栏标签与页头标题是同一个词 —— 一页不许有两个名字', () => {
    // ## 要防的回归
    //
    // 改之前侧栏写「商品工作台」、页头(以及浏览器标签页)写「运营工作台」。
    // 后者是**后端的域名**(`app/workbench/`、`api/workbench.py`、README 接口表),
    // 顺着接口注释渗进了界面。运营点进来看到的名字和他刚点的那一项对不上。
    //
    // ## 为什么是运行期断言而不是扫源码
    //
    // frontend/CLAUDE.md 第 3 条:能用运行期断言就别扫文本。扫源码只能看见
    // 两个中文字面量长得一样,看不见页头**用的是不是**那一个 —— 而这条读的
    // 正是 `WorkbenchListPage` 真正传给 `PageHeader` 与 `useDocumentTitle`
    // 的那个导出常量。任何一侧被改掉,它当场红。
    const item = NAV.flatMap((g) => g.items).find((i) => i.key === '/workbench')

    expect(item).toBeDefined()
    expect(item?.label).toBe(WORKBENCH_PAGE_TITLE)
  })

  it('撤出菜单的六条路由全部还在 —— 手输地址、书签、深链一条都没断', () => {
    // a47 §4.2 撤出菜单的六项。**撤出菜单 ≠ 删路由**:删掉会把一个
    // 说得清楚的页面变成 404,而运营手里那些聊天窗里发过的链接会集体失效。
    // 这条断言故意用字面量清单而不是从 NAV 反推 —— 反推的话,
    // 哪天有人连路由一起删掉,清单跟着变空,断言照样绿
    const paths = declaredPaths()
    for (const withdrawn of [
      '/reviews', '/products', '/spus/new', '/workbench-import', '/workbench-spus', '/tasks',
    ]) {
      expect(paths).toContain(withdrawn)
    }
  })

  it('同一件事不留两个入口 —— 收敛之后运营看不到重复动线', () => {
    const operatorKeys = NAV
      .filter((g) => !g.adminOnly)
      .flatMap((g) => g.items.map((i) => i.key))

    // 两套商品入口、两条并列审核入口、独立 SPU 聚合、独立任务排障入口:
    // 这四组重复正是 §4 收敛要解决的问题
    expect(operatorKeys).not.toContain('/products')
    expect(operatorKeys).not.toContain('/reviews')
    expect(operatorKeys).not.toContain('/workbench-spus')
    expect(operatorKeys).not.toContain('/tasks')
    expect(operatorKeys).toContain('/workbench')
    expect(operatorKeys).toContain('/workbench-review')
  })

  it('同一个 key 不出现在两个组里 —— 否则侧栏会同时点亮两处', () => {
    const keys = NAV.flatMap((g) => g.items.map((i) => i.key))
    expect(new Set(keys).size).toBe(keys.length)
  })

  it('路由本身不按角色裁剪：operator 手输 /settings 得到 403,不是 404', () => {
    // 菜单收敛了,路由**没有**。权限边界在后端 require_admin;前端再加一道
    // 路由守卫只会多一处能与后端不一致的地方,而代价是把一个说得清楚的 403
    // 变成一个看不懂的 404(PRD §31)
    expect(declaredPaths()).toContain('/settings')
  })

  it('根路径与兜底路径都在，手敲错地址不会白屏', () => {
    const paths = declaredPaths()
    expect(paths).toContain('/')
    expect(paths).toContain('*')
  })
})

// ================================================================ URL 筛选

function wrapper(initial: string) {
  return ({ children }: { children: ReactNode }) => (
    <MemoryRouter initialEntries={[initial]}>{children}</MemoryRouter>
  )
}

/**
 * 把 hook 和「地址栏现在长什么样」一起读出来 —— 后者才是这批用例真正的被测对象。
 *
 * ## 下面每一处 `enumParam` 都写了显式类型实参,这不是啰嗦
 *
 * `enumParam<T extends string>(allowed: Iterable<string>)` 的 `T` **在参数表里
 * 没有出现**:`allowed` 收的是 `Iterable<string>`,不是 `readonly T[]`。
 * 于是不给实参、也不给 fallback 时 `T` 无处可推,推成 `never`,
 * 整个 codec 塌成 `Codec<undefined>` —— `values.step` 的类型是 `undefined`,
 * 而 `patch({ step: 'EXPORT' })` 报 `TS2322: Type 'string' is not assignable
 * to type 'undefined'`。
 *
 * 这和 A45-batch15 修掉的那批 `Codec<never>` **是两回事**,只是报文都叫 TS2322:
 * 那一批是 `Spec` 的上界写错(每个调用点都违约,页面侧七条),
 * 这一批是**类型参数无处可推**(只有裸调用的地方中招)。
 * `src/pages/*` 全都写了实参(`enumParam<FlowStep>(...)`),所以只有这份用例文件红。
 *
 * **修法不是去改 `enumParam` 的签名。** 把 `allowed` 改成 `readonly T[]` 确实能推,
 * 但页面传进去的是 `Object.keys(NEXT_ACTION_LABEL)`,类型是 `string[]`,
 * 对 `readonly NextActionCode[]` 不成立 —— 那会把四个页面一起打红。
 *
 * 代价写明:白名单和类型实参是**两份**同样的信息,改一处忘一处不会报错。
 * 这里认这个代价,因为反过来(签名从数组推)要求所有调用点都持有字面量数组,
 * 而受众、流程步骤那几个白名单**本来就是从 label 字典的键上取的**。
 */
function mount<S extends Parameters<typeof useUrlFilters>[0]>(spec: S, initial: string) {
  return renderHook(
    () => {
      const [params] = useSearchParams()
      // navigate 一并暴露出来:「后退」那一条要走 router 自己的历史栈,
      // 不能走 window.history(理由写在那条用例里)
      return { filters: useUrlFilters(spec), query: params.toString(), navigate: useNavigate() }
    },
    { wrapper: wrapper(initial) },
  )
}

describe('useUrlFilters：URL 是唯一真相（GAP-033）', () => {
  it('URL 上的合法参数直接就是当前筛选值', () => {
    const { result } = mount(
      { next_action: enumParam<'CONFIRM_ATTRIBUTE' | 'EXPORT'>(['CONFIRM_ATTRIBUTE', 'EXPORT']) },
      '/workbench?next_action=CONFIRM_ATTRIBUTE',
    )

    expect(result.current.filters.values.next_action).toBe('CONFIRM_ATTRIBUTE')
  })

  it('白名单外的取值退回默认值，而且**不改写地址栏**', () => {
    // 顺手删掉非法参数 = 在渲染期发起一次导航，和上一版 clear() 是同一个错误：
    // 地址栏和用户抢方向盘，而且把一次手敲变成后退栈里的一格
    const { result } = mount(
      { next_action: enumParam<'CONFIRM_ATTRIBUTE'>(['CONFIRM_ATTRIBUTE']) },
      '/workbench?next_action=TELEPORT',
    )

    expect(result.current.filters.values.next_action).toBeUndefined()
    expect(result.current.query).toBe('next_action=TELEPORT')
  })

  it('页面接手之后参数**还在**——这正是上一版擦掉的那一条', () => {
    const { result } = mount(
      { next_action: enumParam<'CONFIRM_ATTRIBUTE'>(['CONFIRM_ATTRIBUTE']) },
      '/workbench?next_action=CONFIRM_ATTRIBUTE',
    )

    // 上一版这里是 clear()，断言 raw === null。刷新保留 / 后退保留 / 地址可复制
    // 三条要求全部死在那一行上
    expect(result.current.query).toContain('next_action=CONFIRM_ATTRIBUTE')
  })

  it('默认值不写进 URL：改回默认等于把参数删掉', () => {
    const { result } = mount(
      { only_blocked: flagParam(), page: intParam(1, { min: 1 }) },
      '/workbench',
    )

    act(() => result.current.filters.patch({ only_blocked: true, page: 3 }))
    expect(result.current.query).toBe('only_blocked=1&page=3')

    act(() => result.current.filters.patch({ only_blocked: false, page: 1 }))
    expect(result.current.query).toBe('')
  })

  it('默认值是 true 的开关，写进 URL 的是「关掉它」那一次', () => {
    const { result } = mount({ workbench_only: flagParam(true) }, '/audit')

    expect(result.current.filters.values.workbench_only).toBe(true)

    act(() => result.current.filters.patch({ workbench_only: false }))
    expect(result.current.query).toBe('workbench_only=0')
  })

  it('认不出来的开关取值退回默认值，不是一律退回 false', () => {
    // 退回 false 会把审计页的降噪悄悄关掉：界面显示"开着"、数据是全量
    const { result } = mount({ workbench_only: flagParam(true) }, '/audit?workbench_only=x')

    expect(result.current.filters.values.workbench_only).toBe(true)
  })

  it('只有几档的参数：不在档位里的值退回默认，下拉框不会变成空白', () => {
    const { result } = mount({ days: oneOfParam(30, [7, 30, 90]) }, '/audit?days=14')

    expect(result.current.filters.values.days).toBe(30)
  })

  it('页码越界退回默认值', () => {
    const { result } = mount({ page: intParam(1, { min: 1 }) }, '/workbench?page=0')
    expect(result.current.filters.values.page).toBe(1)

    const junk = mount({ page: intParam(1, { min: 1 }) }, '/workbench?page=abc')
    expect(junk.result.current.filters.values.page).toBe(1)
  })

  it('patch 一次写多项 —— 换排序 + 回第一页只进一格后退栈', () => {
    const { result } = mount(
      { sort: enumParam<'sku' | 'completion'>(['sku', 'completion']), page: intParam(1, { min: 1 }) },
      '/workbench?page=5',
    )

    act(() => result.current.filters.patch({ sort: 'completion', page: 1 }))

    expect(result.current.query).toBe('sort=completion')
  })

  it('reset 只删自己声明过的参数，URL 上别人的参数留着', () => {
    const { result } = mount(
      { step: enumParam<'GENERATE'>(['GENERATE']) },
      '/workbench?step=GENERATE&tab=material',
    )

    act(() => result.current.filters.reset())

    expect(result.current.query).toBe('tab=material')
  })

  it('signature 是规范形式：?page=1 和不带 page 给出同一个签名', () => {
    // 不规范化的话，这两者之间来回会白白清一次勾选，而"勾选莫名其妙没了"
    // 只要发生过一次，运营下次就不敢用批量了
    const withDefault = mount({ page: intParam(1, { min: 1 }) }, '/workbench?page=1')
    const without = mount({ page: intParam(1, { min: 1 }) }, '/workbench')

    expect(withDefault.result.current.filters.signature).toBe(
      without.result.current.filters.signature,
    )
  })

  it('signature 跟着筛选变 —— 清勾选那条 effect 靠它', () => {
    const { result } = mount({ step: enumParam<'GENERATE' | 'EXPORT'>(['GENERATE', 'EXPORT']) }, '/workbench')
    const before = result.current.filters.signature

    act(() => result.current.filters.patch({ step: 'EXPORT' }))

    expect(result.current.filters.signature).not.toBe(before)
  })

  it('后退能回到上一组筛选条件（§8.2「后退保留」）', () => {
    // 这一条是整批的落点：patch 走 push 而不是 replace，
    // 所以后退回到的是上一组条件，而不是直接离开这一页。
    //
    // ## 为什么是 navigate(-1) 而不是 window.history.back()
    //
    // 这条用例原来写的是 `window.history.back()`，而上面的 wrapper 是
    // `<MemoryRouter>` —— 它的历史栈**在内存里**，根本不监听 window.history。
    // 于是 back() 打在 jsdom 自己那个栈上，router 一无所知，
    // `values.step` 停在 EXPORT，断言恒失败。
    //
    // 它不是 react-router 7 带来的：换 router 之前同样红。这条用例从写下来
    // 那天起就没有通过过，测的也从来不是它声称在测的东西。
    //
    // `navigate(-1)` 走的是 MemoryRouter 自己的栈，push/replace 的区别照样验得到：
    // patch 要是改成 replace，两次 patch 只留一格，navigate(-1) 退不动，这条会红。
    const { result } = mount({ step: enumParam<'GENERATE' | 'EXPORT'>(['GENERATE', 'EXPORT']) }, '/workbench')

    act(() => result.current.filters.patch({ step: 'GENERATE' }))
    act(() => result.current.filters.patch({ step: 'EXPORT' }))
    expect(result.current.filters.values.step).toBe('EXPORT')

    act(() => {
      void result.current.navigate(-1)
    })

    return waitFor(() => expect(result.current.filters.values.step).toBe('GENERATE'))
  })
})
