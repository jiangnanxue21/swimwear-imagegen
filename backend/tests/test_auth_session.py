"""浏览器登录(用户名+密码 -> HttpOnly Session Cookie)的行为测试。

## 为什么这个文件不要数据库

登录这条链路一个字段都不落库 —— 没有 users 表,密码来自 `.env`,身份放在
签名 Cookie 里。让它依赖 `conftest.client`(那条链一路连到 alembic 和真实
Postgres)会把"认证还能不能工作"这件事挂在"测试库起没起来"上面。

所以这里自己起一个不接数据库的 TestClient。代价是 `GET /settings` 那种
既要 admin 又要 db 的路由不能直接打 —— 对应的角色语义改成直接打
`require_admin` / `require_operator`,那正是路由内部用的同一个函数。

## 覆盖的是 PRD §42 的 1~23

结构那两条(中间件 add 顺序、scope.get)在
`tests/pure/test_browser_session_structure.py`,那边零依赖、跑得更早。
"""
from __future__ import annotations

import logging

import pytest
from starlette.requests import Request

from app.api import deps
from app.core.errors import AppError, ErrorCode
from tests.conftest import BROWSER_ADMIN_PASSWORD, BROWSER_OPERATOR_PASSWORD

LOGIN = "/api/auth/login"
LOGOUT = "/api/auth/logout"
WHOAMI = "/api/auth/whoami"


@pytest.fixture
def api(browser_auth):
    """不接数据库的 TestClient。

    `browser_auth` 把两个密码配上、APP_ENV 设成 production、并把 Legacy Token
    留空 —— 也就是本轮之后最常见的那种部署形态。
    """
    from fastapi.testclient import TestClient

    from app.main import app

    app.dependency_overrides[deps.db_session] = lambda: None
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _request(session: dict | None = None, headers: dict[str, str] | None = None) -> Request:
    """直接构造一个 Request,用来打 deps 里的守卫函数。

    `session=None` 表示 scope 里**没有** session 这一项(等价于没装
    SessionMiddleware),不是"有但是空的"。两者要分得开。
    """
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/anything",
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
    }
    if session is not None:
        scope["session"] = session
    return Request(scope)


# ---------------------------------------------------------------- 登录


def test_admin_logs_in_with_the_admin_password(api):
    body = api.post(LOGIN, json={"username": "admin", "password": BROWSER_ADMIN_PASSWORD})
    assert body.status_code == 200
    assert body.json() == {"name": "admin", "role": "admin", "is_admin": True}


def test_operator_logs_in_with_the_operator_password(api):
    body = api.post(
        LOGIN, json={"username": "operator", "password": BROWSER_OPERATOR_PASSWORD}
    )
    assert body.status_code == 200
    assert body.json() == {"name": "operator", "role": "operator", "is_admin": False}


def test_the_login_response_has_the_same_shape_as_whoami(api):
    """两处形状必须一样,前端才敢用登录结果直接填 auth-probe 的缓存。

    分叉了不会报错 —— 只会让菜单在登录后的第一秒按一个错误的 is_admin 渲染,
    然后在下一次探测回来时无声地跳一下。
    """
    login = api.post(LOGIN, json={"username": "admin", "password": BROWSER_ADMIN_PASSWORD})
    who = api.get(WHOAMI)
    assert who.status_code == 200
    assert login.json() == who.json()


def test_a_wrong_password_is_rejected_with_auth_failed(api):
    resp = api.post(LOGIN, json={"username": "admin", "password": "not-the-password"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == ErrorCode.AUTH_FAILED


def test_an_unknown_username_is_indistinguishable_from_a_wrong_password(api):
    """两者的响应必须**逐字节相同**。

    只有两个账号,所以"确认 admin 存在"等于把爆破面砍掉一半。这条断言比
    "都是 401"强:状态码相同但消息不同(「用户不存在」/「密码错误」)
    一样会泄露,而那种差异最容易在改文案时被顺手引入。
    """
    unknown = api.post(LOGIN, json={"username": "root", "password": "whatever"})
    wrong = api.post(LOGIN, json={"username": "admin", "password": "whatever"})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_an_account_without_a_configured_password_cannot_be_logged_into(api, monkeypatch):
    """密码留空 != 人人都对。

    `_password_matches` 对空的期望值一律返回 False。少了这一条,某个环境
    只配了 ADMIN_PASSWORD 忘了配 OPERATOR_PASSWORD 时,operator 会变成
    一个**空密码就能进**的账号。
    """
    monkeypatch.setattr(deps.settings, "OPERATOR_PASSWORD", "", raising=False)
    for password in ("", " ", "anything"):
        resp = api.post(LOGIN, json={"username": "operator", "password": password or "x"})
        assert resp.status_code == 401, f"空密码账号被 {password!r} 登进去了"


def test_the_password_never_reaches_the_response_or_the_log(api, caplog):
    """密码不进响应体,也不进日志。

    `core/redaction.py` 覆盖了 `password` 这个字段名,但那是兜底 ——
    一旦被拼进一句自由文本,它就只是普通文本了。
    """
    secret = "a-very-recognisable-password-value"
    with caplog.at_level(logging.DEBUG):
        resp = api.post(LOGIN, json={"username": "admin", "password": secret})
    assert resp.status_code == 401
    assert secret not in resp.text
    assert secret not in caplog.text


def test_logging_in_again_replaces_the_previous_session(api):
    """登录成功先 `session.clear()`,旧身份不许残留(防 session fixation)。"""
    api.post(LOGIN, json={"username": "admin", "password": BROWSER_ADMIN_PASSWORD})
    assert api.get(WHOAMI).json()["role"] == "admin"

    api.post(LOGIN, json={"username": "operator", "password": BROWSER_OPERATOR_PASSWORD})
    who = api.get(WHOAMI).json()
    assert who == {"name": "operator", "role": "operator", "is_admin": False}


# ---------------------------------------------------------------- Cookie


def test_the_session_cookie_is_httponly_lax_and_not_secure_on_plain_http(api):
    """Cookie 属性。

    `HttpOnly` 与 `Path=/` 由 Starlette 固定写入;`SameSite=Lax` 是本轮
    唯一的 CSRF 防线(不做 Token 体系);`Secure` 在非生产、非 HTTPS 时
    **必须不出现** —— 出现了浏览器就不会在 http 上回传它,表现是
    "登录成功但一刷新就掉线",而没有任何一处报错。
    """
    resp = api.post(LOGIN, json={"username": "admin", "password": BROWSER_ADMIN_PASSWORD})
    raw = resp.headers["set-cookie"].lower()
    assert "swimwear_session=" in raw
    assert "httponly" in raw
    assert "samesite=lax" in raw
    assert "path=/" in raw
    assert "secure" not in raw


def test_the_cookie_survives_a_new_request_so_a_refresh_keeps_you_logged_in(api):
    api.post(LOGIN, json={"username": "admin", "password": BROWSER_ADMIN_PASSWORD})
    for _ in range(3):
        assert api.get(WHOAMI).status_code == 200


def test_rotating_the_signing_secret_invalidates_existing_cookies(monkeypatch):
    """换了 AUTH_SESSION_SECRET,已经发出去的 Cookie 必须失效。

    这是无状态 Session 唯一的"全端吊销"手段。它坏掉的话不会有任何表现 ——
    直到有人换了密钥、以为所有人都被踢了,而实际上没有。
    """
    from fastapi.testclient import TestClient

    from app.core.config import settings
    from app.main import create_app

    monkeypatch.setattr(settings, "APP_ENV", "production", raising=False)
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", BROWSER_ADMIN_PASSWORD, raising=False)
    monkeypatch.setattr(settings, "ADMIN_TOKEN", "", raising=False)
    monkeypatch.setattr(settings, "OPERATOR_TOKENS", "", raising=False)

    monkeypatch.setattr(settings, "AUTH_SESSION_SECRET", "first-secret-" + "x" * 40, raising=False)
    app_a = create_app()
    app_a.dependency_overrides[deps.db_session] = lambda: None
    with TestClient(app_a) as client_a:
        assert client_a.post(
            LOGIN, json={"username": "admin", "password": BROWSER_ADMIN_PASSWORD}
        ).status_code == 200
        cookies = dict(client_a.cookies)
    assert cookies, "登录之后没有拿到 Cookie"

    monkeypatch.setattr(settings, "AUTH_SESSION_SECRET", "second-secret-" + "y" * 40, raising=False)
    app_b = create_app()
    app_b.dependency_overrides[deps.db_session] = lambda: None
    with TestClient(app_b) as client_b:
        resp = client_b.get(WHOAMI, cookies=cookies)
    assert resp.status_code == 401, "换了签名密钥之后,旧 Cookie 仍然被接受"


# ---------------------------------------------------------------- 退出登录


def test_logout_clears_the_cookie(api):
    api.post(LOGIN, json={"username": "admin", "password": BROWSER_ADMIN_PASSWORD})
    resp = api.post(LOGOUT)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    raw = resp.headers.get("set-cookie", "").lower()
    assert "swimwear_session=" in raw
    assert "max-age=0" in raw or "expires=thu, 01 jan 1970" in raw


def test_logout_leaves_you_unauthenticated(api):
    api.post(LOGIN, json={"username": "admin", "password": BROWSER_ADMIN_PASSWORD})
    api.post(LOGOUT)
    assert api.get(WHOAMI).status_code == 401


def test_logout_is_idempotent_even_without_a_session(api):
    """没登录时也要 200。

    Cookie 已经过期的人来调这个接口,如果被守卫 401 挡住,他就"登不出去
    也进不去" —— 而他此刻想做的事(清掉本地登录态)本来就不需要权限。
    """
    first = api.post(LOGOUT)
    second = api.post(LOGOUT)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"ok": True}


# ---------------------------------------------------------------- 匿名白名单


def test_login_is_reachable_without_credentials(api):
    """登录不能要求先登录 —— 打错密码也要走到 401,而不是被兜底守卫挡在门外。"""
    resp = api.post(LOGIN, json={"username": "admin", "password": "wrong"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == ErrorCode.AUTH_FAILED


def test_whoami_still_requires_credentials(api):
    """探测接口必须要凭据,否则它探不出任何东西。"""
    resp = api.get(WHOAMI)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == ErrorCode.AUTH_FAILED


def test_an_unauthenticated_request_says_not_logged_in_not_server_misconfigured(api):
    """只配浏览器登录、不配 Legacy Token 的部署,未登录必须回 401。

    改之前 `rejection()` 只看 Legacy Token 配没配,于是这种部署(本轮之后
    最常见的形态)会对每一次未登录请求回 403 CONFIG_INVALID,那句话让运营
    去改服务器配置,而他真正该做的是点一下登录。403 还不会触发前端的
    跳转登录页(§33 按 401 判)。
    """
    resp = api.get(WHOAMI)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == ErrorCode.AUTH_FAILED


def test_health_tells_an_anonymous_caller_which_auth_mode_this_deployment_uses(api):
    """a46-phase2:`/health` 要说出 `auth_mode`,而且未登录时也答得出来。

    前端在**未登录**那一刻只有一个 401,而两种模式的 401 完全一样(同一个
    `AUTH_FAILED`、同一个状态码)。让它按后端的消息文本去猜该跳登录页还是设置页,
    是 §3.26 明令禁掉的形状 —— 按字符串在别处找东西,找到的不保证是你以为的那个。

    所以这条要求两件事一起成立:字段在,而且**在匿名状态下**拿得到。
    挂到 `/auth/whoami` 上就不成立了:那个接口未登录时就是 401,
    正是要回答问题的那一刻它答不了。
    """
    resp = api.get("/api/health")
    assert resp.status_code == 200
    # `browser_auth` fixture 配了两个密码,所以这里必须是 session
    assert resp.json()["auth_mode"] == "session"


def test_health_reports_token_mode_when_browser_login_is_not_configured(monkeypatch):
    """没配浏览器登录时报 `token` —— 前端据此指向设置页而不是登录页。

    反向的那一半:少了它,`auth_mode` 可以被写成恒为 `"session"` 而上面那条
    照样绿,于是只配 Legacy Token 的部署会把人送进一个永远登不进去的登录页。
    """
    from fastapi.testclient import TestClient

    from app.core.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "")
    monkeypatch.setattr(settings, "OPERATOR_PASSWORD", "")
    monkeypatch.setattr(settings, "AUTH_SESSION_SECRET", "")

    app.dependency_overrides[deps.db_session] = lambda: None
    try:
        with TestClient(app) as c:
            assert c.get("/api/health").json()["auth_mode"] == "token"
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------- 角色语义


def test_an_admin_session_passes_both_guards():
    admin = _request(session={"name": "admin", "role": "admin"})
    assert deps.require_admin(admin) == "admin"
    assert deps.require_operator(_request(session={"name": "admin", "role": "admin"})) == "admin"


def test_an_operator_session_passes_the_operator_guard():
    request = _request(session={"name": "operator", "role": "operator"})
    assert deps.require_operator(request) == "operator"


def test_an_operator_session_is_forbidden_from_admin_work():
    """403 AUTH_FORBIDDEN,不是 401。

    两者都是 4xx,但一个的意思是"你的身份不对,去重新登录",另一个是
    "你的身份没问题,只是这件事你做不了"。混用同一个码时,运营点开设置页
    会看到全局横幅说登录失效,然后被踢回登录页 —— 而他本来就已经登录了。
    """
    with pytest.raises(AppError) as caught:
        deps.require_admin(_request(session={"name": "operator", "role": "operator"}))
    assert caught.value.http_status == 403
    assert caught.value.code == ErrorCode.AUTH_FORBIDDEN


def test_a_session_beats_a_smuggled_admin_token(monkeypatch):
    """§12 的硬约束:已登录的浏览器带什么请求头都仍然是它登录的那个人。

    反过来的话,operator 登录的浏览器只要拿到一次 X-Admin-Token(从别人
    屏幕上、从某份文档里、从一次 XSS 里)塞进请求头就变成了 admin。
    """
    monkeypatch.setattr(deps.settings, "ADMIN_TOKEN", "the-admin-token", raising=False)
    request = _request(
        session={"name": "operator", "role": "operator"},
        headers={"X-Admin-Token": "the-admin-token"},
    )
    identity = deps.resolve_identity(request)
    assert identity is not None
    assert identity.role == deps.ROLE_OPERATOR, "Session 没有优先于 Header Token —— 这是提权"

    with pytest.raises(AppError) as caught:
        deps.require_admin(request)
    assert caught.value.http_status == 403


def test_a_malformed_session_is_treated_as_no_session(monkeypatch):
    """签名保证"这是我们写的",不保证"它今天还有意义"。"""
    monkeypatch.setattr(deps.settings, "ADMIN_TOKEN", "", raising=False)
    monkeypatch.setattr(deps.settings, "OPERATOR_TOKENS", "", raising=False)
    monkeypatch.setattr(deps.settings, "APP_ENV", "production", raising=False)
    for bad in ({}, {"name": "admin"}, {"name": "", "role": "admin"},
                {"name": "admin", "role": "superuser"}, {"name": 1, "role": "admin"}):
        assert deps.resolve_identity(_request(session=bad)) is None, f"{bad!r} 被当成了合法身份"


# ---------------------------------------------------------------- Legacy 兼容


def test_the_legacy_admin_token_still_works_without_a_session(monkeypatch):
    """CLI / 脚本 / pytest 那条路不能断(§37)。"""
    monkeypatch.setattr(deps.settings, "ADMIN_TOKEN", "the-admin-token", raising=False)
    identity = deps.resolve_identity(_request(headers={"X-Admin-Token": "the-admin-token"}))
    assert identity is not None and identity.role == deps.ROLE_ADMIN


def test_the_legacy_operator_token_still_carries_its_audit_name(monkeypatch):
    """具名口令在 API 侧仍然工作 —— 丧失的是**浏览器**侧的具名审计(§3.4)。"""
    monkeypatch.setattr(deps.settings, "ADMIN_TOKEN", "", raising=False)
    monkeypatch.setattr(deps.settings, "OPERATOR_TOKENS", "alice:tok-a,bob:tok-b", raising=False)
    identity = deps.resolve_identity(_request(headers={"X-Operator-Token": "tok-b"}))
    assert identity is not None
    assert (identity.name, identity.role) == ("bob", deps.ROLE_OPERATOR)


# ---------------------------------------------------------------- 开发模式


def test_the_dev_fallback_still_works_when_browser_auth_is_not_configured(monkeypatch):
    monkeypatch.setattr(deps.settings, "APP_ENV", "local", raising=False)
    for field in ("ADMIN_TOKEN", "OPERATOR_TOKENS", "ADMIN_PASSWORD",
                  "OPERATOR_PASSWORD", "AUTH_SESSION_SECRET"):
        monkeypatch.setattr(deps.settings, field, "", raising=False)
    identity = deps.resolve_identity(_request())
    assert identity is not None and identity.role == deps.ROLE_DEV


def test_a_configured_browser_login_switches_the_dev_fallback_off(monkeypatch):
    """本机填了密码就必须真的登录。

    少了这一条,本地怎么点都是通的 —— 而人工验收要验的恰好是 admin/operator
    的差异、退出登录、401 与 403,一条都验不到。
    """
    monkeypatch.setattr(deps.settings, "APP_ENV", "local", raising=False)
    monkeypatch.setattr(deps.settings, "ADMIN_TOKEN", "", raising=False)
    monkeypatch.setattr(deps.settings, "OPERATOR_TOKENS", "", raising=False)
    monkeypatch.setattr(deps.settings, "ADMIN_PASSWORD", BROWSER_ADMIN_PASSWORD, raising=False)
    assert deps.resolve_identity(_request()) is None, "配了浏览器密码的本机仍然免口令放行"


# ---------------------------------------------------------------- 配置校验


def _settings(monkeypatch, **overrides):
    """构造一个隔离的 Settings:不读 .env,也不受进程环境变量影响。"""
    from app.core.config import Settings

    for key in ("APP_ENV", "ADMIN_PASSWORD", "OPERATOR_PASSWORD",
                "AUTH_SESSION_SECRET", "AUTH_SESSION_MAX_AGE_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    return Settings(_env_file=None, **overrides)


GOOD = {
    "ADMIN_PASSWORD": "a-real-admin-password",
    "OPERATOR_PASSWORD": "a-real-operator-password",
    "AUTH_SESSION_SECRET": "z" * 48,
}


def test_a_local_environment_still_starts_with_nothing_configured(monkeypatch):
    assert _settings(monkeypatch, APP_ENV="local").ADMIN_PASSWORD == ""


def test_a_fully_configured_non_local_environment_starts(monkeypatch):
    settings = _settings(monkeypatch, APP_ENV="production", **GOOD)
    assert settings.browser_auth_configured is True


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"ADMIN_PASSWORD": ""}, "ADMIN_PASSWORD"),
        ({"OPERATOR_PASSWORD": ""}, "OPERATOR_PASSWORD"),
        ({"AUTH_SESSION_SECRET": ""}, "AUTH_SESSION_SECRET"),
        ({"AUTH_SESSION_SECRET": "too-short"}, "32"),
        ({"OPERATOR_PASSWORD": GOOD["ADMIN_PASSWORD"]}, "不能相同"),
        ({"ADMIN_PASSWORD": "change_me"}, "占位值"),
        ({"AUTH_SESSION_MAX_AGE_SECONDS": 0}, "AUTH_SESSION_MAX_AGE_SECONDS"),
    ],
)
def test_a_misconfigured_non_local_environment_refuses_to_start(monkeypatch, overrides, expected):
    """配不全就**起不来**,不是打条日志就放行。

    一条启动时刷过去的红字在容器日志里和其余几十行长得一样,没有人会因为
    它去改配置(`secrets_dir_is_exposed` 那条已经是本仓的先例)。
    """
    with pytest.raises(ValueError) as caught:
        _settings(monkeypatch, APP_ENV="production", **{**GOOD, **overrides})
    assert expected in str(caught.value)


@pytest.mark.parametrize("env", ["uat", "staging", "test", "production", "prod"])
def test_every_non_local_environment_is_covered_not_just_production(monkeypatch, env):
    """判据是 LOCAL_ENVS 的补集,不是 is_production。

    用 is_production 的话,uat / staging / test 会落进"既不强制、也没说不
    强制"的未定义区间 —— 而那几个名字对应的往往正是别人也能访问的真机器。
    """
    with pytest.raises(ValueError):
        _settings(monkeypatch, APP_ENV=env)


def test_the_https_only_verdict_follows_the_public_base_url(monkeypatch):
    assert _settings(monkeypatch, APP_ENV="local").session_cookie_https_only is False
    secure = _settings(
        monkeypatch, APP_ENV="production", PUBLIC_BASE_URL="https://imagegen.example.com", **GOOD
    )
    assert secure.session_cookie_https_only is True
