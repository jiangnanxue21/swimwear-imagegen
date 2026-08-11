"""a46-phase2:浏览器登录的**跨语言接缝**。

## 这个文件只放跨语言的那几条

`frontend/CLAUDE.md` 第三条:不要用 Python 扫前端源码来做断言。菜单里有没有
某个按钮、表单提交后跳到哪一页,这些交给 Vitest —— 那一侧读的是真实对象,
而这一侧只能拿正则去猜 TSX 的形状。本批的
`frontend/tests/component/browser-login.test.tsx` 就是那一份。

留在这里的只有两侧**必须一致**、而任何一侧的测试单独都看不见的东西:

    匿名白名单       后端 PUBLIC_PREFIXES ↔ 前端 ANONYMOUS_PATHS
                     (phase1 已经有一条,这里不重复)
    auth_mode 取值   后端 health.py 的字面量 ↔ 前端 health.ts 的联合类型
    调用的路径       前端调 `/auth/login`,后端真的注册了这条路由

第二条是本批新开的一格,而它有一个很安静的失败模式:后端哪天把 `session`
改成 `browser`,前端那个 `mode === 'session'` 不会报错,只会**永远判成口令模式**
—— 于是每一个被踢下线的人都被指去设置页填一把不会被读的口令。TypeScript 看不见
后端的字面量,pytest 看不见前端的联合类型,只有这里能同时看见两边。

## 为什么不去跑那些接口

跑 `/health` 要 FastAPI、要 settings、要一整个 app —— 而这一层的判据是
"两边写的是不是同一个字符串",不需要任何运行时。真的调用那条链路的用例在
`backend/tests/test_auth_session.py`(要 pytest 与依赖)。
"""
from __future__ import annotations

import re

from tests.pure._helpers import BACKEND_ROOT, PROJECT_ROOT, window

FRONTEND_SRC = PROJECT_ROOT / "frontend" / "src"


def _read(path) -> str:
    return path.read_text(encoding="utf-8")


def _backend(rel: str) -> str:
    return _read(BACKEND_ROOT / "app" / rel)


def _fe(rel: str) -> str:
    return _read(FRONTEND_SRC / rel)


# ================================================================ auth_mode


def test_the_two_auth_modes_are_spelled_the_same_on_both_sides():
    """后端发的字面量和前端认的字面量必须逐字相同。

    失败模式是**静默判错**,不是报错:前端 `mode === 'session'` 对一个不认识的
    字符串只会得到 false,于是会话模式的部署被当成口令模式 —— 每一个被踢下线的
    人都被指向设置页,而那里没有他要的东西。
    """
    backend = _backend("api/health.py")
    modes = dict(
        re.findall(r'^(AUTH_MODE_\w+) = "([a-z]+)"$', backend, re.M)
    )
    assert modes == {"AUTH_MODE_SESSION": "session", "AUTH_MODE_TOKEN": "token"}, (
        f"后端的 auth_mode 取值变了:{modes} —— 前端那个等号比较不会报错,只会永远判成口令模式"
    )

    health_ts = _fe("api/health.ts")
    assert "auth_mode?: 'session' | 'token'" in health_ts, (
        "前端 HealthOut 上的 auth_mode 联合类型和后端对不上了"
    )

    # 真正做判断的那一处也要认同一个词。类型对了而比较写错,tsc 一样是绿的
    client_ts = _fe("api/client.ts")
    assert "mode === 'session' ? 'session' : 'token'" in client_ts, (
        "client.ts 里那次归一化不认 'session' 了 —— 类型声明对不代表比较写对"
    )


def test_the_backend_derives_auth_mode_from_the_same_property_the_guard_uses():
    """`auth_mode` 必须取 `browser_auth_configured`,不许在这里重新推一遍。

    重新推的写法(比如"三项都非空")和 `deps.resolve_identity` 的判据会分叉,
    而分叉的方向是最坏的那个:local 下只填了密码、没填 secret 时,守卫已经按
    Session 在拦,而 `/health` 说这是口令模式 —— 前端把人送去设置页,
    填完还是进不来,页面上没有任何一句话解释为什么。
    """
    backend = _backend("api/health.py")
    assert "settings.browser_auth_configured" in backend, (
        "/health 没有读 browser_auth_configured —— 它和守卫的判据会分叉"
    )
    assert '"auth_mode"' in backend, "/health 没有返回 auth_mode"

    # 反向:守卫那边读的是同一个属性。两处一起改才有意义
    deps = _backend("api/deps.py")
    assert "settings.browser_auth_configured" in deps, (
        "resolve_identity / rejection 不再读 browser_auth_configured —— "
        "两处判据必须同源"
    )


# ================================================================ 路径


def test_the_frontend_calls_the_auth_paths_the_backend_registers():
    """前端调的三条 `/auth/*` 后端都要真的注册过。

    路径写错的表现是 404,而 404 在这个前端里会被 `describeError` 归成一句
    通用故障话术 —— 运营看到"处理失败,请重试",看不到"这个地址不存在"。
    """
    auth_ts = _fe("api/auth.ts")
    backend = _backend("api/auth.py")
    for path in ("/auth/whoami", "/auth/login", "/auth/logout"):
        assert f"'{path}'" in auth_ts, f"前端没有调用 {path}"
        assert f'@router.post("{path}")' in backend or f'@router.get("{path}")' in backend, (
            f"后端没有注册 {path} —— 前端会拿到 404,而它显示成一句通用故障"
        )


def test_login_and_logout_are_anonymous_on_both_sides():
    """登录本身不能需要先登录,而退出也不能被 401 挡住。

    退出那一条尤其反直觉:一个已经过期的 Cookie 去调 logout,如果被守卫挡回
    401,前端就会陷入"登不出去,也进不去"—— 而用户此刻想做的事(把本地登录态
    清掉)本来就不需要任何权限。

    `test_gate_a_guards.py` 钉的是两份清单**相等**;这里钉的是这两条**在里面**。
    前者在有人整体重构清单时会红,后者在有人单独摘掉一条时会红。
    """
    main_py = _backend("main.py")
    client_ts = _fe("api/client.ts")
    for path in ("/auth/login", "/auth/logout"):
        assert f'"{path}"' in main_py, f"后端 PUBLIC_PREFIXES 里没有 {path}"
        assert f"'{path}'" in client_ts, f"前端 ANONYMOUS_PATHS 里没有 {path}"


# ================================================================ 最后一跳


def test_the_browser_actually_sends_the_session_cookie():
    """`withCredentials` 必须开着。**这是整条登录链路的最后一跳。**

    后端在 phase1 就会发 HttpOnly Cookie 了,而 axios 默认不带凭据。
    同源部署(nginx 反代 `/api/`、dev 走 vite proxy)下浏览器仍然会带上 Cookie,
    所以**开发环境完全看不出少了这一行**;`VITE_API_BASE_URL` 指向另一个源的
    部署则是每个请求 401,而登录接口本身返回 200 —— "登录成功,然后立刻又要登录"。

    Vitest 那边有一条读 `apiClient.defaults.withCredentials` 的用例,它比这一条
    强(读的是真实对象)。这里再钉一次的理由只有一个:**offline 门禁跑得到,
    而 Vitest 要 node_modules** —— 而这一行恰恰是最容易在合并时被顺手丢掉的那种。
    """
    client_ts = _fe("api/client.ts")
    assert "withCredentials: true" in client_ts, (
        "apiClient 没开 withCredentials —— 跨源部署下 Session Cookie 一次都不会发出去"
    )


def test_the_identity_probe_is_not_gated_on_a_local_token_anymore():
    """会话模式下 whoami 必须发得出去。

    上一版 `enabled` 是 `!health.isError && hasToken`。浏览器登录之后那个条件
    会把链路掐死在起点:Cookie 有了,而 localStorage 里没有任何口令,于是
    `hasToken` 为假、探测一次都不发、顶栏永远"未登录" —— 而**没有任何请求失败**,
    所以横幅也不会说话。这是 §3.43 那一族里最难看见的一种:没有红灯。
    """
    hook = _fe("hooks/useIdentity.ts")
    # a46-phase6:localStorage 口令链删除之后,这个条件收敛成**只依赖后端活着**。
    # 不许再挂任何别的东西 —— `enabled: false` 时 react-query 的 `isLoading`
    # 是 false(v5:`isLoading === isPending && isFetching`),于是"还在检查身份"
    # 会被读成"没登录",表现是已登录的人刷新时先闪一下登录页。
    assert "enabled: !health.isError," in hook, (
        "身份探测的 enabled 又挂上别的条件了 —— 会话模式下它可能一次都不会发"
    )
    for retired in ("hasToken", "readAdminToken", "readOperatorToken"):
        assert retired not in hook, (
            f"`{retired}` 回到 useIdentity 里了 —— 浏览器凭据是 HttpOnly Cookie,"
            "它读不到也不该读;拿 localStorage 当判据会让登录成功的人探测不发出去"
        )
    assert "health.data?.auth_mode === 'session'" in hook, (
        "sessionAuth 不是从后端 /health 读的 —— 前端不许自己推测这件事"
    )


# ================================================================ 有没有人读


def test_the_session_expiry_signal_has_a_consumer():
    """算出来的信号必须有人订阅。**这一条是自审补的,不是一开始就有的。**

    a46-phase2 第一版把 `setSessionExpired` 接进了响应拦截器,导出了
    `isSessionExpired` / `onSessionExpiredChange` / `SESSION_EXPIRED_EVENT`
    三样东西 —— 而 `src/` 里**一处都没有订阅**。于是"用着用着会话过期"
    这条路径整个不生效:身份探测挂着 60 秒 `staleTime`,前端手里那份
    "我是 operator" 能继续有效一分多钟,运营点什么都失败而界面不把他送去
    登录页,页面上也没有登录入口。

    这正是 `test_a45_batch14_27_...` 那个文件整篇在讲的形状:上游算对了、
    接线也在,**最后一跳没有人接**,而两侧的测试全绿 —— 组件测试把
    `useIdentity` 整个 mock 掉了,它验的是"拿到 needsLogin 之后怎么办",
    洞在"needsLogin 永远不会变成真"。

    所以钉的是**消费端**,不是生产端:生产端(拦截器里那次 `setSessionExpired`)
    看起来一直很正常,正常到没有人会去问它算给谁看。
    """
    client_ts = _fe("api/client.ts")
    assert "onSessionExpiredChange" in client_ts, "会话失效信号的订阅入口没有了"

    hook = _fe("hooks/useIdentity.ts")
    assert "onSessionExpiredChange" in hook, (
        "没有人订阅会话失效信号 —— 会话在用着的过程中过期时,界面不会把人送去登录页"
    )
    # `invalidateQueries` 在重新请求失败时**保留旧数据**(React Query 的既定行为),
    # 于是 `probe.data` 还在、`needsLogin` 还是假 —— 等于什么都没做。
    # 这种"做了但没生效"比没做更难查,所以判据钉到具体那个方法上
    assert "resetQueries" in hook, (
        "作废身份缓存用的不是 resetQueries —— invalidate 在失败时会留着旧数据,"
        "needsLogin 永远变不成真"
    )


def test_no_dead_exports_left_behind_by_the_session_signal():
    """信号那一族不许留没人用的导出。

    第一版留了 `SESSION_EXPIRED_EVENT`(一个从没被 dispatch 过的事件名)和
    `isSessionExpired`(没有任何读者)。它们不会让任何东西变红,只会让下一个人
    以为这里有一套事件机制在跑,然后照着它写第二份订阅。

    判据是"要么有人用,要么别导出"。这条守卫只管这两个已经踩过的名字 ——
    一条泛化的"全仓没有未使用导出"要靠 tsc 或 knip,那是另一件事,
    不该由一条读源码的守卫假装自己做到了。
    """
    client_ts = _fe("api/client.ts")
    for dead in ("SESSION_EXPIRED_EVENT", "isSessionExpired"):
        assert dead not in client_ts, (
            f"{dead} 又回来了 —— 它没有任何读者,留着只会让人以为这里有一套事件机制"
        )


# ================================================================ 可部署性


def _required_browser_auth_keys() -> set[str]:
    """`_check_browser_auth` 在非 local 环境下要求**非空**的那几个键。

    从**检查函数本身**解析,不另抄一份清单。抄一份的话,加第四项时这里不会红,
    而它恰恰是这条守卫存在的全部理由。

    判据只取「为空」那一支。同一个函数里还有别的问题码 —— 比如
    `AUTH_SESSION_MAX_AGE_SECONDS 必须为正`、`... 仍然是占位值` —— 那些键
    **有合理的默认值**,要求部署时显式给出反而是错的(`:?` 会让一个本来
    能跑的部署起不来)。「不给就起不来」和「给错了会起不来」是两件事,
    这条守卫只管前者。
    """
    config = (BACKEND_ROOT / "app" / "core" / "config.py").read_text(encoding="utf-8")
    body = window(
        config,
        "    def _check_browser_auth(self)",
        'f"APP_ENV={self.APP_ENV} 不属于',
        what="浏览器登录的启动检查",
    )
    keys = set(re.findall(r'problems\.append\("([A-Z][A-Z0-9_]+) 为空"\)', body))
    assert keys, (
        "解析不出 _check_browser_auth 里「XXX 为空」那几条 —— "
        "那个函数的形状变了,这条守卫要跟着改"
    )
    return keys


def test_every_key_that_blocks_startup_is_demanded_by_the_production_compose():
    """凡是"非 local 配不全就起不来"的键,生产编排必须用 `:?` 显式要求。

    ## 这条守的是一次部署事故的形状,不是代码风格

    检查在 `config._check_browser_auth` 里**抛错**,也就是说配不全的后果是
    容器起来又退出、退出又重启。而 `docker compose up -d` 会打印成功,
    `docker compose ps` 只显示 Restarting,那句「ADMIN_PASSWORD 为空」淹在
    几十行启动日志里 —— 部署的人拿到的信息是"起不来",不是"少配了什么"。

    `${KEY:?消息}` 把这件事提前到**创建容器之前**:变量没设,compose 当场
    退出并把消息打在终端上。这正是同一份文件里 POSTGRES_PASSWORD 那几行的
    理由,文件头第 1 条写着「启动失败是个好错误 —— 它发生在部署那一刻、
    由部署的人看到」。

    ## 为什么三个服务都要

    worker 与 beat 同样构造 Settings,同样会被那条检查拦住。只给 backend 加的
    表现是:API 起来了,而任务队列在悄悄重启 —— 页面能打开,任务永远不动。
    """
    compose = (PROJECT_ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    demanded = set(re.findall(r"\$\{([A-Z][A-Z0-9_]+):\?", compose))

    missing = sorted(_required_browser_auth_keys() - demanded)
    assert not missing, (
        f"这些键配不全会让后端起不来,而 docker-compose.prod.yml 没有用 `:?` 要求它们:"
        f"{missing} —— 部署时的表现是容器反复重启,而 compose 打印成功"
    )

    # 三个读 Settings 的服务都要引到那组变量上。只给 backend 加的话,
    # 上面那个集合断言照样通过 —— 它只看"文件里有没有",不看"接到了谁身上"
    for service in ("backend:", "worker:", "beat:"):
        after = compose[compose.index(service) :]
        nxt = re.search(r"\n  [a-z][a-z-]*:", after)
        block = after[: nxt.start()] if nxt else after
        assert "*browser-auth" in block, (
            f"docker-compose.prod.yml 的 {service.rstrip(':')} 没有引用浏览器登录那组变量 —— "
            f"它也构造 Settings,同样会被启动检查拦住"
        )


#: 部署的人会打开的三份。缺一份,这条守卫就漏掉一条真实的入口路径。
_DEPLOYER_DOCS = (
    "README.md",
    "docs/DEPLOYMENT.md",
    "docs/MANUAL-ACCEPTANCE.md",
)


def test_the_startup_blocking_keys_are_documented_somewhere_a_deployer_reads():
    """会挡住启动的键,必须在**部署的人真的会打开的每一份**文档里写清楚。

    上一版这三个键在**全仓 0 份文档**里出现过 —— 也就是说一个必填到"不填就起不来"
    的配置项,唯一的说明在 `.env.example` 的注释和一个会抛错的 Python 函数里。
    部署的人第一次遇到它的方式是生产环境起不来。

    ## 「单一归属」在这里是错的,a46-phase5 翻掉了它

    phase3 补这条时只钉了 README 一份,理由写的是"单一归属"。而那之后:

        docs/DEPLOYMENT.md        全文「登录」0 次,§三「必须改掉的默认值」
                                  没有这三项,§二首次部署也没提生产 overlay
        docs/MANUAL-ACCEPTANCE.md §3.1 的 UAT 基线 env 缺这三项,而 §5.1 让人跑
                                  生产 overlay —— `${KEY:?}` 会让容器根本不被创建

    也就是说:守卫绿着,而 README 自己写的"部署与运维见 docs/DEPLOYMENT.md"把
    读者**送去了一份没有这件事的文档**。单一归属的前提是"读者会去那一份",
    而这里读者恰恰不会 —— 他按文档地图走。所以判据改成"每一条入口路径上都要有"。

    `.env.example` 里那几行注释仍然不算:那是给已经知道有这回事的人看的,
    而这条守的是"根本不知道有这回事"。
    """
    required = _required_browser_auth_keys()
    problems: list[str] = []
    for name in _DEPLOYER_DOCS:
        doc = PROJECT_ROOT / name
        assert doc.exists(), f"{name} 不见了 —— 这条守卫少了一条入口路径"
        text = doc.read_text(encoding="utf-8")
        missing = sorted(k for k in required if k not in text)
        if missing:
            problems.append(f"{name} 一个字都没提:{missing}")
    assert not problems, (
        "这些键不填就起不来,而部署的人会打开的文档里没有说明:\n  "
        + "\n  ".join(problems)
        + "\n他第一次遇到它的方式会是生产环境起不来。"
    )


def _without_comments(source: str) -> str:
    """剥掉 `//` 行注释与 `/* */` 块注释,只留代码。

    下面那条反向断言要的是「**活的**指路牌一个不剩」。phase6 在 client.ts 里
    留了一段历史注释,原样引用了口令时代的那句话 —— 那是台账,不是路牌,
    和 phase5 的时态判定是同一件事:引用历史不算错,现在时地指错路才算。
    """
    import re as _re

    stripped = _re.sub(r"/\*.*?\*/", "", source, flags=_re.S)
    return _re.sub(r"(?m)//.*$", "", stripped)


def test_the_session_401_copy_sends_people_to_login_not_the_settings_page():
    """401 的会话文案必须说「请重新登录」,而口令时代的指路牌一个都不许活着。

    ## 为什么钉在纯层而不是 Vitest

    `client.test.ts` 那条 401 用例跑在 node 单测环境:`/health` 一次都不会
    返回,模块级 `authMode` 停在默认的 `token`,它天然只测得到免登录那一支。
    phase6 自审时抓到的第一处缺陷就是这个形状 —— 那条用例被改成
    `toContain('重新登录')`,在单测环境里**必红**,而写它的人(我)没法跑
    Vitest,红灯要等下一台有网络的机器才炸。翻转 `authMode` 只能伸进 axios
    拦截器内部,那种写法比不写更糟。所以会话那一支的文案钉在这里:
    源码级、离线可跑、变异可验。

    ## 反向那半是主角

    「到「系统设置」页核对口令」在口令时代是对的指路。设置页上的录入卡
    已经删了 —— 这句话今天指向一个**不存在的输入框**,而被 401 踢下线的人
    恰恰最需要一句对的话。判定剥掉注释:历史引用放行,活代码零容忍。
    """
    src = _fe("api/client.ts")
    assert "请重新登录" in src, (
        "会话模式的 401 文案不再让人重新登录了 —— 被踢下线的人会拿到一句"
        "指不了路的话"
    )
    live = _without_comments(src)
    for stale in ("核对口令", "系统设置"):
        assert stale not in live, (
            f"「{stale}」以活代码的身份回到 client.ts 了 —— 那是口令时代的"
            "指路牌,现在指向一个已删除的输入框"
        )
