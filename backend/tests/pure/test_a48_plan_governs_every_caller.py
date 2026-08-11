"""a48 守卫:`create_task` 的**每一个**调用方都要对方案表态。

## 这一条修的是什么

a47 让生成方案真正接管出图参数,那是对的 —— 但它同时把
`provider` / `model_template_id` / 一轮张数 / 提示词这几个**入参的语义**
改了:从「我说了算」变成「方案在的时候方案说了算」。

改的是 `create_task` 一个函数,受影响的是它**全部**调用方。
而 a47 只跟了一个(HTTP 接口),另一个漏了:

    app/scripts/provider_baseline.py   整个脚本的用途是「固定 seed,
                                       让两边的差异只来自 Provider 本身」。
                                       SPU 上有 ACTIVE 方案时,两条腿的
                                       provider 双双被换成方案里那一个,
                                       而脚本照旧打印一张对比表 ——
                                       **不报错,答案是假的**

这类缺陷的形状和 §3.72 自己在修的那个一模一样:一份记录说 A,实际跑的是 B,
中间没有任何东西报错。区别只在受害者是运营还是排障的人。

## 为什么是「必须显式写」,而不是「必须写成某个值」

两种调用方都合法,而且**答案相反**:

    线上出图    不许绕。方案就是该管它 —— 这正是 a47 修的东西
    基线对比    必须绕。被方案接管的对比不是对比

所以没有一个「正确的默认值」可以判。能判的是**作者有没有意识到这个问题
存在** —— 默认值(`False`)是安全的,但它安静;安静的默认值配上一个
语义刚改过的参数,就是下一个 `provider_baseline`。

写死 `override_plan=` 这个关键字,代价是每个调用方多一行,
收益是**新增第三个调用方时,作者被迫回答一次这个问题**。

## 这条守卫看不见什么

走 HTTP 的调用方(`app/scripts/smoke_test.py` 发 JSON 报文)不在射程里 ——
那是接口的入参,不是这个函数的。smoke 的对策是回读出参并如实报出
「本次由方案接管」,判据在它自己的函数文档里。
"""
from __future__ import annotations

import ast

from tests.pure._helpers import BACKEND_ROOT

APP = BACKEND_ROOT / "app"

#: 生成服务在各调用点的模块别名。`from app.services import generation_service as gs`
#: 与不改名的那种都要认 —— 认少一种就是漏一个调用方,而漏掉的那个正是本批修的。
SERVICE_ALIASES = {"gs", "generation_service"}


def _create_task_calls() -> list[tuple[str, ast.Call]]:
    """全仓 `app/` 下对 `<生成服务>.create_task(...)` 的调用。

    只认属性调用形式。裸 `create_task(...)` 不算:那是模块内部对自己的调用
    (定义处所在的那个文件),它不是"调用方",没有表态的对象。
    """
    found: list[tuple[str, ast.Call]] = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "create_task":
                continue
            if not isinstance(func.value, ast.Name):
                continue
            if func.value.id not in SERVICE_ALIASES:
                continue
            found.append((str(path.relative_to(BACKEND_ROOT)), node))
    return found


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def test_the_guard_is_actually_looking_at_something():
    """先证明这条守卫有射程。

    调用点数一旦归零(改名、换调用形式、模块被搬走),下面两条会**平凡通过**
    并且照样打印 PASS —— 那正是 `audit_source_guards.py` 顶部在讲的那种失效,
    只不过这里的空窗口不是切出来的,是找出来的。
    """
    calls = _create_task_calls()
    assert calls, (
        "app/ 下一个 <生成服务>.create_task(...) 调用都找不到 —— "
        "要么调用形式变了,要么 SERVICE_ALIASES 该补一个别名。"
        "在此之前,本文件其余两条断言不设防"
    )


def test_every_caller_states_its_stance_on_the_plan():
    """§5.2 的入参语义改过一次,所以每个调用方都得回答一次这个问题。"""
    silent = [
        where
        for where, call in _create_task_calls()
        if _keyword(call, "override_plan") is None
    ]
    assert not silent, (
        "这些调用方没写 override_plan=,而 a47 之后这个参数决定了"
        "provider / 模特模板 / 一轮张数 / 提示词到底听谁的:\n  "
        + "\n  ".join(silent)
        + "\n默认值(False,按方案跑)对线上路径是对的,但它安静 —— "
        "而安静的默认值配上一个语义刚改过的参数,就是下一个 provider_baseline"
    )


def test_the_baseline_script_bypasses_the_plan():
    """基线对比必须绕过方案,否则它比的是同一个 Provider 的两条腿。

    这条钉的是**值**,不是形式:`provider_baseline` 的全部意义在于
    「差异只来自 Provider 本身」,而方案会把 `provider` 换掉。
    改回 False 的表现不是报错,是一张两栏写着不同名字、跑的却是同一个
    Provider 的对比表 —— 而人是拿它来决定接哪家的。
    """
    hits = [
        call
        for where, call in _create_task_calls()
        if where.endswith("provider_baseline.py")
    ]
    assert len(hits) == 1, f"provider_baseline.py 里的调用点应当只有一个,实际 {len(hits)}"

    value = _keyword(hits[0], "override_plan")
    assert isinstance(value, ast.Constant) and value.value is True, (
        "provider_baseline.py 必须显式 override_plan=True。"
        "被方案接管的基线不是基线:两条腿会跑在同一个 Provider 上,"
        "而脚本照旧打印一张对比表"
    )
