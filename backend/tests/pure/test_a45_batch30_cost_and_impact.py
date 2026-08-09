"""A45-batch30(阶段 6 批次 6-5):阻塞与费用展示 + 上游变化影响提示(AC-17)。

## 三节

    一   费用预估:查不到价给 None 不给 0,算不出数量的单独列
    二   影响提示:口径全部取自 `stale_matrix`,提示处不另算一份
    三   接线:两样都从 HTTP 出得去,而且都是只读

## 这一批最容易做错的一件事

给一个"预计 ¥0"。它在开发环境里几乎必然出现(Mock 一个都不在价目表里),
看起来无害 —— 而它说的是"这一步不花钱"。真实语义是"我们不知道要花多少"。
本仓为 NULL 与 0 的区别写过整段(`core/pricing.resolve_unit_price`),
这一节把那条规矩扩到预估上。
"""
from __future__ import annotations

import ast

from app.core.pricing import parse_price_book
from app.workbench import impact
from app.workbench.flow import ACTION_LABELS, NextActionCode
from app.workbench.stale_matrix import MATRIX, ChangeSource, Effect, Target, cell
from app.workflows import cost_estimate
from app.workflows.cost_estimate import Demand, UnknownDemand, estimate
from tests.pure._helpers import BACKEND_ROOT, expect_raises

API = BACKEND_ROOT / "app" / "api" / "workbench.py"
IMPACT = BACKEND_ROOT / "app" / "workbench" / "impact.py"
COST_ROLLUP = BACKEND_ROOT / "app" / "workbench" / "cost_rollup.py"

BOOK = parse_price_book('{"fashn": {"*": {"micros": 40000, "currency": "USD"}}}')


# ======================================================== 一、费用预估


def test_an_unpriced_operation_is_unknown_not_free():
    """查不到单价 -> 金额 `None`,**不是 0**。

    给 0 的表现是向导上写着「预计 ¥0」,而运营点下去花了真钱。
    那个数字比不显示更糟:它是一句有具体数值的假话。
    """
    result = estimate(
        [Demand(operation="submit", provider="mock", units=6, basis="缺 6 个角度")],
        price_book=BOOK,
    )
    line = result.lines[0]
    assert line.cost_micros is None, "未配价的动作被算成了 0 元"
    assert line.unit_price_micros is None
    assert not line.priced
    assert result.unpriced_operations == ("submit",)
    # 总额里不含它。含进去(按 0)的话总额永远偏小,而偏小的方向是恒定的
    assert result.totals == {}
    assert not result.is_complete


def test_the_serialised_line_has_no_money_string_when_the_price_is_unknown():
    """未配价的行**不给 `cost_display`**。

    格式化一个不存在的金额就是把"不知道"写成了"¥0.00" —— 而界面上
    那一列会照样右对齐显示,看起来和真的一样。
    """
    payload = cost_estimate.serialize(
        estimate(
            [Demand(operation="submit", provider="mock", units=1, basis="x")],
            price_book=BOOK,
        )
    )
    assert payload["lines"][0]["cost_display"] is None
    assert payload["is_estimate"] is True, "预估必须自报家门 —— 它会和真实台账同屏"


def test_a_priced_operation_multiplies_and_sums_by_currency():
    result = estimate(
        [
            Demand(operation="submit", provider="fashn", units=3, basis="缺 3 个角度"),
            Demand(operation="submit", provider="fashn", units=2, basis="另一个颜色"),
        ],
        price_book=BOOK,
    )
    assert result.totals == {"USD": 40000 * 5}
    # **不合并**:两条各自带着自己的 basis,而运营问"为什么是 5 张"时
    # 要的正是那两句话
    assert len(result.lines) == 2
    assert result.is_complete


def test_currencies_are_never_added_together():
    """不同币种分开合计。micros 是某个币种的百万分之一,相加得到的数没有单位。"""
    book = parse_price_book(
        '{"a": {"*": {"micros": 1000, "currency": "USD"}},'
        ' "b": {"*": {"micros": 2000, "currency": "CNY"}}}'
    )
    result = estimate(
        [
            Demand(operation="submit", provider="a", units=1, basis="x"),
            Demand(operation="submit", provider="b", units=1, basis="y"),
        ],
        price_book=book,
    )
    assert result.totals == {"CNY": 2000, "USD": 1000}


def test_an_uncountable_demand_is_listed_instead_of_counted_as_zero():
    """算不出数量的那一部分单独列,不当成 0。

    一个还没有生成方案的颜色要出几张图今天算不出来。当成 0 的表现是
    预估说"不用花钱",而选完方案之后它突然变成十几张 —— 那种数字
    没有人会再信第二次。这是"没算过 ≠ 没做"在费用上的同一条。
    """
    result = estimate(
        [],
        price_book=BOOK,
        unknown=[
            UnknownDemand(
                operation="submit",
                label="出图",
                reason="这些颜色还没有生成方案",
                refs=("BLU", "RED"),
            )
        ],
    )
    assert not result.is_complete, "有算不出的部分却自称算全了"
    payload = cost_estimate.serialize(result)
    assert payload["unknown"][0]["refs"] == ["BLU", "RED"]
    assert payload["is_complete"] is False


def test_a_zero_unit_demand_produces_no_line():
    """0 次的需求不产出行。一行「0 次 × ¥0.04 = ¥0」占一格却什么也没说。"""
    result = estimate(
        [Demand(operation="submit", provider="fashn", units=0, basis="x")],
        price_book=BOOK,
    )
    assert result.lines == ()


def test_negative_units_fail_loudly():
    """负数当场炸。**不 clamp 到 0** —— 一个算出负张数的取数层有 bug,
    而把它悄悄改成 0 只会让那个 bug 表现成"预估偏小"。"""
    expect_raises(
        Exception,
        estimate,
        [Demand(operation="submit", provider="fashn", units=-1, basis="x")],
        price_book=BOOK,
    )


def test_why_an_operation_is_missing_is_answerable():
    """"这一项为什么不在清单里"要有答案。

    当前评分器是 Mock 时它一分钱不花,而界面上"评分"那一行的消失
    与"我们忘了算评分"长得一模一样。
    """
    payload = cost_estimate.serialize(
        estimate([], price_book=BOOK, notes=["候选图评分:当前评分器不计费,未计入预估"])
    )
    assert payload["notes"], "没有任何地方说得出某一项为什么不在清单里"


def test_the_operation_labels_match_the_operations_that_really_write_usage_rows():
    """计费动作名必须与 `record_usage(operation=...)` 传的字面量一致。

    对不上的表现是预估查了一个厂商永远不会收费的动作,金额恒为"未配价",
    而看板上那个动作的真实花费一直在涨 —— 两个页面各说各的。
    """
    written: set[str] = set()
    for path in (BACKEND_ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name != "record_usage":
                continue
            for kw in node.keywords:
                if kw.arg == "operation" and isinstance(kw.value, ast.Constant):
                    written.add(str(kw.value.value))
    assert written, "没有从 app/ 里解析出任何 record_usage(operation=...)"
    unknown = written - set(cost_estimate.OPERATION_LABELS)
    assert not unknown, (
        f"这些动作会写流水但预估层不认识它们:{sorted(unknown)} —— "
        "预估会漏掉它们的钱,而漏掉的方向恒定是少算"
    )


def test_the_cost_layer_reads_the_same_price_book_as_the_ledger():
    """预估读的价目表与台账是**同一份**入口(`spend.price_book`)。

    自己 `parse_price_book(settings.PROVIDER_PRICE_BOOK)` 的表现是绕开
    「解析不了就当没配价、绝不往上抛」那条规矩,于是 `.env` 里一个 JSON
    拼写错误会让向导整页 500,而台账页照常显示。
    """
    src = COST_ROLLUP.read_text(encoding="utf-8")
    assert "price_book()" in src
    assert "parse_price_book(" not in src, "取数层自己解析了价目表 —— 绕开了那条降级规矩"


def test_the_estimate_only_counts_active_colours():
    """预估只算 ACTIVE 颜色(§6.7 / AC-18 的同一条范围口径)。

    把停售颜色算进去的表现:运营为了让预估变小,去给一个不卖的颜色补图。
    """
    src = COST_ROLLUP.read_text(encoding="utf-8")
    assert "if v.is_active" in src, "取数层没有按 ACTIVE 筛"


# ======================================================== 二、影响提示


def test_every_change_source_has_a_label():
    """八个变更源穷举。少一个的表现是选择器里没有那一项,
    而运营改那样东西之前看不到任何提示 —— AC-17 要的正是"修改之前"。"""
    assert set(impact.SOURCE_LABELS) == set(ChangeSource)
    assert all(impact.SOURCE_LABELS[s].strip() for s in ChangeSource)


def test_every_target_has_a_label_and_an_action():
    """四列穷举:名字与"过期之后该做什么"都要有。

    少了动作,提示说"文案会过期",而运营的下一个问题在界面上没有答案。
    """
    assert set(impact.TARGET_LABELS) == set(Target)
    assert set(impact.TARGET_ACTION) == set(Target)
    for action in impact.TARGET_ACTION.values():
        assert action in ACTION_LABELS, f"{action} 不是一个界面上存在的动作"
        assert isinstance(action, NextActionCode)


def test_the_effect_of_every_cell_comes_from_the_matrix():
    """每一格的结论**逐格等于** `stale_matrix`,提示处不另算一份。

    这是 AC-17 后半句的字面守卫。分叉的表现是提示说"不影响",
    而系统照样把草稿打成 STALE —— 而运营是照着提示决定要不要改的。
    """
    for source in ChangeSource:
        rows = {row.target: row for row in impact.preview(source)}
        assert set(rows) == set(Target), f"{source} 少了列"
        for target in Target:
            expected = cell(source, target)
            assert rows[target].effect is expected.effect
            assert rows[target].mechanism == expected.mechanism


def test_the_module_does_not_carry_a_second_effect_table():
    """模块里没有第二张效应表。

    **先剥 docstring 再扫** —— 本模块的文档正好在解释"过期"这件事,
    照着文本扫会被自己的注释喂饱(本仓同型第十五次的形状,§3.55 记过)。
    """
    tree = ast.parse(IMPACT.read_text(encoding="utf-8"))
    body = "\n".join(
        ast.unparse(node)
        for node in tree.body
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
    )
    for literal in ("'过期'", '"过期"', "'不过期'", '"不过期"'):
        assert literal not in body, (
            f"提示层出现了效应字面量 {literal} —— 口径必须只有 stale_matrix 一份"
        )


def test_unaffected_targets_are_shown_too():
    """"不受影响"也显示出来。

    只列受影响的,清单里没有文案就有两种可能:不受影响,或者这张表漏了它。
    两者在界面上长得一样,而后者本仓真的发生过(FACTS 那一列是补上去的)。
    """
    rows = impact.preview(ChangeSource.COPY_RULES_CHANGED)
    assert len(rows) == len(Target)
    assert any(not row.affected for row in rows), (
        "这个变更源在矩阵里有不受影响的格子,而提示里一个都没有"
    )
    for row in rows:
        if row.affected:
            assert row.action is not None
        else:
            # 不受影响的那一行不给动作:给了的表现是运营对着一个
            # "不用做任何事"的对象点了一次重新生成
            assert row.action is None


def test_the_source_catalogue_lists_what_each_change_will_touch():
    catalogue = {row["source"]: row for row in impact.describe_sources()}
    assert set(catalogue) == {s.value for s in ChangeSource}
    row = catalogue[ChangeSource.CONFIRMED_ATTRIBUTE_MODIFIED.value]
    assert row["trigger"], "没有说这次失效什么时候发生"
    expected = [
        impact.TARGET_LABELS[c.target]
        for c in MATRIX
        if c.source is ChangeSource.CONFIRMED_ATTRIBUTE_MODIFIED
        and c.effect is not Effect.NOT_AFFECTED
    ]
    assert row["affects"] == expected


# ======================================================== 三、接线


def test_both_endpoints_are_reads():
    """影响预览与变更源清单都是 GET。

    "修改之前先看看会影响什么"必须无副作用,否则运营为了看一眼影响
    就先改了一次状态 —— 而他看这一眼恰恰是为了决定要不要改。
    """
    src = API.read_text(encoding="utf-8")
    assert '@router.get("/change-sources")' in src
    assert '@router.get("/products/{product_id}/impact")' in src
    tree = ast.parse(src)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "change_impact"
    )
    body = ast.unparse(fn)
    assert "session.commit()" not in body, "只读端点提交了事务"
    assert "session.rollback()" in body, (
        "只读端点没有回滚 —— `collect` 会算出 STALE 结论,刷新页面就会改状态"
    )


def test_an_unknown_change_source_is_rejected_not_answered_with_an_empty_list():
    """认不出的变更源报错,不返回空清单。

    空清单在界面上是「这次修改不影响任何东西」—— 一句错得很有说服力的话。
    """
    tree = ast.parse(API.read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "change_impact"
    )
    body = ast.unparse(fn)
    assert "ValidationError(" in body and "未知的变更源" in body


def test_the_cost_estimate_reaches_the_wizard_payload():
    """费用预估从 HTTP 出得去,而且挂在向导块上(AC-15 的"一次请求")。"""
    src = API.read_text(encoding="utf-8")
    assert "cost_estimate.serialize(cost_rollup.build_estimate(" in src, (
        "费用预估没有接到出参上 —— 又一次「算好了没人读」"
    )
    assert "cost=cost" in src


def test_no_cost_is_not_the_same_as_free():
    """没有颜色维时费用位是 `None`,不是一个"合计 0"的空壳。

    「没算」与「不要钱」在预算这件事上差得最远。
    """
    tree = ast.parse(API.read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_wizard_block"
    )
    body = "\n".join(ast.unparse(s) for s in fn.body[1:])
    assert "if views else None" in body
