"""`core/clock.py` 的「收敛没有做完」台账必须和真实源码对得上(A45-batch17-2)。

## 它修的是哪一种失效

那份台账是一张**待办清单**:上面每一行是一处还没收敛的 `datetime.now(UTC)`,
下一个来做时间收敛的人照着它开工。清单少一行的后果不是"文档过期",是
**那一处永远不会被收敛** —— 没有人会去找一个清单上不存在的东西。

它已经发生过两次,而且是两种不同的漏法:

    A27 那轮   手数,漏了一处。于是台账里补了一句"别手数,用 AST"
    本批之前   清单建成之后新增/漏计了两处(`cleanup_service.py` /
               `model_license.py`),而那句"用 AST"只写在注释里,**没有人在跑它**

第二次尤其值得记:补救措施本身(那句"用 AST 数")是对的,它只是**不是一个
会自己执行的东西**。规矩要么是不变式,要么是散文(§3.26)。

## 为什么钉的是"两份真相一致",不是"数字等于 14"

§3.31:点名现状的守卫会因为进步而变红,而那种红会训练人去改守卫。
下面这条**容许**清单变短(有人收敛了一处)、也容许变长(有人在新模块里
写了一处,虽然硬规则 4 不许),它只拒绝**清单和源码对不上**。

也就是说:收敛一处 + 更新清单 = 绿;收敛一处 + 不更新清单 = 红。
后者正是这份台账作为待办清单失去意义的那一刻。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from tests.pure._helpers import BACKEND_ROOT, window

APP = BACKEND_ROOT / "app"
CLOCK = APP / "core" / "clock.py"

#: 台账那一行的形状:`    workbench/platform_service.py   5 处`
#: 后面允许跟注释(现有清单里有一行带着 `← 刻意保留,见下`)
_LEDGER_LINE = re.compile(r"^\s{4}(\S+\.py)\s+(\d+)\s*处")


def _declared() -> dict[str, int]:
    """台账自己声明的清单。**从 `clock.py` 的 docstring 里读,不另存一份。**

    另存一份的话,两份都是"我们说的",而真相在源码里 —— 那种守卫钉的是
    两句话一致,不是话和事实一致。
    """
    text = CLOCK.read_text(encoding="utf-8")
    block = window(
        text,
        "全仓仍有 14 处直接",
        "**数这个数的办法**",
        what="clock.py 的收敛清单",
    )
    out: dict[str, int] = {}
    for line in block.splitlines():
        hit = _LEDGER_LINE.match(line)
        if hit:
            out[hit.group(1)] = int(hit.group(2))
    return out


def _actual() -> dict[str, int]:
    """按 AST 现数一遍。**排除 `clock.py` 自己那一处**(它是入口,不是待办)。"""
    out: dict[str, int] = {}
    for path in sorted(APP.rglob("*.py")):
        if path == CLOCK:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - 语法错另有门禁管
            continue
        count = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "now"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "datetime"
        )
        if count:
            out[str(Path(path).relative_to(APP)).replace("\\", "/")] = count
    return out


def test_the_ledger_lists_exactly_the_sites_that_are_really_there():
    """清单里的文件集合与真实的文件集合必须逐个对得上。

    两个方向都要报:

        清单里有、源码里没有   有人收敛了却没划掉 —— 下一个人去找一个不存在的东西
        源码里有、清单里没有   **本批修的那一种** —— 那一处从此不在任何人的视野里
    """
    declared, actual = _declared(), _actual()
    assert declared, "台账那一段读不出任何一行 —— 清单的格式变了,而这条守卫没跟着改"

    only_declared = sorted(set(declared) - set(actual))
    only_actual = sorted(set(actual) - set(declared))
    assert not only_declared, (
        f"台账上这些文件已经没有 `datetime.now(...)` 了:{only_declared} —— "
        "收敛完要把行划掉,否则下一个人会去找一个不存在的东西"
    )
    assert not only_actual, (
        f"这些文件里有 `datetime.now(...)` 但不在台账上:{only_actual} —— "
        "不在清单上的那一处不会被任何人收敛,而硬规则 4 说新代码不许写它"
    )


def test_the_per_file_counts_line_up_too():
    """逐文件的处数也要对。

    只对文件名不对数量的话,一个文件里从 1 处变成 3 处照样是绿的 ——
    而"这个文件已经在清单上了"恰恰是最容易让人放过它的理由。
    """
    declared, actual = _declared(), _actual()
    drift = {
        filename: (declared[filename], actual[filename])
        for filename in declared
        if filename in actual and declared[filename] != actual[filename]
    }
    assert not drift, f"台账声明的处数和实际对不上(文件: (清单, 实际)):{drift}"


def test_the_headline_number_is_the_sum_of_the_ledger():
    """正文里那个总数必须等于清单各行之和。

    它是被引用最多的一个数(根 `CLAUDE.md` 硬规则 4 就在复述它),
    而正文和表格分头维护正是它上一次说错的方式。
    """
    text = CLOCK.read_text(encoding="utf-8")
    hit = re.search(r"全仓仍有 (\d+) 处直接", text)
    assert hit, "正文里那句「全仓仍有 N 处」不见了 —— 引用它的下游文档会失去校对对象"
    assert int(hit.group(1)) == sum(_declared().values()), (
        f"正文写的是 {hit.group(1)} 处,清单各行加起来是 {sum(_declared().values())} 处"
    )


def test_the_forbidden_spelling_is_really_gone_everywhere_but_the_entry_point():
    """那个被点名禁止的写法只许出现在入口自己身上。

    台账上一版写着"全仓唯一一处已由 A34 收掉",而 `model_license.py:53`
    当时原样长着它 —— 一句**说缺口已关**的假话(§3.42 定义的最贵那一类)。
    这条守卫是那句话的替代品:它不需要人记得,也不会说谎。
    """
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            # `datetime.now(...).replace(...)` —— 只看形状,不看参数写法:
            # `UTC` / `timezone.utc` / `tz=UTC` 是同一件事的三种拼法
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "replace" or not isinstance(node.func.value, ast.Call):
                continue
            inner = node.func.value.func
            if (
                isinstance(inner, ast.Attribute)
                and inner.attr == "now"
                and isinstance(inner.value, ast.Name)
                and inner.value.id == "datetime"
            ):
                offenders.append(
                    f"{Path(path).relative_to(APP).as_posix()}:{node.lineno}"
                )
    # 不钉行号:那是做法,而且改一行注释就会让这条守卫变红,红完之后
    # 最省事的消法是改守卫。钉的是"只在入口文件里,且只有一处"
    outside = [o for o in offenders if not o.startswith("core/clock.py:")]
    assert not outside, (
        f"`datetime.now(...).replace(...)` 出现在入口之外:{outside} —— "
        "这正是 `utc_now()` 存在的理由,写在别处就是让 33 个地方各自漂移的那个写法"
    )
    assert len(offenders) == 1, (
        f"入口文件里有 {len(offenders)} 处这个写法({offenders}) —— "
        "`utc_now()` 的全部意义是让它只存在一份"
    )
