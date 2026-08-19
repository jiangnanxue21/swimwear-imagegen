#!/usr/bin/env python3
"""读源码的守卫,窗口必须唯一(a59)。

用法:python3 tools/audit_guard_windows.py    # 或 make audit-guard-windows
退出码非零 = 有断言分不清「这里写没写」和「这个文件里有没有人这么写过」。

## 这条门禁是从三次同样的事故里长出来的

    batch13-3  M11   正则没锚在 JSX 开括号上,`{false && ...}` 照样匹配 —— 渲染死了、守卫还绿
    a54        P1    守卫写 `"_matches" in text`,变异把调用改名成 `_matches_removed`,子串照样在
    a57        M10   `"actor=current_actor(request)" in source`,而同文件另有四处端点也这么写
    a58        M3    `"row.editable && row.ui_reachable" in source`,而「状态」列里也出现一次

四次的形状完全一样:**断言写成"这个文件里有没有出现过这串字",而它想说的是
"这个位置写没写"。** 把该位置改坏之后,子串仍在别处出现,守卫照样绿。

前三次的处置都是"记在决策日志里,下次注意"。§3.90 写完那一段之后,a58 当轮
又犯了一次 —— **文档没有拦住它,因为文档拦不住这一类**:写守卫的人当时想的是
"我要断言这件事存在",而窗口是否唯一取决于**被读的那个文件长什么样**,不在
写守卫的人的视野里。这正是该由机器答的问题。

## 判据

对每一条 `assert "<字面量>" in <某个变量>`,若该变量装的是某个文件的正文,
就数一数那串字面量在那个文件里出现几次。**大于一次 = 窗口不唯一。**

窗口不唯一**不等于错**。多数情况下断言的意图确实是"存在性"——「前端到底发没发
这个头」「这条写入路径在不在」。所以这里不是黑名单而是**台账**:每一条都要写清
为什么它可以不唯一。台账自己也会红 —— 某一条一旦变得唯一(被收紧了、或者被读的
文件里那处重复没了),它的条目就失效并被点名,必须删掉。

## 它看不见什么

- 断言不是 `"字面量" in 变量` 这个形状的(正则、`.count()`、AST 断言)——
  那些各有各的锚法,统一判不了
- 变量的来源解析不出来的(字符串拼接出的路径)
- **正文经过任何变换的**,例如 `code = _strip_comments(_read(...))`。这一类刻意
  不解析:被读的是变换后的字符串,而次数只能在原文上数 —— 数出来会偏多,也就是
  **偏向假红**。假红最省事的消法是把整条门禁删掉(§3.37),所以这个方向必须让。
  代价是 a58 自己那条 `assert "useParams" in code` 就在射程之外,如实记在这里
- **被读文件里那处重复是不是无害** —— 这是人的判断,所以要台账

覆盖率在末尾如实打印。**能解析的那部分才受这条门禁保护**,剩下的和以前一样
靠人;把覆盖率打出来,是为了不让人把"没报错"读成"全都查过了"。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent
PURE = BACKEND / "tests" / "pure"

#: 每个测试文件当前的「窗口不唯一」条数。**双向锁**:多了拦,少了也拦。
#:
#: ## 为什么是棘轮而不是逐条台账
#:
#: 首跑扫出 47 条。逐条写"为什么它可以不唯一"意味着要替 47 个我没写过、也没有
#: 逐个读过的守卫做判断 —— 而写出来的那 47 条理由**看起来都会很像真的**。
#: 一份编出来的豁免理由比没有豁免更贵:下一个人会照着它推断当初的意图。
#:
#: 抽读了其中 12 条,分成两类:
#:
#:     存在性     问的是「这件事在不在」而不是「在第几行」。`Idempotency-Key` 在
#:                batch.ts 里出现三次(常量、请求、注释),任何一处还在都说明这件事
#:                没被整块拆掉 —— 这一类收窄反而会挡住正确的实现
#:     真的洞     断言想说的是位置。a57 那条 `eval.ai_test_record_failed` 就是:
#:                模块文档里也提到这个码(讲的正是为什么要喊),把 `logger.error`
#:                整段删掉之后子串仍在、守卫照样绿
#:
#: 比例大致是 11:1。所以这里守的是**这个数只能降**,不是「47 处都是错的」——
#: 与 `test_error_notice_ratchet.py` 同一个道理,那份文档里那句
#: 「守的是这个数只能下降,不是每一处都是错的」原样成立。
#:
#: 收紧一条就把该文件的数字下调一格,降到 0 删掉该行。新文件不许出现在名单之外。
#:
#: **a62 把否定式断言(`assert "x" not in <源码>`)也纳入射程**,基线因此涨了一批。
#: 否定式比肯定式更容易被自己的注释判红 —— 一句解释「为什么把它删掉」的注释
#: 必然含着那串字。判据:目标是 .py 就走 AST,是 .ts/.tsx 就先剥注释。
BASELINE: dict[str, int] = {
    "test_a64_dataclass_fields.py": 1,
    "test_a62_ai_test_query.py": 5,
    "test_a45_batch13_2_fixes.py": 1,
    "test_a45_batch10_fixes.py": 6,
    "test_a45_batch12_2_fixes.py": 3,
    "test_a45_batch12_3_fixes.py": 4,
    "test_a45_batch12_5_fixes.py": 2,
    "test_a45_batch12_fixes.py": 4,
    "test_a45_batch13_stage1_identity.py": 1,
    "test_a45_batch14_13_scope_outcome.py": 1,
    "test_a45_batch14_20_stage4_image_production.py": 2,
    "test_a45_batch14_27_variant_completeness_and_consumers.py": 5,
    "test_a51_spu_preview_and_lifecycle.py": 5,
    "test_a53_log_console.py": 2,
    "test_a56_prompt_registry.py": 1,
    "test_attribute_merge.py": 1,
    "test_attribute_validation.py": 1,
    "test_browser_session_structure.py": 1,
    "test_engine_timezone_pin.py": 1,
    "test_image_set_rules.py": 4,
    "test_media_layer.py": 1,
    "test_security_audit.py": 2,
    "test_settings_schema.py": 1,
}

def right_id_reads_a_file(fn: ast.FunctionDef, right: ast.expr) -> bool:
    """右边那个名字是不是"某个文件的正文",只是路径没算出来。

    不加这一条的话计数会把 `assert "x" in some_dict` 这类全算进来,报出一个
    虚高的"未覆盖数" —— 而一个虚高的覆盖率缺口和一个虚低的一样有害:
    它让人以为这条门禁比实际更没用,从而不去修真正漏掉的那几条。
    """
    if not isinstance(right, ast.Name):
        return False
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Name) and target.id == right.id):
            continue
        for inner in ast.walk(node.value):
            if isinstance(inner, ast.Call):
                func = inner.func
                if isinstance(func, ast.Attribute) and func.attr == "read_text":
                    return True
    return False


class _Paths:
    """把测试模块里那几个 Path 常量算出来。

    只认这个仓库实际用到的几种写法,算不出来就跳过 —— 跳过意味着少查一条,
    而不是误报一条。方向刻意偏向少报:假红最省事的消法是把整条门禁删掉。
    """

    def __init__(self, test_file: Path) -> None:
        self.test_file = test_file
        self.env: dict[str, Path] = {}
        # `tests/pure/_helpers.py` 的两个常量由 import 带进来,这里直接给种子
        self.env["PROJECT_ROOT"] = PROJECT
        self.env["BACKEND_ROOT"] = BACKEND

    def eval(self, node: ast.AST) -> Path | None:
        if isinstance(node, ast.Name):
            return self.env.get(node.id)
        if isinstance(node, ast.Attribute) and node.attr == "parent":
            base = self.eval(node.value)
            return base.parent if base else None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            base = self.eval(node.left)
            if base is None or not isinstance(node.right, ast.Constant):
                return None
            if not isinstance(node.right.value, str):
                return None
            return base / node.right.value
        if isinstance(node, ast.Subscript):
            # Path(__file__).resolve().parents[N]
            value = node.value
            if (
                isinstance(value, ast.Attribute)
                and value.attr == "parents"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, int)
            ):
                return self.test_file.resolve().parents[node.slice.value]
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "resolve":
                return self.eval(func.value)
            if isinstance(func, ast.Name) and func.id == "Path" and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Name) and arg.id == "__file__":
                    return self.test_file
        return None

    def learn_module_constants(self, tree: ast.Module) -> None:
        # 两遍:第二遍让 `APP = BACKEND / "app"` 这种依赖前一行的写法也算得出来
        for _ in range(2):
            for node in tree.body:
                if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                    continue
                target = node.targets[0]
                if not isinstance(target, ast.Name):
                    continue
                value = self.eval(node.value)
                if value is not None:
                    self.env[target.id] = value


def _read_target(node: ast.AST, paths: _Paths, helpers: dict[str, Path]) -> Path | None:
    """一个表达式如果是"读某个文件的正文",返回那个文件。"""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    # `X.read_text(...)`
    if isinstance(func, ast.Attribute) and func.attr == "read_text":
        return paths.eval(func.value)
    # `_read("pages/X.tsx")` 这类模块内小助手
    if isinstance(func, ast.Name) and func.id in helpers and node.args:
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return helpers[func.id] / arg.value
    return None


def _helper_roots(tree: ast.Module, paths: _Paths) -> dict[str, Path]:
    """找出 `def _read(rel): return (ROOT / rel).read_text(...)` 这类助手的根目录。"""
    roots: dict[str, Path] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            if not (isinstance(func, ast.Attribute) and func.attr == "read_text"):
                continue
            base = func.value
            # `(ROOT / rel).read_text()` —— 右边是参数名,左边应当算得出来
            if isinstance(base, ast.BinOp) and isinstance(base.op, ast.Div):
                root = paths.eval(base.left)
                if root is not None:
                    roots[node.name] = root
    return roots


def main() -> int:
    offenders: list[tuple[str, str, int]] = []
    checked = 0
    unresolved = 0
    seen_keys: set[str] = set()

    for test_file in sorted(PURE.glob("test_*.py")):
        tree = ast.parse(test_file.read_text(encoding="utf-8"))
        paths = _Paths(test_file)
        paths.learn_module_constants(tree)
        helpers = _helper_roots(tree, paths)

        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            sources: dict[str, Path] = {}
            for node in ast.walk(fn):
                if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                    continue
                target = node.targets[0]
                if not isinstance(target, ast.Name):
                    continue
                found = _read_target(node.value, paths, helpers)
                if found is not None:
                    sources[target.id] = found

            for node in ast.walk(fn):
                if not isinstance(node, ast.Assert):
                    continue
                test = node.test
                if not isinstance(test, ast.Compare) or len(test.ops) != 1:
                    continue
                negated = isinstance(test.ops[0], ast.NotIn)
                if not isinstance(test.ops[0], ast.In) and not negated:
                    continue
                left, right = test.left, test.comparators[0]
                if not (isinstance(left, ast.Constant) and isinstance(left.value, str)):
                    continue
                if not (isinstance(right, ast.Name) and right.id in sources):
                    if right_id_reads_a_file(fn, right):
                        unresolved += 1
                    continue
                target_file = sources[right.id]
                if not target_file.is_file():
                    unresolved += 1
                    continue
                checked += 1
                text = target_file.read_text(encoding="utf-8")
                if negated:
                    # **否定式单独一档,而且比肯定式更严。**
                    #
                    # 肯定式要求那串字存在,注释里多一处不影响结论;否定式要求它
                    # **不存在**,而一句解释"为什么把它删掉"的注释必然含着它 ——
                    # 于是断言被自己的墓志铭判红。这一类在 a58 / a60 / a62 各踩过,
                    # a62 一轮就踩了三次,而它们全都不是缺陷、全都要改守卫。
                    #
                    # 判据:目标是 .py 就该走 AST(现成的),是 .ts/.tsx 就该先剥注释。
                    # 两样都没有 = 记一笔。
                    guard_src = test_file.read_text(encoding="utf-8")
                    stripped = "_strip_comments" in guard_src or "strip_comments" in guard_src
                    if target_file.suffix == ".py" or stripped:
                        continue
                    key = f"{test_file.name}::{fn.name}::not in::{left.value}"
                    seen_keys.add(key)
                    offenders.append((key, str(target_file.relative_to(PROJECT)), 0))
                    continue
                hits = text.count(left.value)
                if hits <= 1:
                    continue
                key = f"{test_file.name}::{fn.name}::{left.value}"
                seen_keys.add(key)
                offenders.append((key, str(target_file.relative_to(PROJECT)), hits))

    current: dict[str, int] = {}
    for key in seen_keys:
        name = key.split("::", 1)[0]
        current[name] = current.get(name, 0) + 1

    grew = {f: (n, BASELINE.get(f, 0)) for f, n in current.items() if n > BASELINE.get(f, 0)}
    shrank = {f: (current.get(f, 0), n) for f, n in BASELINE.items() if current.get(f, 0) < n}

    for name, (now, was) in sorted(grew.items()):
        print(f"\033[31m窗口不唯一的断言变多了\033[0m {name}:{was} -> {now}")
        for key, target, hits in offenders:
            if key.startswith(name + "::"):
                print(f"    {key.split('::', 1)[1]}")
                print(f"      在 {target} 里出现 {hits} 次 —— 改坏那一处,子串仍在别处,守卫照样绿")
        print("    要么把断言收窄到位置(AST 定位 / 相邻关系),要么这个数不许涨")
    for name, (now, was) in sorted(shrank.items()):
        print(f"\033[31m基线过期\033[0m {name}:基线写着 {was},实际只剩 {now}")
        print("    收紧了却没调基线 —— 留着虚高的基线等于给未来的新债留缝")

    print()
    print(f"解析到目标文件的断言 {checked} 条,其中 {len(seen_keys)} 条窗口不唯一")
    print(
        f"\033[2m    另有 {unresolved} 条断言读的是文件、但路径算不出来,不在保护范围内\033[0m"
    )
    print("\033[2m    这条门禁只认这一种形状 —— 正则、.count()、AST 断言各有各的锚法,统一判不了\033[0m")

    if grew or shrank:
        return 1
    print("\033[32m读源码的守卫,窗口不唯一的那些都在基线之内\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
