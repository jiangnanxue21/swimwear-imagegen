"""A45-batch31(阶段 6 修复 / F-2):AC-05 那道闸按**表**穷举,不按名字点名。

## 这一批在修什么

上一版的守卫是 `test_a45_batch29_wizard.py` 里的这一条::

    for name, action in (("generate_copy", …), ("build_draft", …), ("export_draft", …)):
        assert "_ensure_action_allowed(" in functions[name]

它证明的是"这三个过了闸",而不是"过闸的是全部"。事实是当时全仓只有那三个
调用点,而 `NextActionCode` 有 22 个成员 —— `POST /image-sets/{id}/approve`
在属性一条都没确认时不会被任何东西拒绝,而向导上那个按钮是灰的。
AC-05 的判据原文要拦的正是这个差。

## 三条守卫合起来的性质:这张表漏一行,测试就红

    一   两张端点表的并集 == 应用里全部写路由,交集为空
         新增一个写端点而没决定它该不该过闸 -> 红
    二   `GATED_ENDPOINTS` 里的每个函数体里真的有一次 `ensure_*` 调用
         写进表却忘了接线 -> 红(这是 §3.43 那一族的形状:算好了没人读)
    三   每个 `NextActionCode` 要么被某个端点执行,要么在
         `ACTIONS_WITHOUT_ENDPOINT` 里给出理由
         新增一个动作码而没人执行它 -> 红

第一条是本批的核心:它把"哪些端点属于前进动作"从**三个人名**换成了
**一张必须穷举的表**。
"""
from __future__ import annotations

import ast

import pytest

from app.api import action_gate
from app.workbench.flow import ACTION_STEP, NextActionCode
from tests.pure._helpers import BACKEND_ROOT

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _write_routes() -> dict[str, tuple[str, str]]:
    """应用里全部写路由 -> (方法, 路径)。键是 `模块名:函数名`。

    **从真的 FastAPI 应用取,不从源码 grep。** grep 会漏掉动态注册的路由,
    而漏掉的那条恰恰是没人想到的那条。

    要走 `original_router`:新版 FastAPI 的 `include_router` 挂进去的是
    `_IncludedRouter` 包装,它自己没有 `.routes`。直接遍历 `app.routes`
    会得到空集 —— 而空集会让下面三条断言**全部假绿**,所以 `write_routes`
    夹具里有一条"一条都没找到就当场失败"。
    """
    from app.main import app

    out: dict[str, tuple[str, str]] = {}
    stack = list(app.routes)
    seen: set[int] = set()
    while stack:
        route = stack.pop()
        if id(route) in seen:
            continue
        seen.add(id(route))
        inner = getattr(route, "original_router", None)
        if inner is not None:
            stack.extend(inner.routes)
            continue
        sub = getattr(route, "routes", None)
        if sub:
            stack.extend(sub)
        endpoint = getattr(route, "endpoint", None)
        methods = getattr(route, "methods", None) or set()
        if endpoint is None or not (methods & WRITE_METHODS):
            continue
        key = f"{endpoint.__module__.rsplit('.', 1)[-1]}:{endpoint.__name__}"
        out[key] = (sorted(methods & WRITE_METHODS)[0], getattr(route, "path", ""))
    return out


@pytest.fixture(scope="module")
def write_routes() -> dict[str, tuple[str, str]]:
    routes = _write_routes()
    assert routes, "一条写路由都没找到 —— 路由枚举本身坏了,后面三条断言会假绿"
    return routes


# ======================================================== 一、按路由穷举


def test_every_write_endpoint_has_been_decided_about(write_routes):
    """**每一个写端点要么过闸,要么写明为什么不过。**

    这是本批的核心断言。上一版没有它,后果是新增一个前进端点永远不会红 ——
    而当时确实有五个前进端点从来没过过闸。

    新增端点时它会红,而让它变绿只有两条路:接闸,或者在
    `UNGATED_ENDPOINTS` 里写一句为什么。两条都要求写的人**做一次决定**,
    这正是它存在的理由。
    """
    gated = set(action_gate.GATED_ENDPOINTS)
    ungated = set(action_gate.UNGATED_ENDPOINTS)

    overlap = gated & ungated
    assert not overlap, f"同时出现在两张表里,判定自相矛盾:{sorted(overlap)}"

    missing = sorted(set(write_routes) - gated - ungated)
    assert not missing, (
        "这些写端点没有出现在任何一张表里 —— 它们该不该过 AC-05 那道闸,"
        f"还没有人做过决定:{missing}"
    )

    stale = sorted((gated | ungated) - set(write_routes))
    assert not stale, f"表里列了不存在的端点(改名或删掉了?):{stale}"


def test_every_ungated_endpoint_says_why(write_routes):
    """豁免必须带理由,而且不能是一句空话。

    空理由下次会被当成"以前就这样"。
    """
    for key, reason in action_gate.UNGATED_ENDPOINTS.items():
        assert reason.strip(), f"{key} 的豁免理由是空的"
        assert len(reason.strip()) >= 6, f"{key} 的豁免理由太短,说不清:{reason!r}"


def test_attribute_repairs_are_not_locked_behind_the_step_they_repair():
    """人工填缺失值与裁决冲突必须能解开属性步,不能被它反锁。

    真库接缝曾经拿同款另一颜色缺图造出这个死锁:属性表给了 material 输入框,
    提交却因「等素材」返回 409。只靠真库用例守会让本地门禁看不见回归,
    所以这里把豁免归属与调用点一起钉住。
    """
    key = "attributes:confirm_attributes"
    assert key in action_gate.UNGATED_ENDPOINTS
    assert key not in action_gate.GATED_ENDPOINTS
    body = _module_functions("attributes")["confirm_attributes"]
    assert "ensure_allowed" not in body, "表里说是解阻编辑,调用点却仍会被上游闸拒绝"


# ======================================================== 二、表里的都真的接了线


def _module_functions(module_name: str) -> dict[str, str]:
    path = BACKEND_ROOT / "app" / "api" / f"{module_name}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name: ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }


def test_every_gated_endpoint_actually_calls_the_gate():
    """写进 `GATED_ENDPOINTS` 的端点,函数体里真的有一次闸调用。

    写进表却忘了接线,是 §3.43 那一族最常见的形状:一张看起来很全的表,
    而它描述的事情没有发生。
    """
    problems: list[str] = []
    for key, action in action_gate.GATED_ENDPOINTS.items():
        module_name, func_name = key.split(":")
        functions = _module_functions(module_name)
        if func_name not in functions:
            problems.append(f"{key}:函数不存在")
            continue
        body = functions[func_name]
        if "ensure_allowed" not in body:
            problems.append(f"{key}:没有调用闸")
            continue
        if action.name not in body:
            problems.append(f"{key}:调的不是 {action.name}")
    assert not problems, f"表说它们过闸,而代码里没有:{problems}"


def test_the_gate_reads_the_decision_layer_and_not_a_local_rule():
    """闸问的是 `wizard.check_action`,不是就地写的一段 if。

    就地写的表现是它与向导上按钮的判据分叉,而两边都不报错。
    (这一条从 batch29 搬过来 —— 闸的实现搬了家,守卫跟着搬。)
    """
    path = BACKEND_ROOT / "app" / "api" / "action_gate.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = {
        n.name: ast.unparse(n)
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
    }
    for name in ("ensure_allowed", "ensure_allowed_for_spu"):
        body = functions[name]
        assert "wizard_rules.check_action(" in body, f"{name} 自己写了一份判定"
        assert "verdict.reason" in body, f"{name} 拒绝时没有把判定层的理由带出去"


def test_the_refusal_is_a_409_and_quotes_the_decision_layer():
    """拒绝走 409,而且那句话里带着判定层给的原因。

    400 会被前端当成"你传错了",而运营什么都没传错 —— 他只是早点了一步。
    """
    src = (BACKEND_ROOT / "app" / "api" / "action_gate.py").read_text(encoding="utf-8")
    assert "http_status=409" in src
    assert "ACTION_LABELS[action]" in src, "409 里没有说这是哪个动作"


# ======================================================== 三、按动作码穷举


def test_every_action_code_is_either_executed_or_explained():
    """**每个 `NextActionCode` 都要有交代。**

    要么某个写端点执行它(在 `GATED_ENDPOINTS` 的值里),要么在
    `ACTIONS_WITHOUT_ENDPOINT` 里写明为什么没有端点。

    漏一个的表现是向导推荐一个动作,而运营点下去没有任何事发生 ——
    一个不出现的按钮没有人会去报缺陷。
    """
    executed = set(action_gate.GATED_ENDPOINTS.values())
    explained = set(action_gate.ACTIONS_WITHOUT_ENDPOINT)

    overlap = executed & explained
    assert not overlap, f"既说有端点又说没有端点:{sorted(c.value for c in overlap)}"

    missing = sorted(
        c.value for c in NextActionCode if c not in executed and c not in explained
    )
    assert not missing, (
        f"这些动作码没人执行,也没有说明为什么:{missing}"
    )

    for code, why in action_gate.ACTIONS_WITHOUT_ENDPOINT.items():
        assert why.strip(), f"{code} 的说明是空的"


def test_the_gated_actions_all_have_a_step():
    """每个过闸的动作码在 `ACTION_STEP` 里有归属步骤。

    没有归属的话 `check_action` 会 KeyError —— 而那会在运营点按钮时
    表现为 500,而不是一句"还不能做"。
    """
    for key, action in action_gate.GATED_ENDPOINTS.items():
        assert action in ACTION_STEP, f"{key} 执行的 {action} 没有归属步骤"


def test_no_forward_action_endpoint_is_only_guarded_by_the_front_end():
    """AC-05 后半句:**不是仅前端置灰。**

    判据落在具体一条上 —— 批准图片集必须过闸。它是上一版漏得最狠的一个:
    图片集是整条链路上唯一一个"批准之后就能导出"的对象,而它的批准端点
    当时对属性状态一无所知。
    """
    assert (
        action_gate.GATED_ENDPOINTS.get("image_sets:approve")
        is NextActionCode.APPROVE_IMAGE_SET
    )
    assert (
        action_gate.GATED_ENDPOINTS.get("workbench:approve_copy")
        is NextActionCode.APPROVE_COPY
    )
    assert (
        action_gate.GATED_ENDPOINTS.get("attributes:extract_attributes")
        is NextActionCode.RUN_EXTRACTION
    )
