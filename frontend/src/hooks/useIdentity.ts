/**
 * 「我是谁」只问一次(A8 依赖 A5 的 `/auth/whoami`)。
 *
 * ## 为什么要抽出来
 *
 * 冷启动横幅要知道"口令被拒了没",顶栏身份区要知道"这个浏览器是不是管理员" ——
 * 两件事都只能由后端回答,而且答案来自同一个请求。各自写一遍 `useQuery` 的后果
 * 不是多一次请求(react-query 按 key 去重),是**两处 `enabled` 条件会分叉**:
 * 横幅那份刻意在"后端连不上"时不问,身份区那份没有这个条件,于是同一个 key 上
 * 挂了两组互相矛盾的开关,谁先挂载谁说话。所以判断只留一份,在这里。
 *
 * ## 拿不到答案时按什么显示
 *
 *     后端答了            用 `is_admin`。这是唯一权威的答案
 *     还没答 / 答不了     **按非管理员显示**(`probe.data?.is_admin === true`
 *                         对 undefined 天然为 false)
 *
 * **第二行原来写的是"退回『本浏览器填了管理口令吗』"—— 那条降级已经不存在了。**
 * 它读的是 localStorage 里那两把口令,而整条口令链随浏览器登录一起退役
 * (见 `api/client.ts` 里那段退役说明,以及下面 `enabled` 的注释)。
 * 今天没有任何本地状态能把一个浏览器说成管理员。
 *
 * 保留那句话的代价不是文字不准:它描述的是一个"可以在本地伪造"的降级,
 * 于是读注释的人会去评估一个并不存在的伪造面,或者反过来照着它把降级加回来。
 *
 * 仍然成立的是那句结论:**这是显示层的判断,不是权限边界**。
 * 真正的边界在后端 `require_admin`,而且只在后端。
 *
 * ## 这里的 `is_admin` 现在真的决定看得见什么
 *
 * A8 计划里的「菜单按角色收敛」在 a46 之前一直没有落地,理由是"新人第一次
 * 打开系统时既不是管理员、又必须进设置页填口令"。**浏览器登录上线之后这个
 * 前提消失了**:密码在 `.env` 里,没有任何人需要先进设置页把自己变成管理员。
 * 所以本轮显式撤掉那条约束 —— `NAV` 的「系统管理」组标了 `adminOnly`,
 * `AppLayout` 按 `isAdmin` 过滤,`nav-and-url-filters.test.tsx` 反过来钉着
 * 「operator 看不到 /settings」。
 *
 * **路由仍然全部注册。** operator 手输 `/settings` 打得开,页面上是一句 403;
 * 真正的边界仍然只在后端 `require_admin`。菜单收敛是可发现性,不是权限。
 * 给路由加前端守卫之前先想清楚这两件事的区别。
 */
import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { authApi, type WhoAmI } from '../api/auth'
import { healthApi } from '../api/health'
import { isAuthError, onSessionExpiredChange } from '../api/client'

export interface Identity {
  /** 后端确认过的身份。没确认到就是 null,别拿它当"不是管理员"用 */
  who: WhoAmI | null
  /** 显示层用的管理员判定(含降级)。权限边界不看这个 */
  isAdmin: boolean
  /** 后端探活失败。横幅据此决定说哪句话 */
  backendDown: boolean
  /** 探活还在路上。首屏据此避免闪一下再改 */
  loading: boolean
  /** 身份探测被后端拒过(401/403) */
  authFailed: boolean
  /**
   * 这个部署走浏览器登录(后端 `/health` 的 `auth_mode`)。
   *
   * 界面上有三处按它分岔,而三处的**错法各不相同**,所以它必须是后端答的:
   *
   *     AppLayout          未登录时跳 /login 还是原地显示空页
   *     ColdStartBanner    说"去填口令"还是什么都不说(登录页会接手)
   *     顶栏身份区          给"退出登录"还是"更换口令"
   */
  sessionAuth: boolean
  /**
   * 这个部署**只**认 Header 口令,浏览器因此进不来(后端 `auth_mode=token`)。
   *
   * 2026-08-11 评审第 8 条。它与 `!sessionAuth` **不是**一回事:后者同时
   * 覆盖本机免登录模式(`auth_mode=dev`),而那一档一切正常、不需要说任何话。
   * 合成一个布尔的代价是登录页只能说一句同时服务两种情况的话,
   * 而当时那句话是"到系统设置页填一次口令即可" —— 指向一个已经删掉的输入框。
   */
  tokenOnly: boolean
  /**
   * 会话模式下当前**没有**有效登录态。`AppLayout` 据此跳登录页。
   *
   * 三个条件缺一不可:是会话模式、探测已经有结论、结论是"没有身份"。
   * 少了第二条的后果是首屏那一帧还没答案就把人踢走 —— 已登录的人每次刷新
   * 都会先闪一下登录页;而后端连不上时也不能踢(那是另一件事,横幅在说)。
   */
  needsLogin: boolean
}

export function useIdentity(): Identity {
  const queryClient = useQueryClient()
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

  /*
   * 会话模式下**凭据是 Cookie,不是 localStorage 里的字符串**。
   *
   * 上一版的 `enabled` 除了后端活着,还挂着「本地存了口令没有」,旧的 `enabled` 读的是
   * localStorage 里那两把口令。浏览器登录落地之后那个条件会把整条链路掐死在
   * 起点:登录成功、Cookie 也发了,而 那两把本地口令仍然是空 ——
   * whoami 一次都不发,顶栏永远停在"未登录",菜单永远按非管理员渲染。
   * 而**没有任何请求失败**,所以横幅也不会说话。这是 §3.43 那一族的又一次:
   * 上游全对,断在最后一跳。
   *
   * 本轮 localStorage 口令链整个退役,`enabled` 因此**只依赖 `!health.isError`**。
   * 这一条不能再挂任何别的条件:`enabled: false` 时 react-query 的
   * `isLoading` 是 **false**(v5 里 `isLoading === isPending && isFetching`),
   * 于是 `RequireAuth` 那一侧不会停在 Loading,而是直接判成未登录 ——
   * 表现是"已登录的人刷新时先闪一下登录页"。
   */
  const sessionAuth = health.data?.auth_mode === 'session'

  /*
   * 会话在**用着的过程中**过期,这里是唯一会发现它的地方。
   *
   * 探测查询挂着 `staleTime: 60_000` 且 `refetchOnWindowFocus: false` ——
   * 这两条对首屏是对的(别为了一个不会变的答案反复打请求),但它们也意味着:
   * 后端那边 Cookie 过期之后,前端手里那份"我是 operator"能继续有效整整一分钟,
   * 甚至更久。运营在这段时间里点什么都失败,而**界面从头到尾不会把他送去登录页**
   * —— 他只会看到一串"请重新登录"的提示,和一个没有登录入口的页面。
   *
   * 所以 401 要能把这份缓存作废。`resetQueries` 而不是 `invalidateQueries`:
   * 后者在重新请求失败时**保留旧数据**(React Query 的既定行为),于是
   * `probe.data` 还在,`needsLogin` 还是假 —— 等于什么都没做,而且这种"做了
   * 但没生效"比没做更难查。`reset` 把数据一并丢掉,再由 whoami 的回答定去留。
   *
   * 判定仍然只有一处:`needsLogin` 读的是重新探测的**结果**,不是这个信号本身。
   * 一次偶发 401 不会把登录态完好的人踢出去 —— 它只是让人再问一遍。
   */
  useEffect(
    () =>
      onSessionExpiredChange((expired) => {
        if (expired) queryClient.resetQueries({ queryKey: ['auth-probe'] })
      }),
    [queryClient],
  )

  const probe = useQuery({
    queryKey: ['auth-probe'],
    queryFn: () => authApi.whoami(),
    enabled: !health.isError,
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: 60_000,
  })

  // 探测有没有**结论**。`isLoading` 只在"正在跑第一次"时为真;
  // `enabled: false` 的查询它是 false(见上面 `enabled` 那段),
  // 所以"还没答案"必须用这个显式判据,不能拿 `isLoading` 凑合
  const probeSettled = probe.isSuccess || probe.isError

  return {
    who: probe.data ?? null,
    // **只认后端。** 上一版这里有个「后端没答就看本地存没存管理口令」的降级,
    // 在 localStorage 里塞一把假口令就能把管理菜单点亮。菜单现在真的按角色
    // 收敛了(§30),这个降级于是从"显示层的猜测"变成"能骗出一整组入口"——
    // 骗出来的入口点进去仍然 403,但那是后端在兜底,不是这里做对了
    isAdmin: probe.data?.is_admin === true,
    backendDown: health.isError,
    loading: health.isLoading || probe.isLoading,
    // 只有 401/403 算"口令被拒"。超时、502 也会让 probe 变 isError,
    // 而把那些也算成口令问题,横幅会在后端抽风时指着运营的口令说错话
    authFailed: probe.isError && isAuthError(probe.error),
    sessionAuth,
    // 判据是**明确等于 `token`**,不是"不等于 session":旧后端不返回
    // `auth_mode`,而那时按"只认 Header 口令"处理会在一个好端端的部署上
    // 挂一条说服务器配错了的横幅
    tokenOnly: health.data?.auth_mode === 'token',
    // **不看 `authFailed`。** 探测失败的原因可能是 502 或超时,而那时把人踢去
    // 登录页是错的:他登得进去,然后回到同一个坏掉的后端。判据只有一条 ——
    // 后端活着、探测已经有结论、而结论里没有身份
    needsLogin: sessionAuth && !health.isError && probeSettled && !probe.data,
  }
}
