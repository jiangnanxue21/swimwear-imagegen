"""a64 守卫:dataclass 字段审计(`tools/audit_dataclass_fields.py`)。

## 这条门禁的来路

`AiTestRecord.prompt_template_version` 声明了、串进了 `as_row_kwargs()`、落到了
模型的那一列上 —— **而两个构造器一个都没传过它**,恒为 NULL,存在六轮,每轮
全套门禁都绿。它是被"带着一条反思去翻渲染逻辑"偶然翻到的,不是被防线找到的。

两道专门为此存在的防线都没看见:`audit_column_writers.py` 判「有没有写入点」
(有),a57 那条守卫判**键集**覆盖模型的列(键在、值一直是 None)。
**键在、值恒空**,是两侧测试之间的一道缝。

## 这里钉的是"门禁自己得能抓住它的来路"

一条抓不到自己motivating case 的门禁,是本仓反复记的那种"绿着的守卫" ——
它看起来在守,而它守的不是那件事。所以下面第一条直接把 a63 的修复退回去,
断言审计当场变红。
"""
from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.pure._helpers import offline_gate_targets

BACKEND = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND / "tools" / "audit_dataclass_fields.py"
PROJECT = BACKEND.parent


def _run(cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(cwd / "tools" / "audit_dataclass_fields.py")],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=600,
    )


def test_the_audit_is_green_on_this_tree():
    proc = _run(BACKEND)
    assert proc.returncode == 0, f"审计自己就是红的:\n{proc.stdout}{proc.stderr}"


def test_the_audit_catches_the_defect_that_motivated_it():
    """**把 a63 的修复退回去,审计必须当场变红。**

    一条抓不到自己来路的门禁,和一条什么都不守的门禁,在 CI 上长得一模一样。
    """
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "backend"
        shutil.copytree(BACKEND, work, ignore=shutil.ignore_patterns("__pycache__"))
        target = work / "app" / "workflows" / "ai_test_record.py"
        source = target.read_text(encoding="utf-8")
        anchor = "        prompt_template_version=_int_version(template_version),\n"
        assert source.count(anchor) == 1, "锚点过期 —— 这条自检什么都没验"
        target.write_text(source.replace(anchor, "", 1), encoding="utf-8")
        proc = _run(work)
    assert proc.returncode != 0, "退回 a63 的缺陷之后审计仍然是绿的"
    assert "AiTestRecord" in proc.stdout
    assert "prompt_template_version" in proc.stdout


def test_the_baseline_is_locked_in_both_directions():
    """补上了却忘了调基线 —— 留着虚高的基线等于给未来的新债留缝。"""
    source = SCRIPT.read_text(encoding="utf-8")
    assert "基线过期" in source, "只拦涨不拦降 —— 那是半个棘轮"
    assert "shrank" in source


def test_the_audit_counts_every_way_a_field_can_get_a_value():
    """漏掉任何一种赋值形态都是**假红**,而假红最省事的消法是把整条门禁删掉。

    四种:构造点位置参数 / 具名参数 / 构造后属性赋值 / `dataclasses.replace`。
    `**kwargs` 保守起见算作全部传过 —— 反过来会让一整批配置类假红。
    """
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    body = ast.unparse(tree)
    assert "ast.AugAssign" in body, "没认 `obj.x += ...`"
    assert "'replace'" in body or '"replace"' in body, "没认 dataclasses.replace"
    assert "given[func].update(order)" in body, "没把 **kwargs 算作全部传过"


def test_the_audit_says_what_it_cannot_see():
    """射程要写出来。**一个被说大的能力比没有能力更贵**(a61 那条同样的理由)。"""
    # 判的是**它每次运行都会说**,不是"源码里提到过"。写在 docstring 里的射程
    # 没人读得到 —— 跑门禁的人看到的只有 stdout。第一版按源码子串判,
    # 而那串字在 docstring 与输出里各有一份,改坏输出那处照样绿
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    printed = "".join(
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print"
    )
    assert "那是数据流" in printed, "射程只写在文档里 —— 跑门禁的人看不到"
    assert "只扫 app/" in printed, "没在输出里说清测试里的构造不算"


def test_the_gate_is_wired_into_all_three_places():
    """硬规则第 3 条:加一条门禁 = 改三个地方。"""
    makefile = (PROJECT / "Makefile").read_text(encoding="utf-8")
    assert "audit-dataclass-fields:" in makefile
    assert "audit-dataclass-fields" in offline_gate_targets(makefile), "没进 check-offline"
    ci = (PROJECT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "audit_dataclass_fields.py" in ci, "CI 没跑它"
    delivery = (BACKEND / "tools" / "verify_delivery.py").read_text(encoding="utf-8")
    assert '"audit_dataclass_fields.py"' in delivery, "门禁清单表里没登记"
