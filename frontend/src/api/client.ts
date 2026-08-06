import axios from 'axios'

/**
 * 默认超时。**它必须短于 nginx 给 `/api/` 的 `proxy_read_timeout`(320s)吗?
 * 恰恰相反 —— 它原来短了一个数量级,那才是问题(A-33)。**
 *
 * 30s 的时候会发生这样一件事:后端还在老老实实地跑(生成上架文件、
 * 跑一批属性识别),网关也还在等,只有浏览器这一端提前放弃了。运营看到
 * 的是「请求超时」,而那次写**很可能已经成功了** —— `describeError` 会把
 * 它判成 `UNKNOWN`,这是对的,但一个本来会成功的请求不该被我们自己
 * 变成结果未知。
 *
 * 更要紧的是接下来那一下:人看到超时就会再点一次。A-16 之前,那正是
 * 「一份导出文件被生成两次」最现实的触发路径。行锁修掉之后重复点击
 * 不再写坏数据,但「不知道成没成」这个体验问题还在。
 *
 * 所以默认值抬到 60s(覆盖绝大多数写),并给已知的长动作单独放宽 ——
 * 不把默认值直接拉到 320s:那样一次真正的网络黑洞会让界面转五分钟,
 * 而运营连"是不是我网断了"都判断不了。
 */
const DEFAULT_TIMEOUT_MS = 60_000

/**
 * 已知的长动作。**上限对齐 nginx 的 `proxy_read_timeout 320s`,略短一点** ——
 * 让超时由浏览器先报,错误里才有我们自己的文案;由网关先断的话,前端拿到
 * 的是一张 504 HTML,`describeError` 认不出来。
 *
 * 用法:`apiClient.post(url, body, { timeout: LONG_TIMEOUT_MS })`。
 * 加新的长接口时往这里挂,不要去动默认值。
 */
export const LONG_TIMEOUT_MS = 300_000

export const apiClient = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL ?? '/api',
  timeout: DEFAULT_TIMEOUT_MS,
})

/**
 * 改配置的口令(后端 ADMIN_TOKEN)。能改 API Key、能把生成切到别家、能烧光额度。
 *
 * 存在 localStorage 里意味着任何 XSS 都能读到它。这是本阶段明确接受的取舍
 * ——它不是账号体系,只是一道运维口令,后端才是真正把关的一方——
 * 但换成账号体系时,这里应当一并换成 httpOnly Cookie 或内存态。
 */
const ADMIN_TOKEN_STORAGE_KEY = 'imagegen.admin-token'

/**
 * 日常业务口令(后端 OPERATOR_TOKENS)。传商品、建任务、审图,以及**读列表**。
 *
 * 以前不存在这个东西:写接口靠 local 开发环境免口令跑通,读接口后端根本不验。
 * 后端现在读写都要身份(评审第 1 条),于是它成了必需品。
 */
const OPERATOR_TOKEN_STORAGE_KEY = 'imagegen.operator-token'

/**
 * 本次会话的口令。**localStorage 写不进去时的唯一去处。**
 *
 * 上一版这里只有一句注释「隐私模式下 localStorage 不可写,不影响本次会话内
 * 继续操作」—— 那句话不成立:异常被吞掉之后没有任何兜底,后续读取仍然只读
 * localStorage,读到的还是空。于是设置页提示「口令已记住」,而**每一个请求
 * 都不带口令**,整站 401。提示和事实相反,比没有提示更糟。
 *
 * 内存态活到刷新为止,这一点必须让用户知道(见 `writeToken` 的返回值)。
 */
const memory = new Map<string, string>()

/**
 * 口令变了。**同一个标签页里 `storage` 事件不触发**,所以要自己发一个(A-10)。
 *
 * `useIdentity` 的 `hasToken` 原来是同步读存储的一个普通变量,注释自己写着
 * "不是响应式的" —— 于是存完口令之后那个组件不会重渲染,`enabled` 不会翻转,
 * 全靠设置页记得手动 invalidate。漏一处就是"口令存好了、界面仍说没登录"。
 *
 * 事件名走 `window`,因为读写口令的地方不止一处,而它们之间没有共同的 React 树。
 */
export const TOKEN_CHANGED_EVENT = 'imagegen:token-changed'

function announceTokenChange(): void {
  try {
    window.dispatchEvent(new Event(TOKEN_CHANGED_EVENT))
  } catch {
    // 非浏览器环境(vitest 的 node 环境)。口令变更没人听也不影响写入本身
  }
}

function read(key: string): string {
  try {
    const stored = window.localStorage.getItem(key)
    if (stored) return stored
  } catch {
    /* 读不到就看内存 */
  }
  return memory.get(key) ?? ''
}

/** 返回是否**持久化**成功。false = 只存在内存里,刷新就没了。 */
function write(key: string, value: string): boolean {
  if (value) memory.set(key, value)
  else memory.delete(key)
  // 无论落到 localStorage 还是只落到内存,**口令都已经变了** —— 所以广播
  // 放在 try 之外。放进 try 的话,隐私模式下写不进存储的那一路就不会通知,
  // 而那一路恰恰是最需要界面立刻跟上的(它只活到刷新为止)
  try {
    if (value) window.localStorage.setItem(key, value)
    else window.localStorage.removeItem(key)
    announceTokenChange()
    return true
  } catch {
    announceTokenChange()
    return false
  }
}

export function readAdminToken(): string {
  return read(ADMIN_TOKEN_STORAGE_KEY)
}

export function writeAdminToken(value: string): boolean {
  return write(ADMIN_TOKEN_STORAGE_KEY, value)
}

export function readOperatorToken(): string {
  return read(OPERATOR_TOKEN_STORAGE_KEY)
}

export function writeOperatorToken(value: string): boolean {
  return write(OPERATOR_TOKEN_STORAGE_KEY, value)
}

/**
 * 全局带上口令。
 *
 * 以前这里刻意**没有**拦截器,理由是"别让拉列表的请求捎上改配置的口令"。
 * 那个理由在后端只保护写接口时成立;现在读接口也要身份,不带就一个页面都打不开。
 *
 * 但那份顾虑是对的,所以两把口令分开处理:
 *
 *   X-Operator-Token   每个请求都带。它只能做日常业务操作
 *   X-Admin-Token      **只在设置页/提示词/Provider 测试显式带**(adminHeaders)
 *
 * 于是"拉一次商品列表"不会把能改 API Key 的那把口令送出去。后端两边都认:
 * admin 天然包含 operator,所以只配了 admin 的开发机照样能用。
 *
 * A5 补的回落:上一版只在有 operator 口令时才带头,只配了 admin 口令的浏览器
 * 于是**一个头都不带**,后端如实回 401 —— 而"后端已经允许 admin 访问业务接口"
 * (`deps.resolve_identity`)这件事在前端被浪费掉了。现在没有 operator 口令时
 * 退而带上 admin 口令。顺序不能反:两把都配了就还是走 operator,
 * 免得日常拉列表也把管理口令送出去。
 */
/**
 * 评审 P2-4:两处写法改掉,行为不变。
 *
 *   `config.headers['X-Admin-Token']`  裸下标是**大小写敏感**的,而 axios 的
 *                                      `AxiosHeaders` 是大小写不敏感的容器。
 *                                      今天能对,只是因为全仓只有 `adminHeaders()`
 *                                      一个写入点、大小写恰好一致。用 `has()`
 *   `config.headers.set?.(…)`          可选调用:一旦 headers 不是 AxiosHeaders
 *                                      实例,它会**静默地不带口令**发出去 ——
 *                                      失败方向是 fail-open,而这是一个鉴权头。
 *                                      改成直接调用:真出这种事要当场炸
 */
apiClient.interceptors.request.use((config) => {
  if (config.headers.has('X-Operator-Token') || config.headers.has('X-Admin-Token')) {
    return config
  }
  const operator = readOperatorToken()
  if (operator) {
    config.headers.set('X-Operator-Token', operator)
    return config
  }
  const admin = readAdminToken()
  if (admin) {
    config.headers.set('X-Admin-Token', admin)
  }
  return config
})

/** 需要管理口令的请求带这个。不做成全局,理由见上面的拦截器注释。 */
export function adminHeaders(): Record<string, string> {
  const value = readAdminToken()
  return value ? { 'X-Admin-Token': value } : {}
}

/**
 * 口令被后端拒过一次(OPS-REVIEW P5:冷启动)。
 *
 * 光看 localStorage 只能发现"没填",发现不了"填错了" —— 而填错的那种更折磨:
 * 每个页面都红一片,每条错误都在说"到设置页核对口令",但没有一处告诉新人
 * **系统整体是好的、只差一个口令**。所以把这件事提升成一个全局事实,
 * 由 `ColdStartBanner` 在所有页面顶部统一说一次。
 *
 * 状态存在模块变量里而不是 React 里:拦截器不在组件树内,而这是整个应用
 * 唯一一份"口令好不好"的判断 —— 两份的话,横幅和请求会各说各话。
 */
let authRejected = false
const authListeners = new Set<(rejected: boolean) => void>()

export function isAuthRejected(): boolean {
  return authRejected
}

/** 订阅口令状态变化。返回退订函数(给 useEffect 用)。 */
export function onAuthRejectedChange(fn: (rejected: boolean) => void): () => void {
  authListeners.add(fn)
  return () => authListeners.delete(fn)
}

function setAuthRejected(value: boolean): void {
  if (authRejected === value) return
  authRejected = value
  for (const fn of authListeners) fn(value)
}

/**
 * 匿名接口:成功了也**不能**证明口令是好的。
 *
 * A5 修的那个缺陷:`/health` 走同一个 apiClient,而响应拦截器对任何成功
 * 响应都执行 `setAuthRejected(false)` —— 于是横幅刚被 401 点亮,
 * 下一次探活成功就把它清掉了,口令错的时候提示反而看不见。
 *
 * 判断仍然只有一份(就在下面那个拦截器里),这里只是把"哪些成功不算数"
 * 说清楚。与后端 `main.PUBLIC_PREFIXES` 对应,加匿名接口时两边一起加。
 */
const ANONYMOUS_PATHS = ['/health', '/media-files']

function isAnonymousPath(url: string | undefined): boolean {
  if (!url) return false
  // 按路径段比,不做裸前缀匹配:`/health-admin` 不是 `/health`
  return ANONYMOUS_PATHS.some((p) => url === p || url.startsWith(`${p}/`))
}

apiClient.interceptors.response.use(
  (response) => {
    // 有一个**带口令的**请求成功了才说明口令没问题。**不等用户手动关掉横幅** ——
    // 一条修好了还赖着不走的警告,下一次会被连同真问题一起忽略
    if (!isAnonymousPath(response.config?.url)) setAuthRejected(false)
    return response
  },
  (error) => {
    if (isAuthError(error)) setAuthRejected(true)
    return Promise.reject(error)
  },
)

export interface ApiErrorBody {
  error: { code: string; message: string; fields?: { loc: string; msg: string }[] }
}

/**
 * 口令不对或缺口令。设置页据此提示"先填口令"而不是笼统的"读取失败"。
 *
 * **`AUTH_FORBIDDEN` 不算。** 后端对「身份有效但角色不够」(operator 去动
 * 管理员接口)也回 403,但那说明这把口令**完全正确** —— 把它算成口令错误的
 * 后果是:运营点开设置页,全局横幅就说「后端不认这把口令」,于是他去改一把
 * 本来就对的口令,改完还是 403,横幅还在。
 *
 * 靠错误码分而不是靠状态码:两者都是 403,状态码上分不出来。
 */
export function isAuthError(err: unknown): boolean {
  if (!axios.isAxiosError<ApiErrorBody>(err)) return false
  if (err.response?.data?.error?.code === 'AUTH_FORBIDDEN') return false
  return err.response?.status === 401 || err.response?.status === 403
}

/**
 * A12:把一次失败拆成**两层**。
 *
 * ## 要修的那件事
 *
 * 上一版只有 `readError` 一个出口,返回一句话,而那句话得同时服务两种人 ——
 * 于是它两边都没服务好:运营读到「无法连接后端服务,检查 docker compose
 * 是否已启动」,那不是他能执行的动作,也不是他该知道的词;管理员读到同一句,
 * 却拿不到错误码,也拿不到能在日志里定位的请求编号(只有 5xx 那一支带)。
 *
 * 所以判定只留这一份,输出分两层:
 *
 *     text        运营看的一句话。**不含命令、容器名、表名、文件名**
 *     technical   管理员展开后才看的:错误码 / HTTP 状态 / 可否重试 / 下一步
 *     requestId   **两层都给**。运营被要求"把请求编号给管理员",
 *                 那他就必须看得见它 —— 方案原文把 request_id 划进技术层,
 *                 但同一段又让运营去转述它,这里按后者落地
 *
 * ## 业务 4xx 为什么不套用那句通用话
 *
 * 方案给的运营文案是「处理失败,请重试。若问题持续存在,请将请求编号提供给
 * 管理员」。那是**故障**的说法,不是**业务规则**的说法:「草稿已过期,禁止导出」
 * 换成「处理失败,请重试」之后,运营会一直重试一件永远不会成功的事,
 * 而真正该做的那一步(去刷新草稿)被这句话盖掉了。
 *
 * 后端的业务 message 本来就是写给运营的,原样透出;通用话只用在
 * 5xx / 超时 / 连不上 / 认不出的失败上 —— 那几种运营确实只能重试或找人。
 *
 * ## 这里**拿不到**的东西
 *
 * 方案的技术层还列了 provider / model / 原始异常。它们不在错误体里,而且
 * 是刻意不在:`AppError.detail` 只进日志,未捕获异常一律回「服务内部错误」
 * (需求第十九章,`main.unhandled_handler`)。前端不该为了凑齐这张清单
 * 去猜,也不该推动后端把异常原文外传 —— 那三样按请求编号在后端日志里查,
 * 技术面板里写明这一句,而不是留三个空字段。
 */

/**
 * 一次失败对**业务状态**的含义(FE-GLOBAL-01)。
 *
 * ## 为什么读和写不能共用一套话
 *
 * 上一版整个前端只有一句「这一步超时了……请重试一次」。对 GET 它是对的:
 * 没读到就是没读到,重读一次没有代价。对 POST / PATCH 它是**错的** ——
 * 超时只说明浏览器不再等了,不说明后端没执行。而这个系统里的写请求包括
 * 建任务、批准、导出、批次重试、平台状态,每一个重复执行都有代价:
 *
 *     重复的付费模型调用      建任务 / 批量识别 / 文案生成
 *     重复的导出与上传        同一份草稿导出两次,平台上出现两条
 *     重复的审核动作          一次批准变成两次,审计里两个人签了同一件事
 *
 * 所以判定分两支,而话术跟着判定走。**判定仍然只有这一份** ——
 * `describeError(err, 'write')`,不是另写一个函数。
 *
 * ## 为什么 5xx 算 FAILED 而不是 UNKNOWN
 *
 * 后端答了话,就说明它至少知道自己失败了;本仓的事务边界(见后端
 * `publish_service.py` 顶部)要求业务写入要么在一个事务里、要么各段
 * 各自提交且可查。真存在"提交了一半再 500"的接口,该由后端返回 2xx
 * 加一个状态字段来表达,而不是让前端对每个 500 都喊"结果未知" ——
 * 那种狼来了的提示三次之后就没人读了。
 */
export type FailureOutcome =
  /** 后端明确拒绝。**没有发生**,重做也是同样的结果 */
  | 'REJECTED'
  /** 后端明确失败。**没有发生**,可以重做 */
  | 'FAILED'
  /** 客户端没拿到答案。**可能已经发生** —— 重做之前必须先核对 */
  | 'UNKNOWN'

/** 这次请求会不会改变业务状态。默认按读处理:**猜错的方向要安全**。 */
export type RequestKind = 'read' | 'write'

/** 技术层。只放后端**真的送过来**的东西 */
export interface ErrorTechnical {
  /** 后端 `ErrorCode`;没走到后端时是 axios 的 code(ERR_NETWORK / ECONNABORTED) */
  code: string
  /** HTTP 状态。请求没到后端就是 null */
  status: number | null
  /** 参数校验明细,一条一行 */
  fields: string[]
  /** 重试有没有意义。**只按状态判**,不猜业务语义 */
  retriable: boolean
  /** 管理员的下一步。业务规则类失败没有下一步,是空串 */
  hint: string
}

export interface ErrorView {
  /** 运营看的一句话 */
  text: string
  /** 请求编号。拿不到时是空串(请求没到后端,或后端没写响应头) */
  requestId: string
  /**
   * 这次失败对业务状态意味着什么。**页面据此决定要不要提供"直接重试"。**
   *
   * `UNKNOWN` 时不允许一键重做:先刷新、先核对,这是 §7.2 那条
   * "超时统一提示重试可能造成重复业务动作"的落点。
   */
  outcome: FailureOutcome
  technical: ErrorTechnical
}

/**
 * 通用故障话术(方案 §3.2 A12 原文)。运营对这几类失败能做的只有两件事:
 * 重试一次,以及把编号交出去。
 */
function genericText(requestId: string): string {
  return requestId
    ? `处理失败,请重试。若问题持续存在,请把请求编号 ${requestId} 提供给管理员`
    : '处理失败,请重试。若问题持续存在,请联系管理员'
}

/**
 * 写请求没拿到答案时的话术。**刻意不出现"请重试"这三个字。**
 *
 * 运营读到"请重试"就会去点重试,而这正是这一支要防的动作。给他的第一个
 * 动作必须是"先看一眼现在是什么状态" —— 那个动作没有任何代价,
 * 而且多数情况下它会直接给出答案(任务已经在跑、批次已经建了、导出已经有了)。
 */
function unknownText(requestId: string, timedOut: boolean): string {
  const cause = timedOut
    ? '后端 30 秒内没有回答'
    : '和后端的连接中断了'
  const tail = requestId ? `,并把请求编号 ${requestId} 一并提供` : ''
  // 注意:这段话会走 `message.error()` 与 <Typography.Text>,**不经过 markdown**。
  // 强调只能靠用词和语序,写 `**未知**` 的话运营会原样看到两个星号
  return (
    `这一步的结果未知:请求已经发出去,但${cause},` +
    '所以无法确认它有没有生效。请先刷新本页确认当前状态,不要直接重做 —— ' +
    `重复的生成、导出或审核不会被自动撤销。` +
    `确认没有生效再重做;拿不准就找管理员核对${tail}`
  )
}

/**
 * 技术层的下一步。**命令只出现在这里**——这个对象整体只在管理员展开时渲染,
 * 所以 A12 那条"运营页面不出现 docker compose / 迁移 / 代码命令"仍然成立。
 */
const HINTS = {
  server: '按请求编号在后端 JSON 日志里搜 request_id,可定位到这一次请求;错误体里不带异常原文是刻意的(需求第十九章)',
  network: '请求没到后端。确认后端在运行(docker compose ps),以及前端的 VITE_API_BASE_URL 指向它',
  timeout: '后端 30 秒未响应。看 worker 是不是卡在 Provider 轮询上(FASHN 不支持取消),或数据库连接被占满',
  auth: '核对后端 OPERATOR_TOKENS / ADMIN_TOKEN 与本浏览器存的那把口令;后端 admin 天然包含 operator',
  forbidden: '这把口令通过了身份验证,但角色是 operator。该接口挂的是 require_admin,需要 ADMIN_TOKEN',
  rate: '被限流。看 Provider 配额与当前并发',
} as const

/** 三样不在错误体里的东西去哪儿找。写出来,而不是留三个空字段。 */
export const TECHNICAL_ELSEWHERE =
  'provider / model / 原始异常不在错误体里(后端从不外传),按请求编号查后端日志'

/** 从响应头里取后端的请求编号(main.py 的 request_context 中间件对每个响应都写)。 */
function requestIdOf(err: unknown): string {
  if (!axios.isAxiosError(err)) return ''
  const headers = err.response?.headers as Record<string, unknown> | undefined
  const value = headers?.['x-request-id']
  return value == null ? '' : String(value)
}

/**
 * 全站唯一一份失败判定。`readError` 与 `<ErrorNotice>` 都从这里取。
 *
 * `kind` 只影响**请求没到后端**那一支(超时 / 连接中断):
 * 读请求那时是"没读到",写请求那时是"不知道有没有写进去"。
 * 其余各支两种请求的结论相同,所以没有第二套分类。
 */
export function describeError(err: unknown, kind: RequestKind = 'read'): ErrorView {
  const requestId = requestIdOf(err)

  if (!axios.isAxiosError<ApiErrorBody>(err)) {
    // 不是请求失败(多半是前端自己抛的)。这里没有任何可展开的技术信息,
    // 真出这种事由 `ErrorBoundary` 接管,它有编号也有"复制详情"
    return {
      text: genericText(''),
      requestId: '',
      outcome: 'FAILED',
      technical: { code: '', status: null, fields: [], retriable: true, hint: '' },
    }
  }

  const status = err.response?.status ?? null
  const body = err.response?.data
  const code = body?.error?.code ?? err.code ?? ''
  const fields = (body?.error?.fields ?? []).map((f) => `${f.loc}: ${f.msg}`)

  // 请求根本没到后端:超时与连不上。两句都不提容器和命令,那些进 hint
  //
  // **这是读写唯一分道的地方。** 对 GET,"没拿到答案"就是"没读到";
  // 对 POST / PATCH,"没拿到答案"不能推出"没执行" —— 后端可能已经
  // 建了任务、导出了文件、批准了一版图,只是回答没能送回来
  if (status === null) {
    const timedOut = err.code === 'ECONNABORTED'
    if (kind === 'write') {
      return {
        text: unknownText(requestId, timedOut),
        requestId,
        outcome: 'UNKNOWN',
        technical: {
          code,
          status,
          fields,
          // 不是"重试无意义",是"重试**不安全**"。页面据此把一键重做换成
          // 先刷新、再人工确认。技术层的措辞照实说,别让管理员以为是前者
          retriable: false,
          hint: `${timedOut ? HINTS.timeout : HINTS.network};写请求超时不代表未执行,先按请求编号查后端日志与审计再决定要不要重做`,
        },
      }
    }
    return {
      text: timedOut
        ? '这一步超时了,后端 30 秒内没有响应。请重试一次;若仍然失败,请联系管理员'
        : '连不上服务。请稍后重试;若一直如此,请联系管理员 —— 这不是你操作错了',
      requestId,
      outcome: 'FAILED',
      technical: {
        code,
        status,
        fields,
        retriable: true,
        hint: timedOut ? HINTS.timeout : HINTS.network,
      },
    }
  }

  if (code === 'AUTH_FORBIDDEN') {
    // 口令是对的,只是这件事这把口令做不了。**不能指他去核对口令** ——
    // 那会让他去改一把正确的口令,而正确的做法是找有管理口令的人
    return {
      text: `${body?.error?.message ?? '权限不足'} —— 你的口令是有效的,这一步需要管理口令,请联系管理员`,
      requestId,
      outcome: 'REJECTED',
      technical: { code, status, fields, retriable: false, hint: HINTS.forbidden },
    }
  }

  if (status === 401 || status === 403) {
    // 口令是手填的,不存在"登录失效",只有"没填/填错"。指到设置页是运营做得到的动作
    return {
      text: `${body?.error?.message ?? '口令缺失或不正确'} —— 到「系统设置」页核对口令后重试`,
      requestId,
      outcome: 'REJECTED',
      technical: { code, status, fields, retriable: false, hint: HINTS.auth },
    }
  }

  if (status >= 500) {
    return {
      text: genericText(requestId),
      requestId,
      // 后端答了话就说明它知道自己失败了(理由见 FailureOutcome 的说明)
      outcome: 'FAILED',
      technical: { code, status, fields, retriable: true, hint: HINTS.server },
    }
  }

  // 业务 4xx:后端的 message 就是写给运营的话,原样透出(理由见文件头)。
  // 字段明细跟着一起给 —— 422 时它是唯一说得清"哪个字段"的东西
  if (body?.error?.message) {
    return {
      text: fields.length ? `${body.error.message}(${fields.join('; ')})` : body.error.message,
      requestId,
      outcome: 'REJECTED',
      technical: {
        code,
        status,
        fields,
        retriable: status === 429,
        hint: status === 429 ? HINTS.rate : '',
      },
    }
  }

  // 4xx 但没有统一错误体:多半是反向代理或网关插进来的,后端没经手
  return {
    text: genericText(requestId),
    requestId,
    outcome: 'REJECTED',
    technical: { code, status, fields, retriable: false, hint: HINTS.server },
  }
}

/**
 * 一句话版本。**只是 `describeError` 的薄封装**,不许在这里另加判断 ——
 * 全站 28 处调用点靠它拿到运营层文案(17 处 `<Alert>`、5 处 toast、
 * 6 处其它:三个 `<Empty>`、一个 `<span>`、`BatchActionBar` 的局部 state、
 * `api/workbench.ts` 的透传),判定一旦分成两份,
 * toast 和面板会对同一次失败说两种话。
 *
 * 本注释原写「全站 100 多处 toast 靠它」。实际 toast 调用点是 16 个
 * (其中 5 个套着 `readError`)—— 差了一个数量级,而那个数字撑着
 * `docs/STATUS.md` 里「toast 按设计不迁移」这个决定,决定的分量正取决于基数。
 *
 * **重新数的时候注意**:直接 grep 会多数出几条,因为本仓库有几处注释
 * (包括这一段)在正文里提到了那个函数名。要真实调用点得先滤掉注释行:
 *
 *     grep -rn 'readError(' frontend/src | grep -vE ':\s*\*|//'
 */
export function readError(err: unknown): string {
  return describeError(err).text
}

/**
 * 写请求版本。**改变了业务状态的接口一律用这个,不要用 `readError`。**
 *
 * 判断哪些算写请求不看 HTTP 动词,看后果:会不会花钱、会不会产出文件、
 * 会不会改变别人看到的状态。`POST /batches/plan` 只算不做,用 `readError`;
 * `GET /batches/{id}/file` 会写一行下载审计,但那是只增流水、重复下载没有
 * 业务后果,也用 `readError`。
 */
export function readWriteError(err: unknown): string {
  return describeError(err, 'write').text
}

/**
 * 这次写请求是不是"结果未知"。
 *
 * 页面用它把「失败了,重试」换成「先刷新核对」—— 两者的区别不在措辞,
 * 在于**下一个按钮是什么**。
 */
export function isResultUnknown(err: unknown): boolean {
  return describeError(err, 'write').outcome === 'UNKNOWN'
}
