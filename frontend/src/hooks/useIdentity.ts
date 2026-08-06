/**
 * 「我是谁」只问一次(A8 依赖 A5 的 `/auth/whoami`)。
 *
 * ## 为什么要抽出来
 *
 * 冷启动横幅要知道"口令被拒了没",菜单要知道"这个浏览器是不是管理员" ——
 * 两件事都只能由后端回答,而且答案来自同一个请求。各自写一遍 `useQuery` 的后果
 * 不是多一次请求(react-query 按 key 去重),是**两处 `enabled` 条件会分叉**:
 * 横幅那份刻意在"后端连不上"时不问,菜单那份没有这个条件,于是同一个 key 上
 * 挂了两组互相矛盾的开关,谁先挂载谁说话。所以判断只留一份,在这里。
 *
 * ## 拿不到答案时按什么显示
 *
 *     后端答了            用 `is_admin`。这是唯一权威的答案
 *     还没答 / 答不了     退回"本浏览器填了管理口令吗"
 *
 * 第二行是计划里点名的降级实现。它可以被骗(在 localStorage 里塞个假口令就能
 * 让菜单长出「系统管理」),但那一栏点进去每个请求都会被后端 `require_admin`
 * 挡回来 —— **这是显示层收敛,不是权限边界**。真正的边界在后端,而且只在后端。
 *
 * ## 一个必须留着的后门
 *
 * 菜单收敛之后,非管理员看不到「设置」入口。但新人第一次打开系统时恰恰
 * **既不是管理员、又必须进设置页填口令** —— 冷启动横幅里那个链接是他唯一的路。
 * 所以 `/settings` 等路由对所有人保持注册(见 App.tsx),这里收敛的只是菜单。
 * 删掉那条路由,或者给它加个前端守卫,新人就会被锁在门外。
 */
import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { authApi, type WhoAmI } from '../api/auth'
import { healthApi } from '../api/health'
import {
  TOKEN_CHANGED_EVENT,
  isAuthError,
  readAdminToken,
  readOperatorToken,
} from '../api/client'

export interface Identity {
  /** 后端确认过的身份。没确认到就是 null,别拿它当"不是管理员"用 */
  who: WhoAmI | null
  /** 显示层用的管理员判定(含降级)。权限边界不看这个 */
  isAdmin: boolean
  /** 后端探活失败。横幅据此决定说哪句话 */
  backendDown: boolean
  /** 探活还在路上。首屏据此避免闪一下再改 */
  loading: boolean
  /** 口令被后端拒过(401/403) */
  authFailed: boolean
  /** 正在拿管理口令跑日常业务(C-04)。界面应当提示配一个运营口令 */
  usingAdminFallback: boolean
}

export function useIdentity(): Identity {
  /*
   * A-09:后端恢复之后,红色横幅必须自己消失。
   *
   * 原来这里写着"后端起来之后第一次成功的业务请求会让红页消失" —— **那句话
   * 是假的**:红页读的是 `['health-probe']` 这个查询的 `isError`,而全仓
   * 没有任何地方 invalidate 它,`refetchOnWindowFocus` 也关着。于是后端
   * 恢复之后横幅仍然挂着,直到运营手动刷新整个页面。
   *
   * 修法不是打开轮询(健康时每 10 秒打一次探针确实只是噪音),而是
   * **只在失败之后轮询**:平时零成本,出问题时自己恢复。
   *
   * 用一个 state 而不是 `refetchInterval` 的函数式写法:那个回调的签名在
   * react-query v4 与 v5 之间变过(`(data, query)` -> `(query)`),写错不会
   * 报错,只会安静地永远不轮询 —— 又是一个不报错的错答案。
   */
  const [backendWasDown, setBackendWasDown] = useState(false)

  const health = useQuery({
    queryKey: ['health-probe'],
    queryFn: () => healthApi.ping(),
    retry: 1,
    refetchInterval: backendWasDown ? 15_000 : false,
    refetchOnWindowFocus: backendWasDown,
    staleTime: 60_000,
  })

  useEffect(() => {
    setBackendWasDown(health.isError)
  }, [health.isError])

  // 从 localStorage 同步读,**不是响应式的**:存完口令之后这个组件不会因为
  // 存储变了而重渲染,`enabled` 也就不会自己翻转。所以 SettingsPage 存完口令
  // 必须显式 `invalidateQueries(['auth-probe'])` —— 那一步是这个判断的配套,
  // 不是可选的优化。
  /*
   * A-10:口令的存在性必须是**响应式**的。
   *
   * 原来这里是一次同步读,注释自己写着"不是响应式的",并把责任推给
   * 设置页去 `invalidateQueries` —— 一个靠"记得手动同步"维持的不变量。
   * 漏一处的表现是:口令存好了,界面仍然说没登录。
   *
   * 现在订阅两个来源:本标签页的 `TOKEN_CHANGED_EVENT`(`storage` 事件在
   * 同一个标签页里**不触发**,这是它最容易被漏掉的地方),以及跨标签页的
   * `storage`。设置页那次 invalidate 保留 —— 它现在是加速,不是必需。
   */
  const [tokenTick, setTokenTick] = useState(0)
  useEffect(() => {
    const bump = () => setTokenTick((n) => n + 1)
    window.addEventListener(TOKEN_CHANGED_EVENT, bump)
    window.addEventListener('storage', bump)
    return () => {
      window.removeEventListener(TOKEN_CHANGED_EVENT, bump)
      window.removeEventListener('storage', bump)
    }
  }, [])

  const hasToken = useMemo(
    () => Boolean(readOperatorToken() || readAdminToken()),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- tokenTick 是刷新信号,不是值
    [tokenTick],
  )

  /**
   * C-04(部分):没有运营口令时,普通请求会带上**管理口令**。
   *
   * 这是 A5 刻意留的回落 —— 只配了 admin 的部署照样能用,而后端本来就认
   * admin 包含 operator。去掉它会让那类部署整站 401。
   *
   * 但代价是真的:**每一次列表 GET 都在发送能改 API Key 的那把口令**。
   * 所以这里不删回落,只把它变成**看得见的** —— 界面据此提示配一个运营口令。
   * 真正消除它要账号体系(C-05'),不是一个前端改动。
   */
  const usingAdminFallback = useMemo(
    () => !readOperatorToken() && Boolean(readAdminToken()),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [tokenTick],
  )

  const probe = useQuery({
    queryKey: ['auth-probe'],
    queryFn: () => authApi.whoami(),
    // 后端都没起来、或一把口令都没填时不必问:那两种情况横幅另有说法
    enabled: !health.isError && hasToken,
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: 60_000,
  })

  return {
    who: probe.data ?? null,
    // `??` 而不是 `||`:后端明确说了 `is_admin: false` 时要**听它的**,
    // 不能因为本地存着一把(已经失效的)管理口令就把系统管理菜单亮回来
    isAdmin: probe.data?.is_admin ?? Boolean(readAdminToken()),
    backendDown: health.isError,
    loading: health.isLoading || probe.isLoading,
    // 只有 401/403 算"口令被拒"。超时、502 也会让 probe 变 isError,
    // 而把那些也算成口令问题,横幅会在后端抽风时指着运营的口令说错话
    authFailed: probe.isError && isAuthError(probe.error),
    usingAdminFallback,
  }
}
