"""FastAPI 公共依赖:数据库会话与身份验证。

## 为什么写接口必须有守卫

MVP 明确排除了账号体系,于是很长一段时间里所有接口都是敞开的。问题是
「没有账号体系」和「不需要身份验证」是两件事:能访问这个服务的人可以上传商品、
**创建按次计费的生成任务**、取消别人的任务、批准图片直接上网站。
这些动作里没有一件可以随便谁都做,和“改配置”的差别只是钱花得慢一点。

所以这里做的不是账号体系,是一道**运维口令**,分两级:

    ADMIN_TOKEN      改配置、改提示词、测 Provider 连接。能改 Key、能烧额度。
    OPERATOR_TOKENS  日常业务写操作:传商品、建任务、审图。

admin 天然包含 operator —— 配了 admin 口令的人本来就能把 operator 口令改掉,
让他为了传一张图再配一个口令没有任何安全收益。

## 审计操作者只能来自已验证的凭据

以前 `X-Actor` 直接进审计日志,而它是个任意请求头:审计记录里写着谁做的,
完全由做这件事的人自己填。那样的审计日志在出事时一文不值。

现在操作者名字**只从验证通过的凭据里取**。`OPERATOR_TOKENS` 支持
``名字:口令`` 的写法,于是每个人一个口令、审计里就有真名;只配一个裸口令时
所有人共用 ``operator`` 这个名字 —— 不精确,但至少不是伪造的。
"""
from __future__ import annotations

import hmac
import re
from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import Depends, Request, UploadFile
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError, ErrorCode, ValidationError
from app.db.session import get_session

# settings 在模块级导入,不在每个函数里 import 一次。
#
# 这不只是省几行:函数内 import 拿到的是 app.core.config 那个模块属性,
# 测试里 `monkeypatch.setattr(deps.settings, "APP_ENV", ...)` 会因为
# `deps.settings` 根本不存在而当场抛 AttributeError —— conftest 里的
# admin_client / guarded_client 一直是这么写的,而这批用例上一轮没跑,
# 所以没人发现守卫测试其实一次都没执行过。


def db_session() -> Iterator[Session]:
    yield from get_session()


#: 这些环境下允许在没有口令时写入 —— 本机开发时再要一道口令纯属添堵。
#:
#: 刻意**不含** ``test``:local/dev/development 只可能是某人的开发机,
#: 而“测试环境”在多数团队里是一台真的、连着真 Key、别人也能访问的机器。
#: 少一个字母的差别不该决定要不要口令。
LOCAL_ENVS = frozenset({"local", "dev", "development"})

ADMIN_HEADER = "x-admin-token"
OPERATOR_HEADER = "x-operator-token"

ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_DEV = "dev"

#: 不改变服务器状态的方法。
#:
#: **它不再决定要不要验身份。** 以前守卫只管非安全方法,读接口默认全开,
#: 于是任何能访问服务的人都能枚举未发布商品、看到审核结论和驳回原因、
#: 拿到源素材和模特模板地址。「不改状态」和「可以给任何人看」是两件事。
#: 现在读写都要过 `require_operator`,这个常量只留给确实需要区分幂等性的地方。
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class Identity:
    """一次请求背后**已经验证过**的身份。

    ``name`` 是唯一允许写进审计日志的操作者标识。
    """

    name: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


def _app_env() -> str:
    return settings.APP_ENV.strip().lower()


def _is_local_env() -> bool:
    return _app_env() in LOCAL_ENVS


#: 审计名的合法形状。**故意不含冒号、空格和标点** —— 它同时是
#: "这一段到底是名字还是共用口令的前半截"的判据(R1-28)。
_NAME_PATTERN = re.compile(r"[A-Za-z0-9_.\-]{1,64}")


def parse_operator_tokens(raw: str) -> list[tuple[str, str]]:
    """解析 ``OPERATOR_TOKENS``。返回 [(名字, 口令), ...]。

    两种写法都接受::

        OPERATOR_TOKENS=s3cret                      # 共用口令,审计名 operator
        OPERATOR_TOKENS=alice:tok-a,bob:tok-b       # 一人一个,审计里有真名

    只在**第一个**冒号处切分:名字里不允许有冒号,口令里可以。

    ## 冒号的歧义,以及这里怎么处理它(R1-28)

    共用口令本身含冒号时,原来会被当成 `名字:口令` 拆开:

        OPERATOR_TOKENS=p@ss:w0rd    ->  名字 "p@ss",口令 "w0rd"

    于是配的那把口令**整个不工作**,而运营在设置页填的是完整的
    `p@ss:w0rd` —— 提示是"口令错误",人只会反复重敲同一串。

    完全消歧是做不到的(`alice:tok` 两种读法都合法),所以这里加一条
    **名字必须长得像名字**的约束:`[A-Za-z0-9_.-]`,1~64 位。前缀不满足时,
    整段按共用口令处理。`alice:tok-a` 仍然拆,`p@ss:w0rd` 不再拆。

    仍然拆错的那一类(前缀恰好是合法名字形状的共用口令)没有别的办法 ——
    `.env.example` 里因此写明:共用口令请避开冒号,或者直接用 `名字:口令` 形式。
    """
    entries: list[tuple[str, str]] = []
    for chunk in (raw or "").replace(";", ",").split(","):
        item = chunk.strip()
        if not item:
            continue
        name, sep, token = item.partition(":")
        name, token = name.strip(), token.strip()
        if sep and token and _NAME_PATTERN.fullmatch(name):
            entries.append((name[:64], token))
            continue
        entries.append((ROLE_OPERATOR, item))
    return entries


def _configured() -> tuple[str, list[tuple[str, str]]]:
    admin = (settings.ADMIN_TOKEN or "").strip()
    operators = parse_operator_tokens(getattr(settings, "OPERATOR_TOKENS", "") or "")
    return admin, operators


def _header(request: Request, name: str) -> str:
    return (request.headers.get(name) or "").strip()


def _matches(supplied: str, expected: str) -> bool:
    """定长比较。空口令一律不匹配,免得“没配”被当成“人人都对”。"""
    if not supplied or not expected:
        return False
    return hmac.compare_digest(supplied, expected)


def _session_identity(request: Request) -> Identity | None:
    """从浏览器 Session Cookie 里取身份。取不到返回 None。

    ## 为什么是 `request.scope.get("session")` 而不是 `request.session`

    后者是个 property,没装 SessionMiddleware 时**抛 AssertionError**。而本仓
    有大量纯测试直接构造 `Request(scope)` 来打这个函数(见本文件头那段说明),
    那些 scope 里当然没有 session。用 `scope.get` 是 fail-safe 的:没有中间件
    就当作"没有 Session",自然回落到 Legacy Token,而不是把整条身份链炸掉。

    ## 为什么还要校验 role

    Cookie 是**签名**的,所以正常情况下伪造不了。但签名保证的是"这个值是
    我们自己写的",不是"这个值今天还有意义" —— 上一版 session 里写的字段、
    别的服务共用同一把密钥写进来的东西,签名一样是合法的。这里只认识
    admin / operator 两个角色,别的一律当作没有 Session。
    """
    session = request.scope.get("session") or {}
    if not isinstance(session, dict):
        return None
    name = session.get("name")
    role = session.get("role")
    if not isinstance(name, str) or not isinstance(role, str):
        return None
    name, role = name.strip()[:64], role.strip()
    if not name or role not in {ROLE_ADMIN, ROLE_OPERATOR}:
        return None
    return Identity(name=name, role=role)


def dev_fallback_active() -> bool:
    """本机免口令模式生效中吗。**这是那条回落的唯一判据。**

    `resolve_identity` 的第三级(`ROLE_DEV`)与 `/health` 报的 `auth_mode`
    必须由同一个函数回答。分开写两遍的后果很具体(2026-08-11 评审的第 8 条):

        local + 配了 ADMIN_TOKEN + 没配浏览器密码

    这个组合下 `ROLE_DEV` 回落**不生效**(下面第三级要求三种凭据都没配),
    而浏览器又不会发 Header 口令 —— 前端那条链路在 PRD §26/§27 里整个退役了。
    于是:

        /health        -> auth_mode=token
        /auth/whoami   -> 401
        /settings      -> 没有口令输入框(随 localStorage 口令链一起删了)
        /login         -> "到系统设置页填一次口令即可"

    **浏览器没有任何办法登录**,而界面给出的每一句指引都指向一个不存在的
    输入框。三处各自都"对",合起来是一条死路。

    把判据提成函数之后,`/health` 能如实报出第三种模式(`dev`),
    界面因此能说真话:这个部署只认 Header 口令,浏览器进不来,
    要么配浏览器密码、要么清掉 Header 口令。
    """
    admin_token, operators = _configured()
    return (
        not admin_token
        and not operators
        and _is_local_env()
        and not settings.browser_auth_configured
    )


def resolve_identity(request: Request) -> Identity | None:
    """认出这次请求是谁。认不出来返回 None,**不抛异常** ——

    中间件和路由依赖都要用它:前者需要自己组装响应,后者需要抛 AppError。

    三级,**顺序是安全约束的一部分**::

        Session(有效)              -> Identity(name, role)      浏览器
              下无
        Legacy Header Token        -> 现有逻辑不变              CLI / 脚本 / pytest
              下无
        local/dev 且未配浏览器登录 -> ROLE_DEV 回落              本机开发

    ## Session 必须**优先**,不是"也支持"

    反过来写(先试 Header)会开一个提权口子:operator 已经登录的浏览器,
    只要拿到一次 X-Admin-Token(从别人屏幕上、从某份文档里、从 XSS 里),
    塞进请求头就变成了 admin。而 Session 优先意味着——**已登录的浏览器
    带什么头都仍然是它登录的那个人**,想换身份只能重新登录。

    Legacy Token 因此只服务于"没有 Browser Session"的那一类客户端。
    """
    session_identity = _session_identity(request)
    if session_identity is not None:
        return session_identity

    admin_token, operators = _configured()

    supplied_admin = _header(request, ADMIN_HEADER)
    if admin_token and _matches(supplied_admin, admin_token):
        return Identity(name=ROLE_ADMIN, role=ROLE_ADMIN)

    supplied_operator = _header(request, OPERATOR_HEADER) or supplied_admin
    for name, token in operators:
        if _matches(supplied_operator, token):
            return Identity(name=name, role=ROLE_OPERATOR)

    if dev_fallback_active():
        # 本机开发:没配任何口令时放行,审计名退回自述的 X-Actor。
        # 这条路径只在 local/dev 存在,所以“自述”的风险仅限于开发机自己的日志。
        #
        # `browser_auth_configured` 这个条件是本轮**唯一**加进来的:填了浏览器
        # 密码的本机,不能再被这条回落悄悄绕过。否则本地怎么点都是通的,
        # 而人工验收要验的正是 admin/operator 的差异、退出登录、401 与 403。
        declared = (request.headers.get("x-actor") or "system").strip()[:64]
        return Identity(name=declared or "system", role=ROLE_DEV)

    return None


def rejection(needs_admin: bool) -> AppError:
    """认不出身份时该回什么。中间件和路由守卫共用同一套措辞。

    ## "服务器没配" 和 "你没登录" 必须分开

    这里原来只看 Legacy Token 配没配。加了浏览器登录之后那个判据就不成立了:
    一个**只配浏览器登录、不配 Legacy Token** 的部署(这正是本轮之后最常见的
    形态)会让每一次未登录请求都回 403 CONFIG_INVALID,而那句话说的是
    "当前环境没有配置 ADMIN_TOKEN 或 OPERATOR_TOKENS,请在 .env 里配置后重启" ——

        运营看到的:  一句让他去改服务器配置的话
        实际该做的:  点一下登录

    更糟的是状态码:前端按 401 判"登录态失效 -> 跳登录页"(§33)。403 不会
    触发跳转,于是他停在一个说着配置错误的空页面上,而重启后端不会有任何改善。

    所以配置缺失这一条现在要求**三种凭据都没有**才成立。
    """
    admin_token, operators = _configured()
    if not admin_token and not operators and not settings.browser_auth_configured:
        # 三种凭据一个都没配、又不是本机:这是部署配置问题,不是调用方填错了。
        # 401 会让人一直去找口令,403 + 这段话才指向真正要做的事。
        return AppError(
            f"当前环境({settings.APP_ENV})没有配置浏览器登录密码,"
            "也没有配置 ADMIN_TOKEN 或 OPERATOR_TOKENS,已拒绝所有请求。"
            "请在 .env 里配置后重启后端",
            code=ErrorCode.CONFIG_INVALID,
            http_status=403,
        )
    if settings.browser_auth_configured:
        # 浏览器登录已配:未登录就是未登录,让前端按 401 跳登录页。
        return AppError(
            "未登录或登录已失效,请重新登录",
            code=ErrorCode.AUTH_FAILED,
            http_status=401,
        )
    if admin_token or operators:
        # 只配了 Header 口令,而浏览器不会发 Header 口令(那条链在 PRD §26/§27
        # 里整个退役了)。这一支是 2026-08-11 评审第 8 条的落点。
        #
        # 走到这里的请求**有两种来源**,而它们要的话完全不同:
        #
        #     CLI / 脚本 / pytest   口令没填或填错。它们能改请求头,401 是对的
        #     浏览器                口令**填不了**。界面上没有输入框,也不该有 ——
        #                           Session 才是浏览器的凭据
        #
        # 分不开的时候按"两边都看得懂"写:说清 Header 口令这条路和浏览器无关,
        # 并把两条真正可行的出路都点名。原来这里落到下面那句"缺少或错误的
        # 操作口令",而运营会照着它去找一个不存在的输入框 —— 而且
        # `/login` 与 `/settings` 当时也各有一句指向同一个不存在的输入框。
        #
        # 码仍然是 401(对脚本是准确的),但话必须把浏览器那一半说出来。
        what = "X-Admin-Token" if needs_admin else "X-Operator-Token"
        return AppError(
            f"当前环境({settings.APP_ENV})只配置了 Header 口令,而浏览器不使用"
            "它 —— 界面上没有、也不应该有填口令的地方。"
            f"脚本或 curl 请带上 {what} 请求头;要用浏览器,请让管理员配置 "
            "ADMIN_PASSWORD / OPERATOR_PASSWORD / AUTH_SESSION_SECRET 后重启后端,"
            "或者在本机开发时清空 ADMIN_TOKEN 与 OPERATOR_TOKENS 以启用免登录模式",
            code=ErrorCode.AUTH_FAILED,
            http_status=401,
        )
    what = "管理口令" if needs_admin else "操作口令"
    return AppError(
        f"缺少或错误的{what},无法执行该操作",
        code=ErrorCode.AUTH_FAILED,
        http_status=401,
    )


def require_operator(request: Request) -> str:
    """日常业务写接口的守卫。返回**已验证的**操作者名,直接用于审计。

    ## 用法:挂在路由器上,不要逐个路由挂

        router = APIRouter(dependencies=[Depends(require_operator)])

    **读接口也要挂。** 逐个挂的失败模式太安静:新加一个 `@router.get` 忘了写
    依赖不会有任何报错,它只会对全世界开放。历史上二十几个写接口全裸、
    一批读接口全裸,根因都是同一个 ——「默认是开的」。

    这段话原先逐字复制在八个路由模块的头上。复制件的问题不是占地方,
    是它们会各自漂移:改了其中一份的人没有任何理由去看另外七份。
    """
    identity = resolve_identity(request)
    if identity is None:
        raise rejection(needs_admin=False)
    request.state.identity = identity
    return identity.name


def require_admin(request: Request) -> str:
    """设置页 / 提示词 / Provider 连接测试的守卫。

    改配置意味着能改 API Key、能把生成切到别家、能把额度烧光,
    不该和“上传一张商品图”共用同一个门槛。
    """
    identity = resolve_identity(request)
    if identity is None:
        raise rejection(needs_admin=True)
    if identity.role == ROLE_OPERATOR:
        # AUTH_FORBIDDEN 而不是 AUTH_FAILED:这把口令是**对的**,只是权限不够。
        # 用同一个码的时候前端分不出来,于是运营点开设置页就会看到全局横幅说
        # 「后端不认这把口令」,然后去改一把本来就正确的口令。
        raise AppError(
            "该操作需要管理口令(X-Admin-Token),操作口令权限不足",
            code=ErrorCode.AUTH_FORBIDDEN,
            http_status=403,
        )
    request.state.identity = identity
    return identity.name


def has_admin_rights(request: Request) -> bool:
    """这次请求有没有管理员权限。**判据与 `require_admin` 逐字相同。**

    给"同一个端点里只有一部分字段要管理员"的场景用 —— 今天只有一处:
    模特模板的 §11 合规背书字段组(`schemas/asset.LICENSE_ENDORSEMENT_FIELDS`)。
    那个端点整体是 operator 的(上传素材是日常动作),但其中十个字段是签字。

    **不许在调用点自己写 `identity.role == ROLE_ADMIN`。** 那会漏掉 `ROLE_DEV`:
    本机免口令模式下角色是 `dev`,`is_admin` 为 False —— 于是本地开发与
    人工验收会被自己的守卫挡在门外,而那道门在真部署里根本不是这么判的。
    `require_admin` 的判据是"**不是** operator",这里必须同向;两处分叉的表现是
    "同一个身份在 A 端点是管理员、在 B 端点不是"。

    返回 False 也包含"根本认不出身份"。调用点走到这里之前一定过了
    `require_operator`(路由器级依赖),所以那种情况不会发生;真发生了,
    按"没有管理员权限"处理是安全方向。
    """
    identity = getattr(request.state, "identity", None) or resolve_identity(request)
    return identity is not None and identity.role != ROLE_OPERATOR


def current_actor(request: Request) -> str:
    """已验证的操作者名。**不接受任何请求头自述的身份。**

    读接口不强制验证,所以认不出来时返回 ``anonymous`` —— 一个明确表示
    “不知道是谁”的值,而不是一个可以被伪造的名字。
    """
    identity = getattr(request.state, "identity", None) or resolve_identity(request)
    return identity.name if identity else "anonymous"


DbSession = Depends(db_session)
OperatorActor = Depends(require_operator)
AdminActor = Depends(require_admin)


#: 每次从上传流里取多少。1MB:小到不会让一次超限上传先占满内存,
#: 大到不至于让一个 20MB 的文件循环两万次
_UPLOAD_CHUNK_BYTES = 1024 * 1024


async def read_within_limit(file: UploadFile, limit: int) -> bytes:
    """把上传读进内存,超限**尽早**停下(C-11)。

    ## 改之前是什么样

    `data = await file.read(limit + 1)` —— 一次性把 20MB + 1 读进来再判断。
    单个请求看没问题,并发起来就是 `20MB × 并发数` 的常驻峰值,而这条路径
    没有任何并发限制。

    ## 现在做的两件事

    1. **先看 `Content-Length`。** 头里就写着超限的,一个字节都不读就拒。
       它可以撒谎,所以它不是判据、只是快速路径。
    2. **分块读,超一块就停。** 真正的判据在这里:累计超过上限立刻抛,
       而不是等到把 `limit + 1` 全部收进内存之后。

    ## 仍然没做的:流式落盘

    `asset_service.upload_asset()` 收的是 `bytes`(它要算哈希、探图片尺寸、
    再交给存储层),所以整份内容终究要在内存里过一遍。真正的流式要把它
    改成收文件对象,连同存储层的分段上传一起动 —— 那是另一件事。
    **这条只把峰值从"必然 20MB+"降到"超限时早停",不是完整的 C-11。**
    """
    declared = file.headers.get("content-length") if file.headers else None
    if declared and declared.isdigit() and int(declared) > limit:
        raise ValidationError(
            f"文件超过上限 {settings.MAX_UPLOAD_SIZE_MB} MB",
            code=ErrorCode.FILE_TOO_LARGE,
        )

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            # 手上这些立刻扔掉:异常向上抛的过程里没人再需要它们,
            # 而超限上传正是最不该继续占着内存的那一种
            chunks.clear()
            raise ValidationError(
                f"文件超过上限 {settings.MAX_UPLOAD_SIZE_MB} MB",
                code=ErrorCode.FILE_TOO_LARGE,
            )
        chunks.append(chunk)
    return b"".join(chunks)
