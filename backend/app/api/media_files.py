"""私有素材的代发接口(评审第 2 条)。

    GET /api/media-files/{storage_path}?exp=...&sig=...

## 为什么私有素材不能走静态挂载

商品原始图、模特参考图、还没过审的候选图,和"已经批准上网站的成品图"
不是同一个东西。以前它们共用一个 ``/files`` 静态挂载,意味着:

- 补上 API 鉴权也没用 —— 泄露过的文件 URL 仍然永久匿名可读;
- 而这些 URL 会进日志、进浏览器历史、进发给第三方 Provider 的请求体。

现在 ``/files`` 只挂 ``outputs/``,其余一律从这里出去,每次都要验签名和有效期。

## 为什么用签名而不是请求头口令

这些地址会直接进 ``<img src>``。浏览器加载图片时不会带自定义请求头,
所以"要求 X-Operator-Token"在界面上根本走不通 —— 结果只会是有人为了
让图显示出来,把这个路径重新开成匿名的。签名放在 URL 里,既能被 img 标签
用,又是短期的:凭据的有效期从"永久"变成十分钟。
"""
from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import Response

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.core.paths import is_hidden_path
from app.services.storage import (
    _content_type,
    is_public_path,
    verify_local_signature,
)

# 路径是 /media-files 而不是 /media:素材库的增删改查在 api/media_library.py,
# 走正常的操作口令。**两者鉴权方式不同**,所以不能共用一个前缀 ——
# 这一组必须能被 <img> 标签直接加载,而浏览器加载图片时不带自定义请求头。
router = APIRouter(tags=["media-files"])
logger = get_logger(__name__)

#: 浏览器可以缓存多久。比签名有效期短一点,避免缓存里躺着一个已经失效的响应。
CACHE_SECONDS = 300


def _reject() -> AppError:
    """验不过一律回同一个错误。

    刻意不区分"签名不对"和"已过期":两者分开回答等于告诉试探的人
    哪一半猜对了。对正常用户也没有损失 —— 界面重新拉一次列表就会拿到新地址。
    """
    return AppError(
        "素材地址无效或已过期,请刷新页面重新获取",
        code=ErrorCode.AUTH_FAILED,
        http_status=403,
    )


@router.get("/media-files/{storage_path:path}")
def get_media(
    storage_path: str,
    exp: str = Query("", description="到期时间戳"),
    sig: str = Query("", description="签名"),
) -> Response:
    """按签名代发一个私有对象。"""
    if settings.STORAGE_BACKEND != "local":
        # S3 走的是对象存储自己的预签名 URL,请求根本不会到后端来。
        # 真到了说明地址是拼出来的,不是我们发的。
        raise _reject()

    path = (storage_path or "").strip("/")
    if not path or is_hidden_path(path):
        raise _reject()

    if is_public_path(path):
        # 公开对象不该从这里出去 —— 那条路是 /files,有静态服务和 CDN 兜着。
        # 允许它从这里出去只会多一条没人维护的等价路径。
        raise _reject()

    if not verify_local_signature(path, exp, sig):
        logger.warning(
            "rejected a media request with a bad or expired signature",
            extra={"extra_fields": {"event": "auth.media_signature_rejected", "path": path}},
        )
        raise _reject()

    from app.services.storage import LocalObjectStorage

    storage = LocalObjectStorage(settings.storage_dir, settings.PUBLIC_BASE_URL)
    try:
        payload = storage.read(path)
    except FileNotFoundError:
        raise AppError(
            "素材不存在", code=ErrorCode.NOT_FOUND, http_status=404
        ) from None
    except ValueError:
        # resolve_within 挡下的路径穿越。当成验不过处理,不回显路径。
        raise _reject() from None

    extension = path.rsplit(".", 1)[-1] if "." in path else ""
    return Response(
        content=payload,
        media_type=_content_type(extension),
        headers={
            # private:签名是发给某个人的,不该被共享缓存留下来发给别人
            "Cache-Control": f"private, max-age={CACHE_SECONDS}",
            "X-Content-Type-Options": "nosniff",
        },
    )
