"""身份自查(A5:冷启动横幅要能区分"没填口令"和"填错了")。

`/health` 是匿名的,它只能回答"后端起来了没"。横幅要说的第三句话——
"后端在跑,但它不认这把口令"——需要一个**带口令**的探测,而在此之前
前端只能靠"localStorage 里有没有字符串"来猜,于是:

    只配了管理口令        -> 猜"已配置",实际业务请求 401,横幅却不出声
    口令复制时多了空格    -> 猜"已配置",每页一片红,没有一处说是口令的问题

这个接口本身不产生任何副作用,也不碰数据库:它就是把 `resolve_identity`
已经算出来的结论回给前端。

顺带解决 A8 的一个依赖:菜单按角色收敛需要知道"这个浏览器是不是管理员",
而这件事同样不该由前端读 localStorage 自己判断——那是显示层的判断,
真正的权限边界仍然在后端的 `require_admin`。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.api.deps import require_operator

router = APIRouter(tags=["auth"])


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
