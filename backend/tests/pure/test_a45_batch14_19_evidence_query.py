"""A45-batch14-19:§5.1 白名单成为**取数入口**(PRD 阶段 2 交付第四项)。

## 这一批修的是什么

判定从 A45-batch14-7 起就是对的:`run_extraction` 会用
`evidence_rules.asset_is_extraction_input()` 把 AI 图挡在识别输入之外。
所以**今天并没有在烧钱** —— `docs/STATUS.md`「已知限制」里那条说
「今天 `run_extraction` 走的仍是 `usable_assets()`,只过滤 status = READY」
是**过期的**,本批一并改掉。

真正没做完的是 PRD 阶段 2 交付原文里的那半句:**「白名单查询助手成为
唯一取数入口」**。搬家之前的形状是:

    取数    usable_assets()              未过滤,谁都能调
    过滤    run_extraction 里一段推导式   只保护这一个调用点

于是任何一个新写的调用点只要照着 `usable_assets()` 抄一行,就能把 AI 图
喂进付费抽取器,而**没有任何地方会红**。§5.1 要消灭的正是这个形状:
它防的不是今天的代码,是明天的调用点。

## 这一批能验到什么、验不到什么

**验不到 SQL。** 这台机器没有 sqlalchemy,粗筛的 WHERE 子句一次都没执行过。
真正的行为验证在 `tests/test_a45_batch14_19_evidence_query_db.py`(要真库):
那边拿一张覆盖矩阵比对「SQL 粗筛 + Python 判定」与「全量 + Python 判定」
是否给出同一批行 —— 那是粗筛"必须是超集"这条性质的唯一真验证。

这里守的是结构:过滤有没有搬到位、旧入口还有没有人在识别路径上调、
粗筛用的列在不在那张被证明过的清单里。
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.core.enums import MediaSource
from tests.pure._helpers import BACKEND_ROOT

APP = BACKEND_ROOT / "app"
MEDIA_SERVICE = APP / "media" / "service.py"
EXTRACTION_SERVICE = APP / "attributes" / "service.py"
EVIDENCE_RULES = APP / "media" / "evidence_rules.py"

ENTRY = "evidence_assets_for"
ADAPTER = "asset_is_extraction_input"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _func(path: Path, name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(_src(path))):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里找不到函数 {name}()")


def _attr_calls(node: ast.AST) -> set[str]:
    return {
        c.func.attr
        for c in ast.walk(node)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
    }


# ==========================================================
# 一、入口存在,而且它自己过滤
# ==========================================================


def test_the_entry_point_exists_and_runs_the_whitelist_itself():
    """取数入口必须自己过滤,不是把过滤留给调用方。

    留给调用方的表现就是本批之前那样:入口看起来是收口的,而**保护的强度
    取决于每一个调用点记不记得再过一道**。
    """
    entry = _func(MEDIA_SERVICE, ENTRY)
    assert ADAPTER in _attr_calls(entry), (
        f"{ENTRY}() 没有调用 {ADAPTER}() —— 取数入口不过滤等于没有白名单"
    )


def test_the_entry_point_returns_the_filtered_rows_not_the_raw_ones():
    """**「算出来了但没用」是接线最常见的坏法。**

    过滤算了、返回的却是粗筛结果,表现是白名单整条失效,而调用点、
    函数名、文档全都看不出差别。
    """
    entry = _func(MEDIA_SERVICE, ENTRY)
    returns = [n for n in ast.walk(entry) if isinstance(n, ast.Return) and n.value]
    assert returns, f"{ENTRY}() 没有返回值"
    assert all(
        any(
            isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr == ADAPTER
            for c in ast.walk(r.value)
        )
        for r in returns
    ), (
        f"{ENTRY}() 有一条 return 绕过了 {ADAPTER}() —— "
        "多一条提前返回的分支就多一条不过滤的路"
    )


def test_the_filter_condition_is_the_bare_adapter_call():
    """条件必须**就是那一个调用**,不许取反、不许拼、不许只有半个。

    取反的后果比不修还严重:识别会**只跑在 AI 图上** —— 付费调用一次不少,
    而证据全部来自模型自己的输出。这一条是 14-7 变异 P2 撞出来的洞,
    本批跟着过滤一起搬过来。
    """
    entry = _func(MEDIA_SERVICE, ENTRY)
    comps = [
        c
        for c in ast.walk(entry)
        if isinstance(c, ast.ListComp)
        and any(
            isinstance(x, ast.Call)
            and isinstance(x.func, ast.Attribute)
            and x.func.attr == ADAPTER
            for x in ast.walk(c)
        )
    ]
    assert len(comps) == 1, f"白名单过滤出现了 {len(comps)} 处,应当只有一处"
    conditions = comps[0].generators[0].ifs
    assert len(conditions) == 1, f"过滤条件有 {len(conditions)} 个,应当只有一个"
    only = conditions[0]
    assert isinstance(only, ast.Call) and isinstance(only.func, ast.Attribute), (
        f"过滤条件不是一个裸调用,而是 {type(only).__name__} —— "
        "取反或拼接都会让白名单反过来筛"
    )
    assert only.func.attr == ADAPTER, "过滤条件调用的不是白名单适配器"


# ==========================================================
# 二、粗筛只许用被证明过的那几列
# ==========================================================


def test_the_coarse_filter_only_uses_columns_the_predicate_implies():
    """**本批最要紧的一条。**

    SQL 粗筛的每一条都必须被判定蕴含 —— 判定为 True 的行一定满足它。
    否则粗筛会把本该识别的图挡在库里,而**「本该识别却没识别」没有任何
    地方会报**:运营看到的是一次成功的识别,只是少了几个字段。

    这里不去证明蕴含关系(那要枚举,在 14-7 那份里做过),只钉一件事:
    粗筛用到的列必须出现在 `_COARSE_FILTER_COLUMNS` 那张表里 ——
    加一列就必须先去改那张表,而改那张表的地方写着要去哪里补证明。
    """
    src = _src(MEDIA_SERVICE)
    entry = _func(MEDIA_SERVICE, ENTRY)
    declared = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_COARSE_FILTER_COLUMNS"
            for t in node.targets
        ):
            declared = {
                c.value
                for c in ast.walk(node.value)
                if isinstance(c, ast.Constant) and isinstance(c.value, str)
            }
    assert declared, "找不到 _COARSE_FILTER_COLUMNS 声明"

    #: 作用域相关的列不算白名单条件:它们回答"要哪个商品/哪个颜色的证据",
    #: 不回答"这张图算不算证据"。混在一起的话这条守卫会连正常的作用域
    #: 参数一起拦下来,然后被人加白名单绕过。
    scoping = {"product_id", "color_variant_id"}

    # 只扫 `where()` 的实参。扫整个函数会把 `order_by(MediaAsset.created_at)`
    # 一起算成过滤条件 —— 排序不改变结果集合,拦它只会逼下一个人去加白名单,
    # 而白名单一旦开始变长,这条守卫就没用了
    used: set[str] = set()
    for call in ast.walk(entry):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "where"
        ):
            continue
        for arg in call.args:
            used |= {
                node.attr
                for node in ast.walk(arg)
                if isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "MediaAsset"
            }
    assert used, "where() 里一个 MediaAsset 列都没有?粗筛的形状变了,这条守卫的假设过期了"
    extra = used - declared - scoping
    assert not extra, (
        f"粗筛用了没在 _COARSE_FILTER_COLUMNS 里声明的列:{sorted(extra)} —— "
        "先去 tests/pure/test_a45_batch14_7_evidence_class.py 证明判定蕴含它,"
        "再把它加进那张表。加错一条的表现是识别输入静静少了几张图"
    )


def test_the_coarse_filter_actually_excludes_the_two_expensive_sources():
    """粗筛必须真的把 AI 图和渠道回抓图挡在库里。

    删掉这一条 SQL 不会让任何结果变错(Python 判定还在兜),但会让每一次
    识别都把整个素材库读出来再丢掉 —— 而生成流水线**每跑完一轮就往里加图**。
    也就是说这条 SQL 一旦没了,代价随时间线性增长,而且不会有任何征兆。
    """
    entry = ast.unparse(_func(MEDIA_SERVICE, ENTRY))
    for source in (MediaSource.AI_GENERATED, MediaSource.PLATFORM_SYNC):
        assert source.name in entry or source.value in entry, (
            f"粗筛没有排除 {source.name} —— 每一次识别都会把它们读出来再丢掉,"
            "而生成流水线每跑完一轮就往素材库里加几张"
        )
    assert "generation_task_id" in entry and "generation_candidate_id" in entry, (
        "粗筛没有用上两条溯源列 —— 它们正是为了这一刻在 14-16 落的库"
    )


# ==========================================================
# 三、旧入口不许再出现在识别路径上
# ==========================================================


def test_the_extraction_path_never_touches_the_unfiltered_query():
    """`run_extraction` 里不许出现 `usable_assets`。

    这条是"唯一取数入口"的真正落点。只钉"有没有调用白名单"是不够的 ——
    白名单调用还在、旁边同时留着一个装未过滤行的变量时,守卫全绿,
    而下一个人拿哪个变量去喂抽取器全看运气。

    要条数请用 `usable_asset_count()`:它返回 int,**拿不到行,
    就不会有人用错行**。
    """
    node = _func(EXTRACTION_SERVICE, "run_extraction")
    used = _attr_calls(node)
    assert "usable_assets" not in used, (
        "run_extraction 里出现了 usable_assets —— 它返回未过滤的行"
    )
    assert ENTRY in used, f"run_extraction 没有走 {ENTRY}()"


def test_the_extraction_path_has_exactly_one_binding_for_its_asset_list():
    """喂给抽取器的那个清单,在 `run_extraction` 里只许被绑一次。

    ## 这条补的是一个被本批改动关掉的洞

    14-7 的变异 P1 是「过滤结果算出来了但没赋回 `assets`」。在旧形状里那是
    静默致命的:`assets` 先被绑成 `usable_assets()` 的未过滤结果,删掉赋值
    之后代码照跑,**用的是未过滤的那一份**。

    本批把未过滤取数移出了这个函数,于是 `assets` 只剩一个绑定点 ——
    删掉它变成 NameError,第一次调用就炸。P1 因此退役(理由写在
    `tools/mutate_batch14_7.py` 里),而"只剩一个绑定点"这条性质从此
    需要有人从正面钉住:**它一旦不成立,P1 那个洞就重新打开。**

    第二个绑定点长什么样不重要 —— 未过滤取数、缓存、兜底的空列表,
    任何一个都会让"喂进去的到底是哪一份"重新变成一个要读代码才知道的问题。
    """
    node = _func(EXTRACTION_SERVICE, "run_extraction")
    bindings = [
        stmt
        for stmt in ast.walk(node)
        if isinstance(stmt, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "assets" for t in stmt.targets)
    ]
    assert len(bindings) == 1, (
        f"`assets` 在 run_extraction 里被绑了 {len(bindings)} 次 —— "
        "多一个绑定点,「喂进抽取器的是哪一份」就重新变成读代码才知道的事"
    )
    traced = ast.unparse(bindings[0].value)
    names = {n.id for n in ast.walk(bindings[0].value) if isinstance(n, ast.Name)}
    for stmt in ast.walk(node):
        if isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in names for t in stmt.targets
        ):
            traced += "\n" + ast.unparse(stmt.value)
    assert ENTRY in traced, (
        f"`assets` 追不到 {ENTRY}():\n{traced}\n"
        "喂进付费抽取器的清单来路不明"
    )


def test_every_remaining_caller_of_the_unfiltered_query_is_accounted_for():
    """**这条守卫是一笔账。** 未过滤取数今天还剩哪些调用点,逐个点名。

    `usable_assets()` 本身没有错 —— §4.5 说得很清楚,对**上架链路**来说
    AI 图和人工上传的图从落库那一刻起就是同一种东西。§5.1 是它的那条例外,
    只管识别链路。

    所以这里不禁用它,而是要求每一个调用点都是被想过的。新增一个时这条会红,
    那时问一句:这个调用点喂的是上架还是识别?**喂识别就换成 `evidence_assets_for`。**
    """
    allowed = {
        # `build_canonical`:识别层与渠道层之间唯一的商品契约(§5.5)。
        # §4.5 的正例 —— 渠道要的是"这个商品有哪些图",而 AI 生成的成品图
        # 正是要发出去的那些。这里换成 evidence_assets_for() 才是错的
        ("service.py", "build_canonical"),
    }
    found: set[tuple[str, str]] = set()
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(_src(path))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            if "usable_assets" in _attr_calls(fn):
                found.add((path.name, fn.name))

    assert found == allowed, (
        f"未过滤取数的调用点变了。多出来的:{sorted(found - allowed)};"
        f"消失的:{sorted(allowed - found)}。"
        "新增一个时先问:这个调用点喂的是上架还是识别?"
        "喂识别就换成 evidence_assets_for(),然后回来改这张表"
    )


# ==========================================================
# 四、几处过期说明,顺手钉住
# ==========================================================


def test_the_provenance_columns_the_adapter_reads_really_exist_now():
    """适配器用 `getattr` 读的那两列,现在是真列了。

    14-7 写它的时候两列还不存在。14-16 落了库,而 14-19 的粗筛正是靠它们
    把 AI 图挡在库里 —— 所以这条钉的是"那两列还在,并且粗筛还在用它们"。

    列没了的表现:粗筛静静少一个条件,Python 判定还兜得住结果,
    只是每一次识别都要把整个素材库读出来再丢掉。
    """
    model = _src(APP / "models" / "media_asset.py")
    for column in ("generation_task_id", "generation_candidate_id"):
        assert f"{column}: Mapped[UUID | None]" in model, (
            f"{column} 不是 MediaAsset 上的真列了?那 evidence_assets_for 的粗筛要改"
        )
    entry = ast.unparse(_func(MEDIA_SERVICE, ENTRY))
    for column in ("generation_task_id", "generation_candidate_id"):
        assert column in entry, (
            f"粗筛没有用 {column} —— 它正是为了这一刻在 14-16 落的库"
        )

    # **这里刻意不去扫 `evidence_rules.py` 的注释文字。**
    #
    # 第一版写的是 `assert "今天不存在" not in rules`,它当场被自己的修复说明
    # 命中了 —— 那句话现在出现在"原来这里写着…那句话过期了"里面。
    # 这是这个仓库反复栽的同一个坑:**注释里出现的字样不是代码在做的事**。
    #
    # 更根本的是 CLAUDE.md 那条:「规矩要么是不变式,要么是散文」。
    # "注释别过期"是散文,拿字符串匹配假装成门禁,结果是它既拦不住真正过期的
    # 说法(换个措辞就绕开),又会拦住正确的改动。注释的准确性靠上面那两条
    # 结构断言间接守着:列在、粗筛用着它们。


def test_the_scope_vocabulary_matches_the_fingerprint_module():
    """`SHARED_SCOPE` 和指纹那边的 `SHARED` 必须是同一个约定。

    两处各写一个字面量 `None` 的话,哪天有人把其中一处改成 `""`,
    表现是共享素材在指纹里算、在取数里不算 —— 两边各自都"对",
    而**事实过期判定和识别输入从此看的不是同一批图**。

    ## 为什么静态比对而不是 import 两个常量比一下

    `import app.media.service` 要 sqlalchemy,这台机器没有 —— 那样写的话
    这条守卫在**最常被执行的那个运行器上恒为 SKIP**,也就是等于不存在。
    两个常量各自绑的是不是字面量 `None`,AST 看得一清二楚。
    """
    targets = {
        (APP / "attributes" / "scope_fingerprint.py", "SHARED"),
        (MEDIA_SERVICE, "SHARED_SCOPE"),
    }
    for path, name in sorted(targets):
        bound: list[ast.AST] = []
        for node in ast.walk(ast.parse(_src(path))):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id == name and node.value is not None:
                    bound.append(node.value)
            elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets
            ):
                bound.append(node.value)
        assert len(bound) == 1, f"{path.name} 里 {name} 绑定了 {len(bound)} 次"
        only = bound[0]
        assert isinstance(only, ast.Constant) and only.value is None, (
            f"{path.name} 的 {name} 不是字面量 None,而是 {ast.unparse(only)} —— "
            "共享作用域在两处的含义从此不同,而两边各自都不会红"
        )
