"""A45-batch14-24:「每一列都答得出谁写它」这条审计自己的守卫。

## 为什么审计本身需要被守

`tools/audit_column_writers.py` 把 §3.38 那个类从"要有人逐条核 PRD"变成
一条会红的门禁。而门禁自己没有人守时,它有两种安静的失效方式:

    台账烂掉    某一列有了写入点,它的台账条目却留着 ——
                台账从"这些是已知欠账"退化成"这些是我们不看的列"
    范围塌掉    某一处动态 setattr 把全部模型划进"判不了",
                审计输出「0 列答得出」而退出码是 0

第二种是这份审计第一版真实发生过的:全仓四处动态 setattr,其中一处让
40 个模型全部出局。**一份把所有对象都划成判不了的审计,和没有这份审计
完全等价** —— 而它当时是绿的。

所以这里钉三件事:台账条目都有原因、真欠账那一类都有还款日、
以及审计的覆盖面不许塌到地板以下。

## 这一条不测审计"算得对不对"

那件事由 `tools/mutate_batch14_24.py` 的变异验证承担:掐掉某一类写入点的
识别,看审计认不认得出。这里只测**它自己的形状**。
"""
from __future__ import annotations

import ast
import subprocess
import sys

from tests.pure._helpers import BACKEND_ROOT

SCRIPT = BACKEND_ROOT / "tools" / "audit_column_writers.py"


def _audit_module():
    """把审计脚本作为模块载入。**零三方依赖,可以安全 import。**

    它没有模块级副作用(判定全在 `report()` 里,`main()` 由 `__main__` 守着),
    所以 import 它不会像 `mutate_contract_tests.py` 那样当场改写工作树。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("_audit_columns", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module

#: 覆盖面的地板。当前 534 列在射程内、1 个模型判不了。
#:
#: 取整数而不是精确值:加列是常态,精确值会让每一次加列都要来改这个数字,
#: 而那种数字改着改着就没人看了(这个仓库有 19 处这样的数字冻在自己那一刻)。
#: 地板要挡的是**塌方**,不是波动。
COVERAGE_FLOOR = 400
MAX_UNKNOWABLE_MODELS = 3


def _ledger() -> dict[str, str]:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "LEDGER":
            return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "LEDGER" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("`LEDGER` 不见了 —— 审计的台账被删掉或改名了")


def test_every_ledger_entry_says_why():
    """台账每一条都要有原因,而且不许是敷衍的一个词。

    没有原因的条目在三个月后与"我们不看这一列"无法区分,而后者正是这份
    审计要消灭的东西。
    """
    for key, reason in sorted(_ledger().items()):
        assert "." in key, f"台账的键要写成 `Model.col`,而不是 {key!r}"
        assert len(reason) >= 8, f"{key} 的原因太短:{reason!r}"


def test_the_real_debts_in_the_ledger_carry_a_due_stage():
    """台账里凡是写着"欠账 / 未接线要还"的条目,都要有还款日。

    判据取"这一条描述的是**将来要补**还是**本来就不该由应用写**":

        运维灌数据 / 整套特性未接线   不是应用的欠账,不要求还款日
        其余                          是欠账,必须写「还款日:阶段 N」

    没有还款日的欠账有两种下场 —— 被忘掉,或者在还清之后继续吓唬人。
    与 `verify_delivery.check_no_debt_guard_is_past_its_due_stage` 同一套语义。
    """
    for key, reason in sorted(_ledger().items()):
        if "运维灌数据" in reason:
            continue
        if "未接线" in reason and "还款日" not in reason:
            continue
        assert "还款日:阶段" in reason, (
            f"{key} 既不是「运维灌数据」也不是「整套特性未接线」,"
            f"那它是一笔欠账,要写「还款日:阶段 N」。当前原因:{reason!r}"
        )


def test_the_audit_still_covers_most_of_the_schema():
    """审计的射程不许塌到地板以下。

    这条钉的是第一版真实发生过的那种失效:一处动态 `setattr(obj, name, ...)`
    把 40 个模型全部划进"判不了",审计输出「0 列答得出」**而退出码是 0**。

    那种绿是最坏的一种绿 —— 它和"全都查过了"在报告上长得一模一样。
    """
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=SCRIPT.parent.parent,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"审计自己就是红的:\n{proc.stdout}{proc.stderr}"
    text = proc.stdout
    covered = int(text.split(" 列都答得出")[0].strip().split()[-1])
    assert covered >= COVERAGE_FLOOR, (
        f"审计只覆盖到 {covered} 列(地板 {COVERAGE_FLOOR})—— 多半是某处"
        "动态写入把大批模型划进了「判不了」。查那一处,不要调低地板。"
    )
    unknowable = text.count("用 **kwargs 构造") + text.count("用变量名 setattr")
    assert unknowable <= MAX_UNKNOWABLE_MODELS, (
        f"有 {unknowable} 个模型判不了(上限 {MAX_UNKNOWABLE_MODELS})。"
        "判不了的模型是审计的盲区,盲区扩大要有人知道。"
    )


def test_a_ledger_entry_that_stopped_being_true_gets_reported():
    """台账的自净:某一列一旦有了写入点,它的条目要被点名。

    这是台账与白名单的唯一区别。少了它,条目会在还清之后继续留着 ——
    那时它不再是"已知欠账",而是"我们不看这一列"。

    ## 这条为什么直接算,不读源码

    第一版把判定写在 `main()` 里,守卫只能断言源码里有那一段。而读源码的
    断言在判定被搬走时会安静地继续绿 —— §3.37 那条"判据落在别人身上的
    守卫,守不住自己"的同一个形状。所以本批把审计拆成 `report()` + `main()`,
    这里注入一个**已知有写入点**的列,直接看它报不报。
    """
    audit = _audit_module()
    written_column = "MediaAsset.sha256"
    out = audit.report(ledger={written_column: "故意放一条已经不成立的条目"})
    assert written_column in out.stale, (
        f"{written_column} 明明有写入点,台账里放着它却没有被点名 —— "
        "台账失去了自净能力"
    )
    assert not out.ok, "台账里有失效条目时,审计必须是红的"


def test_the_audit_is_registered_as_a_delivery_gate():
    """审计必须挂在 `verify_delivery` 上,不是一个要人记得手动跑的脚本。

    单独存在的审计脚本会在第二个月变成没人跑的脚本。这个仓库对此的处理
    一律是「挂到门禁上,再给门禁加一条盯它有没有被挂」——
    `check_every_frontend_gate_script_is_invoked` 是同一件事的前例。
    """
    verify = (BACKEND_ROOT / "tools" / "verify_delivery.py").read_text(encoding="utf-8")
    tree = ast.parse(verify)
    checks: list[str] = []
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "CHECKS" for t in node.targets
        )):
            continue
        for item in getattr(node.value, "elts", []):
            entry = getattr(item, "elts", [])
            if len(entry) == 2 and isinstance(entry[1], ast.Name):
                checks.append(entry[1].id)
    assert checks, "`verify_delivery.CHECKS` 读不出来了 —— 这条守卫失去了对象"
    assert "check_every_column_can_say_who_writes_it" in checks, (
        "列写入点审计从 `verify_delivery.CHECKS` 上被摘掉了。**函数还在不算数** —— "
        "第一版这里断言的是源码里有没有出现脚本名,而摘出 CHECKS 之后那个名字"
        "仍然出现在函数体里,守卫照样绿。"
    )
    ci = (BACKEND_ROOT.parent / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "audit_column_writers.py" in ci, "CI 里没有这条审计"
