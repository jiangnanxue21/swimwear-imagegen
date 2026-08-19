#!/usr/bin/env python3
"""dataclass 的字段,必须有某条路径真的给过值(a64)。

用法:python3 tools/audit_dataclass_fields.py    # 或 make audit-dataclass-fields
退出码非零 = 有字段从来没被任何构造点/赋值/`replace()` 给过值,恒等于默认值。

## 这条门禁是从 a63 那个缺陷长出来的

`AiTestRecord.prompt_template_version` 声明了、串进了 `as_row_kwargs()`、
落到了模型的那一列上 —— **而两个构造器一个都没传过它**,从建表起恒为 NULL。
它存在了六轮,每轮全套门禁都绿,最后是"带着一条反思去翻渲染逻辑"偶然翻到的。

**两道专门为此存在的防线都没看见它:**

    audit_column_writers.py   判「有没有写入点」。那一列有写入语句
                              (`prompt_template_version=kwargs[...]`),它满意了
    a57 那条 kwargs 守卫       判**键集**覆盖模型的列。那个键一直在,值一直是 None

键在、值恒空 —— 两侧各自的测试都绿,没有一条跨过中间那道缝。这条门禁跨的就是那道缝。

## 判据

对每个 `@dataclass`,收集它的字段;再扫全仓,凡是

    构造点的位置参数或具名参数        `X(a, b=1)`
    构造点的 `**kwargs`               保守起见算作**全部字段都传过**
    构造之后的属性赋值                `obj.field = ...` / `obj.field += ...`
    `dataclasses.replace(obj, f=...)`

任何一种给过值的字段,就算"有人给过"。剩下的字段恒等于默认值。

**属性赋值按名字认,不解析接收者的类型** —— 与 `audit_column_writers.py` 同一个
取舍,而且是同一个方向:多认一次的代价是漏掉一个字段,少认一次的代价是一条假红,
而假红最省事的消法是把整条门禁删掉(§3.37 第一版当场踩过)。

## 为什么是棘轮而不是逐条台账

首跑 16 个类。逐条写"为什么这个字段可以恒等于默认值"意味着替 16 个我没写过、
也没有逐个读过的数据类做判断,而写出来的那 16 条理由**看起来都会很像真的**
(§3.92 记过这个取舍)。其中相当一部分确实是良性的:`schema_version` 这类
版本号常量、配置对象的默认档位。所以守的是**这个数只能降**。

## 它看不见什么

- 字段被传的是一个**恒为 None 的变量**(`x=maybe` 而 `maybe` 永远是 None)——
  那是数据流,静态判不了。a63 那个缺陷在修复前恰好不是这一种(它压根没被传过),
  但下一个同类不一定
- 测试里构造的不算(只扫 `app/`)——一个只有测试给过值的字段,在生产里仍然恒定
"""
from __future__ import annotations

import ast
import collections
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
APP = BACKEND / "app"

#: 每个文件当前"有字段从没被给过值"的数据类个数。**双向锁**:多了拦,少了也拦。
#:
#: 少了也拦的理由与 `test_error_notice_ratchet.py` 一致:有人补上了却忘了调基线,
#: 留着虚高的基线等于给未来的新债留缝。
BASELINE: dict[str, int] = {
    "app/attributes/contracts.py": 4,
    "app/channels/registry.py": 1,
    "app/evaluators/rules.py": 1,
    "app/listings/contracts.py": 2,
    "app/listings/copy_generator.py": 2,
    "app/listings/copy_rules.py": 1,
    "app/providers/base.py": 1,
    "app/services/formatting_service.py": 1,
    "app/services/product_import.py": 1,
    "app/services/publish_service.py": 1,
    "app/workbench/flow.py": 1,
}


def _dataclasses_in(tree: ast.Module) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        decorated = any(
            getattr(d, "id", None) == "dataclass"
            or getattr(getattr(d, "func", None), "id", None) == "dataclass"
            for d in node.decorator_list
        )
        if not decorated:
            continue
        fields = [
            stmt.target.id
            for stmt in node.body
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
        ]
        if fields:
            found[node.name] = fields
    return found


def main() -> int:
    trees = {}
    for path in sorted(APP.rglob("*.py")):
        trees[path] = ast.parse(path.read_text(encoding="utf-8"))

    declared: dict[str, tuple[list[str], Path]] = {}
    for path, tree in trees.items():
        for name, fields in _dataclasses_in(tree).items():
            declared[name] = (fields, path)

    by_field: dict[str, set[str]] = collections.defaultdict(set)
    for name, (fields, _) in declared.items():
        for field in fields:
            by_field[field].add(name)

    given: dict[str, set[str]] = collections.defaultdict(set)
    constructed: set[str] = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if func in declared:
                    constructed.add(func)
                    order = declared[func][0]
                    for index, _ in enumerate(node.args):
                        if index < len(order):
                            given[func].add(order[index])
                    for kw in node.keywords:
                        if kw.arg:
                            given[func].add(kw.arg)
                        else:
                            # `X(**payload)` —— 保守起见算作全部传过。
                            # 反过来(算作一个都没传)会让一整批配置类假红
                            given[func].update(order)
                if func == "replace":
                    for kw in node.keywords:
                        if kw.arg:
                            for owner in by_field.get(kw.arg, ()):
                                given[owner].add(kw.arg)
            if isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute):
                        for owner in by_field.get(target.attr, ()):
                            given[owner].add(target.attr)

    offenders: dict[str, list[tuple[str, list[str]]]] = collections.defaultdict(list)
    for name, (fields, path) in sorted(declared.items()):
        if name not in constructed:
            # 全仓没有构造点的类不在射程内 —— 它多半只作类型标注用
            continue
        gap = [f for f in fields if f not in given[name] and not f.startswith("_")]
        if gap:
            offenders[path.relative_to(BACKEND).as_posix()].append((name, gap))

    current = {rel: len(items) for rel, items in offenders.items()}
    grew = {f: (n, BASELINE.get(f, 0)) for f, n in current.items() if n > BASELINE.get(f, 0)}
    shrank = {f: (current.get(f, 0), n) for f, n in BASELINE.items() if current.get(f, 0) < n}

    for rel, (now, was) in sorted(grew.items()):
        print(f"\033[31m有字段从没被给过值的数据类变多了\033[0m {rel}:{was} -> {now}")
        for name, gap in offenders[rel]:
            print(f"    {name}: {gap}")
        print("    这些字段恒等于默认值 —— 要么补上给值的路径,要么这个数不许涨")
    for rel, (now, was) in sorted(shrank.items()):
        print(f"\033[31m基线过期\033[0m {rel}:基线写着 {was},实际只剩 {now}")
        print("    补上了却没调基线 —— 留着虚高的基线等于给未来的新债留缝")

    total = sum(current.values())
    print()
    print(f"扫了 {len(declared)} 个 dataclass,其中 {total} 个有字段恒等于默认值")
    print(
        "\033[2m    只扫 app/ —— 一个只有测试给过值的字段,在生产里仍然恒定\033[0m"
    )
    print(
        "\033[2m    看不见「传进来的变量恒为 None」那一种:那是数据流,静态判不了\033[0m"
    )
    if grew or shrank:
        return 1
    print("\033[32mdataclass 的字段,恒等于默认值的那些都在基线之内\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
