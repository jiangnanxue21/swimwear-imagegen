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
 *
 * ## multipart 上传一律挂它,不看后端算得快不快
 *
 * axios 的 timeout 盖的是**整个请求周期,含把字节推上去的那段**。
 * 一个 20MB 的素材(后端上限)在办公室上行带宽下 60 秒传不完是常态,
 * 而超时的那一刻后端可能刚收完、正在写库。
 *
 * 这一条原来漏了全部五个上传点(素材、商品 CSV、批量导入预览与提交、
 * 模特模板),其中 `importCommit` 是**写**:它超时会走进本仓最重的那一支
 * ——「结果未知,不许直接重做」—— 而那个"未知"是前端自己用一个偏短的
 * 超时造出来的。A-33 修的正是这个病灶,只是当时只看了 JSON 接口。
 */
export const LONG_TIMEOUT_MS = 300_000

export const apiClient = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL ?? '/api',
  timeout: DEFAULT_TIMEOUT_MS,
  /*
   * 浏览器登录的 Session Cookie 靠它送出去。**这一行是整条登录链路的最后一跳。**
   *
   * 后端已经在 `/auth/login` 上发了 HttpOnly Cookie,而 axios 默认
   * `withCredentials: false`。同源部署(nginx 把 `/api/` 反代到后端、
   * dev 走 vite proxy)下 axios 会因为这一行把 Cookie 带上,
   * **少了它,一次同源的 XHR 也不会携带凭据**。
   *
   * ## 它**修不了**跨源部署 —— 这一段原来说反了
   *
   * 原文写的是:出问题的是 `VITE_API_BASE_URL` 指向另一个源的那种部署,
   * Cookie 一次都不会发出去,表现是"登录成功了,然后立刻又要登录",
   * 言下之意这一行把它修好了。**没有。** 后端那枚 Cookie 是
   * `SameSite=Lax`(`backend/app/main.py` 的 `same_site="lax"`),
   * 跨站 XHR 本来就不带它;`withCredentials` 管的是"要不要带",
   * 不能让浏览器违反 SameSite。也就是说真配了跨源地址的部署会**精确复现**
   * 那段注释声称已修复的症状。
   *
   * 今天的结论只有一句:**同源反代是唯一受支持的会话部署形态。**
   * 跨源要可用得后端改 `SameSite=None; Secure` 并配带 credentials 的 CORS,
   * 那是另一个安全权衡(`core/config.py` 里 `CORS_ORIGINS=*` 那一段写着:
   * 今天那个洞正是被 Lax 兜着的)。同样的话写在 `.env.example` 上。
   *
   * 这一行本身仍然是必需的,只是它服务的是同源那一档。
   *
   * 这正是本仓 §3.43 那一族缺陷:上游全对,断在最后一跳,而两侧的测试都绿。
   * 所以它有一条源码级守卫
   * (`backend/tests/pure/test_a46_phase2_browser_login_seam.py` 的
   * `test_the_browser_actually_sends_the_session_cookie`)。
   */
  withCredentials: true,
})

/*
 * ## 这里原来有一整条浏览器 Token 链,随 Browser Login 一起退役(PRD §26/§27)
 *
 * 退役的是:`imagegen.admin-token` / `imagegen.operator-token` 两个 localStorage
 * 键、写不进去时的 `memory` 兜底、`TOKEN_CHANGED_EVENT` 与 `announceTokenChange`、
 * `read/write` 两个私有函数、`readAdminToken` / `writeAdminToken` /
 * `readOperatorToken` / `writeOperatorToken`、自动带 `X-Operator-Token` 并回落
 * `X-Admin-Token` 的请求拦截器、`adminHeaders()`,以及 `authRejected` 那一组
 * (`isAuthRejected` / `onAuthRejectedChange` / `setAuthRejected`)。
 *
 * **不是简化,是换了一套凭据。** 浏览器现在拿的是 HttpOnly 签名 Cookie
 * (`withCredentials: true`,见上面),它读不到、也不需要读。`ADMIN_TOKEN` /
 * `OPERATOR_TOKENS` 仍然在后端好好活着,只服务 CLI、脚本、pytest 与服务间调用。
 *
 * ## 为什么 `token` 模式下删掉它是安全的
 *
 * 会有人担心:`/health` 回 `auth_mode: token` 的部署,浏览器岂不是没凭据了?
 * 不会。`config._check_browser_auth` 规定 `APP_ENV ∉ LOCAL_ENVS` 时三项必填、
 * 配不全直接起不来 —— 也就是说 `token` 模式**只可能出现在 local/dev**,
 * 而那里 `deps.resolve_identity` 的 `ROLE_DEV` 回落本来就不要任何凭据。
 * 「口令模式的浏览器」这个组合在今天的配置空间里不存在。
 *
 * ## 别照着旧门禁的失败信息把它加回来
 *
 * 曾经有三条门禁守着这条链,其中一条的失败信息是「回落被整个删掉了 ——
 * 只配 admin 的部署会整站 401,那不是修复」。那句话在口令时代是对的,今天不是:
 * 今天整站 401 的正确解法是登录,不是往 localStorage 里塞一把口令。
 * 三条门禁已随本轮改写(见 `test_gate_a_guards.py` / `test_a44_batch6_fixes.py`)。
 */

/**
 * 匿名接口:成功了也**不能**证明登录态是好的;失败了也不该把人踢去登录页。
 *
 * A5 修的那个缺陷:`/health` 走同一个 apiClient,而响应拦截器对任何成功
 * 响应都执行 `setAuthRejected(false)` —— 于是横幅刚被 401 点亮,
 * 下一次探活成功就把它清掉了,口令错的时候提示反而看不见。
 *
 * 判断仍然只有一份(就在下面那个拦截器里),这里只是把"哪些成功不算数"
 * 说清楚。与后端 `main.PUBLIC_PREFIXES` 对应,加匿名接口时两边一起加。
 */
const ANONYMOUS_PATHS = ['/health', '/media-files', '/auth/login', '/auth/logout']

function isAnonymousPath(url: string | undefined): boolean {
  if (!url) return false
  // 按路径段比,不做裸前缀匹配:`/health-admin` 不是 `/health`
  return ANONYMOUS_PATHS.some((p) => url === p || url.startsWith(`${p}/`))
}

/**
 * 这个部署认哪种凭据。**真相来源是后端 `/health` 的 `auth_mode`,不是这里。**
 *
 * 记一份的唯一理由:`describeError` 不在 React 树里,它拿不到那个查询的结果,
 * 而它必须对 401 说对话 —— 三种模式下该说的话完全不同:
 *
 *     session  "请重新登录"
 *     dev      本机免口令,没有登录这个动作 -> "后端配置刚变过"
 *     token    只配了 Header 口令,**浏览器根本进不来** ->
 *              得说清这一点,并给出两条真出路
 *
 * `dev` 与 `token` 原来合成一档(都叫 `token`),而那两句话指向的动作相反:
 * 前者刷新一下就好,后者要改服务器配置。合并的代价在 2026-08-11 评审第 8 条
 * 里写着 —— 界面当时给的每一句指引都指向一个已经被删掉的口令输入框。
 *
 * 由响应拦截器在 `/health` 回来时写入 —— 也就是说它**永远不早于**那次响应,
 * 而 React 那一侧读的是同一次响应的 `data`。两处不会分叉:它们是同一个来源,
 * 不是两份判断。
 *
 * ## 默认值是 `unknown`,不是三种模式里的任何一种
 *
 * 原来默认 `token`,理由是"多解释一句总比把人送进登不进去的登录页好"。
 * 那条理由在 `token` 只是"免登录"的同义词时成立;`token` 现在有了自己那句
 * **很重的**话(整个部署的浏览器都进不来),拿它当默认就变成了:
 * `/health` 还没回来的那一小段时间里,任何一个 401 都会声称服务器配错了。
 *
 * 所以第四个值不是三种模式之一,而是"还没问到" —— 它的话只说事实
 * (登录态可能失效了),不对部署配置下任何结论。这与本仓反复出现的
 * "NULL 是不知道,0 是免费"是同一条:**不知道要能被表达出来。**
 */
type AuthMode = 'session' | 'dev' | 'token' | 'unknown'
let authMode: AuthMode = 'unknown'

function isSessionAuth(): boolean {
  return authMode === 'session'
}

/**
 * 登录态**可能**失效了(**401,不含 403**)。
 *
 * ## 它不是结论,是一句"去重新问一遍"
 *
 * 这个信号**不直接决定跳不跳登录页**。判定只有一处:`useIdentity` 那次
 * `/auth/whoami`。这里发生的全部事情是:某个业务请求撞了 401,于是通知
 * `useIdentity` 把缓存的身份丢掉、重新问后端一次,由后端的回答决定去留。
 *
 * 这么绕一圈是有理由的。反过来"看见 401 就跳登录页"会把两件事混在一起:
 * 一次偶发的 401(某个接口配错了守卫、网关抽风)会把一个登录态完好的人
 * 踢出去,而他重新登一次、回来撞上同一个接口,再被踢出去 —— 一个能无限
 * 循环的动线。让 whoami 来回答,"我到底登没登着"就仍然只有一个答案来源。
 *
 * ## 和 `authRejected` 的分工
 *
 *     authRejected   401 或 403(除 AUTH_FORBIDDEN)。点亮横幅,说一句话
 *     sessionExpired 只有 401。让 `useIdentity` 重新探一次身份
 *
 * 403 一律不算:`AUTH_FORBIDDEN` 说明这次身份是**有效的**(operator 去动了
 * 管理接口),`CONFIG_INVALID` 说明服务端没配凭据。这两种情况重新探身份
 * 探不出任何新东西,只会多打一次请求。
 *
 * ## 只在**翻转**时通知
 *
 * `if (sessionExpired === value) return` 那一行不是优化,是**防死循环**:
 * 重新探身份本身也会撞 401,如果每次 401 都通知一遍,订阅者就会被自己
 * 触发的请求无限唤醒。已经是 true 时再来一个 401,什么都不做才是对的。
 */
let sessionExpired = false
const sessionListeners = new Set<(expired: boolean) => void>()

/** 订阅"该重新探一次身份了"。返回退订函数,给 `useEffect` 当清理用 */
export function onSessionExpiredChange(fn: (expired: boolean) => void): () => void {
  sessionListeners.add(fn)
  return () => {
    sessionListeners.delete(fn)
  }
}

function setSessionExpired(value: boolean): void {
  if (sessionExpired === value) return
  sessionExpired = value
  for (const fn of sessionListeners) fn(value)
}

/** 登录成功后调用,把上一次的 401 记录清掉 —— 否则它属于上一个会话 */
export function clearSessionExpired(): void {
  setSessionExpired(false)
}

apiClient.interceptors.response.use(
  (response) => {
    // 一次成功的**带身份**请求,就是"会话还活着"的证据。匿名接口不算:
    // `/health` 在没登录时也会 200,拿它当证据等于永远不会发现登录态没了
    if (!isAnonymousPath(response.config?.url)) {
      setSessionExpired(false)
    }
    if (response.config?.url === '/health') {
      const mode = (response.data as { auth_mode?: string } | undefined)?.auth_mode
      // 只认这三个值。后端将来加第四种模式而前端没跟上时,退回 `token` ——
      // 它的话最详细、也最不容易把人引错方向。按一个不认识的字符串猜是不行的:
      // 猜错的方向是把人锁在登录页外面
      authMode = mode === 'session' ? 'session' : mode === 'dev' ? 'dev' : 'token'
    }
    return response
  },
  (error) => {
    if (
      axios.isAxiosError(error) &&
      error.response?.status === 401 &&
      !isAnonymousPath(error.config?.url)
    ) {
      setSessionExpired(true)
    }
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
 * 这一次请求**实际**等了多久,写成一句人话("60 秒" / "5 分钟")。
 *
 * ## 为什么是派生的,不是写死的常量
 *
 * 这三句话原来都写着"30 秒",而默认超时在 A-33 那一轮就抬到了 60s、
 * 长动作还挂着 `LONG_TIMEOUT_MS`(300s)。也就是说一个跑满 5 分钟才超时的
 * 发布请求,界面会告诉运营"后端 30 秒内没有回答" —— 差一个数量级,
 * 而这句话恰恰出现在**结果未知、禁止直接重做**那一支上,是要他据此做判断的。
 *
 * 把 30 改成 60 只是把下一次过期推迟到下一次调超时。axios 把这次请求真实
 * 用的 `timeout` 放在 `err.config` 里,从那儿读,文案就永远说得准 ——
 * 这与本仓「会变的数字不写死」是同一条规矩,只是以前只用在文档上。
 *
 * 拿不到 config(极少数情况下 axios 不带它)就回落到默认值:说一个可能
 * 偏小的数,好过说一个凭空来的数。
 */
function timeoutPhrase(err: unknown): string {
  const ms = (axios.isAxiosError(err) ? err.config?.timeout : undefined) || DEFAULT_TIMEOUT_MS
  const seconds = Math.round(ms / 1000)
  // 120 秒是分界:再往上"秒"这个单位就不再帮助人建立时间感了
  return seconds < 120 ? `${seconds} 秒` : `${Math.round(seconds / 60)} 分钟`
}

/**
 * 写请求没拿到答案时的话术。**刻意不出现"请重试"这三个字。**
 *
 * 运营读到"请重试"就会去点重试,而这正是这一支要防的动作。给他的第一个
 * 动作必须是"先看一眼现在是什么状态" —— 那个动作没有任何代价,
 * 而且多数情况下它会直接给出答案(任务已经在跑、批次已经建了、导出已经有了)。
 */
function unknownText(requestId: string, timedOut: boolean, waited: string): string {
  const cause = timedOut
    ? `后端 ${waited}内没有回答`
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
  auth: '这是 local/dev 的免登录模式(后端三项浏览器登录配置全空),正常不该出现 401。先看后端是不是换了 APP_ENV 或补了 ADMIN_PASSWORD —— 补了任意一项,本机也会真的走登录',
  session: '浏览器会话已失效或从未建立。签名 Cookie 是无状态的,换过 AUTH_SESSION_SECRET 会让全部已登录会话当场作废;多机部署各节点必须配同一把',
  forbidden: '当前账号是运营(operator),身份是有效的;该接口挂的是 require_admin,要用管理员(admin)账号登录。设置页里那两把 ADMIN_TOKEN / OPERATOR_TOKENS 是给命令行和脚本用的机器凭据,改它们不影响谁能登录',
  rate: '被限流。看服务商配额与当前并发',
} as const

/**
 * 超时的技术层提示。**和运营那句话读同一个 `timeoutPhrase`**,
 * 所以两层永远不会各说一个数 —— 它原来是 `HINTS` 里一条写死"30 秒"的常量,
 * 而管理员正是拿这个数去判断"worker 是不是卡住了"的。
 */
function timeoutHint(waited: string): string {
  return `后端 ${waited}未响应。看后台执行进程(worker)是不是卡在服务商轮询上(FASHN 不支持取消),或数据库连接被占满`
}

/** 三样不在错误体里的东西去哪儿找。写出来,而不是留三个空字段。 */
export const TECHNICAL_ELSEWHERE =
  '服务商、模型与原始异常都不在错误体里(后端从不外传),按请求编号查后端日志'

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
    // 这次请求真实等了多久。三处话术(运营那句、管理员那句、读超时那句)
    // 全部从它派生 —— 一个 300s 的长动作就该说"5 分钟",不是"30 秒"
    const waited = timeoutPhrase(err)
    if (kind === 'write') {
      return {
        text: unknownText(requestId, timedOut, waited),
        requestId,
        outcome: 'UNKNOWN',
        technical: {
          code,
          status,
          fields,
          // 不是"重试无意义",是"重试**不安全**"。页面据此把一键重做换成
          // 先刷新、再人工确认。技术层的措辞照实说,别让管理员以为是前者
          retriable: false,
          hint: `${timedOut ? timeoutHint(waited) : HINTS.network};写请求超时不代表未执行,先按请求编号查后端日志与审计再决定要不要重做`,
        },
      }
    }
    return {
      text: timedOut
        ? `这一步超时了,后端 ${waited}内没有响应。请重试一次;若仍然失败,请联系管理员`
        : '连不上服务。请稍后重试;若一直如此,请联系管理员 —— 这不是你操作错了',
      requestId,
      outcome: 'FAILED',
      technical: {
        code,
        status,
        fields,
        retriable: true,
        hint: timedOut ? timeoutHint(waited) : HINTS.network,
      },
    }
  }

  if (code === 'AUTH_FORBIDDEN') {
    // 登录是有效的,只是这件事这个账号做不了。**不能指他去改凭据** ——
    // 重新登录、换密码都不会让 operator 变成 admin,正确的做法是找管理员
    return {
      text: `${body?.error?.message ?? '权限不足'} —— 你的登录是有效的,这一步需要管理员账号,请联系管理员`,
      requestId,
      outcome: 'REJECTED',
      technical: { code, status, fields, retriable: false, hint: HINTS.forbidden },
    }
  }

  if (status === 401 || status === 403) {
    /*
     * 这句尾巴分**三**种说法,因为三种部署下运营该做的事完全不同:
     *
     *     会话模式   重新登录一次。设置页里那两把是**机器凭据**,改它没有用
     *     免登录模式 local/dev 且三种凭据全空。既没有"登录"也没有口令可填,
     *                所以只能说"配置刚变过"
     *     口令模式   只配了 Header 口令。**浏览器进不来**,而且它不该进得来 ——
     *                这一支必须说出这件事,并给出两条真出路。原来它和免登录
     *                模式合成一支,说的是"这台机器处于免登录的开发模式",
     *                而那句话在这里每一个字都是错的(2026-08-11 评审第 8 条)
     *
     * 上一版这里还有一句"到「系统设置」页核对口令"。浏览器登录落地、
     * localStorage 口令链删除之后,那句话指向一个**已经不存在的输入框**。
     */
    const tail =
      authMode === 'session'
        ? ' —— 登录状态已失效,请重新登录'
        : authMode === 'dev'
          ? ' —— 这台机器处于免登录的开发模式,出现这一条说明后端配置刚变过,请刷新或联系管理员'
          : authMode === 'token'
            ? ' —— 这个部署只配置了 Header 口令,浏览器无法用它登录(界面上没有填口令的地方)。请让管理员配置浏览器登录密码,或在本机开发时清空 Header 口令'
            : // 还没问到这个部署认哪种凭据(`/health` 尚未返回)。**只说事实,
              // 不对部署配置下结论** —— 见上面 `AuthMode` 那一段
              ' —— 请刷新本页重新登录;若刷新后仍然如此,请联系管理员'
    return {
      text: `${body?.error?.message ?? '登录状态已失效'}${tail}`,
      requestId,
      outcome: 'REJECTED',
      technical: {
        code,
        status,
        fields,
        retriable: false,
        hint: isSessionAuth() ? HINTS.session : HINTS.auth,
      },
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
 * 全站几十处调用点靠它拿到运营层文案(`<Alert>` 占多数,其余是 toast、
 * 几个 `<Empty>`、`BatchActionBar` 的局部 state、`api/workbench.ts` 的透传)。
 * 判定一旦分成两份,toast 和面板会对同一次失败说两种话。
 *
 * ## 这里**刻意不写精确条数**(前端评审 P2-2)
 *
 * 这段注释的历史值得留着:先写「全站 100 多处」,被订正为「28 处(17 处
 * Alert、5 处 toast、6 处其它)」,并附上重数的命令 —— 也就是说作者明知道
 * 它会漂,还是把精确值写了回去。半年后实测 30 处。
 *
 * **一个没人守着的精确数字,每一次订正只是把下一次过期往后推。** 本仓
 * 「会变的数字不写死」这条规矩一直只用在文档上,而 P1-1 那三处"30 秒"
 * 证明了注释里的数字同样会漂,而且能漂进用户可见的文案。
 *
 * 所以改成量级描述。真要数就现数一遍(得先滤掉注释行,本仓有几处注释
 * 在正文里提到了这个函数名):
 *
 *     grep -rn 'readError(' frontend/src | grep -vE ':\s*\*|//'
 *
 * 唯一还需要一个**基数**的地方是 `docs/STATUS.md` 里「toast 按设计不迁移」
 * 那个决定 —— 它要的是"十几个还是一百多个"这个量级,而不是某一天的确值。
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

/** 一条字段级校验明细。`loc` 与后端 `FieldProblem.loc` 逐字相同 */
export interface FieldProblem {
  loc: string
  msg: string
}

/**
 * 字段级校验明细,**保留结构**。
 *
 * ## 为什么要有这个函数
 *
 * `describeError` 早就把 `fields` 读出来了,但它当场拍成了
 * `` `${loc}: ${msg}` `` 字符串数组 —— 那份是给 `ErrorNotice` 的技术详情
 * 折叠面板看的,一行一句就够。
 *
 * 而后端为 `loc` 付过一次真金白银的代价:`spu_service._api_loc()` 专门把
 * 纯层的 `variant_codes[1]` 翻成 `color_variants[1].variant_code`,
 * 那个函数的 docstring 里还记着上一版翻错时的表现(「前端高亮不到任何一行」)。
 * 翻它的**唯一**理由就是让表单能定位到具体那一行,而前端把结构丢在了
 * 字符串拼接里 —— 后端付了钱,前端没接。
 *
 * 拼过的字符串再解析回来是不行的:`msg` 里本来就可能含冒号。
 *
 * 拿不到时回空数组,不是 null:调用方一律 `.filter()` / `.find()`,
 * 少一次判空就少一次漏判。
 */
export function fieldProblems(err: unknown): FieldProblem[] {
  if (!axios.isAxiosError<ApiErrorBody>(err)) return []
  return (err.response?.data?.error?.fields ?? []).map((f) => ({
    loc: String(f.loc ?? ''),
    msg: String(f.msg ?? ''),
  }))
}
