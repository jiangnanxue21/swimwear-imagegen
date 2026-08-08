#!/usr/bin/env python3
"""变异脚本的锚点体检:每一条 `old` 在它的目标文件里是不是**恰好出现一次**。

## 它钉的是哪一件事

失锚的变异**不报错、不变红,只是安静地什么都没验**。而失锚是这个仓库里
最常发生的一件事 —— 任何人重构被变异的那段源码都会触发它(A45-batch14-2
的 F3 重构 `call_budget` 时,一次让 M33/M34 同时失锚)。

变异脚本自己发现不了这件事:它跑一次要复制几十份工作树、跑几十遍套件,
十几分钟。所以它不进 CI(见 `mutate_batch14.py` 顶部),于是锚点可以
过期很久没人知道。这份审计只读文件、零子进程,**跑完不到一秒**,
补的正是那个空档。

## 为什么是「解析」而不是「执行」

理由来自一份**已经退役**的脚本(`tools/mutate_contract_tests.py`,batch14-3 退役,
今天不在树里)。它**没有 `if __name__ == "__main__"` 保护** —— 变异循环写在
模块顶层。`import` 它一次,就是当场改写真实工作树里的 `App.tsx` /
`TodayPage.tsx` / `flow.py`,再起 18 个子进程跑测试。
**一份「审计工具」把被审对象跑起来,这本身就是事故。**

退役不等于理由过期:今天在树里的 23 份脚本都补了 `__main__` 保护,
但这份审计的口径不能建立在「大家都记得加那一行」上面。

即便三份脚本将来都补上了 `__main__` 保护,执行仍然是错的口径:
审计要能在这些脚本**依赖缺失、甚至自己有 bug** 的时候照样给出答案,
而 import 会把被审对象的每一个 import 和每一处模块级副作用都拖进来。
`ast` 只要求它语法合法。

## 两种形状,都认

    MUTATIONS  (编号, 一句话, 路径, 原文, 替换成)      路径基准 backend/
    CASES      (一句话, 路径, (原文, 替换成), 测试名)   路径基准 项目根

`CASES` 是 A8/A9 那一轮的老形状。**今天全仓零个脚本用它。**

这里原来写的是「今天只剩 `mutate_contract_tests.py` 用它,不认它就等于漏掉
18 条锚点」—— 那句话在 batch14-3 退役该脚本、batch14-4 把那 18 条按
`MUTATIONS` 形状造回 `mutate_batch14_4.py` 之后就**不成立了**,却一直挂在这里
替这段分支背书。**一段被失实理由养活的代码,比一段没有理由的代码更难删** ——
下一个人读到「不认它就漏 18 条」会直接放过它。

保留而不是删除,理由改成真的那一条:`CASES` 比 `MUTATIONS` 多一列
`dispatch`(点名要跑哪个测试函数),而 `dispatch_at` / `dispatch_module` 那套
「点名的测试还在不在」的检查是**按 `dispatch_at is not None` 泛化写的**,
不是 `CASES` 专用。删掉 `CASES` 就删掉了这套检查今天唯一的形状证据,
而它挡的是比失锚更隐蔽的一类(见下文)。**代价写明:这条分支今天没有用户,
`audit_doc_refs.py` 之外没有任何东西会在它烂掉时说话。**

## 它顺带补上了两份老脚本缺的那道闸

只有 `mutate_batch14_2.py` 用 `text.count(old) != 1` 拒绝不唯一的锚点
(batch13-3 的 M2 就是被这条拦下的:注释里也含那串字)。另外两份是
`if old not in text` 和 `assert old in original` —— **零次会被发现,
两次不会**,然后 `replace(old, new, 1)` 悄悄改掉第一处,而那一处
可能是注释。这里对三份一视同仁地要求恰好一次。

## 用法

    python3 tools/audit_anchors.py        # 或 make audit-anchors

零三方依赖,只读文件。退出码非零 = 有锚点需要重新对准。
"""
from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 与 `run_pure_tests.py` 共用同一份「过滤器挑中哪些文件」的判定。
# `suite_filter` 刻意做成没有任何模块级副作用的小模块 —— 直接 import
# `run_pure_tests` 是不行的,它在顶层 `mkdtemp()` 并写环境变量,
# 而这份审计的卖点是"只读文件、零子进程、不到一秒"。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from suite_filter import describe, select  # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[2m",
    "\033[0m",
)

#: 扫描时跳过的目录。与 `verify_delivery.py` 的 `_SKIPPED_DIRS` 同一份用途,
#: 刻意不 import 它 —— 那会让这份工具依赖另一份工具的内部名字
SKIPPED_DIRS = frozenset({"node_modules", ".git", "dist", "__pycache__", "venv", ".venv"})


class Unreadable(Exception):
    """这个字面量静态读不出来。**当作失败**,不当作"跳过"。

    读不出来的行 = 没有被审计的行。把它算成通过,这份工具就变成了
    它自己要防的那种东西:看起来在保护。
    """


@dataclass(frozen=True)
class Shape:
    """一种变异表的形状。

    加第三种形状时只动这张表 —— 不是在 `main()` 里加一条 `if`。
    """

    #: 模块级变量名,也是识别依据
    table: str
    #: 元组宽度。不匹配就报错,而不是尽力而为地猜 ——
    #: 形状变了而审计还在按老宽度解包,读出来的"锚点"会指向别的字段
    arity: int
    #: 路径基准,相对项目根。空串 = 项目根本身
    base: str
    #: 拼给人看的那几列(下标)
    label_at: tuple[int, ...]
    #: 目标文件路径在哪一列
    path_at: tuple[int, ...]
    #: `old` 在行里的位置。元组 = 逐层下钻(`CASES` 把它嵌在第 3 列里)
    old_at: tuple[int, ...]
    #: `new` 的位置
    new_at: tuple[int, ...]
    #: 这一行点名要跑的测试函数在哪一列;`None` = 这种形状跑的是整个套件
    dispatch_at: tuple[int, ...] | None = None


SHAPES: dict[str, Shape] = {
    "MUTATIONS": Shape(
        table="MUTATIONS",
        arity=5,
        base="backend",
        label_at=(0, 1),
        path_at=(2,),
        old_at=(3,),
        new_at=(4,),
    ),
    # A45-batch20。第三种形状,多的那一列是**运行器**,不是测试函数名。
    #
    # 存在的理由:5-2 的守卫分在两个运行器上(纯层 + 真库),而
    # 「`build_draft` 有没有把颜色维违规拼进 `violations`」纯层看不见。
    # 一张表里的行因此要各自说明自己该由谁跑。
    #
    # 不复用 `MUTATIONS` 的 5 列、把运行器塞进描述里,是因为那样锚点审计
    # 读到的仍是 5 列、而脚本按 6 列解包 —— 两边对同一张表的宽度理解不同,
    # 表现是审计说"锚点全对"而脚本在解包时静默取错列。
    "RUNNER_MUTATIONS": Shape(
        table="RUNNER_MUTATIONS",
        arity=6,
        base="backend",
        label_at=(0, 1),
        path_at=(2,),
        old_at=(3,),
        new_at=(4,),
    ),
    "CASES": Shape(
        table="CASES",
        arity=4,
        base="",
        label_at=(0,),
        path_at=(1,),
        old_at=(2, 0),
        new_at=(2, 1),
        dispatch_at=(3,),
    ),
}


@dataclass(frozen=True)
class Anchor:
    script: Path
    table: str
    row: int
    label: str
    rel: str
    target: Path
    old: str
    new: str
    #: 这一行点名要跑的测试函数(`CASES` 形状才有)
    dispatch: str | None = None
    #: 它所在的模块(点号路径),从脚本里读出来的
    dispatch_module: str | None = None


# ================================================================ 点名的测试还在不在
#
# 锚点对得上、变异也改进去了,**而那条测试早就不叫这个名字了** ——
# 这一行照样什么都没验,并且比失锚更隐蔽:
#
# 已退役的 `mutate_contract_tests.py`(batch14-3)当年的 `run_one()` 用
# `getattr(m, name)()` 派发,名字没了就是 `AttributeError`,子进程退出码 1,
# 而它按 `rc != 0` 判定"响了"。**测试被删掉,报告上写的是「被抓住」。**
# 那份脚本今天不在树里,这段检查也就没有活着的调用方 —— 留着是因为
# 它挡的失效方式与形状无关(见上文 `CASES` 那一段的保留理由)。
#
# 这正是 `mutate_batch14.py` 顶部记的那个教训("只看红绿的变异验证会把
# 假红当成证据")在老脚本上的原样重演 —— 新脚本补了 crashed 检测,
# 而它的 marker 名单里也没有 AttributeError。

_IMPORT_MODULE = re.compile(r"import_module\(\s*['\"]([\w.]+)['\"]")

#: 点号模块名可能落在哪个根下面。脚本自己往 sys.path 塞的就是这两个之一
_IMPORT_ROOTS = ("backend", "")


def _dispatch_module_of(source: str) -> str | None:
    """从脚本源码里找它 import 哪个测试模块。

    刻意用正则而不是 AST:`mutate_contract_tests.py` 把整段派发代码写在
    一个 f-string 模板里,`import_module(...)` 根本不是这份脚本的 AST 节点。
    """
    found = _IMPORT_MODULE.search(source)
    return found.group(1) if found else None


@lru_cache(maxsize=None)
def _defined_in(module: str) -> frozenset[str] | None:
    """模块里定义了哪些顶层函数。模块找不到 -> `None`。"""
    relative = Path(*module.split(".")).with_suffix(".py")
    for root in _IMPORT_ROOTS:
        path = (PROJECT_ROOT / root / relative) if root else (PROJECT_ROOT / relative)
        if path.is_file():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError:
                return None
            return frozenset(
                node.name
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
    return None


# ================================================================ 静态求值


def _literal(node: ast.AST, consts: dict[str, str]):
    """把字面量节点变成 str 或嵌套 tuple。求不出来就抛 `Unreadable`。

    认这几种:字符串常量、模块级字符串常量的名字引用、`+` 拼接、
    元组/列表。**不认 f-string 和函数调用** —— 那些的值取决于运行时,
    而这份工具的前提是不运行。
    """
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return node.value
        raise Unreadable(f"不是字符串常量:{node.value!r}")
    if isinstance(node, ast.Name):
        if node.id in consts:
            return consts[node.id]
        raise Unreadable(f"`{node.id}` 不是模块级字符串常量")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left, consts), _literal(node.right, consts)
        if isinstance(left, str) and isinstance(right, str):
            return left + right
        raise Unreadable("`+` 的两边不都是字符串")
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(_literal(e, consts) for e in node.elts)
    raise Unreadable(f"读不出来的表达式:{type(node).__name__}")


def _dig(row: tuple, path: tuple[int, ...], where: str) -> str:
    value: object = row
    for step in path:
        if not isinstance(value, tuple) or step >= len(value):
            raise Unreadable(f"{where}:第 {step} 项取不到")
        value = value[step]
    if not isinstance(value, str):
        raise Unreadable(f"{where}:取到的不是字符串")
    return value


def _assigned_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        # `MUTATIONS: list[tuple[...]] = [...]` 走的是这一支
        return [node.target.id]
    return []


@lru_cache(maxsize=None)
def _pure_test_files() -> tuple[str, ...]:
    """`tests/pure/` 下的用例文件名。挑选规则见 `tools/suite_filter.py`。"""
    pure = PROJECT_ROOT / "backend" / "tests" / "pure"
    if not pure.is_dir():
        return ()
    return tuple(sorted(p.name for p in pure.glob("test_*.py")))


def _suite_filter_of(tree: ast.Module, consts: dict[str, str]) -> str | None:
    for node in tree.body:
        for name in _assigned_names(node):
            if name == "SUITE_FILTER":
                try:
                    value = _literal(getattr(node, "value", None), consts)
                except (Unreadable, TypeError):
                    return None
                return value if isinstance(value, str) else None
    return None


def check_suite_filter(script: Path, tree: ast.Module, consts: dict[str, str]) -> str | None:
    """跑整套的那种脚本,过滤器必须**恰好**挑中一个套件。

    判定不在这里 —— 用的是 `tools/suite_filter.py`,和 `run_pure_tests.py`
    同一份。原来这里抄了一句 `suite in name`,靠文档字符串写「只能照着它判,
    不能自己发明一套」维持同步;而"两处各抄一份、靠注释保持一致"正是
    这个仓库反复出问题的地方,注释拦不住重构。

    ## 零个:平凡通过

    这是失锚的**镜像**。锚点没了,变异改不进去,套件照常绿 -> 报 GREEN,
    脚本退出非零,人看得见。而过滤器一个文件都挑不中时,套件里一条用例
    都没有 -> 平凡通过 -> **每一条变异都报 GREEN**,同样退出非零,
    但报告会指向"守卫不咬人",让人去改守卫 —— 而真正坏掉的是过滤器。

    ## 多个:归因是假的(batch14-4 §五之二)

    `mutate_batch14.py` 的 `"a45_batch14"` 同时挑中 5 个套件。耗时线性上涨
    是看得见的那一半;看不见的那一半是**这条变异到底被谁抓住的**:
    它可以因为 `test_a45_batch14_4_fixes.py` 里的守卫而变红,报告却记在
    `mutate_batch14.py` 头上。于是"我这条守卫咬人"是假的,而删掉那条
    真正咬人的守卫时,变异仍然红着,没有任何人会发现。

    与 F7 是同一种病:**红是真的,红的原因不是。**
    """
    suite = _suite_filter_of(tree, consts)
    if suite is None:
        return None
    matched = select(_pure_test_files(), suite)
    if not matched:
        return (
            f"SUITE_FILTER = {suite!r}({describe(suite)})在 tests/pure/ 里"
            f"挑不中任何用例文件 —— 套件会平凡通过,**每一条变异都会报 GREEN**,"
            f"而坏掉的是过滤器不是守卫"
        )
    if len(matched) > 1:
        return (
            f"SUITE_FILTER = {suite!r}({describe(suite)})挑中了 {len(matched)} 个套件:"
            f"{', '.join(matched)} —— 变异可能被**别的套件**里的守卫抓住,"
            f"而报告会记在这份脚本头上(归因是假的),顺带每条变异白跑 "
            f"{len(matched) - 1} 份。改成精确文件名,例如 "
            f"SUITE_FILTER = {matched[0]!r}"
        )
    return None


# ================================================================ 读一份脚本


def read_anchors(script: Path) -> tuple[list[Anchor], list[str]]:
    """解析一份变异脚本,返回(锚点, 这份脚本自身的问题)。"""
    problems: list[str] = []
    try:
        tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
    except SyntaxError as exc:
        return [], [f"语法错误,解析不了:{exc}"]

    consts: dict[str, str] = {}
    anchors: list[Anchor] = []
    seen_tables: list[str] = []
    dispatch_module = _dispatch_module_of(script.read_text(encoding="utf-8"))

    # 按 body 顺序单趟走:与 Python 自己的绑定顺序一致 ——
    # 两趟会让"在表后面才定义的名字"通过审计,而它在运行时是 NameError
    for node in tree.body:
        for name in _assigned_names(node):
            value = getattr(node, "value", None)
            if value is None:
                continue
            if name not in SHAPES:
                try:
                    resolved = _literal(value, consts)
                except Unreadable:
                    continue
                if isinstance(resolved, str):
                    consts[name] = resolved
                continue

            shape = SHAPES[name]
            seen_tables.append(name)
            if not isinstance(value, (ast.List, ast.Tuple)):
                problems.append(f"`{name}` 不是列表字面量,读不出锚点")
                continue
            if not value.elts:
                problems.append(f"`{name}` 是空表 —— 一条都没验")
                continue

            for index, element in enumerate(value.elts):
                where = f"{name}[{index}]"
                if not isinstance(element, ast.Tuple):
                    problems.append(f"{where}:不是元组")
                    continue
                if len(element.elts) != shape.arity:
                    problems.append(
                        f"{where}:{len(element.elts)} 列,不是 {shape.arity} 列 —— "
                        f"形状变了就要改 SHAPES,按老宽度解包会读到别的字段"
                    )
                    continue
                try:
                    row = _literal(element, consts)
                    label = " ".join(_dig(row, (i,), where) for i in shape.label_at)
                    rel = _dig(row, shape.path_at, where)
                    old = _dig(row, shape.old_at, where)
                    new = _dig(row, shape.new_at, where)
                    dispatch = (
                        _dig(row, shape.dispatch_at, where) if shape.dispatch_at else None
                    )
                except Unreadable as exc:
                    problems.append(f"{where}:{exc}")
                    continue

                base = PROJECT_ROOT / shape.base if shape.base else PROJECT_ROOT
                anchors.append(
                    Anchor(
                        script=script,
                        table=name,
                        row=index,
                        label=label,
                        rel=rel,
                        target=(base / rel).resolve(),
                        old=old,
                        new=new,
                        dispatch=dispatch,
                        dispatch_module=dispatch_module,
                    )
                )

    suite_problem = check_suite_filter(script, tree, consts)
    if suite_problem:
        problems.append(suite_problem)

    if not seen_tables:
        # 恒绿的守卫与"一条都没匹配上的不变式"是同一类问题
        # (batch13 的 M9、batch13-3 的 M11)。一份没有已知变异表的
        # `mutate_*.py` 只有两种可能:新形状,或者根本不是变异脚本 ——
        # 两种都要有人看一眼,不能当成"审计通过"
        problems.append(
            "没有找到任何已知形状的变异表(认得的是:"
            + "、".join(sorted(SHAPES))
            + ")—— 新形状要加进 SHAPES,否则这份脚本一条锚点都没被审"
        )
    return anchors, problems


# ================================================================ 查一条锚点


@dataclass(frozen=True)
class Verdict:
    """一条锚点的问题。

    `kind` 是归类依据:同一份脚本里同一类只解释一遍,后面只列行号。
    18 条一模一样的长解释会把真正需要逐条看的那几条淹掉 ——
    与 F2 给 `MissingEvidenceNotice` 定的"同一个动作说一遍就够"同一条理由。
    """

    kind: str
    #: 这一行独有的部分
    detail: str
    #: 这一类共有的解释,每份脚本每类只打印一次
    why: str = ""


def check(anchor: Anchor) -> list[Verdict]:
    """这一行的**全部**问题,一次报完。

    刻意不在第一条上 `return`:派发目标没了和锚点过期是两件独立的事,
    一行上可以同时成立。只报第一条的话,修完派发名字再跑一次才发现
    锚点也过期了 —— 一次能说完的事分成两轮说。
    """
    found: list[Verdict] = []
    if anchor.dispatch is not None:
        if not anchor.dispatch_module:
            found.append(Verdict(
                "读不出派发模块",
                f"点名 `{anchor.dispatch}`",
                "脚本里找不到 `import_module(...)`,派发目标无从核对",
            ))
        defined = _defined_in(anchor.dispatch_module) if anchor.dispatch_module else None
        if anchor.dispatch_module and defined is None:
            found.append(Verdict(
                "派发模块找不到",
                f"点名 `{anchor.dispatch}`",
                f"`{anchor.dispatch_module}` 这个模块不存在 —— 每一行都跑不起来",
            ))
        if defined is not None and anchor.dispatch not in defined:
            found.append(Verdict(
                "派发目标不存在",
                f"-> {anchor.dispatch}",
                f"这些测试在 `{anchor.dispatch_module}` 里已经不叫这个名字了。"
                f"\n    `getattr(m, ...)` 抛 AttributeError、退出码非零,而脚本按"
                f"「非零 = 响了」判定 —— **它们会被报告成「被抓住」,实际一条都没验**。"
                f"\n    要么把名字对回去,要么连着这几行一起删掉。",
            ))
    if anchor.old == anchor.new:
        found.append(
            Verdict("改了个寂寞", "", "原文和替换成是同一串字,这条变异永远不会红")
        )
    if not anchor.target.exists():
        hint = ""
        # 基准搞错是最容易犯的一种:两种形状的路径基准不一样
        for other in {s.base for s in SHAPES.values()}:
            candidate = (PROJECT_ROOT / other / anchor.rel) if other else (
                PROJECT_ROOT / anchor.rel
            )
            if candidate.exists():
                hint = f"(但它在 `{other or '项目根'}/` 下面 —— 路径基准写错了?)"
                break
        found.append(Verdict("目标文件不存在", f"{anchor.rel}{hint}"))
        return found
    try:
        text = anchor.target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        found.append(Verdict("目标文件不是 UTF-8", anchor.rel))
        return found

    count = text.count(anchor.old)
    if count == 1:
        return found
    excerpt = anchor.old.strip().splitlines()[0][:64] if anchor.old.strip() else "(空串)"
    if count == 0:
        found.append(Verdict(
            "锚点过期",
            f"{anchor.rel}  首行:{excerpt}",
            "在目标文件里一次都找不到 —— 这些变异什么都没验",
        ))
        return found
    found.append(Verdict(
        "锚点不唯一",
        f"{anchor.rel}  出现 {count} 次  首行:{excerpt}",
        "改了也不知道改的是哪一处(可能是注释)—— batch13-3 的 M2 就是这个形状",
    ))
    return found


# ================================================================ 入口


def discover() -> list[Path]:
    """全树找 `mutate_*.py`,不维护第二张名单。

    `_is_identity_column()` 骂过手写名单的那个形状:名单会和真实情况分叉,
    而分叉的表现是某一天有人加了一份变异脚本、忘了加进名单。
    """
    found = [
        path
        for path in PROJECT_ROOT.rglob("mutate_*.py")
        if not any(part in SKIPPED_DIRS for part in path.parts)
    ]
    return sorted(found)


def main() -> int:
    scripts = discover()
    if not scripts:
        print(f"{RED}一份变异脚本都没找到{RESET} —— 审计对象为空,这不是通过")
        return 1

    reports: list[tuple[str, dict[str, tuple[str, list[str]]], list[str]]] = []
    total = 0
    broken = 0

    for script in scripts:
        rel = str(script.relative_to(PROJECT_ROOT))
        anchors, problems = read_anchors(script)
        total += len(anchors)

        # kind -> (why, [每行的细节])
        grouped: dict[str, tuple[str, list[str]]] = {}
        bad_rows = 0
        for anchor in anchors:
            verdicts = check(anchor)
            if not verdicts:
                continue
            bad_rows += 1
            broken += 1
            for verdict in verdicts:
                why, rows = grouped.setdefault(verdict.kind, (verdict.why, []))
                rows.append(
                    f"{anchor.table}[{anchor.row}] {anchor.label}"
                    + (f"  {verdict.detail}" if verdict.detail else "")
                )

        labels = [a.label for a in anchors]
        for label in sorted({x for x in labels if labels.count(x) > 1}):
            problems.append(f"编号/描述重复了 {labels.count(label)} 次:{label}")

        ok = len(anchors) - bad_rows
        color = GREEN if not bad_rows and not problems else RED
        tables = "+".join(sorted({a.table for a in anchors})) or "-"
        note = ""
        if grouped:
            note += "  " + RED + "、".join(
                f"{kind} ×{len(rows)}" for kind, (_, rows) in grouped.items()
            ) + RESET
        if problems:
            note += f"  {RED}脚本自身 {len(problems)} 条{RESET}"
        print(f"  {color}{ok:>3}/{len(anchors):<3}{RESET} {rel:<38} {DIM}{tables}{RESET}{note}")
        reports.append((rel, grouped, problems))

    print()
    for rel, grouped, problems in reports:
        if not grouped and not problems:
            continue
        for kind, (why, rows) in grouped.items():
            print(f"{RED}{rel}  {kind} ×{len(rows)}{RESET}")
            if why:
                print(f"    {why}")
            for row in rows:
                print(f"      {DIM}·{RESET} {row}")
            print()
        for problem in problems:
            print(f"{RED}{rel}{RESET}\n    {problem}\n")

    failed = broken + sum(len(p) for _, _, p in reports)
    color = GREEN if not failed else RED
    print(
        f"{color}{total - broken}/{total} 条锚点对得上{RESET}"
        f"  {DIM}({len(scripts)} 份脚本){RESET}"
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
