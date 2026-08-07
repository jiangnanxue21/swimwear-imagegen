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
 * `client.ts` 只在函数体里碰 `window.localStorage`,模块加载期不碰。
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
  describeError,
  isAuthError,
  readError,
  writeAdminToken,
  writeOperatorToken,
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
}

function axiosError({ status, data, code, requestId }: FakeErrorInit): unknown {
  return {
    isAxiosError: true,
    code,
    message: 'boom',
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

  it('连不上:明确告诉运营「这不是你操作错了」', () => {
    const view = describeError(axiosError({ code: 'ERR_NETWORK' }))
    expect(view.text).toContain('不是你操作错了')
    expect(view.text).not.toContain('docker')
    expect(view.technical.hint).toContain('docker compose ps')
  })

  it('401 / 403:指到设置页,且判定为不可重试', () => {
    for (const status of [401, 403]) {
      const view = describeError(axiosError({ status, data: { error: { code: 'UNAUTHORIZED', message: '口令缺失或不正确' } } }))
      expect(view.text).toContain('系统设置')
      // 口令是手填的,重试一百次也不会变对 —— P1-5 的 retry 判断依赖这个字段
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

describe('请求拦截器:两把口令的优先级', () => {
  let captured: InternalAxiosRequestConfig | null = null

  beforeEach(() => {
    installFakeStorage()
    captured = null
    apiClient.defaults.adapter = async (config) => {
      captured = config
      return {
        data: {},
        status: 200,
        statusText: 'OK',
        headers: {},
        config,
      } as AxiosResponse
    }
  })

  afterEach(() => {
    // 口令要**通过公开接口**清掉,而不是只拆掉假 localStorage。
    // client 现在多了一层内存兜底(隐私模式下 localStorage 写不进去时用),
    // 那层是模块级的,`removeStorage()` 碰不到它,活到模块卸载为止。
    // 不清的话,上一条用例写的 op-1 会漏进下一条,
    // 而下一条断言的正是"一个头都不带"。
    writeOperatorToken('')
    writeAdminToken('')
    apiClient.defaults.adapter = undefined
    removeStorage()
  })

  const header = (name: string): unknown => captured?.headers?.get?.(name)

  it('只配了 operator:带 operator,不带 admin', async () => {
    writeOperatorToken('op-1')
    await apiClient.get('/products')
    expect(header('X-Operator-Token')).toBe('op-1')
    expect(header('X-Admin-Token')).toBeUndefined()
  })

  it('只配了 admin:回落带 admin(A5 修的那个缺陷)', async () => {
    // 上一版只在有 operator 时才带头,于是只配了 admin 的浏览器一个头都不带,
    // 后端如实回 401 —— 而后端本来是允许 admin 访问业务接口的
    writeAdminToken('ad-1')
    await apiClient.get('/products')
    expect(header('X-Admin-Token')).toBe('ad-1')
  })

  it('两把都配了:只带 operator —— 顺序不能反', async () => {
    // 免得日常拉列表也把能改 API Key 的那把口令送出去
    writeOperatorToken('op-1')
    writeAdminToken('ad-1')
    await apiClient.get('/products')
    expect(header('X-Operator-Token')).toBe('op-1')
    expect(header('X-Admin-Token')).toBeUndefined()
  })

  it('请求已显式带了 admin 头:不覆盖,也不追加 operator', async () => {
    writeOperatorToken('op-1')
    writeAdminToken('ad-1')
    await apiClient.post('/settings', {}, { headers: { 'X-Admin-Token': 'ad-1' } })
    expect(header('X-Admin-Token')).toBe('ad-1')
    expect(header('X-Operator-Token')).toBeUndefined()
  })

  it('一把都没有:一个头都不带,且不抛异常', async () => {
    await apiClient.get('/health')
    expect(header('X-Operator-Token')).toBeUndefined()
    expect(header('X-Admin-Token')).toBeUndefined()
  })

  it('localStorage 不可用(隐私模式):请求照常发出,不抛异常', async () => {
    removeStorage()
    await expect(apiClient.get('/products')).resolves.toBeDefined()
  })

  it('隐私模式下写口令:如实回报没持久化,但本次会话仍然带得上', async () => {
    // 上一版这里是把异常吞掉就算完,于是设置页提示"口令已记住",
    // 而后续每一个请求都不带口令 —— 提示和事实相反,比没有提示更糟。
    removeStorage()

    expect(writeOperatorToken('op-mem')).toBe(false)

    await apiClient.get('/products')
    expect(header('X-Operator-Token')).toBe('op-mem')
  })
})
