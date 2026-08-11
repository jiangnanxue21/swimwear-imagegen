"""a47 §8.1:款级聚合的三个口径,以及**没有款级 next_action** 这件事。

## 三个口径,各一条

    completion      旗下 SKU 的**最小值**
    blocking_count  旗下 SKU 的**和**
    blocked_steps   `{步骤: 卡在该步的 SKU 数}` —— 分布,不是单值

前两个在 a45 就落了(PRD v2.1 §0 的 M3 说"款级一个都没有",在 r3 基线上
只对一半:它数的是 dataclass 字段,而这两个是计算属性)。本批加第三个。

## 为什么没有款级 next_action —— 这一条比三个口径更重要

v2.0 要求聚合出款级 `next_action_label`,规则是"取阻断数最大的那只 SKU 的
下一步"。那和明令禁止的"拿第一个 SKU 的 next_action 当款级"**只差在任意性
的形式**,本质一样:把一个没有真实语义的推导从前端搬到后端,
并不会让它变成事实。

款级"下一步"在这套状态机里**没有定义**。所以这里有一条负向守卫。
"""
from __future__ import annotations

from app.workbench import spu as spu_rules


def _row(sku: str, **kw) -> spu_rules.SkuRow:
    base = dict(product_id=f"p-{sku}", sku=sku, name=sku)
    base.update(kw)
    return spu_rules.SkuRow(**base)  # type: ignore[arg-type]


def _group(*rows: spu_rules.SkuRow) -> spu_rules.SpuGroup:
    (group,) = spu_rules.aggregate(("SW-001", "泳衣", row) for row in rows)
    return group


# ================================================ 一、completion 取最小值


def test_completion_is_the_slowest_sku_not_the_average():
    """**取平均会让"9 只完成、1 只没开始"显示成 90%",而那一只正是要处理的。**

    上架文件是整个 SPU 一起导的,所以"这款还能不能导出"取决于最慢那只。
    """
    group = _group(
        _row("A", completion=100),
        _row("B", completion=100),
        _row("C", completion=20),
    )

    assert group.completion == 20


def test_an_empty_group_reports_zero_rather_than_crashing():
    """`aggregate()` 不产出空组,但属性不许因此假设 `skus` 非空。"""
    empty = spu_rules.SpuGroup(
        spu="SW-000", name="", skus=(), common=(), inconsistent=(), variants={}
    )

    assert empty.completion == 0
    assert empty.blocking_count == 0
    assert empty.blocked_steps == {}


# ================================================ 二、blocking_count 求和


def test_blocking_count_is_the_sum_no_ambiguity_here():
    group = _group(_row("A", blocking_count=3), _row("B", blocking_count=1))

    assert group.blocking_count == 4


# ================================================ 三、blocked_steps 是分布


def test_the_distribution_counts_skus_per_step():
    """"这款有 3 只卡在图片集、1 只卡在文案" —— 运营据此决定先动哪一只。"""
    group = _group(
        _row("A", blocked_steps=("IMAGE_SET",)),
        _row("B", blocked_steps=("IMAGE_SET",)),
        _row("C", blocked_steps=("IMAGE_SET",)),
        _row("D", blocked_steps=("COPY",)),
    )

    assert group.blocked_steps == {"IMAGE_SET": 3, "COPY": 1}


def test_one_sku_stuck_in_two_steps_counts_in_both():
    """一只 SKU 可以同时卡在两步(属性没确认 + 图片集缺角度)。

    两边都算,因为运营去修图片集时它在那一格里,去修属性时它也在。
    """
    group = _group(_row("A", blocked_steps=("ATTRIBUTE", "IMAGE_SET")))

    assert group.blocked_steps == {"ATTRIBUTE": 1, "IMAGE_SET": 1}


def test_the_distribution_does_not_add_up_to_the_blocking_count():
    """**这一条是写给拿它们对账的人的。**

    `blocking_count` 数的是问题条数(一只 SKU 在一步里可以有三条阻断),
    `blocked_steps` 数的是 SKU 个数。两个数字回答两个不同的问题,
    不该相等 —— 而"看起来该相等的两个数不相等"是最容易被当成 bug 报上来的
    那一类,所以它得有一条用例摆在这里。
    """
    group = _group(_row("A", blocking_count=3, blocked_steps=("IMAGE_SET",)))

    assert group.blocking_count == 3
    assert sum(group.blocked_steps.values()) == 1


def test_a_step_nobody_is_stuck_in_does_not_show_up_as_zero():
    """一张全是 0 的表会让真正有值的那一格淹掉。"""
    group = _group(_row("A", blocked_steps=("COPY",)))

    assert "IMAGE_SET" not in group.blocked_steps


def test_duplicate_steps_on_one_row_count_once():
    """去重在判定层,不指望取数层送进来的已经是集合。

    取数层今天送的是 `sorted({...})`,而"今天是集合"和"永远是集合"是
    两件事 —— 下一个人把它改成 list comprehension 时这里不会红,
    但那一改会让一只 SKU 在同一格里被数三次。
    """
    group = _group(_row("A", blocked_steps=("COPY", "COPY")))

    assert group.blocked_steps == {"COPY": 1}


def test_a_group_with_nothing_blocked_reports_an_empty_distribution():
    assert _group(_row("A"), _row("B")).blocked_steps == {}


# ================================================ 四、负向:没有款级 next_action


def test_there_is_no_spu_level_next_action():
    """**款级"下一步"在这套状态机里没有定义。**

    不同 SKU 卡在不同步骤时,任何单选都是编的 —— 而编出来的那个值会被
    运营当成系统的判断照着做。SKU 级的 `next_action_label` 留着
    (那一层有真实定义),款级一个都不给。

    这条守卫看的是**属性不存在**,不是"值为空":给一个恒空的属性同样能
    让这条绿,而界面上会长出一列永远空着的"下一步",然后有人去把它填上。
    """
    group = _group(_row("A", next_action_label="去确认属性"))

    assert not hasattr(group, "next_action_label")
    assert not hasattr(group, "next_action")
    # SKU 级那一份还在 —— 展开行要用它
    assert group.skus[0].next_action_label == "去确认属性"


def test_the_serialized_group_carries_the_distribution_and_no_next_action():
    (row,) = spu_rules.serialize([_group(_row("A", blocked_steps=("COPY",)))])

    assert row["blocked_steps"] == {"COPY": 1}
    assert "next_action_label" not in row
    assert "next_action" not in row
    # 展开行那一层照旧
    assert row["skus"][0]["next_action_label"] == ""
    assert row["skus"][0]["blocked_steps"] == ["COPY"]


# ================================================ 五、取数层真的填了这一列


def test_the_query_layer_fills_blocked_steps_from_the_blocking_issues():
    """级别判定必须留在 `flow.py`,取数层只收集。

    在取数层重判一次"什么算阻断"就是给这件事造第二个答案 —— 而两个答案
    分叉时,列表页的阻断数和按款视图的卡点分布会互相矛盾,
    两边都不报错。
    """
    import ast

    from tests.pure._helpers import BACKEND_ROOT

    source = (BACKEND_ROOT / "app" / "api" / "workbench_batch.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    call = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "SkuRow"
    )
    dumped = ast.dump(call)

    assert "blocked_steps" in dumped, "取数层没有填 `blocked_steps`,那一列会恒为空"
    assert "IssueLevel" in dumped and "BLOCKING" in dumped, (
        "卡点分布不是按 BLOCKING 级别收集的"
    )
    assert "target_step" in dumped, (
        "收集的不是 issue 的 target_step —— 那才是「这条问题该去哪一页解决」"
    )
