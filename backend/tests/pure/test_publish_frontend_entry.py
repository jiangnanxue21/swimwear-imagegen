"""发布链路的前端入口门禁(台账 B-02 / A45-#8)。

## 为什么单独一个文件,而不是并进 `test_a45_batch8_fixes.py`

那个文件盯的是「唯一入口被绕过」这一类(admin 头、配置读取、saveBlob 复制件)。
这里盯的是另一类,而且是本仓返工里更贵的那一类:

    后端做完了,但这条链路没有任何人能用。

两者的失效方式不一样。前者是有两条路,走错一条;后者是**一条路都没有**,
而且不报错 —— 接口测试全绿、门禁全过、STATUS.md 写着"已完成",
只有真的坐下来点一遍才会发现动线在导出那一步就断了。

所以这条门禁问的问题也不一样:不是"实现对不对",是"点得到吗"。

## 这一类的判据

一个端点算「已交付」要同时满足三件事 —— 有客户端、有页面、**路由挂上了**。
只做前两样是这类返工最常见的形态:页面写完了,忘了挂路由或忘了加侧栏项,
于是它在仓库里存在、在界面上不存在。三段分开断言就是为了让这一步漏掉时
测试能指出漏的是哪一段。
"""
from __future__ import annotations

import re

from tests.pure._helpers import BACKEND_ROOT

PROJECT = BACKEND_ROOT.parent
FRONTEND = PROJECT / "frontend" / "src"


def _read(path) -> str:
    return path.read_text(encoding="utf-8")


def test_publish_endpoints_have_a_frontend_entry() -> None:
    """`/publish/*` 必须有前端入口(台账 B-02 / A45-#8)。

    后端从任务 18 起就有完整的发布链路(提交 / 清单 / 详情 / 刷新 / 下架 /
    清理清单),而前端曾长期零消费 —— 后果不是"少一个页面":上架是这条流水线的
    **终点**,没有入口意味着人工评测只能走到导出为止,再往后要拿 curl 打接口,
    而运营不会。

    最后一段(ExportTab 的按钮)不是锦上添花:第一次提交之前发布清单页是空的,
    没有那个按钮,运营打开发布页看到的是一张空表,不知道从哪儿开始。
    """
    api = FRONTEND / "api" / "publish.ts"
    page = FRONTEND / "pages" / "PublishPage.tsx"
    assert api.exists(), "没有 publish 的前端客户端"
    assert page.exists(), "没有发布页"

    client = _read(api)
    for endpoint in ("/publish/listings", "/publish/cleanup/inventory", "/submit"):
        assert endpoint in client, f"前端没有消费 {endpoint}"

    app = _read(FRONTEND / "App.tsx")
    assert 'path="/publish"' in app, "发布页没有挂路由 —— 页面存在但走不到"
    assert "发布上架" in app, "侧栏里没有发布入口 —— 只有知道 URL 的人进得去"

    export_tab = _read(FRONTEND / "components" / "workbench" / "ExportTab.tsx")
    assert "/publish?product_id=" in export_tab, (
        "导出页没有通向发布的入口 —— 第一次提交之前清单页是空的,运营找不到从哪儿开始"
    )


def test_publish_page_does_not_second_guess_the_backend() -> None:
    """发布页只渲染后端算好的结论(CLAUDE.md 硬规则 4)。

    发布链路是这条规则最容易破的地方:"现在到底怎么样了"散在四张表里
    (listing / attempt / outbox / rejection),而最贵的那种拼错已经写在
    `workflows/publish_view.py` 顶部 —— 一次退避耗尽落进 DEAD 的提交,
    listing 仍写着 SUBMITTING、attempt 仍写着 IN_FLIGHT,只看这两样的界面
    会说「正在处理」,而实际上什么都不会再发生,运营会一直等下去。

    后端为此把结论算好了(`display_status` / `next_action` / `blocking_reasons` /
    `allowed_actions`)。这条测试盯的是**前端没有长出第二份判定**:
    按钮可用性读 `allowed_actions`,状态文案读 `display_status_label`。

    最后那条正则钉的是"没有第二张文案表"。它是这里唯一一条形态断言,
    留着的理由是这张表一旦出现就必然与后端漂移,而漂移不会有任何报错 ——
    只会在某天让界面显示一个后端已经不再产生的状态。
    """
    page = _read(FRONTEND / "pages" / "PublishPage.tsx")

    assert "allowed_actions.map(" in page, "按钮不是从 allowed_actions 渲染的"
    assert "display_status_label" in page, "状态文案没用后端给的那句"
    assert "blocking_reasons" in page, "没有展示后端给的阻断原因"

    assert not re.search(r"(SUBMITTING|IN_FLIGHT)\s*:\s*['\"][^'\"]*[\u4e00-\u9fa5]", page), (
        "发布页自己建了一张状态->中文的表 —— 那会和后端的 display_status_label 漂移"
    )
