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

from app.core.config import settings
from app.db.session import engine

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    """存活探针:进程起来就返回 200,不依赖 PostgreSQL/Redis。"""
    return {"status": "ok", "app": settings.APP_NAME, "env": settings.APP_ENV}


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
