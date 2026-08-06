import { useCallback, useRef, useState } from 'react'
import type { SorterResult } from 'antd/es/table/interface'

/**
 * 服务端排序的状态与接线。
 *
 * ## 为什么不是直接在列上挂 antd 的 sorter
 *
 * 上一轮这四张表**刻意没有排序**,理由写在当时的注释里:它们是服务端分页的,
 * 每次只拿一页(20 条)。在列上挂一个比较函数,antd 排的是**当前这 20 行**,
 * 而运营以为排的是 500 行 —— 那不是修好了,是把「没有排序」换成
 * 「有排序但结果是错的」,更贵。
 *
 * 后端现在收 `sort` / `order` 了(白名单在各自的 service 里),于是这里做的事很窄:
 * 把 antd 的排序事件翻译成两个查询参数,并把参数塞进 react-query 的 queryKey。
 *
 * ## 三个容易踩的地方
 *
 * **`sorter: true` 而不是 `sorter: (a, b) => ...`。** 传函数就是客户端排序,
 * 正是要避免的那件事。传 `true` 只让表头出现排序箭头,排序交给后端。
 *
 * **换排序必须回第一页。** 不回的话:运营在第 5 页点了「按阻断数倒序」,
 * 后端老老实实返回新排序的第 5 页,而屏幕上出现的是一批他从没见过的商品,
 * 看起来像是筛选条件被清空了。
 *
 * **`sortOrder` 受控。** 不受控的话,翻页时 antd 会自己清掉表头箭头,
 * 而请求里还带着 sort —— 界面说「没排序」,数据是排过的。
 *
 * ## 排序状态存在哪:调用方说了算(GAP-033)
 *
 * 默认存在这个 hook 自己的 `useState` 里。筛选搬进 URL 的那几页要把排序
 * 一起搬 —— **只搬一半比不搬更糟**:刷新之后筛选还在、排序回到默认,
 * 而表头箭头是受控的、会跟着回到默认,于是界面和数据仍然自洽,
 * 没有任何地方报错,只有运营发现「我明明按阻断数排过」。
 *
 * 所以开一个可选的 `store`:给了就拿它当唯一真相,不给就照旧。
 * 没搬的那两页(`ProductListPage` / `ReviewQueuePage`)行为一个字没变 ——
 * 它们的排序仍然活不过一次刷新,这一条记在 STATUS「已知限制」里。
 */
export type SortDirection = 'ascend' | 'descend'

export interface ServerSortParams {
  sort: string
  /** 后端认 asc/desc,也认 antd 的 ascend/descend;这里发短的那种。 */
  order: 'asc' | 'desc'
}

export interface ServerSort {
  /** 直接展开进请求参数。 */
  params: ServerSortParams
  /** 给列的 `sortOrder`:当前列被排时返回方向,否则 `null`(受控必须给 null 而非 undefined)。 */
  orderFor: (field: string) => SortDirection | null
  /** 挂到 `<Table onChange>`。只处理排序事件,分页仍走 `pagination.onChange`。 */
  onTableChange: (
    _pagination: unknown,
    _filters: unknown,
    sorter: SorterResult<any> | SorterResult<any>[],
    extra: { action: 'paginate' | 'sort' | 'filter' },
  ) => void
  /** 给非表格的页面(素材库是图片栅格,不是 Table)用的直接设值口。 */
  setSort: (field: string, order: 'asc' | 'desc') => void
}

/**
 * 把排序状态外置。URL 化的页面传它进来,由 `useUrlFilters` 持有那两个参数。
 *
 * `set` 拿到的是**一对**值,不是两次调用:分两次写 URL 会进两格后退栈,
 * 而运营按一次后退只会看到「order 变了但 sort 没变」这种没人产生过的中间态。
 */
export interface SortStore {
  value: ServerSortParams
  set: (next: ServerSortParams) => void
}

export function useServerSort(
  defaults: ServerSortParams,
  /** 换排序时回到第一页。调用方把自己的 setPage 传进来。 */
  onReset?: () => void,
  /** 给了就用它存排序(URL 化的页面),不给就用内部 state。 */
  store?: SortStore,
): ServerSort {
  // 两个 state 只有一个在用。hook 不能条件调用,所以内部这份总是建 ——
  // 但 `store` 在时它一次都不被读,不存在两份真相
  const [ownParams, setOwnParams] = useState<ServerSortParams>(defaults)
  const storeRef = useRef(store)
  storeRef.current = store
  const params = store ? store.value : ownParams
  const setParams = useCallback((next: ServerSortParams) => {
    const external = storeRef.current
    if (external) external.set(next)
    else setOwnParams(next)
  }, [])

  // `defaults` 与 `onReset` 几乎都是调用点内联写的字面量 / 箭头函数,
  // 每次渲染换一个身份 —— 直接放进 useCallback 的依赖数组,等于每次渲染
  // 都重建这几个回调,`useCallback` 一个都不生效。放进 ref 之后依赖数组
  // 是空的,而回调内部读到的永远是最新值。
  const defaultsRef = useRef(defaults)
  defaultsRef.current = defaults
  const onResetRef = useRef(onReset)
  onResetRef.current = onReset

  const setSort = useCallback((field: string, order: 'asc' | 'desc') => {
    setParams({ sort: field, order })
    onResetRef.current?.()
  }, [setParams])

  const onTableChange = useCallback<ServerSort['onTableChange']>(
    (_pagination, _filters, sorter, extra) => {
      if (extra?.action !== 'sort') return
      // 多列排序这套接口不支持(后端只收一个 sort 字段),取第一个即可 ——
      // 页面上也没有任何一列开了 multiple
      const single = Array.isArray(sorter) ? sorter[0] : sorter
      const field = String(single?.columnKey ?? single?.field ?? '')
      // 点第三下取消排序:antd 给 order === undefined。回到默认排序而不是
      // 「不排序」—— 后端总要有一个 ORDER BY,不给就是让数据库随便定序。
      if (!field || !single?.order) {
        setParams(defaultsRef.current)
        onResetRef.current?.()
        return
      }
      setSort(field, single.order === 'ascend' ? 'asc' : 'desc')
    },
    [setParams, setSort],
  )

  const orderFor = useCallback(
    (field: string): SortDirection | null =>
      params.sort === field ? (params.order === 'asc' ? 'ascend' : 'descend') : null,
    [params],
  )

  return { params, orderFor, onTableChange, setSort }
}
