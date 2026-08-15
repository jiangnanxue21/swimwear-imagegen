/**
 * `api/client.ts` 的用例(前端评审 T-1 / T-2)。
 *
 * ## 为什么先测这个文件
 *
 * 走查时前端有 86 条契约测试(全在 `backend/tests/pure/test_frontend_contract.py`),
 * 但它们**一行前端代码都没执行过** —— 全部是对源码文本与 AST 的静态分析。
 * 而这个文件同时是:
 *
 *     全站唯一一份失败判定    28 处 `readError` 调用点与 `<ErrorNotice>` 都从它取
 *     每一个请求的鉴权入口    判错的后果是拉个列表把能改 API Key 的口令送出去
 *
 * 被依赖最多、出错最安静、而且完全是纯输入输出 —— 从它开始性价比最高。
 *
 * ## 为什么不需要 jsdom
 *
 * `client.ts` 自 phase6 起不再碰 `window.localStorage`(口令链已退役);
 * 假 localStorage 留给仍要覆盖"隐私模式写不进去"历史行为的旧用例。
 * 所以在 node 环境里临时塞一个假的 `globalThis.window` 就够,
 * 不必为这几条用例背上一个 DOM 实现。少一个依赖,少一次 `npm ci` 变慢。
 *
 * ## 为什么放在 `frontend/tests/` 而不是挨着源码
 *
 * **不是偏好,是约束。** 交付环境没有网络,装不上 vitest,而
 * `test_package_json_and_lockfile_agree` 要求 package.json 与 lockfile 严格一致 ——
 * 所以只改 package.json 加依赖会让 `npm ci` 整个挂掉,比现在更糟。
 *
 * 而 `tsconfig.json` 的 `include` 是 `["src"]`、`eslint.config.js` 的 `files`
 * 是 `src/**` —— 把用例放进 `src/` 会让 `npm run typecheck` 和 `npm run lint`
 * 立刻因为找不到 `describe` / `expect` 而失败,也就是**为了补测试把两道门禁弄红**。
 *
 * 所以先放在外面。装上 vitest 的那一天要一起做三件事(缺一条这批用例就还是死的):
 *
 *     npm install -D vitest
 *     package.json  加 "test": "vitest run"
 *     Makefile      fe-check 里加 npm run test
 *
 * **这三件事今天已经全部做完了**(vitest 在 devDependencies、`npm run test` 在
 * package.json、`fe-check` 里有 `npm run test`),这批用例是活的。
 *
 * 盯着它们不被拆掉的是 `backend/tools/verify_delivery.py` —— 这里原来写的是
 * `backend/tests/pure/test_delivery_hygiene.py`,那个文件在方案 v4.1 §8.2
 * 已整体搬进 `verify_delivery.py`,树里不存在。两处失实叠在一起:
 * **点错了文件,而且把已经发生的事写成了将来时**。后半句更贵 ——
 * 它让人以为这批用例还在等一个未来的开关。
 *
 * ## 为什么用 adapter 而不是去翻 interceptors.handlers
 *
 * `apiClient.interceptors.request.handlers` 是 axios 的内部字段,类型里没有它。
 * 换成替换 adapter:请求照常走完整条拦截器链,我们在最后一站把 config 截下来 ——
 * 测的是**真实路径**,而不是被单独拎出来的那个回调。
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import {
  apiClient,
  clearSessionExpired,
  describeError,
  isAuthError,
  onSessionExpiredChange,
  readError,
} from '../../src/api/client'

// ---------------------------------------------------------------- 假 localStorage

function installFakeStorage(): Map<string, string> {
  const store = new Map<string, string>()
  const localStorage = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
  }
  ;(globalThis as unknown as { window: unknown }).window = { localStorage }
  return store
}

function removeStorage(): void {
  delete (globalThis as unknown as { window?: unknown }).window
}

// ---------------------------------------------------------------- 假 axios 错误
//
// `axios.isAxiosError` 判的就是 `payload.isAxiosError === true`,
// 所以不必真发一次请求。构造得越薄,用例读起来越像它要说的那句话。

interface FakeErrorInit {
  status?: number
  data?: unknown
  code?: string
  requestId?: string
  /** 这次请求配的超时(毫秒)。话术里的秒数从它派生,不是常量 */
  timeout?: number
}

function axiosError({ status, data, code, requestId, timeout }: FakeErrorInit): unknown {
  return {
    isAxiosError: true,
    code,
    message: 'boom',
    config: timeout === undefined ? undefined : { timeout },
    response:
      status === undefined
        ? undefined
        : {
            status,
            data,
            headers: requestId ? { 'x-request-id': requestId } : {},
          },
  }
}

function businessError(message: string, fields: { loc: string; msg: string }[] = []) {
  return axiosError({ status: 400, data: { error: { code: 'DRAFT_STALE', message, fields } } })
}

// ================================================================ describeError

describe('describeError:失败判定只有这一份', () => {
  it('不是 axios 错误时给通用话术,技术层全空', () => {
    // 这一支多半是前端自己抛的。真出这种事由 ErrorBoundary 接管,
    // 它有编号也有「复制详情」,所以这里不该硬凑技术信息
    const view = describeError(new Error('前端自己炸了'))
    expect(view.text).toContain('请联系管理员')
    expect(view.requestId).toBe('')
    expect(view.technical.code).toBe('')
    expect(view.technical.status).toBeNull()
  })

  it('超时:话术里说超时,命令留在技术层', () => {
    const view = describeError(axiosError({ code: 'ECONNABORTED' }))
    expect(view.text).toContain('超时')
    expect(view.technical.retriable).toBe(true)
    // A12 的硬约束:运营那一层不出现命令、容器名、表名、文件名
    expect(view.text).not.toContain('docker')
    expect(view.technical.hint).toContain('worker')
  })

  /*
   * 超时秒数**从这次请求真实的 timeout 派生**,不是写死的常量。
   *
   * 修之前三处话术都写着"30 秒",而默认超时在 A-33 就抬到了 60s、
   * 长动作挂着 LONG_TIMEOUT_MS(300s)。一个跑满 5 分钟才超时的发布请求
   * 会被告知"后端 30 秒内没有回答" —— 差一个数量级,而这句话恰恰出现在
   * **结果未知、禁止直接重做**那一支上,是要运营据此做判断的。
   *
   * 这几条钉的是"派生"这件事本身:把常量改成 60 能让第一条过,
   * 但第二条(300s 说 5 分钟)只有真的从 config 读才可能绿。
   */
  describe('超时秒数说的是这一次请求真实等了多久', () => {
    it('默认超时的请求说 60 秒,不是 30 秒', () => {
      const view = describeError(axiosError({ code: 'ECONNABORTED', timeout: 60_000 }))
      expect(view.text).toContain('60 秒')
      expect(view.text).not.toContain('30 秒')
    })

    it('长动作(300s)说 5 分钟 —— 秒在这个量级已经帮不上忙', () => {
      const view = describeError(axiosError({ code: 'ECONNABORTED', timeout: 300_000 }))
      expect(view.text).toContain('5 分钟')
      // 管理员那一层读同一个数,两层不许各说一个
      expect(view.technical.hint).toContain('5 分钟')
    })

    it('写请求的「结果未知」那句也用同一个数', () => {
      const view = describeError(
        axiosError({ code: 'ECONNABORTED', timeout: 300_000 }),
        'write',
      )
      expect(view.outcome).toBe('UNKNOWN')
      expect(view.text).toContain('5 分钟')
      // 这一支刻意不出现"请重试" —— 它要防的正是那个动作
      expect(view.text).not.toContain('请重试')
    })

    it('拿不到 config 时回落到默认值,不出现 NaN 或 undefined', () => {
      const view = describeError(axiosError({ code: 'ECONNABORTED' }))
      expect(view.text).toContain('秒')
      expect(view.text).not.toContain('NaN')
      expect(view.text).not.toContain('undefined')
    })
  })

  it('连不上:明确告诉运营「这不是你操作错了」', () => {
    const view = describeError(axiosError({ code: 'ERR_NETWORK' }))
    expect(view.text).toContain('不是你操作错了')
    expect(view.text).not.toContain('docker')
    expect(view.technical.hint).toContain('docker compose ps')
  })

  it('401 / 403:不再指向设置页,且判定为不可重试', () => {
    /*
     * 这条只测得到**"还没问到凭据模式"那一支**:单测环境里 `/health` 一次都
     * 不会返回,模块级 authMode 停在默认的 `'unknown'`。另外三支(session /
     * dev / token)的措辞钉在纯层守卫上
     * (`test_a46_phase2_browser_login_seam.py`),因为在这里翻转 authMode
     * 只能伸进 axios 拦截器内部 —— 那种写法比不写更糟。
     * phase6 自审:第一版把这条改成 toContain('重新登录'),在这个环境里必红。
     *
     * 2026-08-11:默认值从 `'token'` 改成 `'unknown'`,所以这里原来断言的
     * "开发模式"没了。**那不是回归,是修掉了一句撒谎的默认**:`token` 现在
     * 有了自己那句很重的话(整个部署的浏览器都进不来),拿它当默认会让
     * `/health` 还没回来时的任何一个 401 都声称服务器配错了。
     */
    for (const status of [401, 403]) {
      const view = describeError(axiosError({ status, data: { error: { code: 'UNAUTHORIZED', message: '登录状态已失效' } } }))
      // 还没问到模式时**只说事实**,不对部署配置下任何结论
      expect(view.text).toContain('刷新本页')
      expect(view.text).not.toContain('开发模式')
      expect(view.text).not.toContain('Header 口令')
      // 口令时代的指路牌一个都不许剩:设置页上那张录入卡已经不存在了
      expect(view.text).not.toContain('系统设置')
      expect(view.text).not.toContain('核对口令')
      // 凭据问题重试一百次也不会变对 —— P1-5 的 retry 判断依赖这个字段
      expect(view.technical.retriable).toBe(false)
      expect(isAuthError(axiosError({ status }))).toBe(true)
    }
  })

  it('5xx:请求编号出现在**运营**那一层', () => {
    // 刻意的设计:方案原文把 request_id 划进技术层,但同一段又要求运营去转述它。
    // 转述得看得见,所以两层都给
    const view = describeError(axiosError({ status: 500, requestId: 'req-abc123' }))
    expect(view.requestId).toBe('req-abc123')
    expect(view.text).toContain('req-abc123')
    expect(view.technical.retriable).toBe(true)
  })

  it('5xx 但后端没写响应头:降级成不带编号的那句,不出现 undefined', () => {
    const view = describeError(axiosError({ status: 500 }))
    expect(view.requestId).toBe('')
    expect(view.text).not.toContain('undefined')
    expect(view.text).toContain('请联系管理员')
  })

  it('业务 4xx:原样透出后端 message,不套通用话术', () => {
    // 这是文件头论证最长的一条。「草稿已过期,禁止导出」被换成
    // 「处理失败,请重试」之后,运营会一直重试一件永远不会成功的事
    const view = describeError(businessError('草稿已过期,禁止导出'))
    expect(view.text).toContain('草稿已过期')
    expect(view.text).not.toContain('请重试')
    expect(view.technical.retriable).toBe(false)
  })

  it('业务 4xx 带字段明细:追加在括号里', () => {
    const view = describeError(
      businessError('参数不合法', [{ loc: 'body.price', msg: '必须大于 0' }]),
    )
    expect(view.text).toContain('body.price')
    expect(view.technical.fields).toEqual(['body.price: 必须大于 0'])
  })

  it('429 是业务 4xx 里唯一可重试的一支', () => {
    const view = describeError(
      axiosError({ status: 429, data: { error: { code: 'RATE_LIMITED', message: '太快了' } } }),
    )
    expect(view.technical.retriable).toBe(true)
    expect(view.technical.hint).toContain('配额')
  })

  it('4xx 但没有统一错误体(网关插的)→ 通用话术', () => {
    const view = describeError(axiosError({ status: 404, data: '<html>404</html>' }))
    expect(view.text).toContain('请重试')
    expect(view.technical.retriable).toBe(false)
  })

  it('readError 只是薄封装,不许另加判断', () => {
    // 判定一旦分成两份,toast 和技术面板会对同一次失败说两种话
    const samples: unknown[] = [
      new Error('x'),
      axiosError({ code: 'ECONNABORTED' }),
      axiosError({ status: 500, requestId: 'r1' }),
      businessError('草稿已过期'),
    ]
    for (const err of samples) {
      expect(readError(err)).toBe(describeError(err).text)
    }
  })
})

// ================================================================ 口令注入

/*
 * 这里原来是 `describe('请求拦截器:两把口令的优先级')` —— 四条用例钉
 * "有 operator 口令就用它、只有 admin 口令就回落"。请求拦截器与两把 localStorage
 * 口令随浏览器登录整个退役(PRD §26),这一组因此删除,不是"暂时跳过"。
 *
 * 今天等价强度的不变量在两处:`withCredentials: true`(下面那条),
 * 以及纯层 `test_a45_batch8_fixes.py::test_no_frontend_call_site_carries_an_admin_token`
 * ——它反过来钉"正常前端一处都不许带 X-Admin-Token"。
 */

describe('会话失效信号:只在该重新探身份的时候响一次', () => {
  /**
   * ## 这一组补的是自审时发现的洞
   *
   * a46-phase2 第一版把信号算出来了,而**没有任何人订阅**。后果不是少一个
   * 功能,是"用着用着会话过期"这条路径整个不生效:探测查询挂着 60 秒
   * `staleTime`,前端手里那份身份能继续有效一分多钟 —— 运营在这段时间里点
   * 什么都失败,而界面**从头到尾不会把他送去登录页**,页面上也没有登录入口。
   *
   * 当时两侧的用例全绿。`browser-login.test.tsx` 把 `useIdentity` 整个 mock
   * 掉了,它验的是"拿到 `needsLogin` 之后怎么办",而洞在"`needsLogin` 永远
   * 不会变成真"。这正是本仓 §3.43 那一族:上游全对,断在最后一跳。
   *
   * 所以这一组**一个 mock 都不用**,让真实的响应拦截器自己跑一遍。
   */
  let seen: boolean[] = []
  let off: (() => void) | null = null

  /** 让适配器直接抛一个 axios 形状的失败。写法沿用本文件上面那个 `axiosError` */
  const failWith = (status: number, code?: string) =>
    async (config: InternalAxiosRequestConfig) => {
      throw {
        isAxiosError: true,
        message: 'boom',
        config,
        response: {
          data: code ? { error: { code, message: 'x' } } : {},
          status,
          statusText: 'ERR',
          headers: {},
          config,
        } as AxiosResponse,
      }
    }

  beforeEach(() => {
    installFakeStorage()
    seen = []
    off = onSessionExpiredChange((v) => seen.push(v))
  })

  afterEach(() => {
    off?.()
    off = null
    // 模块级状态,不清会漏进下一条用例 —— 而下一条断言的正是"响了几次"
    clearSessionExpired()
    apiClient.defaults.adapter = undefined
    removeStorage()
  })

  it('业务接口 401 会通知订阅者', async () => {
    apiClient.defaults.adapter = failWith(401)
    await expect(apiClient.get('/products')).rejects.toBeTruthy()
    expect(seen).toEqual([true])
  })

  it('403 不通知 —— 身份是有效的,重新探也探不出新东西', async () => {
    // operator 点了一个 require_admin 的接口。把他踢去登录页的话,他会重新登
    // 一次、回来看到同样的 403 —— 一个能无限循环的动线
    apiClient.defaults.adapter = failWith(403, 'AUTH_FORBIDDEN')
    await expect(apiClient.get('/settings')).rejects.toBeTruthy()
    expect(seen).toEqual([])
  })

  it('匿名接口的 401 不通知 —— 那条路本来就不带身份', async () => {
    apiClient.defaults.adapter = failWith(401)
    await expect(apiClient.post('/auth/login', {})).rejects.toBeTruthy()
    expect(seen).toEqual([])
  })

  it('连着两次 401 只通知一次 —— 否则订阅者会被自己触发的请求无限唤醒', async () => {
    apiClient.defaults.adapter = failWith(401)
    await expect(apiClient.get('/products')).rejects.toBeTruthy()
    await expect(apiClient.get('/tasks')).rejects.toBeTruthy()

    // 订阅者收到通知后会重新探一次身份,而那次探测**自己也会撞 401**。
    // 每次都通知的表现是浏览器里整页卡死,而不是一条红用例
    expect(seen).toEqual([true])
  })

  it('登录成功后清掉,下一次 401 才会重新响', async () => {
    apiClient.defaults.adapter = failWith(401)
    await expect(apiClient.get('/products')).rejects.toBeTruthy()
    clearSessionExpired()
    await expect(apiClient.get('/products')).rejects.toBeTruthy()
    expect(seen).toEqual([true, false, true])
  })

  it('带身份的请求成功之后信号自己会落下来', async () => {
    apiClient.defaults.adapter = failWith(401)
    await expect(apiClient.get('/products')).rejects.toBeTruthy()

    apiClient.defaults.adapter = async (config) =>
      ({ data: {}, status: 200, statusText: 'OK', headers: {}, config }) as AxiosResponse
    await apiClient.get('/products')

    // 一次成功的带身份请求就是"会话还活着"的证据。不落下来的话,
    // 下一次真的过期时它已经是 true,`setSessionExpired` 那条防抖就把
    // 真正该响的那一次吃掉了
    expect(seen).toEqual([true, false])
  })
})
