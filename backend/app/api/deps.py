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


def resolve_identity(request: Request) -> Identity | None:
    """认出这次请求是谁。认不出来返回 None,**不抛异常** ——

    中间件和路由依赖都要用它:前者需要自己组装响应,后者需要抛 AppError。
    """
    admin_token, operators = _configured()

    supplied_admin = _header(request, ADMIN_HEADER)
    if admin_token and _matches(supplied_admin, admin_token):
        return Identity(name=ROLE_ADMIN, role=ROLE_ADMIN)

    supplied_operator = _header(request, OPERATOR_HEADER) or supplied_admin
    for name, token in operators:
        if _matches(supplied_operator, token):
            return Identity(name=name, role=ROLE_OPERATOR)

    if not admin_token and not operators and _is_local_env():
        # 本机开发:没配任何口令时放行,审计名退回自述的 X-Actor。
        # 这条路径只在 local/dev 存在,所以“自述”的风险仅限于开发机自己的日志。
        declared = (request.headers.get("x-actor") or "system").strip()[:64]
        return Identity(name=declared or "system", role=ROLE_DEV)

    return None


def rejection(needs_admin: bool) -> AppError:
    """认不出身份时该回什么。中间件和路由守卫共用同一套措辞。"""
    admin_token, operators = _configured()
    if not admin_token and not operators:
        # 没配任何口令、又不是本机:这是部署配置问题,不是调用方填错了口令。
        # 401 会让人一直去找口令,403 + 这段话才指向真正要做的事。
        return AppError(
            f"当前环境({settings.APP_ENV})没有配置 ADMIN_TOKEN 或 OPERATOR_TOKENS,"
            "已拒绝所有写操作。请在 .env 里配置后重启后端",
            code=ErrorCode.CONFIG_INVALID,
            http_status=403,
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
