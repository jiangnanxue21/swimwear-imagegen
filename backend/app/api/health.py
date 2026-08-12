"""健康检查。liveness 不碰外部依赖,readiness 才检查 DB/Redis。

两个探针的语义完全不同,搞混了后果很实在:

    liveness   进程还活着吗?失败 -> 编排系统**重启容器**
    readiness  现在能接请求吗?失败 -> 编排系统**把它从负载均衡摘掉**

所以 liveness 绝不能依赖 PostgreSQL —— 数据库抖一下就把所有后端重启一轮,
只会让恢复变得更慢。
"""
from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.api import deps
from app.core.config import settings
from app.db.session import engine

router = APIRouter(tags=["health"])


#: 这个部署认哪种凭据。前端据此决定"未登录"该把人送到哪儿。
#:
#: ``session``  浏览器登录已配(`ADMIN_PASSWORD` / `OPERATOR_PASSWORD` /
#:              `AUTH_SESSION_SECRET` 三项里有任意一项非空)。401 的意思是
#:              "登录失效了",前端跳 `/login`
#: ``dev``      本机免口令模式(`deps.dev_fallback_active()`)。没有登录这个
#:              动作,任何请求都被当成 `ROLE_DEV` 放行。出现 401 说明后端配置
#:              刚变过
#: ``token``    只配了 Legacy Header Token,而且不在免口令模式里。
#:              **这一档意味着浏览器进不来** —— 前端那条自动带
#:              `X-Admin-Token` 的链在 PRD §26/§27 里整个退役了,设置页也
#:              没有口令输入框。界面必须如实说这件事,而不是指向一个
#:              不存在的输入框
#:
#: ``dev`` 是 2026-08-11 评审补的第三档。在它之前 `dev` 与 `token` 合报成
#: `token`,于是"本机免登录、一切正常"和"浏览器彻底进不来"在前端是同一个值,
#: 而后者需要的那句话完全说不出来。
AUTH_MODE_SESSION = "session"
AUTH_MODE_DEV = "dev"
AUTH_MODE_TOKEN = "token"


@router.get("/health")
def health() -> dict:
    """存活探针:进程起来就返回 200,不依赖 PostgreSQL/Redis。

    ## 为什么 `auth_mode` 挂在这里

    前端在**未登录**的状态下需要知道该把人送到登录页还是设置页,而那一刻它
    手上只有一个 401 —— 两种模式的 401 长得一模一样(同一个 `AUTH_FAILED`,
    同一个状态码)。让前端按后端消息文本去猜是 §3.26 明令禁掉的形状。

    所以由后端说出来,挂在**唯一一个匿名接口**上:未登录时它是前端仅剩的
    信息来源。挂到 `/auth/whoami` 上是不行的 —— 那个接口未登录时就是 401,
    正是要回答问题的那一刻它答不了。

    ## 这不是信息泄露

    "这个部署开没开浏览器登录"本来就是匿名可观测的:往 `/auth/login` POST
    一次就知道。这里只是把一件已经能被探到的事说清楚,省掉前端的猜测,
    而没有说出任何一个密码、密钥或账号是否存在。
    """
    return {
        "status": "ok",
        "app": settings.APP_NAME,
        "env": settings.APP_ENV,
        # 两处判据必须是**同一个东西**,不是两份长得像的条件:
        #
        #   session  取 `browser_auth_configured` 而不是"三项齐全" —— 后者在
        #            local 下会把"只填了密码、没填 secret"报成 token 模式,
        #            而 `resolve_identity` 那边已经按 Session 在拦了,
        #            前端于是被引去设置页填一把根本不会被读的口令。
        #            `test_a46_phase2_browser_login_seam.py` 的「同一个属性」
        #            那条钉着这一点
        #   dev      取 `deps.dev_fallback_active()`,也就是 `resolve_identity`
        #            第三级用的那个函数本身。抄一份条件过来的话,两边会分叉,
        #            而分叉的表现是界面说"免登录"而后端在 401
        "auth_mode": (
            AUTH_MODE_SESSION
            if settings.browser_auth_configured
            else AUTH_MODE_DEV
            if deps.dev_fallback_active()
            else AUTH_MODE_TOKEN
        ),
    }


@router.get("/health/ready")
def readiness(response: Response) -> dict:
    """就绪探针:任一依赖不可用返回 **503**。

    以前这里无论如何都返回 200,只在响应体里写一个 ``degraded``。
    那等于没有探针:Kubernetes、负载均衡器、Consul 全都只看状态码,
    没有一个会去解析 JSON 里的 status 字段。结果是数据库连不上的实例
    照样留在轮转里接请求,每一个都 500,而看板上一片绿。

    响应体保持不变 —— 里面逐项列出了是哪个依赖坏了,那是给人看的。
    改的只是状态码,也就是给机器看的那部分。
    """
    checks: dict[str, str] = {}

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 - 探针需吞掉细节
        checks["database"] = f"error: {type(exc).__name__}"

    try:
        import redis  # 延迟导入,避免未装 redis 时影响启动

        client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2)
        client.ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"error: {type(exc).__name__}"

    try:
        # 走 build_storage 拿**当前配置的那个**后端,再让它自检。
        #
        # 以前无论 STORAGE_BACKEND 是什么,这里都只 mkdir + is_dir 检查本地目录。
        # S3 部署下那个目录多半确实存在(容器里就有),于是凭据过期、Bucket 被删、
        # 网络不通时,实例照样报告 storage=ok 并留在负载均衡里 ——
        # 每一次上传和出图都失败,而看板一片绿。探针检查的对象必须是
        # 真正会被用到的那个后端,不是碰巧躺在磁盘上的一个路径。
        from app.services.storage import build_storage

        build_storage(
            settings.STORAGE_BACKEND,
            settings.storage_dir,
            settings.PUBLIC_BASE_URL,
            settings.API_PREFIX,
        ).healthcheck()
        checks["storage"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["storage"] = f"error: {type(exc).__name__}"

    ready = all(v == "ok" for v in checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    payload: dict = {"status": "ok" if ready else "degraded", "checks": checks}

    # 派发积压是"能不能接请求"之外的事,所以它**不影响状态码** ——
    # Broker 挂了的时候把后端全摘掉,只会让人连界面都打不开、更没法排查。
    # 但这个数必须报出来:放弃投递之后记录就离开 PENDING 了,
    # 只看 pending 的话曲线会漂亮地回落到零,而实际是一批任务再也没人管。
    if checks["database"] == "ok":
        try:
            from app.db.session import SessionLocal
            from app.services import dispatch_service

            session = SessionLocal()
            try:
                payload["dispatch"] = dispatch_service.queue_health(session)
            finally:
                session.close()
        except Exception as exc:  # noqa: BLE001
            payload["dispatch"] = {"error": type(exc).__name__}

    return payload
