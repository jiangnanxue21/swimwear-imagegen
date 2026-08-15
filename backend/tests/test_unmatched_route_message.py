"""路由没匹配上时,错误体里那句话必须说得出下一步。

## 修的是什么

Starlette 对一条不存在的路由抛的是 ``HTTPException(404, "Not Found")``,
405 是 ``"Method Not Allowed"``。`main.http_error_handler` 原来把 `detail`
原样塞进统一错误体的 `message`,于是它一路走到前端的 `message.error()` ——
**运营看到的是一个英文单词。**

这不是文案瑕疵。2026-08-15 的实况:界面上新加了「删除素材」按钮,
而当时运行着的后端进程还是加它之前那一版,点下去只说 `Not Found`。
它看起来像"这条素材找不到了",实际是"这个接口在当前后端上还不存在" ——
两者的下一步完全相反(刷新页面 vs 重启/更新后端),而那个英文单词
指向的恰好是错的那一个。

## 这个文件为什么不要数据库

打的是一条**不存在**的路由,压根走不到任何依赖。和 `test_auth_session.py`
同一个理由:把"错误体长什么样"挂在"测试库起没起来"上面是错的接线。
"""
from __future__ import annotations

import pytest


@pytest.fixture
def api():
    """不接数据库的 TestClient。这里的用例都打不到路由,不需要覆盖任何依赖。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        yield client


def test_unmatched_route_says_what_to_do_instead_of_not_found(api):
    """404 的 message 是中文,而且点名"接口"与"后端版本"。"""
    response = api.post("/api/media/2f1d9c62-0000-0000-0000-000000000000/no-such-action")

    assert response.status_code == 404
    message = response.json()["error"]["message"]
    # 原样透出的英文默认值是这条用例要防的东西
    assert message != "Not Found"
    # 成因只有一种:请求打到了一条这个后端没有的路由。所以话里必须出现
    # "接口",否则运营会把它读成"这条数据不存在"
    assert "接口" in message
    # 而最可能撞见它的场景是前端比后端新 —— 少了这四个字他会去刷新页面,
    # 刷新一百次也不会让那条路由长出来
    assert "后端" in message


def test_wrong_method_gets_its_own_sentence(api):
    """405 与 404 不共用一句话:一个是"没有这条路",一个是"路在方法不对"。"""
    response = api.delete("/api/media/2f1d9c62-0000-0000-0000-000000000000/delete")

    assert response.status_code == 405
    message = response.json()["error"]["message"]
    assert message != "Method Not Allowed"
    assert "请求方式" in message


def test_a_handwritten_chinese_detail_is_never_overwritten(api):
    """兜底**只认 Starlette 自己那两句原文**,不按状态码一刀切。

    业务代码里手写的 `HTTPException(404, "这一版图片集不存在")` 比这里这句
    泛泛的话准得多。只按状态码兜底会把它盖掉 —— 那是退步,所以键里带着原文。
    """
    import asyncio
    import json

    from starlette.exceptions import HTTPException as StarletteHTTPException

    from app.main import app

    handler = app.exception_handlers[StarletteHTTPException]
    response = asyncio.run(
        handler(None, StarletteHTTPException(status_code=404, detail="这一版图片集不存在"))
    )
    assert json.loads(response.body)["error"]["message"] == "这一版图片集不存在"


def test_every_media_write_route_is_actually_mounted():
    """**这条用例本身就是那次事故的复盘。**

    那天 `POST /api/media/{id}/delete` 在源码里是好好的,红的是运行着的进程。
    源码级的守卫救不了那种情况 —— 但它能保证下一次有人重构路由、把某一条
    删掉或改名时,当场就知道,而不是等运营点下去看见一个英文单词。

    读 OpenAPI 而不是真发一次请求:发请求会走进 handler 要数据库,
    于是"这条路在不在"就又挂在"库起没起来"上面 —— 正是这个文件不要库的理由。
    """
    from app.main import app

    paths = app.openapi()["paths"]
    for path in (
        "/api/media/{media_id}/delete",
        "/api/media/{media_id}/quarantine",
        "/api/media/{media_id}/release",
        "/api/media/{media_id}/role",
    ):
        assert path in paths, f"{path} 没挂上"
        assert "post" in paths[path], f"{path} 不是 POST"
