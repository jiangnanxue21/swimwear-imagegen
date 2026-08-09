"""FastAPI 应用入口。

设计要点:
- 未配置任何第三方 Provider 时必须能正常启动(需求第十七章);
- 数据库不可用不阻止启动,只让 /health/ready 显示 degraded;
- 所有异常统一走 AppError 处理器,不向客户端泄露数据库信息。
"""
from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.router import api_router
from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger, request_id_var, setup_logging
from app.core.paths import is_hidden_path
from app.services.storage import PUBLIC_PREFIXES as _STORAGE_PUBLIC_PREFIXES

#: HTTP 状态码 -> 业务错误码。给 `HTTPException` 用 —— 它只带状态码,
#: 而前端的错误分支是按 code 写的。映射不到的按内部错误处理。
#:
#: 位置:必须在 import 块**之后**。原来它夹在两行 import 中间,ruff 默认
#: select 含 E4(E402 module-import-not-at-top-of-file),`make lint` 会红。
_HTTP_STATUS_TO_CODE: dict[int, ErrorCode] = {
    401: ErrorCode.AUTH_FAILED,
    403: ErrorCode.AUTH_FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    409: ErrorCode.INPUT_INVALID,
    422: ErrorCode.INPUT_INVALID,
}

#: 静态挂载出去的那个前缀。取自存储层的公开前缀清单,不在这里另写一份 ——
#: 两处各写一份字符串,改一处忘一处的那次就是把私有素材挂上公网。
PUBLIC_STORAGE_PREFIX = _STORAGE_PUBLIC_PREFIXES[0]

setup_logging(settings.LOG_LEVEL)
logger = get_logger(__name__)

#: 允许匿名访问的 API 路径前缀。**读写一视同仁。**
#:
#: 以前这个白名单只管写方法,读接口默认全开。那不是一个疏漏,是一个错误的
#: 默认值:能读就能枚举未发布的商品、看到审核结论和驳回原因、拿到源素材和
#: 模特模板的地址。这些东西没有一件是"反正也不算机密"。
#:
#: 现在默认是关的,要开放得显式加进这个列表 —— 于是"开一个匿名接口"
#: 变成一次会出现在 diff 里的动作,而不是某个新路由忘了挂守卫。
#:
#: 目前只有两类:
#:   /health      探针。编排系统不会带口令,而且它只回答"活着吗"
#:   /media-files 私有素材代发。它自己验 URL 签名 —— 见 api/media_files.py
#:                里关于 <img> 标签带不了自定义请求头的那段说明。
#:                注意**不是** /media:素材库的增删改查在那个前缀下,
#:                走正常的操作口令,不能被这条白名单顺带放开
PUBLIC_PREFIXES: tuple[str, ...] = ("/health", "/media-files")


def is_public_api_path(path: str, prefix: str) -> bool:
    """这条路径是否落在匿名白名单里。**按路径段比,不按字符串前缀比。**

    `path.startswith(prefix + p)` 没有边界(评审第 16 条):`/api/health`
    这一条会顺带放行将来任何以它开头的路径 —— `/api/health-admin`、
    `/api/healthcheck-internal`;`/api/media-files` 会放行
    `/api/media-files-export`。

    这类问题的特征是**加接口的人不会发现**:他新加的路由看起来工作正常,
    只是恰好不需要口令。要么整条路径相等,要么它是白名单路径的子路径。
    """
    for allowed in (prefix + p for p in PUBLIC_PREFIXES):
        if path == allowed or path.startswith(allowed + "/"):
            return True
    return False


class SafeStaticFiles(StaticFiles):
    """拒绝一切以点开头的路径段。

    Starlette 的 StaticFiles 只防目录穿越,不防隐藏文件 —— 目录里有什么就发什么。
    这个目录装的是用户素材和生成结果,本来就该是公开的;但\"公开\"不该顺带包括
    某天被谁放进去的 ``.env``、``.settings.key``、``.git`` 之类。

    白名单式地只发普通文件,比每次记得别往这个目录放东西可靠。
    """

    async def get_response(self, path: str, scope):
        if is_hidden_path(path):
            logger.warning(
                "blocked a dotfile request under the static mount",
                extra={"extra_fields": {"path": path}},
            )
            return PlainTextResponse("Not Found", status_code=404)
        return await super().get_response(path, scope)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    if settings.secrets_dir_is_exposed:
        detail = {
            "secrets_dir": str(settings.secrets_dir),
            "storage_dir": str(settings.storage_dir),
        }
        if settings.is_production:
            # 生产环境直接不许起来。
            #
            # 以前这里只打一条 error 日志就放行,理由是「半夜起不来比配置错更糟」。
            # 那个权衡是错的:主密钥躺在对外目录里意味着**所有已加密的配置等同明文**,
            # 而且这条日志在一台正常跑着的服务上没有任何人会去看。
            #
            # 应用自己的 SafeStaticFiles 会挡掉点文件,但它挡不住 Nginx、CDN、
            # 对象存储同步或任何一个直接对着这个目录发文件的东西 —— 只要有一个环节
            # 不经过这段 Python,密钥就是可下载的。这层防护不能靠“别人也都记得”。
            logger.error(
                "refusing to start: SETTINGS_KEY_DIR is inside the publicly served "
                "storage directory",
                extra={"extra_fields": detail},
            )
            raise RuntimeError(
                f"SETTINGS_KEY_DIR({settings.secrets_dir})位于对外托管的存储目录"
                f"({settings.storage_dir})之内,主密钥可能被直接下载。"
                "请把它移到存储目录之外(例如项目根下的 .secrets/ 或独立的卷)后再启动"
            )
        logger.error(
            "SETTINGS_KEY_DIR is inside the publicly served storage directory; "
            "move it out or the master key is downloadable",
            extra={"extra_fields": detail},
        )
    # 价目表在启动时校验一次。热路径上它已经降级成「没配价」不再抛
    # (`spend._price_book_or_empty`),所以这条日志是**唯一**会主动说
    # 「配置写错了」的地方 —— 少了它,一份坏价目表就只表现为看板上的 0。
    from app.services.spend import price_book_error

    reason = price_book_error()
    if reason:
        logger.error(
            "PROVIDER_PRICE_BOOK is unparseable; every call will be recorded as "
            "unpriced and the spend page will show zero",
            extra={"extra_fields": {"reason": reason}},
        )

    # 批次租约与识别预算的一致性(A45-batch18 / P2-1)。
    #
    # `workbench/batch.py` 里那条模块级 `if` 用的是三项配置的**默认值**;
    # 这一条用的是**这台机器实际生效的配置**(含设置页在数据库里的覆盖)。
    # 两者缺一不可:前者挡"改了常量忘了改租约",后者挡"运维把单张超时
    # 从 60 调到 120"。后者不报错、不影响任何测试,而后果是一件正在正常
    # 跑的条目被回收器抢走,那次已经付过费的调用结果被丢弃。
    from app.workbench.batch_service import lease_budget_shortfall

    shortfall = lease_budget_shortfall()
    if shortfall:
        logger.error(
            "item lease is shorter than the configured extraction budget; "
            "a healthy worker can lose items it is still running",
            extra={"extra_fields": {"reason": shortfall}},
        )

    logger.info(
        "application started",
        extra={"extra_fields": {"env": settings.APP_ENV, "storage": str(settings.storage_dir)}},
    )
    yield
    logger.info("application stopped")


def create_app() -> FastAPI:
    # 生产环境关掉接口文档(BE-GLOBAL-03)。
    #
    # `/docs`、`/redoc`、`/openapi.json` 默认对**匿名**开放,而这套 API 的
    # 结构本身就是敏感信息:它把全部高权限路径(设置、提示词、Provider 测试、
    # 强制重试)、入参形状和鉴权头名称一次性列给任何访问者。公网部署时
    # 这等于免费提供一份攻击面清单。
    #
    # 非生产环境保持开启 —— 联调时没有文档会把成本转嫁给每一个人。
    # 判据用 `is_production` 那一份,不在这里再写一遍字符串比较。
    docs_enabled = not settings.is_production
    app = FastAPI(
        title="网站商品展示图自动生成系统",
        version="0.2.0",
        description="泳衣/服装商品展示图生产流水线。当前完成阶段 1-2(骨架 + 商品与素材)。",
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    origins = settings.cors_origin_list
    if "*" in origins:
        # `*` 与 allow_credentials=True 是一个**无效**组合:浏览器会拒绝
        # 带凭据的通配来源响应,于是配了等于没配 —— 而表现是跨域请求全挂,
        # 没有一处说是 CORS 配错了。宁可当场说清楚。
        raise RuntimeError(
            "CORS_ORIGINS 不允许配成 `*`:本服务带凭据(口令请求头),"
            "通配来源在浏览器侧无效。请逐条列出前端地址"
        )

    # ------------------------------------------------------------------
    # 中间件顺序:**后 add 的在外层。**
    #
    # 所以下面两个必须按「守卫在内、request_context 在外」的顺序注册,
    # 也就是先 add 守卫、后 add request_context。
    #
    # 反过来的后果(上一版就是反的):守卫在最外层,它自己组装的 401 响应
    # 根本不经过 request_context —— 于是那个响应**没有 x-request-id 头**,
    # 而前端 `describeError` 的 401/403 分支正要读它,拿到的是空串;
    # 守卫自己那条 warning 日志也没有 request_id,查不到是哪一次请求。
    # ------------------------------------------------------------------

    @app.middleware("http")
    async def guard_api_requests(request: Request, call_next):
        """所有 API 请求的兜底守卫,**读和写一样管**。

        每个路由上也各自挂了 ``require_operator`` / ``require_admin``,这一层是
        **第二道**。理由很简单:忘了挂守卫的新路由不会有任何报错,它只会安静地
        对全世界开放 —— 上一轮是二十几个写接口全裸,这一轮是一批读接口全裸,
        两次的根因是同一个:默认值是"开"。守卫写在这里,新增路由默认是关着的。

        CORS 预检必须放行:浏览器发 OPTIONS 时不会带任何自定义请求头
        (口令正是自定义请求头),挡掉它等于把整个前端挡在跨域部署之外,
        而预检本身不返回任何数据。

        中间件抛异常不会走 FastAPI 的 exception handler(handler 装在更内层),
        所以这里自己组装响应。
        """
        from app.api.deps import resolve_identity

        path = request.url.path
        prefix = settings.API_PREFIX
        needs_guard = (
            request.method.upper() != "OPTIONS"
            and path.startswith(prefix)
            and not is_public_api_path(path, prefix)
        )
        if needs_guard and resolve_identity(request) is None:
            from app.api.deps import rejection

            exc = rejection(needs_admin=False)
            logger.warning(
                "rejected an unauthenticated api request",
                extra={"extra_fields": {"path": path, "method": request.method}},
            )
            return JSONResponse(status_code=exc.http_status, content=exc.to_payload())
        return await call_next(request)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
            fields = {
                "method": request.method,
                # 只记 path,不记 query string。查询参数里可能出现外部 ID 或口令。
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000),
            }
            if response.status_code >= 500:
                logger.error("http request completed", extra={"extra_fields": fields})
            elif response.status_code >= 400:
                logger.warning("http request completed", extra={"extra_fields": fields})
            else:
                logger.info("http request completed", extra={"extra_fields": fields})
            response.headers["x-request-id"] = rid
            return response
        finally:
            request_id_var.reset(token)

    # ------------------------------------------------------------------
    # CORS **最后 add,因此在最外层**。这不是风格问题。
    #
    # 后 add 的在外层,所以上一版把 CORS 第一个 add 等于把它放在最内层 ——
    # 它只包住路由。而守卫中间件的 401 是**它自己组装的 JSONResponse**,
    # 从守卫那一层就返回了,根本不经过 CORS:
    #
    #     跨域部署 -> 401 响应没有 Access-Control-Allow-Origin
    #             -> 浏览器直接拦掉,不交给 JS
    #             -> 前端拿到的是「网络错误」而不是 401
    #             -> `isAuthError` 判不出来,冷启动横幅那套
    #                「口令没填 / 填错了」的区分整个失效
    #
    # 也就是说:口令错的时候,前端反而说不清是口令错了。
    # 放最外层之后,任何一层返回的响应都会被套上 CORS 头。
    # ------------------------------------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # 跨域时浏览器默认只把几个安全头暴露给 JS,自定义头一律看不见。
        # 前端读 `X-Export-Filename` 决定另存的文件名,读 `X-Request-Id`
        # 让运营能把编号转述给管理员 —— 不暴露的话前者退化成默认文件名、
        # 后者变成空字符串,两处都**不报错**,只是安静地少了东西。
        expose_headers=["X-Export-Filename", "X-Request-Id", "Content-Disposition"],
    )

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        logger.warning(
            "business error",
            extra={"extra_fields": {"code": str(exc.code), "path": request.url.path, **exc.detail}},
        )
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload())

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """把 FastAPI/Starlette 的 `HTTPException` 也压成统一错误体
        (BE-GLOBAL-01)。

        不装这个处理器的话,`raise HTTPException(404, "...")` 产出的是
        `{"detail": "..."}`,而 `AppError` 产出的是
        `{"error": {"code": ..., "message": ...}}` —— 前端要写两套解析,
        而漏掉第二套的表现是错误提示变成 `undefined`。404(路由不存在)、
        405、以及任何第三方中间件抛出的 HTTPException 都会走这里。

        `detail` 已经是结构化对象时原样放进 `fields`,不强行字符串化。
        """
        detail = exc.detail
        code = _HTTP_STATUS_TO_CODE.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        payload: dict = {
            "error": {
                "code": str(code),
                "message": detail if isinstance(detail, str) else "请求无法处理",
            }
        }
        if not isinstance(detail, str) and detail is not None:
            payload["error"]["fields"] = detail
        return JSONResponse(
            status_code=exc.status_code,
            content=payload,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = [
            {"loc": ".".join(str(x) for x in e.get("loc", [])), "msg": e.get("msg", "")}
            for e in exc.errors()
        ]
        # 只记字段位置与校验结论,不记用户提交的值。值里可能含提示词、URL 或密钥。
        logger.warning(
            "request validation failed",
            extra={
                "extra_fields": {
                    "method": request.method,
                    "path": request.url.path,
                    "fields": problems,
                }
            },
        )
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": str(ErrorCode.INPUT_INVALID),
                    "message": "请求参数不合法",
                    "fields": problems,
                }
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        # 只记录类型与 request_id,绝不把数据库异常原文返回给客户端
        logger.exception(
            "unhandled error", extra={"extra_fields": {"path": request.url.path}}
        )
        return JSONResponse(
            status_code=500,
            content={"error": {"code": str(ErrorCode.INTERNAL_ERROR), "message": "服务内部错误"}},
        )

    app.include_router(api_router, prefix=settings.API_PREFIX)

    # 开发模式下用后端直接托管本地存储,生产应由 Nginx/S3 承担。
    #
    # **只挂 outputs/。** 以前整个存储目录被挂出去,商品原图、模特参考图、
    # 还没过审的候选图和成品图共用同一个访问级别 —— 补上 API 鉴权也没用,
    # 泄露过的文件 URL 仍然永久匿名可读。其余前缀现在走 /api/media,
    # 每次都要验签名和有效期(见 services/storage.py 的 PUBLIC_PREFIXES)。
    #
    # 挂载点写成 /files/outputs 而不是 /files,是为了让 URL 一个字都不用改:
    # 成品图的 storage_path 本来就以 outputs/ 开头。
    if settings.STORAGE_BACKEND == "local":
        public_dir = settings.storage_dir / PUBLIC_STORAGE_PREFIX
        public_dir.mkdir(parents=True, exist_ok=True)
        app.mount(
            f"/files/{PUBLIC_STORAGE_PREFIX}",
            SafeStaticFiles(directory=public_dir),
            name="files",
        )

    return app


app = create_app()
