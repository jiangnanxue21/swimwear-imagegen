#!/usr/bin/env python3
"""在内存里重放整条迁移链,再和 ORM 声明比对(a65)。

用法:python3 tools/audit_migration_chain.py    # 或 make audit-migration-chain
退出码非零 = 某一步操作的对象不存在,或最终 schema 与 ORM 对不上。

## 这条门禁回答的是一个我自己留了一轮的问题

a64 的交接里写着:「`make fe-check` 与真库这两条我已经连着八轮记录它们,而**记录不是
处理** —— a61 证明过至少有一部分是能在环境限制内做成的,值得再问一次真库那一侧
有没有同样的余地。」**然后那一轮就结束了。** 已经识别出正确的问题却留给下一轮,
和 a61 反思里批判的"重复六遍的免责声明"是同一个动作。

问了之后的答案:**有余地,而且不小。** 迁移是纯声明式的 —— 56 份里 13 种 `op.*`,
只有 9 处 raw `execute` 含 DDL 且全部是索引。表与列的演化因此可以在内存里重放,
一行 SQL 都不用跑。

## 它抓得到什么

    步内   alter_column / add_column / drop_column 的目标表或列不存在
           create_table 重建已存在的表、drop_table 删不存在的表
    步末   最终 schema 与 ORM 的 `__tablename__` + `Mapped[...]` 对不上
    列型   两侧都解析得出类型时,类型名与宽度必须一致(a66 扩)

第二格是重点。`models/prompt_template.py` 顶部记着这个坑的上一次:一条唯一约束
**只写在迁移里**,ORM 层没有,于是 `create_all()` 建出来的测试库缺了它 ——
「测试库和生产库的结构不一致,比没有测试更危险:它让人以为测过了」。
a57 的 BE-307 又踩了同一类(迁移改了列型而 ORM 没跟),当时的处置是"五个面一起改",
**而没有任何东西在看着这五个面是否同向**。这条门禁看的就是其中最要紧的两个面。

## 已知的失真方向(向"少报"偏)

- `op.execute` 里的 raw DDL 一律不解析。本仓这 9 处全是 CREATE/DROP INDEX,
  不影响表与列;哪天有人用它加列,这里会**漏报**而不是误报
- 名字解析不出来的(拼接出的表名、从函数返回的列名)记入 `unresolved` 并**打印出来**,
  不当成"列不存在"。少报一次的代价是漏掉一条,误报一次的代价是一条假红,
  而假红最省事的消法是把整条门禁删掉(§3.37 第一版当场踩过)
- **a66 起比类型名与宽度**,a68 起再比可空与 server default 缺失(下一段)。
  仍然不比精度的第二位。两侧只要有一侧拿不出确定值就跳过那一列 ——
  少比一列好过报一条假红。
  a66 扩容当天抓到一条真缺陷:`platform_rejections.status` 库里 16 而 ORM 32,
  而 `FIXED_PENDING_EXPORT` 是 20 个字符(迁移 0057 修)

## a68 扩容:可空与 server default(A67-ISSUE-001)

外部审计在 a67 抓到的那条 P1,正好长在这个工具当时**明说不看**的地方:
0019 把 `platform_rejections.created_at` 建成可空无默认,而 `TimestampMixin`
声明的是 NOT NULL + `now()`。ORM 因此在 INSERT 里不带这一列(它认为库会填),
库里又没有默认 —— 真实库上拿到 NULL,而"驳回之后有没有重新导出"那道闸拿它
当时间用,拿不到就永远说"缺"。

**门禁全绿、ORM 正确、类型审计正确、纯测试也绿,唯独真实 migration schema 错。**

扩容当天,这个工具独立算出的差异和外部审计手工列的**完全一致**:698 个两侧
都写明可空的列里只有那 4 条,没有第二组同类。迁移 0058 修掉之后归零。

比法两条,都跟着"两侧都拿得出确定值才比"这条口径:

    可空            只认写出来的 `nullable=`。不写时两侧默认相反
                    (`sa.Column` 默认可空,`mapped_column` 看注解),
                    从注解推得出来,但推出来的是这条门禁自己的判断
    server default  只报**危险的那个方向**:ORM 有而迁移没有。反过来
                    (迁移有、ORM 没有)库照样填得上,ORM 只是不知道;
                    而本仓有一批列走 Python 侧 `default=`,一起报就是一片假红
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
VERSIONS = BACKEND / "migrations" / "versions"
MODELS = BACKEND / "app" / "models"

#: 混入提供的列。ORM 侧不声明它们,而迁移侧逐个建出来
MIXIN_COLUMNS = {
    "UUIDPrimaryKeyMixin": {"id"},
    "TimestampMixin": {"created_at", "updated_at"},
}


def _text(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _module_strings(tree: ast.Module) -> dict[str, str]:
    """模块级的字符串常量:`_TABLE = "channel_listings"`。"""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            value = _text(node.value)
            if value:
                out[node.targets[0].id] = value
    return out


def _module_name_lists(tree: ast.Module) -> dict[str, list[str]]:
    """模块级的"列清单":`_COLUMNS = (("next_poll_at", ...), ...)`。

    本仓有两份迁移(0023 / 0024)用 `for name, ... in _COLUMNS:` 循环加列。
    不认这一种的话,那两张表会被误报成"ORM 多了五列" —— 而它们完全正常。
    **这正是这条门禁第一版跑出来的样子**,记在这里,免得下一个人以为那是缺陷。
    """
    out: dict[str, list[str]] = {}
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)):
            continue
        value = node.value
        if not isinstance(value, (ast.Tuple, ast.List)):
            continue
        names: list[str] = []
        for item in value.elts:
            if isinstance(item, (ast.Tuple, ast.List)) and item.elts:
                first = _text(item.elts[0])
                if first:
                    names.append(first)
            else:
                first = _text(item)
                if first:
                    names.append(first)
        if names:
            out[node.targets[0].id] = names
    return out


def _chain() -> list[tuple[str, Path, ast.Module]]:
    """按 `down_revision` 串起整条链。多 head 由 `verify_delivery` 单独看着。"""
    parsed: dict[str, tuple[Path, ast.Module]] = {}
    down: dict[str, str | None] = {}
    for path in sorted(VERSIONS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rev = prev = None
        for node in tree.body:
            target = None
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target = node.target.id
            elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                target = node.targets[0].id
            if target == "revision":
                rev = _text(node.value)
            elif target == "down_revision":
                prev = _text(node.value)
        if rev:
            parsed[rev] = (path, tree)
            down[rev] = prev
    heads = [r for r in down if r not in set(down.values())]
    if len(heads) != 1:
        raise SystemExit(f"迁移链不是单一 head:{sorted(heads)}")
    order: list[str] = []
    cur: str | None = heads[0]
    while cur:
        order.append(cur)
        cur = down.get(cur)
    order.reverse()
    return [(rev, *parsed[rev]) for rev in order]


#: 两侧对同一个类型的不同写法。左边是 ORM / 迁移里可能出现的别名
_TYPE_ALIASES = {"PGUUID": "UUID", "VARCHAR": "String", "BIGINT": "BigInteger"}


def _module_ints(tree: ast.Module) -> dict[str, int]:
    """模块级的整数常量:`_OWNER_ID_WIDTH = 160`。

    不认这一种的话,0027 那两处 `sa.String(length=_OWNER_ID_WIDTH)` 会解析成
    "没有宽度",最终报 `owner_id` 类型不一致 —— **而那是门禁自己看不见**。
    这是这一轮第二次踩同一个形状(第一次是循环变量),两次都改解析而不是加豁免。
    """
    out: dict[str, int] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, int)
            and not isinstance(node.value.value, bool)
        ):
            out[node.targets[0].id] = node.value.value
    return out


def _column_type(node: ast.AST, ints: dict[str, int]) -> tuple[str, int | None] | None:
    """一个类型表达式归一成 (类型名, 宽度)。认不出来返回 None —— **不猜**。

    宽度取第一个整数:位置参数优先,其次 `length=` / `precision=`。
    `sa.Numeric(precision=5, scale=2)` 与 `Numeric(5, 2)` 因此归一到同一个值 ——
    不认关键字的话那五列全部假红(实测)。
    """
    if isinstance(node, ast.Call):
        base, args, kwargs = node.func, node.args, node.keywords
    else:
        base, args, kwargs = node, [], []
    name = getattr(base, "attr", None) or getattr(base, "id", None)
    if name is None:
        return None
    name = _TYPE_ALIASES.get(name, name)

    def as_int(value: ast.AST) -> int | None:
        if isinstance(value, ast.Constant) and isinstance(value.value, int):
            return None if isinstance(value.value, bool) else value.value
        if isinstance(value, ast.Name):
            return ints.get(value.id)
        return None

    for arg in args:
        got = as_int(arg)
        if got is not None:
            return (name, got)
    for kw in kwargs:
        if kw.arg in ("length", "precision"):
            got = as_int(kw.value)
            if got is not None:
                return (name, got)
    return (name, None)


def _column_facts(node: ast.AST) -> dict[str, bool | None]:
    """一个 `sa.Column(...)` / `mapped_column(...)` 调用里的可空与 server default。

    只认**显式写出来的**:

        nullable=        写了就取,没写返回 None(= 这一列不参与可空比对)
        primary_key=True 主键隐含 NOT NULL,这一条认
        server_default=  presence 就是值本身 —— 两边的 API 里"没写"都明确
                         等于"这一列没有库级默认",所以它不需要 None 这一档

    **为什么可空只认显式的。** 不写 `nullable=` 时两侧的默认相反:
    `sa.Column` 默认可空,而 SQLAlchemy 2 的 `mapped_column` 看注解 ——
    `Mapped[str]` 不可空、`Mapped[str | None]` 可空。从注解推得出来,但推出来的
    每一条都是这条门禁**自己的判断**,而不是代码里写着的事实。少比一列好过
    报一条假红(§3.37 第一版当场踩过:假红最省事的消法是把整条门禁删掉)。
    """
    if not isinstance(node, ast.Call):
        return {"nullable": None, "server_default": False}
    nullable: bool | None = None
    server_default = False
    for kw in node.keywords:
        if kw.arg == "nullable" and isinstance(kw.value, ast.Constant):
            if isinstance(kw.value.value, bool):
                nullable = kw.value.value
        elif kw.arg == "primary_key" and isinstance(kw.value, ast.Constant):
            if kw.value.value is True:
                nullable = False
        elif kw.arg == "server_default":
            # `server_default=None` 是**去掉默认**,不是"写了一个默认"
            server_default = not (
                isinstance(kw.value, ast.Constant) and kw.value.value is None
            )
    return {"nullable": nullable, "server_default": server_default}


def _alter_facts(node: ast.Call) -> dict[str, bool | None]:
    """`op.alter_column(...)` 这一次**改了**可空 / server default 吗。

    `existing_nullable=` / `existing_server_default=` 一律不认:那两个是写给
    alembic 看的"这一列现在是什么",不是这一步要把它改成什么。把它们当成改动
    会让 0057 那种"只改宽度"的迁移看起来还顺手改了可空。
    """
    out: dict[str, bool | None] = {"nullable": None, "server_default": None}
    for kw in node.keywords:
        if kw.arg == "nullable" and isinstance(kw.value, ast.Constant):
            if isinstance(kw.value.value, bool):
                out["nullable"] = kw.value.value
        elif kw.arg == "server_default":
            out["server_default"] = not (
                isinstance(kw.value, ast.Constant) and kw.value.value is None
            )
    return out


def _targets(node, call, resolve, loop_tables) -> list[str]:
    """一次操作作用在哪几张表上。

    表名可能是常量、模块级字符串,或**循环变量**(0020 是
    `for table in _TABLES: for name, _ in _COLUMNS:` 两层)。不收循环那一层的
    后果实测过:0020 给两张表各加四列,解析不出表名 -> 那八列在重放里不存在 ->
    最终对照报「ORM 多了 reject_* 四列」,**而那是这条门禁自己看不见,不是代码
    有问题**。一条报着假红的门禁活不过第二轮。
    """
    one = resolve(node)
    if one:
        return [one]
    return list(loop_tables.get(id(call)) or [])


def replay() -> tuple[
    dict[str, set[str]],
    dict[tuple[str, str], tuple],
    dict[tuple[str, str], dict[str, bool | None]],
    list[str],
    list[str],
]:
    schema: dict[str, set[str]] = {}
    types: dict[tuple[str, str], tuple] = {}
    facts: dict[tuple[str, str], dict[str, bool | None]] = {}
    problems: list[str] = []
    unresolved: list[str] = []

    for rev, path, tree in _chain():
        strings = _module_strings(tree)
        name_lists = _module_name_lists(tree)
        ints = _module_ints(tree)
        upgrade = next(
            (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade"),
            None,
        )
        if upgrade is None:
            continue

        def resolve(node: ast.AST | None) -> str | None:
            value = _text(node)
            if value:
                return value
            if isinstance(node, ast.Name):
                return strings.get(node.id)
            return None

        # 循环里加/删列:`for name, ... in _COLUMNS:` —— 循环变量在循环外解析不出来,
        # 所以先把这一层摊平。**表名也可能是循环变量**(0020 是
        # `for table in _TABLES: for name, _ in _COLUMNS:` 两层),所以两样都收。
        #
        # 不收表名那一层的后果实测过:0020 给两张表各加四列,解析不出表名 ->
        # 那八列在重放里不存在 -> 最终对照报"ORM 多了 reject_* 四列",
        # **而那是这条门禁自己看不见,不是代码有问题**。少报好过误报,
        # 但这一处收得起来就该收 —— 一条报着假红的门禁活不过第二轮。
        loop_names: dict[int, list[str]] = {}
        loop_tables: dict[int, list[str]] = {}
        for node in ast.walk(upgrade):
            if not isinstance(node, ast.For):
                continue
            iterable = node.iter
            if isinstance(iterable, ast.Call) and iterable.args:
                iterable = iterable.args[0]      # `reversed(_COLUMNS)`
            if not isinstance(iterable, ast.Name):
                continue
            names = name_lists.get(iterable.id)
            if not names:
                continue
            # **按循环变量的用法分,不按循环的形状猜。**
            #
            # 第一版按形状:单个目标 = 表名,元组目标 = (列名, 类型)。0020 对了,
            # 0049 就错了 —— 它是 `_COLUMNS = ("upstream_versions", ...)` 加
            # `for name in _COLUMNS:`,形状是"单个",内容却是列名。于是那两列被
            # 当成表名,重放里不存在,最终报「ORM 多了两列」——**又一次自己
            # 看不见而报出的假红**。
            #
            # 正确的判据在用法里:循环变量出现在 `sa.Column(x, ...)` 的第一个位置
            # 就是列名,出现在 `op.add_column(x, ...)` 的第一个位置就是表名。
            bound = {
                t.id
                for t in ast.walk(node.target)
                if isinstance(t, ast.Name)
            }
            as_column = as_table = False
            for inner in ast.walk(node):
                if not (isinstance(inner, ast.Call) and inner.args):
                    continue
                first = inner.args[0]
                if not (isinstance(first, ast.Name) and first.id in bound):
                    continue
                if getattr(inner.func, "attr", None) == "Column":
                    as_column = True
                elif getattr(inner.func, "attr", None) in {"add_column", "drop_column"}:
                    as_table = True
            for inner in ast.walk(node):
                if as_column:
                    loop_names[id(inner)] = names
                if as_table:
                    loop_tables[id(inner)] = names

        for node in ast.walk(upgrade):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if getattr(node.func.value, "id", None) != "op":
                continue
            op, args = node.func.attr, node.args
            here = f"[{rev}] {path.name}"

            if op == "create_table" and args:
                table = resolve(args[0])
                if table is None:
                    unresolved.append(f"{here} create_table 的表名解析不出来")
                    continue
                columns: set[str] = set()
                for arg in args[1:]:
                    if (
                        isinstance(arg, ast.Call)
                        and getattr(arg.func, "attr", None) == "Column"
                        and arg.args
                    ):
                        column = resolve(arg.args[0])
                        if column:
                            columns.add(column)
                            facts[(table, column)] = _column_facts(arg)
                            if len(arg.args) >= 2:
                                kind = _column_type(arg.args[1], ints)
                                if kind:
                                    types[(table, column)] = kind
                if table in schema:
                    problems.append(f"{here} create_table 重建已存在的表:{table}")
                schema[table] = columns

            elif op == "add_column" and len(args) >= 2:
                tables = _targets(args[0], node, resolve, loop_tables)
                if not tables:
                    unresolved.append(f"{here} add_column 的表名解析不出来")
                    continue
                columns = loop_names.get(id(node))
                if columns is None:
                    column = args[1]
                    one = (
                        resolve(column.args[0])
                        if isinstance(column, ast.Call) and column.args
                        else None
                    )
                    columns = [one] if one else None
                for table in tables:
                    if table not in schema:
                        problems.append(f"{here} add_column 目标表不存在:{table}")
                    elif columns is None:
                        unresolved.append(f"{here} add_column 的列名解析不出来({table})")
                    else:
                        schema[table].update(columns)
                        if len(columns) == 1 and isinstance(args[1], ast.Call):
                            facts[(table, columns[0])] = _column_facts(args[1])
                            if len(args[1].args) >= 2:
                                kind = _column_type(args[1].args[1], ints)
                                if kind:
                                    types[(table, columns[0])] = kind

            elif op == "drop_column" and len(args) >= 2:
                tables = _targets(args[0], node, resolve, loop_tables)
                if not tables:
                    unresolved.append(f"{here} drop_column 的表名解析不出来")
                    continue
                columns = loop_names.get(id(node))
                if columns is None:
                    one = resolve(args[1])
                    columns = [one] if one else None
                for table in tables:
                    if table not in schema:
                        problems.append(f"{here} drop_column 目标表不存在:{table}")
                    elif columns is None:
                        unresolved.append(f"{here} drop_column 的列名解析不出来({table})")
                    else:
                        for column in columns:
                            if column not in schema[table]:
                                problems.append(
                                    f"{here} drop_column 目标列不存在:{table}.{column}"
                                )
                            schema[table].discard(column)
                            facts.pop((table, column), None)

            elif op == "drop_table" and args:
                table = resolve(args[0])
                if table is None:
                    unresolved.append(f"{here} drop_table 的表名解析不出来")
                elif table not in schema:
                    problems.append(f"{here} drop_table 目标表不存在:{table}")
                else:
                    del schema[table]
                    for key in [k for k in facts if k[0] == table]:
                        del facts[key]

            elif op == "alter_column" and len(args) >= 2:
                table = resolve(args[0])
                column = resolve(args[1])
                if table is None:
                    unresolved.append(f"{here} alter_column 的表名解析不出来")
                elif table not in schema:
                    # **a57 手工发现的那一条就是这个形状** —— 迁移 0055 指着
                    # `evaluations`,而全仓没有这张表。它在任何真实库上直接失败,
                    # 而当时没有任何东西在看
                    problems.append(f"{here} alter_column 目标表不存在:{table}")
                elif column is None:
                    # **原来这一支是静默的。** table 解析得出、column 解析不出时,
                    # 两个 elif 都不成立,于是这一步既没被应用也没被记下来 ——
                    # 一次改可空 / 改默认的操作对这条门禁**完全隐形**。
                    #
                    # 0058 第一版就撞在这里:它写的是 `for column in ("created_at",
                    # "updated_at"):`,而循环那一层只映射到 add_column/drop_column。
                    # 于是门禁继续报着已经修好的那 4 条差异 —— 这次是门禁自己
                    # 看不见,而不是代码有问题。少报好过误报,但少报也得**出声**。
                    unresolved.append(f"{here} alter_column 的列名解析不出来({table})")
                elif column not in schema[table]:
                    problems.append(f"{here} alter_column 目标列不存在:{table}.{column}")
                elif column:
                    for kw in node.keywords:
                        if kw.arg == "type_":
                            kind = _column_type(kw.value, ints)
                            if kind:
                                types[(table, column)] = kind
                    changed = _alter_facts(node)
                    current = facts.setdefault(
                        (table, column), {"nullable": None, "server_default": False}
                    )
                    for key, value in changed.items():
                        if value is not None:
                            current[key] = value

    return schema, types, facts, problems, unresolved


def mixin_facts() -> dict[str, dict[str, dict[str, bool | None]]]:
    """混入类各自给出的列事实,从 `db/base.py` **读**出来,不在这里抄一份。

    抄一份的话,这个工具就成了同一个事实的第三个副本 —— 而它存在的理由
    正是"同一个事实的两个副本会各说各话"。ISSUE-001 就是 `TimestampMixin`
    与 0019 各说各话,没道理用同样的手法去检查它。
    """
    out: dict[str, dict[str, dict[str, bool | None]]] = {}
    tree = ast.parse((BACKEND / "app" / "db" / "base.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name not in MIXIN_COLUMNS:
            continue
        columns: dict[str, dict[str, bool | None]] = {}
        for stmt in node.body:
            if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)):
                continue
            if not isinstance(stmt.value, ast.Call):
                continue
            columns[stmt.target.id] = _column_facts(stmt.value)
        out[node.name] = columns
    return out


def orm_schema() -> tuple[
    dict[str, set[str]],
    dict[tuple[str, str], tuple],
    dict[tuple[str, str], dict[str, bool | None]],
]:
    """ORM 声明的表与列。

    `relationship()` 也写成 `Mapped[...]`,但它**不是列** —— 不排掉的话
    每一张有关系的表都会被误报成"ORM 多了一列"。第一版正是这么跑出来的。
    """
    out: dict[str, set[str]] = {}
    types: dict[tuple[str, str], tuple] = {}
    facts: dict[tuple[str, str], dict[str, bool | None]] = {}
    mixins = mixin_facts()
    for path in MODELS.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        ints = _module_ints(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            table = None
            columns: set[str] = set()
            local_facts: dict[str, dict[str, bool | None]] = {}
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and getattr(stmt.targets[0], "id", None) == "__tablename__"
                ):
                    table = _text(stmt.value)
                if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)):
                    continue
                if not ast.unparse(stmt.annotation).startswith("Mapped["):
                    continue
                factory = (
                    getattr(stmt.value.func, "id", None)
                    or getattr(stmt.value.func, "attr", None)
                    if isinstance(stmt.value, ast.Call)
                    else None
                )
                if factory == "relationship":
                    continue
                name = stmt.target.id
                kind = None
                if isinstance(stmt.value, ast.Call):
                    for arg in stmt.value.args:
                        explicit = _text(arg)
                        if explicit:
                            name = explicit
                            continue
                        kind = _column_type(arg, ints)
                        break
                columns.add(name)
                if kind and table:
                    types[(table, name)] = kind
                if isinstance(stmt.value, ast.Call):
                    local_facts[name] = _column_facts(stmt.value)
            if not table:
                continue
            for name, fact in local_facts.items():
                facts[(table, name)] = fact
            for base in node.bases:
                base_name = getattr(base, "id", "")
                columns |= MIXIN_COLUMNS.get(base_name, set())
                for name, fact in mixins.get(base_name, {}).items():
                    facts[(table, name)] = fact
            out[table] = columns
            for (t, c), kind in list(types.items()):
                if t is None:
                    types.pop((t, c))
    return out, types, facts


def main() -> int:
    schema, mig_types, mig_facts, problems, unresolved = replay()
    orm, orm_types, orm_facts = orm_schema()

    for line in problems:
        print(f"\033[31m迁移链\033[0m {line}")

    drift: list[str] = []
    for table in sorted(set(schema) - set(orm)):
        drift.append(f"{table}:迁移建了它,而 ORM 里没有对应的模型")
    for table in sorted(set(orm) - set(schema)):
        drift.append(f"{table}:ORM 声明了它,而迁移链里没有建过")
    for table in sorted(set(schema) & set(orm)):
        extra_mig = sorted(schema[table] - orm[table])
        extra_orm = sorted(orm[table] - schema[table])
        if extra_mig or extra_orm:
            drift.append(
                f"{table}:迁移多 {extra_mig or '—'} / ORM 多 {extra_orm or '—'}"
            )
    # 列型比对(a66)。**两侧只要有一侧解析不出类型就跳过那一列** ——
    # 少比一列好过报一条假红,而假红最省事的消法是把整条门禁删掉(§3.37)
    comparable = set(mig_types) & set(orm_types)
    for table, column in sorted(comparable):
        if mig_types[(table, column)] != orm_types[(table, column)]:
            mine, theirs = mig_types[(table, column)], orm_types[(table, column)]
            drift.append(
                f"{table}.{column}:迁移 {mine[0]}({mine[1]}) / ORM {theirs[0]}({theirs[1]})"
            )

    # 可空与 server default(a68,A67-ISSUE-001)。**这两样此前明说不比**,
    # 而那正是 ISSUE-001 的藏身处:0019 把 `platform_rejections.created_at`
    # 建成可空无默认,`TimestampMixin` 声明的却是 NOT NULL + `now()`。于是
    # ORM 编译 INSERT 时把时间戳留给库生成,库里没有默认 —— 真实库上拿到
    # 一个 NULL,而"驳回之后有没有重新导出"这道闸拿它当时间用。
    #
    # 门禁全绿、ORM 正确、类型审计正确、测试也绿,唯独真实 migration schema 错。
    #
    # 比法与列型那一格同一条口径:**两侧都拿得出确定值才比**。可空只认写出来的
    # `nullable=`(理由见 `_column_facts`);server default 比的是"有没有",
    # 两边的 API 里"没写"都确定等于"没有",所以它不需要那一档 None。
    nullable_pairs = 0
    for table, column in sorted(set(mig_facts) & set(orm_facts)):
        mine, theirs = mig_facts[(table, column)], orm_facts[(table, column)]
        if mine["nullable"] is not None and theirs["nullable"] is not None:
            nullable_pairs += 1
            if mine["nullable"] != theirs["nullable"]:
                drift.append(
                    f"{table}.{column}:迁移 nullable={mine['nullable']} / "
                    f"ORM nullable={theirs['nullable']}"
                )
        # **只报危险的那个方向。** ORM 有默认而迁移没有 = ORM 认定"库会自己填",
        # 库不会 —— 那一列在真实库上收到 NULL,ISSUE-001 就是这一支。
        # 反过来(迁移有、ORM 没有)库照样填得上,ORM 只是不知道,不会写坏数据;
        # 而本仓有一批列走 Python 侧 `default=`,把那些一起报出来就是一片假红。
        if theirs["server_default"] and not mine["server_default"]:
            drift.append(
                f"{table}.{column}:ORM 声明了 server_default 而迁移没建出来 —— "
                f"INSERT 不会带这一列的值,库也不会填"
            )

    for line in drift:
        print(f"\033[31mschema 与 ORM 对不上\033[0m {line}")
        print("    「测试库和生产库的结构不一致,比没有测试更危险:它让人以为测过了」")

    print()
    # **两个计数各占一行。** 挤在一行里的话,`两侧都解析出类型的列 554,两侧...`
    # 会让按空白切词的读法拿到 `554,两侧都写明可空的列` —— a65 那条守卫正是
    # 这么读的,扩容当天当场变红。摘要是给人读的,也是给守卫读的
    print(f"重放 {len(schema)} 张表,ORM 声明 {len(orm)} 张")
    print(f"两侧都解析出类型的列 {len(comparable)}")
    print(f"两侧都写明可空的列 {nullable_pairs}")
    if unresolved:
        print(f"\033[33m解析不出来的 {len(unresolved)} 处(不当成缺陷,但也没被检查):\033[0m")
        for line in unresolved:
            print(f"    {line}")
    print(
        "\033[2m    比列名、列型(名字+宽度)、可空、server default 缺失;"
        "一侧拿不出确定值的列直接跳过\033[0m"
    )
    print("\033[2m    op.execute 里的 raw DDL 不解析(本仓 9 处全是索引)\033[0m")

    if problems or drift:
        return 1
    print("\033[32m迁移链自洽,最终 schema 与 ORM 逐表逐列对得上\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
