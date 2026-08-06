"""第九批:Celery 任务的故障恢复语义。

本轮**唯一**一条修复,但它是自查发现的,不在 A44/A45 任何一份报告里:

    `run_batch_task` 的 `except Exception` 把 `OperationalError` 一起吞了,
    而 `task_acks_late=True` 下**正常返回也算完成** —— 消息就此消失。

`run_generation_task` 早就修过一模一样的形状(它的注释里写得非常清楚),
批量那一侧漏了。所以这一批的重点同样不是那一处修复,是把它变成一条规则。
"""
from __future__ import annotations

import ast

from tests.pure._helpers import BACKEND_ROOT

TASKS = BACKEND_ROOT / "app" / "tasks"


def _read(path) -> str:
    return path.read_text(encoding="utf-8")


def _celery_tasks() -> dict[str, tuple[str, ast.FunctionDef, str]]:
    """{任务名: (文件名, 函数节点, 装饰器源码)}。"""
    out: dict[str, tuple[str, ast.FunctionDef, str]] = {}
    for path in sorted(TASKS.glob("*.py")):
        source = _read(path)
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.FunctionDef):
                continue
            for deco in node.decorator_list:
                text = ast.get_source_segment(source, deco) or ""
                if ".task(" not in text and "shared_task" not in text:
                    continue
                name = node.name
                for kw in getattr(deco, "keywords", []):
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                        name = kw.value.value
                out[name] = (path.name, node, text)
    return out


def _beat_task_names() -> set[str]:
    source = _read(TASKS / "celery_app.py")
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for _key, value in zip(node.keys, node.values, strict=True):
                if not isinstance(value, ast.Dict):
                    continue
                for k2, v2 in zip(value.keys, value.values, strict=True):
                    if (
                        isinstance(k2, ast.Constant)
                        and k2.value == "task"
                        and isinstance(v2, ast.Constant)
                    ):
                        names.add(v2.value)
    return names


def test_every_task_can_recover_from_a_database_outage():
    """每个 Celery 任务要么**自己重试**,要么**在 beat 里**。

    ## 为什么这是一条规则而不是一次修复

    `task_acks_late=True` 的语义是"执行完成后 ack",而**正常返回也算完成**。
    于是一个把 `OperationalError` 吞掉并正常返回的任务,会让消息静默消失 ——
    没有异常、没有告警,只有一件事停在半路,而界面上它看起来是活的。

    两条出路,各自成立:

        自己重试    用户触发的任务(生成、批量)。没人会再叫它第二次
        在 beat 里  节拍任务。下一拍就是重试,再叠一层反而会在库恢复时
                    让堆积的重试和正常节拍同时到达

    两者都没有的任务,就是一颗静默失败的地雷。
    """
    tasks = _celery_tasks()
    assert tasks, "一个 Celery 任务都没扫到 —— 是扫描坏了"
    beat = _beat_task_names()

    offenders: list[str] = []
    for name, (filename, _node, decorator) in sorted(tasks.items()):
        if name in beat:
            continue
        if "OperationalError" in decorator:
            continue
        offenders.append(f"{filename}::{name}")

    # 健康探针不属于任何一类:它没有要落库的状态,失败就是失败本身
    offenders = [o for o in offenders if not o.endswith("::health.ping")]

    assert not offenders, (
        "这些任务既不自己重试、也不在 beat 里 —— 库抖一下它们就静默消失 -> "
        + "; ".join(offenders)
    )


def test_the_batch_task_reraises_infrastructure_errors_before_the_catch_all():
    """`except OperationalError` 必须排在 `except Exception` **前面**,且原样抛。

    `autoretry_for` 只能看见**抛出函数**的异常。被兜底分支接住的话装饰器
    永远不触发 —— 装饰器改了而这里不改,等于什么都没做。

    **这是本条修复最容易做错的地方**,所以单独钉一条。
    """
    source = _read(TASKS / "batch_tasks.py")
    tree = ast.parse(source)
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "run_batch_task"
    )
    tries = [node for node in ast.walk(fn) if isinstance(node, ast.Try)]
    assert tries, "run_batch_task 里没有 try"

    handlers = tries[0].handlers
    kinds = [
        ast.dump(h.type) if h.type is not None else "bare" for h in handlers
    ]
    operational = next(
        (i for i, k in enumerate(kinds) if "OperationalError" in k), None
    )
    catch_all = next((i for i, k in enumerate(kinds) if "'Exception'" in k), None)
    assert operational is not None, "run_batch_task 不再单独处理 OperationalError"
    assert catch_all is not None, "兜底分支不见了 —— 业务失败会变成整批重试"
    assert operational < catch_all, (
        "OperationalError 排在兜底之后 —— 它永远轮不到,autoretry 不会触发"
    )
    assert any(
        isinstance(node, ast.Raise) and node.exc is None
        for node in ast.walk(handlers[operational])
    ), "OperationalError 分支没有原样抛出去,装饰器看不见它"


def test_business_failures_still_do_not_retry_the_whole_batch():
    """放开重试**只对基础设施故障**,业务失败一次都不许重投。

    整批重试会绕过 FE-304 那个"由运营决定要不要重跑"的入口,
    而每一次重试都可能是一次真金白银的模型调用。
    """
    source = _read(TASKS / "batch_tasks.py")
    tree = ast.parse(source)
    deco = next(
        ast.get_source_segment(source, d)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "run_batch_task"
        for d in node.decorator_list
    )
    assert "autoretry_for=(OperationalError,)" in deco, (
        "autoretry 的范围变宽了 —— 业务失败会被整批重投"
    )
    # 兜底分支必须是"记日志 + 返回",不能抛
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "run_batch_task"
    )
    catch_all = next(
        h
        for h in next(n for n in ast.walk(fn) if isinstance(n, ast.Try)).handlers
        if h.type is not None and "'Exception'" in ast.dump(h.type)
    )
    assert not any(
        isinstance(node, ast.Raise) for node in ast.walk(catch_all)
    ), "业务失败被抛出去了,Celery 会把整批重投"
