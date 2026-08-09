"""A45-batch26(阶段 6 批次 6-2 的前置):`FlowStep` 的每一张表都必须穷举。

## 这一批是怎么来的

6-1 的结论文件里写着 6-2 的开工方法:

    先跑一遍让穷举契约测试变红,红的清单就是要改的清单。
    不要反过来先改代码再看哪里红。

照着做了。给 `FlowStep` 加上 `SETUP` 与 `PLAN` 两个成员之后,**后端一条都
没红** —— 2650 条纯用例里只红了 3 条,其中 2 条是前端契约测试
(`test_every_frontend_label_table_covers_its_backend_enum` 与
`..._union_type_matches_...`),1 条是 6-1 自己埋的 `_PLAN_PENDING` 守卫。

也就是说:

    STEP_ORDER   加了成员不进来 -> 这一步在流程里**不存在**
    STEP_LABELS  加了成员不进来 -> 界面上没有名字
    完成度       `STEP_WEIGHT = 20.0` 那句「5 步 × 20 = 100」是**算术**,
                 不是断言 —— 七步之后合计 140,而没有任何地方会说一声
    evaluate()   `by_step` 是手写的五项字典,新步骤不在里面

红的清单是空的,而这恰恰是问题:**6-2 可以把两个步骤加进枚举、只接一半,
而全仓保持绿色**。这与 §3.54 记的三条缺口是同一族(接了一半、守卫覆盖着
接上的那一半),区别只是这一次它还没发生 —— 所以本批把它堵在发生之前。

## 为什么把它单独作为一批,而不是塞进 6-2

因为它要证明的是「6-2 做错了会红」。和 6-2 同批的话,守卫与被守的东西
一起进来,没有人能说清那些守卫是不是**在改完之后照着结果写的**。
先落守卫、再增维,顺序本身就是证据。

## 本批同时改了一处表达方式

`STEP_WEIGHT = 20.0`(常量)换成 `STEP_WEIGHTS`(逐步表)。**口径没变**
(仍是五步等权、合计 100),换的是它能不能被断言:一个常量加一句注释,
断言不出"合计是 100";一张表可以。而 6-2 的口径迁移会集中发生在这张表上。
"""
from __future__ import annotations

import ast

from app.workbench import flow
from app.workbench.flow import FlowStep, StepState
from tests.pure._helpers import BACKEND_ROOT

FLOW = BACKEND_ROOT / "app" / "workbench" / "flow.py"


def test_every_step_is_in_the_order():
    """`STEP_ORDER` 必须覆盖 `FlowStep` 的每一个成员。

    不覆盖的表现最安静:那一步**在流程里不存在** —— 不算完成度、不进
    「唯一下一步」、不出现在任何页面上,而枚举里明明有它。
    实测过:加两个成员,后端一条不红。
    """
    missing = [s.value for s in FlowStep if s not in flow.STEP_ORDER]
    assert not missing, (
        f"{missing} 在 `FlowStep` 里而不在 `STEP_ORDER` 里 —— "
        "这些步骤不参与流程判定,而没有任何地方会说一声"
    )
    assert len(flow.STEP_ORDER) == len(set(flow.STEP_ORDER)), "STEP_ORDER 里有重复"


def test_every_step_has_a_label():
    """每一步都要有中文名。缺了的表现是界面上出现一个英文枚举值。"""
    missing = [s.value for s in FlowStep if s not in flow.STEP_LABELS]
    assert not missing, f"{missing} 没有中文标签"


def test_every_step_has_a_weight_and_they_sum_to_one_hundred():
    """完成度权重必须逐步给,且**合计 100**。

    这一条是本批的由来。原来这里是 `STEP_WEIGHT = 20.0` 加一句注释
    「5 步 × 20 = 100」—— 那是算术:七步之后合计 140,完成度变成一个
    最大值 140 的数,而它驱动列表排序与审阅队列。没有任何地方会红。
    """
    missing = [s.value for s in FlowStep if s not in flow.STEP_WEIGHTS]
    assert not missing, f"{missing} 没有完成度权重 —— 它们对完成度不产生任何影响"
    total = sum(flow.STEP_WEIGHTS.values())
    assert total == 100.0, (
        f"完成度权重合计 {total},不是 100 —— "
        "完成度会超出或达不到 100%,而它驱动列表排序与审阅队列"
    )


def test_evaluate_returns_one_result_per_step():
    """`evaluate()` 的返回值必须每一步一条,与 `STEP_ORDER` 逐项对齐。

    `by_step` 在 `evaluate()` 里是手写的字典。加一步而忘了写进去,
    `ordered = tuple(by_step[s] for s in STEP_ORDER)` 会 KeyError ——
    那是**好的失败**(响亮)。而如果有人为了让它别炸改成 `.get()` 或
    加个 `if s in by_step`,失败就变安静了:那一步从此不在结果里,
    完成度少一块,而界面照常渲染。所以这里同时钉两件事。
    """
    result = flow.evaluate(flow.ProductFlow(product_id="p", sku="s", spu="S"))
    assert [s.step for s in result.steps] == list(flow.STEP_ORDER), (
        "evaluate 返回的步骤与 STEP_ORDER 对不上"
    )

    source = FLOW.read_text(encoding="utf-8")
    tree = ast.parse(source)
    evaluate_fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "evaluate"
    )
    body = "\n".join(ast.unparse(s) for s in evaluate_fn.body)
    for escape_hatch in ("by_step.get(", "if s in by_step", "if step in by_step"):
        assert escape_hatch not in body, (
            f"`evaluate` 里出现了 {escape_hatch} —— "
            "漏写一步会从 KeyError(响亮)变成静默少一步"
        )


def test_the_completion_of_a_finished_product_is_exactly_one_hundred():
    """全部 DONE 的商品完成度正好 100,不是 99 也不是 140。

    上面那条钉的是权重表自己合计 100;这一条钉的是**算完之后**也是 100 ——
    两者之间还隔着 `STATE_PROGRESS[DONE]` 与 `round()`。
    """
    class _Done:
        state = StepState.DONE
        issues = ()

    done_by_step = {step: _Done() for step in flow.STEP_ORDER}
    total = round(
        sum(
            flow.STATE_PROGRESS[done_by_step[s].state] * flow.STEP_WEIGHTS[s]
            for s in flow.STEP_ORDER
        )
    )
    assert total == 100, f"全部完成算出来是 {total}%"


def test_the_weight_table_is_the_only_place_the_split_is_written():
    """完成度的分配只写在 `STEP_WEIGHTS` 一处。

    第二处的表现是改了表而完成度没变(或反过来),而两处都不报错。
    `STEP_WEIGHT`(单数)是改动前那个常量的名字 —— 它不该再出现。
    """
    source = FLOW.read_text(encoding="utf-8")
    tree = ast.parse(source)
    assigned = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }
    assert "STEP_WEIGHT" not in assigned, (
        "`STEP_WEIGHT` 又回来了 —— 完成度的分配有两个来源"
    )
    assert "STEP_WEIGHTS" in assigned


def test_the_seven_steps_are_the_pipeline_in_section_16():
    """**今天是七步,而且这七步不是新编的。**

    这条守卫的上一版断言「今天是五步」,失败信息里列着 6-2 要同批交付的
    四件事。batch27 增维时它红了,四件事照着做完 —— 它现在换方向:
    钉住七步与 §16 最终产品定义那条管线**逐行对应**。

    另编一套七步的表现是:PRD 说"按颜色上传原始样品"是第二步,而代码里
    第二步叫别的名字、含义也不完全一样 —— 于是"完成"又变成一个只存在于
    实现里的概念,而验收要对着 PRD 做。
    """
    assert [s.value for s in flow.STEP_ORDER] == [
        "SETUP",      # 1 创建 SPU / ColorVariant / SKU
        "MATERIAL",   # 2 按颜色上传原始样品
        "ATTRIBUTE",  # 3 异步识别并确认共享事实与颜色事实
        "PLAN",       # 4 选择模特与生成方案
        "IMAGE_SET",  # 5 按颜色生成、核对并批准图片
        "COPY",       # 6 生成并批准 SPU Listing 与颜色字段
        "DRAFT",      # 7 形成完整 Draft
    ], "步骤序与 PRD §16 的管线对不上了"
    assert set(flow.STEP_ORDER) == set(FlowStep)


def test_the_two_new_steps_are_not_free_points():
    """建档与方案**没做的时候拿不到分**。

    七步增维最省事的错法是给两个新步骤一个宽松默认值:那样所有存量商品
    的完成度当场涨 15 分,列表排序整体上移,而没有任何人做过任何事。
    空视图必须判"未核对",而不是"完成"。
    """
    empty = flow.evaluate(flow.ProductFlow(product_id="p", sku="s", spu="S"))
    assert empty.step_state(FlowStep.SETUP) is not StepState.DONE, (
        "空的建档视图判成了完成 —— 存量商品会凭空得到 5 分"
    )
    assert empty.step_state(FlowStep.PLAN) is not StepState.DONE, (
        "空的方案视图判成了完成 —— 存量商品会凭空得到 10 分"
    )

