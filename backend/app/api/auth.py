"""身份自查(A5:冷启动横幅要能区分"没填口令"和"填错了")。

`/health` 是匿名的,它只能回答"后端起来了没"。横幅要说的第三句话——
"后端在跑,但它不认这把口令"——需要一个**带口令**的探测,而在此之前
前端只能靠"localStorage 里有没有字符串"来猜,于是:

    只配了管理口令        -> 猜"已配置",实际业务请求 401,横幅却不出声
    口令复制时多了空格    -> 猜"已配置",每页一片红,没有一处说是口令的问题

这个接口本身不产生任何副作用,也不碰数据库:它就是把 `resolve_identity`
已经算出来的结论回给前端。

顺带解决 A8 的一个依赖:顶栏身份区要显示"这个浏览器是不是管理员"
(菜单**不**按角色裁剪 —— 那一版没落地,见 `frontend/src/App.tsx`),
而这件事同样不该由前端读 localStorage 自己判断——那是显示层的判断,
真正的权限边界仍然在后端的 `require_admin`。
"""
from __future__ import annotations

import hmac
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.api.deps import ROLE_ADMIN, ROLE_OPERATOR, Identity, require_operator
from app.core.client_ip import client_key
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.workflows import login_throttle

logger = get_logger(__name__)

router = APIRouter(tags=["auth"])

#: 只有这两个账号。用户名 -> 角色。
#:
#: 没有用户表、没有注册、没有自助改密 —— 本轮明确不做。要按人追溯时,
#: 正确的下一步是"用户表 + 每人一个账号",不是回头去配 OPERATOR_TOKENS
#: 的具名口令(那是机器凭据,不是人的账号)。
_ACCOUNTS: dict[str, str] = {"admin": ROLE_ADMIN, "operator": ROLE_OPERATOR}


# ---------------------------------------------------------------- 失败计数(a51)
#
# 判定在 `workflows/login_throttle.py`(零依赖、可穷举),这里只保管状态。
#
# ## 进程内,不是 Redis —— 这个取舍要写明白
#
# 本仓的部署是**单个 uvicorn 进程**(Dockerfile 的 CMD 没有 `--workers`),
# 所以进程内的表就是全局的表。改成多 worker 之后,每个 worker 各有一份,
# 阈值实际上会变成 `worker 数 × MAX_FAILURES` —— 那时该换成 Redis
# (broker 已经在了),而不是把这里的阈值除一除。
#
# 之所以现在不直接上 Redis:限流会挡在验密码**之前**,也就是说 Redis 一挂,
# 登录就得决定"放行还是全拦"。放行等于限流形同虚设,全拦等于 Redis 变成了
# 登录的硬依赖 —— 而目前登录是这套系统里唯一不碰任何外部依赖的接口。
# 进程内的表没有这个问题:它不会不可用。
#
# ## 表会不会涨爆
#
# 会,如果不清理:键是 (来源, 用户名),而来源可以是任意多个。所以每次写入
# 顺手扫一遍过期键(`_prune_store`)。上限再兜一道:超过 `_MAX_KEYS` 时
# 丢掉最旧的一批 —— 丢掉的后果是那些键的计数归零(放宽),不是误拦。
# **宁可漏放几次也不能因为一张内存表把登录拖垮。**
_FAILURES: dict[tuple[str, str], list[float]] = {}
_FAILURES_LOCK = threading.Lock()

#: 表里最多留多少个键。10k 个键约几 MB,而正常部署只会有个位数。
_MAX_KEYS = 10_000


def _prune_store(now: float) -> None:
    """丢掉窗口外的失败时刻,以及空掉的键。**调用方必须持有锁。**"""
    for key in list(_FAILURES):
        kept = login_throttle.recent_failures(_FAILURES[key], now)
        if kept:
            _FAILURES[key] = kept
        else:
            del _FAILURES[key]
    if len(_FAILURES) > _MAX_KEYS:
        # 按最后一次失败时间排,丢最旧的。dict 在 3.7+ 保序,但那是插入序,
        # 不是"最后活跃"序 —— 用插入序会丢掉一个刚刚在被爆破的键。
        doomed = sorted(_FAILURES, key=lambda k: max(_FAILURES[k]))[: len(_FAILURES) - _MAX_KEYS]
        for key in doomed:
            del _FAILURES[key]


def _throttle_key(request: Request, username: str) -> tuple[str, str]:
    source = client_key(
        request.headers,
        request.client.host if request.client else None,
        trust_proxy=settings.TRUST_PROXY_HEADERS,
    )
    return (source, username[:64])


def _check_throttle(key: tuple[str, str], now: float) -> login_throttle.ThrottleVerdict:
    with _FAILURES_LOCK:
        return login_throttle.evaluate(_FAILURES.get(key, ()), now)


def _record_failure(key: tuple[str, str], now: float) -> None:
    with _FAILURES_LOCK:
        _prune_store(now)
        _FAILURES.setdefault(key, []).append(now)


def _clear_failures(key: tuple[str, str]) -> None:
    """登录成功就清零 —— 连错几次之后终于想起来的人不该带着惩罚走路。"""
    with _FAILURES_LOCK:
        _FAILURES.pop(key, None)


def reset_login_throttle() -> None:
    """清空整张表。**只给测试用。**

    不加这个的话,同一个进程里跑的两条用例会互相看见对方的失败计数,
    而那种耦合的表现是"单跑绿、全量红",最难查的一类。
    """
    with _FAILURES_LOCK:
        _FAILURES.clear()


class LoginRequest(BaseModel):
    """登录入参。

    ``role`` **不在这里** —— 角色一律由服务端在校验通过后写进 Session。
    让前端提交角色等于把权限判定交给调用方。
    """

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=512)


def _configured_password(username: str) -> str:
    """这个账号配的密码。未知账号返回空串。

    空串在 `_password_matches` 里一律不匹配,于是"用户名不存在"和"密码错误"
    走的是**同一条**返回路径、同一个响应体 —— 调用方问不出哪个账号存在。
    """
    if username == "admin":
        return (settings.ADMIN_PASSWORD or "").strip()
    if username == "operator":
        return (settings.OPERATOR_PASSWORD or "").strip()
    return ""


def _password_matches(supplied: str, expected: str) -> bool:
    """定长比较。空的一律不匹配,免得"没配密码"变成"人人都对"。

    两侧都 strip:配置侧是因为 `.env` 行尾的空格看不见,输入侧是因为复制
    密码时多带一个空格是这个仓库已经踩过的事(见 ColdStartBanner 那段说明)。
    """
    supplied, expected = supplied.strip(), expected.strip()
    if not supplied or not expected:
        return False
    return hmac.compare_digest(supplied, expected)


def _identity_payload(identity: Identity) -> dict[str, Any]:
    """登录成功与 whoami 必须返回**同一个形状**。

    前端据此在登录后直接 `queryClient.setQueryData(['auth-probe'], data)`,
    不必再补一次请求。两处形状一旦分叉,那次 setQueryData 写进去的就是一个
    和探测结果不同构的对象,而它**不会报错** —— 只会让菜单在登录后的第一秒
    按错误的 is_admin 渲染一次。
    """
    return {
        "name": identity.name,
        "role": identity.role,
        "is_admin": identity.is_admin,
    }


@router.get("/auth/whoami")
def whoami(
    request: Request, actor: str = Depends(require_operator)
) -> dict[str, Any]:
    """这次请求带的口令是谁。认不出来时守卫已经抛 401/403,走不到这里。

    返回的 `name` 就是会写进审计日志的那个名字——让运营在设置页能核对
    "我这把口令在审计里叫什么",比事后翻日志猜要省事。
    """
    identity = getattr(request.state, "identity", None)
    role = getattr(identity, "role", "operator")
    return {
        "name": actor,
        "role": role,
        "is_admin": bool(getattr(identity, "is_admin", False)),
    }


@router.post("/auth/login")
def login(request: Request, payload: LoginRequest) -> dict[str, Any]:
    """用户名 + 密码换一个 HttpOnly Session Cookie。

    这个接口在 `main.PUBLIC_PREFIXES` 白名单里 —— 它必须匿名可访问,
    否则登录本身需要先登录。白名单加的是**精确路径段** `/auth/login`,
    不是 `/auth` 整个前缀:放开前缀会把 `/auth/whoami` 一起放匿名,
    于是身份探测永远成功,冷启动横幅那套区分整个失效。

    ## 失败一律同一句话

    用户名不存在、密码错误、账号没配密码 —— 三种情况返回**完全一致**的
    401。分开说会把"哪个账号存在"免费告诉调用方,而这个系统只有两个账号,
    确认其中一个存在等于把爆破面砍掉一半。

    ## 不记密码

    不打印、不进日志、不进 AppError.detail。`core/redaction.py` 的正则已经
    覆盖 `password` 这个字段名(`test_logging_redaction` 在守这件事),
    但那是**兜底**,不是这里可以随手记的理由。
    """
    username = payload.username.strip()

    # ---- 限流。**在比对口令之前** ----
    #
    # 顺序是这道闸的全部意义所在:先验口令、只对失败计数,读起来更友好,
    # 但那样每一发猜测都仍然被完整地验了一次 —— 限流一次也没有限制过速率。
    #
    # 时钟用 `time.monotonic()` 而不是 `core/clock.utc_now()`:这张表只活在
    # 本进程内存里,不落库、不出参,而 monotonic 不会被 NTP 校时或虚拟机
    # 挂起恢复往回拨。仓库那条"时间一律走 clock"的约定管的是**会被写进
    # timestamptz 或序列化出去**的时刻,不是这种进程内的间隔测量。
    key = _throttle_key(request, username)
    now = time.monotonic()
    verdict = _check_throttle(key, now)
    if not verdict.allowed:
        logger.warning(
            "browser login throttled",
            extra={
                "extra_fields": {
                    "username": username[:64],
                    "source": key[0],
                    "failures": verdict.failures_in_window,
                    "retry_after_seconds": verdict.retry_after_seconds,
                }
            },
        )
        # 不设 `Retry-After` 响应头:`AppError` 没有携带响应头的通道,
        # 而唯一的消费者是我们自己的界面 —— 它展示 message。要给自动化客户端
        # 用的话,该做的是给 AppError 加一条 headers 通道,那是单独一件事。
        raise AppError(
            f"尝试过于频繁,请 {verdict.retry_after_seconds} 秒后重试",
            code=ErrorCode.RATE_LIMITED,
            http_status=429,
        )

    role = _ACCOUNTS.get(username)
    matched = _password_matches(payload.password, _configured_password(username))

    if role is None or not matched:
        _record_failure(key, now)
        # 只记用户名和失败这件事。用户名本身不是秘密(总共就两个),
        # 而没有它的话,一次真实的爆破在日志里和一次手滑长得一模一样。
        logger.warning(
            "browser login rejected",
            extra={"extra_fields": {"username": username[:64]}},
        )
        raise AppError(
            "用户名或密码错误",
            code=ErrorCode.AUTH_FAILED,
            http_status=401,
        )

    # 先清空,再写入。**顺序是防 session fixation 的关键**:不清空的话,
    # 攻击者事先塞给受害者的那份 session 里的其余字段会跨越这次登录活下来。
    # 本轮 session 里只有 name/role 两个字段,清空看起来多余 —— 但"以后
    # 有人往 session 里加了第三个字段"是必然会发生的事,而那时没有人会
    # 回头来补这一行。
    # 成功就清零。连错几次之后终于想起来的人,不该在之后的一整个窗口里
    # 带着惩罚走路 —— 而这也不给攻击者任何东西:他要先猜对才清得掉。
    _clear_failures(key)

    request.session.clear()
    identity = Identity(name=username, role=role)
    request.session["name"] = identity.name
    request.session["role"] = identity.role

    logger.info(
        "browser login accepted",
        extra={"extra_fields": {"actor": identity.name, "role": identity.role}},
    )
    return _identity_payload(identity)


@router.post("/auth/logout")
def logout(request: Request) -> dict[str, Any]:
    """退出登录。**幂等** —— 没登录时也返回 200。

    清空 session 之后,Starlette 会在响应上发一个 `max-age=0` 的 Set-Cookie
    把 Cookie 删掉(仅当这次请求带进来的 session 原本非空;本来就空的话
    没有 Cookie 可删,也就不必发)。

    所以它同样进匿名白名单:一个已经过期的 Cookie 去调 logout,如果被守卫
    401 挡住,前端就会陷入"登不出去,也进不去"——而用户此刻想做的事
    (把本地的登录态清掉)本来就不需要任何权限。

    ## 签名 Cookie 的边界,本轮明确接受

    这只让**当前浏览器**丢掉 Cookie。有人事先复制走一份合法 Cookie 的话,
    服务端在 TTL 到期前吊销不了那一份 —— 签名 Session 是无状态的,服务端
    没有一张"哪些 Session 还有效"的表。需要"管理员强制踢人""改密后立即
    全端失效"时,再上 Redis Session 或 session version,不在本轮扩范围。
    """
    actor = (request.scope.get("session") or {}).get("name")
    request.session.clear()
    if actor:
        logger.info("browser logout", extra={"extra_fields": {"actor": str(actor)[:64]}})
    return {"ok": True}
