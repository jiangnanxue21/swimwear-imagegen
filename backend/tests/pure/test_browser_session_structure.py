"""浏览器 Session 登录的**结构**门禁。

这个文件里没有一条断言在测业务行为 —— 行为在 `tests/test_auth_session.py`。
这里守的全是「改错了不会报错、只会在某条路径上安静失效」的那一类东西:

    中间件 add 顺序        写反了不抛异常,只是某一层的响应绕过了另一层
    scope.get vs .session  在装了中间件的 uvicorn 里两者表现完全一致,
                           只在没装中间件的纯测试里才炸
    两份 LOCAL_ENVS        漂移之后"要不要密码"和"要不要口令"给出两个答案
    itsdangerous 依赖      开发机上别的包顺带装过它,缺声明看不出来

这四条的共同点是:**本地跑一遍完全正常。** 所以它们必须由静态断言守住,
而不是靠"跑一次就知道了"。
"""
from __future__ import annotations

import ast

from tests.pure._helpers import BACKEND_ROOT

APP = BACKEND_ROOT / "app"
MAIN_PY = APP / "main.py"
DEPS_PY = APP / "api" / "deps.py"
CONFIG_PY = APP / "core" / "config.py"
AUTH_PY = APP / "api" / "auth.py"
PYPROJECT = BACKEND_ROOT / "pyproject.toml"


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def _fn(tree, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"找不到函数 {name}")


# ---------------------------------------------------------------- 中间件顺序


def _middleware_registration_order() -> list[str]:
    """`create_app()` 里四次中间件注册的**源码顺序**。

    两种写法都要认:`@app.middleware("http")` 装饰器底下也是一次
    `add_middleware`,只是长得不像。只认其中一种的话,把某一个改成另一种
    写法就能绕过这条门禁 —— 而那正是重构时最容易顺手做的事。
    """
    fn = _fn(_tree(MAIN_PY), "create_app")
    found: list[tuple[int, str]] = []
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_middleware"
            and node.args
        ):
            found.append((node.lineno, ast.unparse(node.args[0])))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and getattr(dec.func, "attr", "") == "middleware":
                    found.append((node.lineno, node.name))
    return [name for _, name in sorted(found)]


def test_middleware_is_registered_in_the_only_order_that_works():
    """add 顺序必须是 guard -> request_context -> Session -> CORS。

    ## 为什么这条值得一个门禁

    Starlette 的 `add_middleware` 是 `user_middleware.insert(0, ...)`,
    **后 add 的在外层**。所以源码里从上到下读是"从内到外"读,而人读代码
    的直觉恰好相反 —— 这个反直觉本身就是缺陷的来源。

    实际执行链因此是::

        CORS -> Session -> request_context -> guard_api_requests -> routes

    四个位置各自有一条"写错了会怎样",而没有一条会报错:

        Session 放到 CORS 外  -> 登录失败的 401 不经过 CORS,跨域时浏览器
                                 直接拦掉,前端拿到的是"网络错误"而不是 401
        Session 放到 guard 内 -> guard 调 resolve_identity 时 scope 里还没有
                                 session,**已登录的浏览器被兜底守卫当成匿名**
        guard 放到 context 外  -> 守卫自己组装的 401 没有 x-request-id,
                                 前端 describeError 读到空串(本文件已有注释)
        CORS 不在最外           -> 同上,任何一层提前返回的响应都没有 CORS 头

    四条里没有一条会在本地单机 HTTP 上表现出来。
    """
    order = _middleware_registration_order()
    expected = [
        "guard_api_requests",
        "request_context",
        "SessionMiddleware",
        "CORSMiddleware",
    ]
    assert order == expected, (
        f"中间件注册顺序变了。\n"
        f"  期望(源码自上而下,即执行链由内向外): {expected}\n"
        f"  实际:                                {order}\n"
        "后 add 的在外层。改这个顺序之前先读 main.py 里那三段注释。"
    )


def test_session_is_registered_outside_the_guard_but_inside_cors():
    """把上一条的两条**硬约束**单独钉一遍,失败信息各自指向真正的后果。

    上一条断的是完整顺序,读起来是"和期望的列表不一样"。中间那两个
    (request_context / Session)的相对位置其实是可调的,而这两条不是 ——
    所以它们值得各自一句话,而不是淹在一个列表 diff 里。
    """
    order = _middleware_registration_order()
    assert "SessionMiddleware" in order, "SessionMiddleware 没有注册,浏览器登录不可能工作"
    session_at = order.index("SessionMiddleware")

    assert session_at > order.index("guard_api_requests"), (
        "SessionMiddleware 必须比 guard_api_requests 后 add(也就是在它外层)。"
        "反过来的话,guard 调 resolve_identity 时 scope 里还没有 session —— "
        "已登录的浏览器会被兜底守卫当成匿名挡在门外"
    )
    assert session_at < order.index("CORSMiddleware"), (
        "CORSMiddleware 必须留在最外层。Session 顶到它外面之后,登录失败的 "
        "401 就拿不到 CORS 头,跨域部署下前端只能看到「网络错误」"
    )


# ---------------------------------------------------------------- scope.get


def test_resolve_identity_reads_the_session_out_of_scope_not_off_the_request():
    """读 session 必须走 `request.scope.get("session")`。

    `request.session` 是个 property,没装 SessionMiddleware 时**抛
    AssertionError**。而本仓有大量纯测试直接构造 `Request(scope)` 来打
    `resolve_identity`(deps.py 文件头那段注释就在讲这件事),那些 scope 里
    没有 session。

    差别只在"没装中间件"这一种场景里显现 —— 也就是说,在真实服务里两种写法
    表现完全一致,**改错了要等到某条测试路径上才炸**,而那时报错信息指向的
    是测试,不是这行代码。
    """
    src = DEPS_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)

    # 按 AST 找**属性访问**,不按字符串找。
    #
    # 字符串匹配在这里会自伤:这条测试和被测函数的 docstring 里都要提到
    # `request.session` 才能把话说清楚,而 `ast.unparse` 会把 docstring 一起
    # 吐出来。一条会规律性误报的门禁,最后一定会被人加豁免、再被整个注释掉。
    for name in ("_session_identity", "resolve_identity"):
        fn = _fn(tree, name)
        for node in ast.walk(fn):
            bad = (
                isinstance(node, ast.Attribute)
                and node.attr == "session"
                and isinstance(node.value, ast.Name)
                and node.value.id == "request"
            )
            assert not bad, (
                f"{name} 第 {node.lineno} 行用了 `request.session`。它是个 property,"
                "没装 SessionMiddleware 时抛 AssertionError —— 而本仓大量纯测试"
                "正是直接构造 Request(scope) 来打这个函数的。改用 "
                'request.scope.get("session"):拿不到就当作没有 Session。'
            )

    assert 'scope.get("session")' in src, 'deps.py 里找不到 scope.get("session")'


def test_the_session_branch_comes_before_the_header_token_branch():
    """Session 必须**优先于** Header Token,不是"也支持"。

    反过来写会开一个提权口子:operator 已经登录的浏览器,只要拿到一次
    X-Admin-Token(从别人屏幕上、从某份文档里、从一次 XSS 里)塞进请求头,
    就变成了 admin。Session 优先意味着**已登录的浏览器带什么头都仍然是
    它登录的那个人**,想换身份只能重新登录。

    用行号比,而不是看有没有出现 —— 两个分支都在的时候,顺序才是全部。
    """
    fn = _fn(_tree(DEPS_PY), "resolve_identity")
    session_line = header_line = None
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_session_identity":
            session_line = node.lineno
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_configured":
            header_line = node.lineno
    assert session_line is not None, "resolve_identity 里没有 Session 分支"
    assert header_line is not None, "resolve_identity 里找不到 Legacy Token 分支"
    assert session_line < header_line, (
        "Legacy Header Token 被排在了 Session 前面。已登录的 operator 浏览器"
        "只要塞一个 X-Admin-Token 就能变成 admin —— 这是提权,不是兼容"
    )


def test_the_dev_fallback_cannot_bypass_a_configured_browser_login():
    """local 的 ROLE_DEV 回落必须让位给已配置的浏览器登录。

    少了这个条件,本地怎么点都是通的 —— 而人工验收要验的恰好是
    admin/operator 的差异、退出登录、401 与 403,一条都验不到。

    2026-08-11:那四个条件从 `resolve_identity` 的行内 if 提成了
    `dev_fallback_active()`,因为 `/health` 要报同一件事(第三档 `auth_mode`)。
    **判据必须跟着搬,不能两边各写一遍** —— 抄一份过去的表现是界面说
    "免登录"而后端在 401。所以这里同时钉两件事:那个函数里有这个条件,
    而且 `resolve_identity` 真的调它(不是自己又写了一遍)。
    """
    tree = _tree(DEPS_PY)
    predicate = ast.unparse(_fn(tree, "dev_fallback_active"))
    assert "browser_auth_configured" in predicate, (
        "ROLE_DEV 回落没有排除「浏览器登录已配置」的情况:填了密码的本机"
        "仍然会被免口令放行,于是登录、logout、403 在本地全都测不到"
    )

    body = ast.unparse(_fn(tree, "resolve_identity"))
    assert "dev_fallback_active()" in body, (
        "resolve_identity 不再走 `dev_fallback_active()` 了 —— 那个函数同时是"
        "`/health` 报 auth_mode 的判据,两边分叉的表现是界面说免登录而后端在 401"
    )
    assert "browser_auth_configured" not in body, (
        "回落条件在 resolve_identity 里又被写了一遍 —— 一份判据两处实现,"
        "改一处的人不会知道另一处存在"
    )


# ---------------------------------------------------------------- 两份常量


def _local_envs_literal(path) -> set[str]:
    node = next(
        n.value
        for n in ast.walk(_tree(path))
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", "") == "LOCAL_ENVS" for t in n.targets)
    )
    return {c.value for c in ast.walk(node) if isinstance(c, ast.Constant)}


def test_local_envs_agree_between_config_and_deps():
    """`config.LOCAL_ENVS` 与 `deps.LOCAL_ENVS` 必须是同一个集合。

    ## 为什么允许有两份字面量

    单份做不到,而且不是因为懒:

      1. `deps.py` 模块级 `from app.core.config import settings`。core 反过来
         import api 是循环导入,`.importlinter` 的「core 是最底层」契约也禁止
         它 —— 包括函数体内 import(import-linter 建的是完整依赖图,藏不住);
      2. `test_security_audit.py::test_local_bypass_does_not_cover_a_test_environment`
         用 AST 直接读 `deps.py` 里那条**赋值语句**的字符串常量,把
         "APP_ENV=test 不得免口令"钉在那个文件上。改成 import 会让它失锚 ——
         而失锚的门禁不报错,只是不再验任何东西。

    所以处置和前后端匿名白名单是同一个套路:**允许两份,但钉成相等。**
    漂移的后果很具体:`_check_browser_auth` 判"这个环境要不要密码",
    `resolve_identity` 判"这个环境要不要口令",两份一旦分叉,就会出现
    一个既不要密码、也不要口令,但自己觉得两样都要了的环境。
    """
    config_envs = _local_envs_literal(CONFIG_PY)
    deps_envs = _local_envs_literal(DEPS_PY)
    assert config_envs == deps_envs, (
        f"两份 LOCAL_ENVS 漂了:\n"
        f"  config.py: {sorted(config_envs)}\n"
        f"  deps.py:   {sorted(deps_envs)}\n"
        "它们必须逐字相同 —— 一份判要不要密码,一份判要不要口令"
    )
    assert "test" not in config_envs, "APP_ENV=test 免了浏览器登录密码"
    assert "local" in config_envs, "本机开发被误伤了"


# ---------------------------------------------------------------- 依赖与配置


def test_itsdangerous_is_declared_with_a_pinned_upper_bound():
    """`starlette.middleware.sessions` 在导入期就 import 它。

    缺声明不是运行期降级,是 `app.main` 直接 ImportError、应用起不来。
    它长期能在开发机上 import 成功,只因为别的包顺带带过它 —— 也就是说
    "本地能跑"完全不能说明这条依赖已经声明了。

    上界照 redis / ruff / import-linter 的既有惯例钉住:一个"今天绿明天红、
    而中间没有人改过任何代码"的门禁,最后一定会被关掉。
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    line = next(
        (ln for ln in text.splitlines() if "itsdangerous" in ln and not ln.strip().startswith("#")),
        None,
    )
    assert line is not None, (
        "pyproject.toml 里没有 itsdangerous。starlette 自己不声明它,"
        "缺了它 app.main 在 import 期就 ImportError"
    )
    assert "<" in line, f"itsdangerous 没有钉上界:{line.strip()}"


def test_the_browser_login_secrets_stay_out_of_the_settings_page():
    """五个新配置**不得**进 `settings_schema`。

    进了那里意味着值可以被设置页写入、加密落库、并受 SETTINGS_ENV_LOCK
    影响。于是出现两件事:一是"用管理员会话去改管理员密码"的自指闭环,
    二是密码的真相来源变成两个(env 与 DB),而登录校验只读其中一个 ——
    表现是"在设置页把密码改了,但登录还认旧的"。
    """
    from app.core.settings_schema import FIELDS

    forbidden = {
        "ADMIN_PASSWORD",
        "OPERATOR_PASSWORD",
        "AUTH_SESSION_SECRET",
        "AUTH_SESSION_MAX_AGE_SECONDS",
        "AUTH_SESSION_COOKIE_NAME",
    }
    leaked = sorted(forbidden & set(FIELDS))
    assert not leaked, (
        f"这些配置进了设置页:{leaked}。改密码和改 Provider Key 不该共用同一条通路"
    )


def test_login_never_puts_the_password_anywhere_it_could_be_logged():
    """登录接口里不许出现"把 password 交出去"的形状。

    `core/redaction.py` 的正则已经覆盖 `password` 这个字段名,但那是**兜底**,
    不是这里可以随手记的理由 —— 兜底只在字段名恰好叫那个的时候有效,
    而 `f"...{payload.password}"` 拼进一句话之后,它就只是一段普通文本了。
    """
    fn = ast.unparse(_fn(_tree(AUTH_PY), "login"))
    for bad in ("payload.password}", "password=payload.password", '"password":'):
        assert bad not in fn, f"login 里出现了 {bad!r} —— 密码可能被记进日志或响应体"


def test_login_clears_the_session_before_writing_the_new_identity():
    """防 session fixation:`session.clear()` 必须在写入**之前**。

    本轮 session 里只有 name/role 两个字段,清空看起来多余 —— 两个字段
    反正都会被覆盖。但"以后有人往 session 里加了第三个字段"是必然会发生的
    事,而那时没有人会回头补这一行。顺序错了不会有任何表现,直到某个
    攻击者事先塞进去的字段跨越了一次登录活下来。
    """
    fn = _fn(_tree(AUTH_PY), "login")
    clear_line = write_line = None
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "clear"
            and "session" in ast.unparse(node.func.value)
        ):
            clear_line = node.lineno
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "session"
            and write_line is None
        ):
            write_line = node.lineno
    assert clear_line is not None, "login 里没有 session.clear() —— session fixation 没防"
    assert write_line is not None, "login 里没有往 session 写身份"
    assert clear_line < write_line, (
        f"session.clear()(第 {clear_line} 行)排在了写入(第 {write_line} 行)之后,"
        "它会把刚写进去的身份一起清掉"
    )


def test_login_and_logout_are_anonymous_but_whoami_is_not():
    """登录不能要求先登录;whoami 必须要求。

    这两件事写在同一条测试里,因为它们是**同一个决定的两面**:白名单加的
    是精确路径段而不是 `/auth` 前缀。分开写的话,某天有人图省事把两条
    换成一条 `/auth`,只会有一条变红,而它的失败信息不会提到 whoami。
    """
    src = MAIN_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    node = next(
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "PUBLIC_PREFIXES"
    )
    public = {c.value for c in ast.walk(node) if isinstance(c, ast.Constant)}

    assert "/auth/login" in public, "登录接口不是匿名的 —— 登录需要先登录"
    assert "/auth/logout" in public, (
        "退出接口不是匿名的。Cookie 已过期的人调它会被 401 挡住,"
        "于是他登不出去也进不去"
    )
    assert "/auth/whoami" not in public and "/auth" not in public, (
        "whoami 被放匿名了,身份探测会永远成功"
    )
