/**
 * 筛选状态住在 URL 里(GAP-033 / FE-GLOBAL-06)。
 *
 * ## 它取代了什么,以及为什么不是打补丁
 *
 * 上一版是 `useUrlSeed`:**URL 参数是初值,不是真相** —— 页面接手之后立刻
 * `clear()` 把参数擦掉。那个设计自己把代价写清楚了:「参数擦掉之后地址栏
 * 不再带筛选条件,刷新页面会回到全量列表」。于是 §8.2 的四条要求里,
 * URL 可复制、刷新保留、后退保留三条都不成立,只剩「点进来能带条件」一条。
 *
 * 擦参数这件事本身也是一条评审项(FE-GLOBAL-06「URL 筛选被消费后清除」)。
 * 它没法靠给 `useUrlSeed` 打补丁修好:**只要 URL 是初值,组件内那份 state
 * 就是第二处真相**,而两处真相之间任何一次重新同步都要在「谁顶掉谁」上
 * 做选择 —— 擦参数正是当时为了不让 URL 顶掉运营的手选而做的取舍。
 * 换句话说,补丁修的是症状,病根是「有两份」。
 *
 * 所以这里反过来:**URL 是唯一真相**,组件内不再存筛选值。
 *
 * ## 默认值不写进 URL
 *
 * `write()` 返回 `null` 表示「把这个参数从 URL 上删掉」。每个 codec 在
 * 值等于默认值时都返回 `null`,于是 `/workbench` 和 `/workbench?page=1&page_size=20`
 * 是同一个状态,而地址栏上只会出现**运营真的改过的那几项**。
 *
 * 这不只是好看:URL 可复制这条要求的实际形态是「运营把地址贴进群里」,
 * 一串二十个参数的地址没有人会贴。
 *
 * ## 非法值只忽略,不改写地址栏
 *
 * 手敲 `?step=TELEPORT` 应该看到一张全量列表,不是一个错误页 —— 这条沿用
 * `useUrlSeed`。但**不许顺手把非法参数从 URL 上删掉**:那是在渲染期发起一次
 * 导航,和 `clear()` 是同一个形状的错误(地址栏和用户抢方向盘),
 * 而且会把一次手敲变成后退栈里的一格。读的时候退回默认值就够了。
 *
 * ## push 而不是 replace
 *
 * `useUrlSeed.clear()` 用的是 `replace: true`,理由是「这不是一次导航」。
 * 这里相反:改筛选**就是**一次导航,后退应该回到上一组条件 —— 那正是
 * §8.2「后退保留」要的东西。代价是连点五个筛选要按五次后退,
 * 那是筛选类界面的正常行为,也是运营唯一能撤销一次误点的手段。
 *
 * ## 它不管什么
 *
 * **不管勾选、不管游标。** 勾了哪几件、快审停在第几张,这些不是筛选条件,
 * 是一次会话里的临时位置;写进 URL 的话,贴给同事的地址会带上「我勾了这 20 件」,
 * 而那 20 件对他没有意义。它们仍然是组件内 state。
 *
 * 但**筛选变了就要清掉它们**,而这件事有一个新的失效方向,见 `signature`。
 */
import { useCallback, useMemo, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'

/**
 * 一个参数怎么读、怎么写。
 *
 * `write` 返回 `null` = 从 URL 上删掉这个参数(默认值走这条路)。
 */
export interface Codec<T> {
  read: (raw: string | null) => T
  write: (value: T) => string | null
}

/**
 * 固定取值的字符串参数(流程步骤、下一步动作、受众、快审分段器……)。
 *
 * 不给 `fallback` 时默认「不筛」(`undefined`);给了就以它为默认值 ——
 * 快审那个分段器没有"不选"这一档,它的默认是「全部待批准」,
 * 而默认值同样不该出现在地址栏里。
 */
export function enumParam<T extends string>(allowed: Iterable<string>): Codec<T | undefined>
export function enumParam<T extends string>(allowed: Iterable<string>, fallback: T): Codec<T>
export function enumParam<T extends string>(
  allowed: Iterable<string>,
  fallback?: T,
): Codec<T | undefined> {
  const set = new Set(allowed)
  return {
    // 白名单外的取值当作没给。手敲打错一个码 -> 全量列表,不是错误页
    read: (raw) => (raw !== null && set.has(raw) ? (raw as T) : fallback),
    write: (value) => (value === undefined || value === fallback ? null : value),
  }
}

/**
 * 开放取值的文本参数(搜索词、操作人名)。默认空串。
 *
 * 不给白名单是刻意的:操作人名来自后端配置,前端没有、也不该有那份名单。
 * 查一个不存在的操作人得到的是一张空列表,那本来就是正确答案。
 */
export function textParam(): Codec<string> {
  return {
    read: (raw) => raw ?? '',
    write: (value) => (value === '' ? null : value),
  }
}

/**
 * 开关。默认值可以是 `true`(审计页「只看上架闭环」默认开着)。
 *
 * 认不出来的取值退回默认值,而不是一律退回 `false` —— 默认开的那个开关
 * 碰上 `?workbench_only=x` 时,退回 `false` 会**悄悄把降噪关掉**。
 */
export function flagParam(fallback = false): Codec<boolean> {
  return {
    read: (raw) => (raw === '1' ? true : raw === '0' ? false : fallback),
    write: (value) => (value === fallback ? null : value ? '1' : '0'),
  }
}

/** 连续整数参数(页码)。越界退回默认值。 */
export function intParam(
  fallback: number,
  bounds: { min?: number; max?: number } = {},
): Codec<number> {
  const { min = Number.MIN_SAFE_INTEGER, max = Number.MAX_SAFE_INTEGER } = bounds
  return {
    read: (raw) => {
      if (raw === null || !/^-?\d+$/.test(raw)) return fallback
      const value = Number(raw)
      return value >= min && value <= max ? value : fallback
    },
    write: (value) => (value === fallback ? null : String(value)),
  }
}

/**
 * 只有几档的数值参数(每页条数、时间范围)。
 *
 * 和 `intParam` 分开而不是加一个 `oneOf` 选项:这两个参数背后**有一个下拉框**,
 * 而下拉框只列得出那几档。允许区间内的任意值,`?days=14` 会得到一张按 14 天
 * 筛过的列表配一个空白下拉框 —— 界面说了一件和数据不一致的话,
 * 正是硬规则 4 在禁的那种事。
 */
export function oneOfParam<T extends number>(fallback: T, values: readonly T[]): Codec<T> {
  const set = new Set<number>(values)
  return {
    read: (raw) => {
      if (raw === null || !/^\d+$/.test(raw)) return fallback
      const value = Number(raw)
      return set.has(value) ? (value as T) : fallback
    },
    write: (value) => (value === fallback ? null : String(value)),
  }
}

/**
 * `Spec` 的上界。**不能写成 `Codec<never>`。**
 *
 * `Codec<T>` 对 `T` 是**不变**的:`T` 在 `read` 的返回位置(协变)和 `write` 的
 * 参数位置(逆变)各出现一次。所以 `Codec<never>` 既不是任何 `Codec<T>` 的
 * 父类型、也装不下它们 —— 它只装得下 `Codec<never>` 自己,而没有人会写那个。
 * 结果是**每一个真实调用点都违约**:七个筛选项的页面报七条 `TS2322`,
 * 报文长这样 `Type 'Codec<string>' is not assignable to type 'Codec<never>'`。
 *
 * 修法是把两个方向分开取各自的极值,而不是让一个类型参数同时当两头:
 *
 *     read  的返回是协变位置 -> 上界取 `unknown`(什么都装得下)
 *     write 的参数是逆变位置 -> 上界取 `never` (什么都装得下)
 *
 * `never` 在这里是**逆变方向的顶**,不是"空类型"那个直觉意思 ——
 * 逆变位置的包容关系是反的,参数越窄的函数越通用。原来那一版的错误正是
 * 把这个 `never` 顺手用到了协变的 `read` 上。
 *
 * 这么写之后 `Codec<T>` 对任意 `T` 都满足 `AnyCodec`,而 `Values<S>` 里那个
 * `infer T` 仍然从**实参的精确类型**上取回 `T`(约束只管准入、不参与推断),
 * 所以 `filters.values.page` 照旧是 `number`,不是 `unknown`。
 * 反面守卫:把 `values.search` 赋给 `number` 必须仍然报错 —— 如果不报,
 * 说明这次"修好"是把类型塌成了 `any`,那等于把门禁拆了。
 *
 * `AnyCodec` **不导出**:它的 `write` 只收 `never`,是给约束用的形状,
 * 不是给人用的。导出之后早晚有人拿它当 `Codec` 的别名去标注变量。
 */
interface AnyCodec {
  read: (raw: string | null) => unknown
  write: (value: never) => string | null
}

type Spec = Record<string, AnyCodec>
type Values<S> = { [K in keyof S]: S[K] extends Codec<infer T> ? T : never }

export interface UrlFilters<S> {
  /** 当前筛选值。全部从 URL 解出来,组件内不存第二份。 */
  values: Values<S>
  /** 改一项或几项。**一次 URL 写入**,所以只进一格后退栈、只触发一次请求。 */
  patch: (changes: Partial<Values<S>>) => void
  /** 清除筛选。只删自己声明过的参数,URL 上别人的参数(`?tab=`)留着。 */
  reset: () => void
  /**
   * 当前筛选的规范形式。**给「筛选变了就清掉勾选/游标」那条 effect 当依赖。**
   *
   * ## 为什么必须是它,而不是在 setter 里清
   *
   * URL 化之前,「换筛选就清空勾选」(BLOCK-09)靠的是所有入口都走
   * `changeFilter()` 那一个口子。URL 化之后**后退、前进、以及直接改地址栏
   * 都会改筛选,而它们不经过任何 setter** —— 勾选 20 件、按一次后退,
   * 动作条仍然说「已选 20 件」,点下去提交的是一批不在他眼前的商品。
   * 这正是 BLOCK-09 那个缺陷,换了一扇门进来。
   *
   * 挂在 signature 上就没有这扇门:不管筛选是怎么变的,它都变。
   *
   * ## 为什么要规范化,不直接用 `params.toString()`
   *
   * `?page=1` 和不带 page 是同一个状态。拿原始查询串当依赖的话,
   * 这两者之间来回会白白清一次勾选 —— 而「勾选莫名其妙没了」这种事
   * 只要发生过一次,运营下次就不敢用批量了。
   *
   * 所以走 `write(read(x))`:两条路都得到 `page=`。
   */
  signature: string
}

export function useUrlFilters<S extends Spec>(spec: S): UrlFilters<S> {
  const [params, setParams] = useSearchParams()

  // `spec` 在调用点是内联字面量,每次渲染换一个身份。直接进依赖数组的话
  // 下面几个 memo/callback 一个都不生效(useServerSort 顶部踩过同一个坑)。
  const specRef = useRef(spec)
  specRef.current = spec

  // 依赖只有查询串:URL 没变时 `values` 保持同一个身份,
  // 于是它进 react-query 的 queryKey 不会造成多余的重新取数。
  const search = params.toString()

  const values = useMemo(() => {
    const current = new URLSearchParams(search)
    const out = {} as Values<S>
    for (const key of Object.keys(specRef.current)) {
      out[key as keyof S] = specRef.current[key].read(current.get(key)) as never
    }
    return out
  }, [search])

  const signature = useMemo(
    () =>
      Object.keys(specRef.current)
        .map((key) => `${key}=${specRef.current[key].write(values[key] as never) ?? ''}`)
        .join('&'),
    [values],
  )

  const patch = useCallback(
    (changes: Partial<Values<S>>) => {
      setParams((prev) => {
        const next = new URLSearchParams(prev)
        for (const key of Object.keys(changes)) {
          const encoded = specRef.current[key].write(changes[key as keyof S] as never)
          if (encoded === null) next.delete(key)
          else next.set(key, encoded)
        }
        return next
      })
    },
    [setParams],
  )

  const reset = useCallback(() => {
    setParams((prev) => {
      const next = new URLSearchParams(prev)
      for (const key of Object.keys(specRef.current)) next.delete(key)
      return next
    })
  }, [setParams])

  return { values, patch, reset, signature }
}
