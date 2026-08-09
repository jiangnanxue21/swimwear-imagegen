"""每一步的完成条件,前端必须造得出来(阶段 6 回归修复 / F-10)。

## 这个文件存在的理由

F-10 的形状:PRD §14.1 把方案步的完成定义成「有一份当前生效的方案,
**且方案里选了模特**」;`service._plan_facts` 照着写了
`has_model = active.model_template_id is not None`;`flow._evaluate_plan`
照着判 `NEEDS_CONFIRM "方案缺模特"`。三层全对。

而 `GenerationPlanPanel` 的创建表单只有四个字段,`model_template_id`
不在里面。于是**向导里能造出来的方案,`model_template_id` 恒为 `null`** ——
方案步在向导内永远停在 `NEEDS_CONFIRM`,七步永远走不完,完成度上限 96%。

后端一侧一切正常:schema 收这个字段、`save_plan` 收它、还跑
`assert_usable` 校验它。缺的是**最后一跳的另一端** —— 表单里的一个
`<Form.Item>`。§3.43 那一族("上游算对了、没人接最后一跳")在这里
第一次跨了语言:两端各自完整,而它们对不上,两边的测试都绿。

判定层的用例更是看不见:`test_a45_batch29_wizard._flow()` 的夹具写着
`PlanFacts(has_model=True)` —— **一个前端一天都造不出来的状态**。
所有向导用例都在这个状态下跑。

## 所以这条守卫按"字段"对账,不按"步骤是否 DONE"对账

它读两样东西:

    后端   完成条件依赖哪些列(下面那张表,人工登记 + 逐条给出判定出处)
    前端   创建这个对象的表单里有哪些 `name="..."`

前者有而后者没有 = 那一步在界面上做不完。**这一条不需要浏览器,
也不需要跑前端** —— 它读的是 `.tsx` 源文件里的字符串。

## 它不能证明什么

它证明"表单里有这个字段",不证明"填了之后真的能走通"。后者要 UAT。
但 F-10 那一档("字段根本不存在")是它能当场抓住的,而那一档在本仓
活了整个阶段 6。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
FRONTEND = REPO / "frontend" / "src"


#: 步骤完成条件 -> (判定出处, 前端表单文件, 表单里必须出现的字段名)
#:
#: **每一行都要写出判定出处**,否则下一个人无从判断这行是不是还成立。
#: 新增一个"完成条件依赖某列"的判定而不登记在这里,守卫抓不到 ——
#: 这是本表的已知边界,写在这里而不是假装没有。
COMPLETION_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    (
        "PLAN",
        # flow._evaluate_plan: `if not facts.has_model -> NEEDS_CONFIRM`
        # service._plan_facts: `has_model = active.model_template_id is not None`
        "service._plan_facts.has_model",
        "components/GenerationPlanPanel.tsx",
        "model_template_id",
    ),
    (
        "PLAN",
        # flow._evaluate_plan: `if not facts.has_active_plan -> TODO + PLAN_MISSING`
        # 方案必须带角度,否则 `plan_problems` 拒收 —— 有方案而角度为空
        # 在库里是造不出来的,所以这一行验的是"表单能造出一份合法方案"
        "generation_plan.angles(plan_problems 要求非空)",
        "components/GenerationPlanPanel.tsx",
        "angles",
    ),
)


def _form_field_names(path: Path) -> set[str]:
    """从 `.tsx` 里抠出所有 `name="..."` / `name={'...'}`。

    正则而不是解析 TSX:本仓的纯测试层不许引入 node 工具链,而这一层
    只需要回答"这个字符串在不在表单里"。误判方向是**偏宽**(把注释里的
    `name="x"` 也算上),而偏宽的守卫只会漏报不会误报 —— 对一条
    "缺字段"守卫来说,这个方向是对的。
    """
    src = path.read_text(encoding="utf-8")
    return set(re.findall(r"""name=\{?["']([A-Za-z_][A-Za-z0-9_]*)["']\}?""", src))


def test_the_form_can_produce_what_the_step_needs_to_be_done():
    for step, source, form, field in COMPLETION_FIELDS:
        path = FRONTEND / form
        assert path.exists(), f"{form} 不存在 —— 表单挪了位置,这张表要跟着改"

        names = _form_field_names(path)
        assert field in names, (
            f"{step} 步的完成条件依赖 `{field}`(判定出处:{source}),"
            f"而 {form} 的表单里没有这个字段。"
            f"后果不是报错,是这一步在界面上**永远做不完** —— "
            f"判定层照样绿,因为它的夹具直接把那个字段填好了。"
            f"表单里现有的字段:{sorted(names)}"
        )


def test_the_plan_form_actually_sends_the_model_template():
    """光有 `<Form.Item name=...>` 不够,提交时还要真的带上它。

    这两件事分开验是因为它们真的会分开坏:表单加了字段而
    `createGenerationPlan({...})` 的对象字面量忘了带,界面上看得见、
    填得进、点保存之后无声丢弃 —— 比字段不存在更难查。
    """
    src = (FRONTEND / "components" / "GenerationPlanPanel.tsx").read_text(
        encoding="utf-8"
    )
    call = re.search(r"createGenerationPlan\(\{(.*?)\n      \}\)", src, re.S)
    assert call, "找不到 `createGenerationPlan({...})` 调用 —— 提交路径改了形状"

    # **只认键赋值,不认字面量出现。** 第一版写的是
    # `"model_template_id" in call.group(1)`,而那段调用里有一整段注释
    # 在解释这个字段为什么必须在 —— 于是把赋值那一行删掉之后,守卫
    # 被自己的注释喂饱了,照样绿。变异测试当场把这件事顶了出来。
    assigned = re.search(
        r"^\s*model_template_id\s*:", call.group(1), re.M
    )
    assert assigned, (
        "表单里有模特字段,但 `createGenerationPlan` 的入参里没有给它赋值:"
        "填进去的模特会被无声丢弃,方案步照样停在「方案缺模特」"
    )


def test_this_table_stays_in_step_with_the_decision_layer():
    """登记表里的判定出处必须还在。

    出处那一列是这张表**唯一**的可信来源。判定改了名字而这里没跟,
    下一个人读到的是一条指向不存在函数的理由,于是他只能选择相信它。
    """
    service = (REPO / "backend" / "app" / "workbench" / "service.py").read_text(
        encoding="utf-8"
    )
    flow = (REPO / "backend" / "app" / "workbench" / "flow.py").read_text(
        encoding="utf-8"
    )

    assert "has_model=" in service and "model_template_id" in service, (
        "`service._plan_facts` 不再从 `model_template_id` 推 `has_model` —— "
        "PLAN 那两行登记要重新核对"
    )
    assert "if not facts.has_model:" in flow, (
        "`_evaluate_plan` 不再按 `has_model` 判 —— 同上"
    )
