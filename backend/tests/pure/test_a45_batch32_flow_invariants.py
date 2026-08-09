"""判定层的三条结构不变量(阶段 6 回归修复 / R-1、R-2、R-3)。

## 这个文件存在的理由

审阅 F-1 的修法是给 `evaluate()` 加一张兜底网 `_no_done_while_blocking`:
带着阻断问题的一步不许显示 DONE。网本身是对的,接的位置错了 ——
它在**最后**统一过一遍,而 `attribute_ready` / `image_set_ready` /
`copy_ready` 和 `_decide_next` 收到的都是**降级前**那一份。于是降级只改
显示,不改判定,出参里会出现这种自相矛盾的组合:

    ATTRIBUTE=IN_PROGRESS(带 AUDIENCE_INVALID)
    IMAGE_SET=DONE  COPY=DONE
    current_step=ATTRIBUTE   next_action=EXPORT

三处后果都不是显示问题:`wizard.step_is_open` 只把 BLOCKED 当关,那些
本该关着的步骤全开,AC-05 那道闸(`api/action_gate`)读的正是这份判定,
`EXPORT` 会被放行;前端 `detectFlowAnomaly` 把这种组合判成 P0,
列表页与详情页的 CTA 一起消失,商品点不动。

而这三条缺陷**一条都没有被现有测试看见**,原因是同一个:
判定层的穷举验的是"给定这组 facts,某一步的状态对不对",没有任何一条
断言问过"这些状态**放在一起**自洽不自洽"。所以本文件不按用例写,
按**不变量**写 —— 它们要对全部事实组合成立,而不是对我想得到的那几个。

## 三条不变量

    I-1  没有任何一步能同时是 DONE 和带着 BLOCKING 问题
    I-2  上游未就绪时下游一律 BLOCKED(降级要顺着 `*_ready` 往下传)
    I-3  `current_step` 与 `next_action.step` 永远是同一步(AC-15「唯一下一步」)

I-3 还有一条独立的破法,与兜底网无关:`_decide_next` 的 docstring 写着
「按 `STEP_ORDER` 走,第一个没做完的步骤决定动作」,而实现是从 `MATERIAL`
开始的 —— `SETUP` 与 `PLAN` 两步整个不在里面。七步增维(batch27)加了步骤,
没加对应的分支。
"""
from __future__ import annotations

import itertools
import random
from dataclasses import replace

import pytest

from app.workbench import flow as flow_module
from app.workbench.flow import (
    AttributeFacts,
    AudienceFacts,
    CopyFacts,
    DraftFacts,
    FlowStep,
    ImageSetFacts,
    Issue,
    IssueLevel,
    MaterialFacts,
    NextActionCode,
    PlanFacts,
    ProductFlow,
    SetupFacts,
    StepState,
    evaluate,
)

# ================================================================ 事实空间
#
# 每一维只取"能改变判定分支"的取值,不取等价值 —— 目标是覆盖分支,
# 不是把数字堆大。全积约 15 亿,所以下面按维度笛卡尔积 + 定量随机抽样。

SETUPS = [
    SetupFacts(has_spu_ref=a, has_colour_ref=b)
    for a in (True, False, None)
    for b in (True, False, None)
]
MATERIALS = [
    MaterialFacts(
        total=t,
        usable=t,
        quarantined=q,
        usable_roles=frozenset(r),
        gate_roles=frozenset(r),
        confirmable_roles=c,
    )
    for t in (0, 2)
    for q in (0, 1)
    for r in ((), ("PRODUCT_FRONT",))
    for c in ((), ("PRODUCT_FRONT",))
]
AUDIENCES = [
    AudienceFacts(audience=a, invalid=i)
    for a, i in ((None, False), ("WOMEN", False), ("???", True))
]
ATTRIBUTES = [
    AttributeFacts(
        required_fields=("fabric",),
        confirmed=frozenset(c),
        suggested=frozenset(s),
        conflicted=frozenset(x),
        candidate_only=frozenset(k),
        extraction_count=e,
    )
    for c in ((), ("fabric",))
    for s in ((), ("fabric",))
    for x in ((), ("fabric",))
    for k in ((), ("fabric",))
    for e in (0, 1)
]
PLANS = [
    PlanFacts(has_active_plan=p, has_model=m)
    for p in (True, False, None)
    for m in (True, False)
]
IMAGE_SETS = [
    ImageSetFacts(
        exists=ex,
        status=st,
        version=1,
        item_count=2,
        violation_codes=vc,
        downgraded=dg,
        reject_reason=rr,
    )
    for ex in (True, False)
    for st in ("APPROVED", "DRAFT", "REJECTED")
    for vc in ((), ("MISSING_PRIMARY_FOR_VARIANT",))
    for dg in (False, True)
    for rr in (None, "OFF_BRAND")
]
COPIES = [
    CopyFacts(
        exists=ex,
        status=st,
        version=1,
        blocking_violations=bv,
        stale=sl,
        reject_reason=rr,
    )
    for ex in (True, False)
    for st in ("APPROVED", "DRAFT", "REJECTED")
    for bv in (0, 1)
    for sl in (False, True)
    for rr in (None, "OFF_BRAND")
]
DRAFTS = [
    DraftFacts(
        exists=ex,
        status=st,
        error_count=ec,
        stale=sl,
        exported=xp,
        open_rejections=orj,
    )
    for ex in (True, False)
    for st in ("VALID", "INVALID")
    for ec in (0, 1)
    for sl in (False, True)
    for xp in (False, True)
    for orj in (0, 1)
]

#: 抽样量。**种子写死** —— 随机数只用来铺开覆盖面,不用来制造不确定性。
#: 一条失败必须能靠重跑复现,否则它会被当成 flaky 忽略掉。
SAMPLE_SIZE = 20000
SEED = 20260809

#: 各维度的字段名,给分层抽样用。顺序与 `STEP_ORDER` 对齐,不是字母序 ——
#: 失败输出里"第几维坏了"要能直接对上是第几步。
DIMENSIONS: dict[str, list] = {
    "setup": SETUPS,
    "material": MATERIALS,
    "audience": AUDIENCES,
    "attribute": ATTRIBUTES,
    "plan": PLANS,
    "image_set": IMAGE_SETS,
    "copy": COPIES,
    "draft": DRAFTS,
}


def _ready(**kw) -> ProductFlow:
    """一件七步全就绪的商品。每条回归用例只改坏一处。"""
    base = dict(
        product_id="p1",
        sku="SW-1",
        spu="SW",
        audience=AudienceFacts(audience="WOMEN"),
        setup=SetupFacts(has_spu_ref=True, has_colour_ref=True, colour_count=1),
        material=MaterialFacts(
            total=3,
            usable=3,
            usable_roles=frozenset({"PRODUCT_FRONT"}),
            gate_roles=frozenset({"PRODUCT_FRONT"}),
        ),
        attribute=AttributeFacts(
            required_fields=("fabric",),
            confirmed=frozenset({"fabric"}),
            extraction_count=1,
        ),
        plan=PlanFacts(has_active_plan=True, has_model=True),
        image_set=ImageSetFacts(
            exists=True, status="APPROVED", version=1, item_count=4
        ),
        copy=CopyFacts(exists=True, status="APPROVED", version=1),
        draft=DraftFacts(exists=True, status="VALID"),
    )
    base.update(kw)
    return ProductFlow(**base)


def _uniform(rng: random.Random, size: int) -> list[ProductFlow]:
    """每一维独立随机。铺开的是**组合**,不保证走得深。"""
    return [
        ProductFlow(
            product_id="p",
            sku="SW-1",
            spu="SW",
            **{name: rng.choice(values) for name, values in DIMENSIONS.items()},
        )
        for _ in range(size)
    ]


def _focused() -> list[ProductFlow]:
    """前缀全就绪 + 只坏一维。**没有这一半,深层步骤根本抽不到。**

    均匀随机走不到 `COPY` / `DRAFT`:要让下一步落在文案上,得同时满足
    建档挂好、素材过门禁、受众合法、属性全确认、方案有模特、图片集已批准 ——
    八维独立采样时这个合取的概率远小于 1/20000,于是那两步在整个抽样里
    一次都不出现,而"这一步永远不会被推荐"恰恰就是 R-3 的形状。

    换句话说:只靠均匀随机的话,这个文件会**漏掉它要抓的那个缺陷**。
    """
    return [
        _ready(**{name: value})
        for name, values in DIMENSIONS.items()
        for value in values
    ]


def _sample() -> list[ProductFlow]:
    return _focused() + _uniform(random.Random(SEED), SAMPLE_SIZE)


@pytest.fixture(scope="module")
def sample() -> list[ProductFlow]:
    return _sample()


# ================================================================ I-1


def test_no_step_is_ever_done_while_carrying_a_blocking_issue(sample):
    """完成与阻断不能同时成立。

    这是 `_no_done_while_blocking` 的性质,但断言写在 `evaluate()` 的出参上
    而不是那个函数上 —— 直接测那个函数证明的是"它算得对",而 F-1 的教训
    正是"算得对但没人在对的位置调它"。
    """
    bad = []
    for pf in sample:
        for step in evaluate(pf).steps:
            if step.state is not StepState.DONE:
                continue
            blocking = [i.code for i in step.issues if i.level is IssueLevel.BLOCKING]
            if blocking:
                bad.append((step.step.name, tuple(blocking), pf))
    assert not bad, (
        f"{len(bad)} 例出现「这一步 DONE,而它自己带着阻断」。"
        f"首例:{bad[0][0]} 带 {bad[0][1]};facts={bad[0][2]}"
    )


# ================================================================ I-2


#: 下游步骤 -> 它的上游必须**全部** DONE。与 `evaluate()` 里的
#: `upstream_ready=` 实参一一对应;这张表和那些实参分叉时,分叉本身就是缺陷。
UPSTREAM: dict[FlowStep, tuple[FlowStep, ...]] = {
    FlowStep.PLAN: (FlowStep.ATTRIBUTE,),
    FlowStep.IMAGE_SET: (FlowStep.ATTRIBUTE,),
    FlowStep.COPY: (FlowStep.ATTRIBUTE,),
    FlowStep.DRAFT: (FlowStep.IMAGE_SET, FlowStep.COPY),
}


def test_a_downstream_step_is_blocked_whenever_its_upstream_is_not_done(sample):
    """上游没就绪 = 下游 BLOCKED,**降级也算没就绪**。

    这一条是 R-1 的本体。兜底网把上游从 DONE 降到 IN_PROGRESS 之后,
    如果 `*_ready` 仍取降级前的值,下游会保持 DONE —— 前端
    `detectFlowAnomaly` 把这种组合判成 P0,商品在列表页与详情页都点不动。
    """
    bad = []
    for pf in sample:
        state = {s.step: s.state for s in evaluate(pf).steps}
        for down, ups in UPSTREAM.items():
            if state[down] is StepState.BLOCKED:
                continue
            not_done = [u.name for u in ups if state[u] is not StepState.DONE]
            if not_done:
                bad.append((down.name, state[down].name, tuple(not_done), pf))
    assert not bad, (
        f"{len(bad)} 例出现「上游未就绪而下游没有 BLOCKED」。"
        f"首例:{bad[0][0]}={bad[0][1]},上游 {bad[0][2]} 未 DONE;facts={bad[0][3]}"
    )


# ================================================================ I-3


def test_the_current_step_and_the_next_action_never_point_at_different_steps(sample):
    """AC-15 的「唯一下一步」:一个响应里只能有一个下一步。

    `current_step` 与 `next_action` 是**两次独立的走查**(前者按 `STEP_ORDER`
    找第一个非 DONE,后者按分支链)。两次走查就有两个答案,而运营
    看到的是:向导落在 A 步,而按钮说去做 B 步。
    """
    bad = []
    for pf in sample:
        result = evaluate(pf)
        if result.current_step is not result.next_action.step:
            bad.append((result.current_step.name, result.next_action.code.value, pf))
    assert not bad, (
        f"{len(bad)} 例出现「当前步与下一步动作指向不同步骤」。"
        f"首例:current={bad[0][0]} 而 next={bad[0][1]};facts={bad[0][2]}"
    )


def test_every_step_in_the_order_can_become_the_next_action(sample):
    """七步**每一步**都要能成为下一步。

    R-3 的形状:`_decide_next` 从 `MATERIAL` 开始,`SETUP` 与 `PLAN` 整个
    不在分支链里。七步增维时加了步骤、加了权重、加了标签,漏了这一处 ——
    而漏掉的表现不是报错,是那两步**永远不会被推荐**。

    这一条与上面那条互补:只有 I-3 的话,把 `current_step` 改成跟着
    `next_action` 走也能让它绿,而那样两步会一起漏掉。
    """
    reachable = {evaluate(pf).next_action.step for pf in sample}
    missing = [s.name for s in FlowStep if s not in reachable]
    assert not missing, (
        f"这些步骤在 {len(sample)} 例抽样里从来没有成为过下一步:{missing}。"
        f"要么 `_decide_next` 漏了它们的分支,要么它们不该在 `STEP_ORDER` 里"
    )


# ================================================================ 回归定点
#
# 上面三条是性质,下面四条是**具体的那几例**。两者都要有:性质会在
# 重构里被整体删掉而不留痕迹,定点用例的名字会留在失败输出里。




def test_an_illegal_audience_value_does_not_leave_the_downstream_steps_open():
    """R-1 的原始复现例:受众是个认不出的字符串。

    `AUDIENCE_INVALID` 是 BLOCKING,而属性步的状态阶梯以前不看它 ——
    字段都确认完时判 DONE,再被兜底网降成 IN_PROGRESS。降级不回流的话,
    图片集与文案保持 DONE,`EXPORT` 被放行。
    """
    result = evaluate(_ready(audience=AudienceFacts(audience="???", invalid=True)))
    state = {s.step: s.state for s in result.steps}

    assert state[FlowStep.ATTRIBUTE] is not StepState.DONE
    for down in (FlowStep.PLAN, FlowStep.IMAGE_SET, FlowStep.COPY, FlowStep.DRAFT):
        assert state[down] is StepState.BLOCKED, f"{down.name} 应随上游一起关上"
    assert result.next_action.code is NextActionCode.CONFIRM_AUDIENCE
    assert result.current_step is FlowStep.ATTRIBUTE


def test_an_approved_copy_that_later_fails_validation_is_not_done():
    """R-2:规则包变严之后,已批准的那一版文案被重扫出硬失败。

    与 F-1 在图片集上的缺陷**同一个形状**(`elif status == "APPROVED"`
    排在 violation 判断之前),而 F-1 当时只修了图片集那一边。
    """
    result = evaluate(
        _ready(
            copy=CopyFacts(
                exists=True, status="APPROVED", version=1, blocking_violations=2
            )
        )
    )
    state = {s.step: s.state for s in result.steps}

    assert state[FlowStep.COPY] is StepState.IN_PROGRESS
    assert state[FlowStep.DRAFT] is StepState.BLOCKED
    assert result.next_action.code is NextActionCode.FIX_COPY


def test_a_row_without_ownership_is_told_to_finish_setup():
    """R-3(建档):没挂 SPU/颜色的一行。

    以前 `current_step=SETUP` 而 `next_action=EXPORT` —— 一个响应两个下一步。
    """
    result = evaluate(_ready(setup=SetupFacts(has_spu_ref=False, has_colour_ref=True)))

    assert result.current_step is FlowStep.SETUP
    assert result.next_action.code is NextActionCode.COMPLETE_SETUP
    # 理由取判定层自己产出的那句话,不在动作层重拼
    assert result.next_action.reason == "这一行还没挂到SPU上"


def test_a_future_step_that_reports_done_while_blocking_still_closes_downstream(
    monkeypatch,
):
    """兜底网必须挂在**每一步算完的当场**,不是最后统一过一遍。

    ## 为什么这一条要用 monkeypatch

    上面那些用例验的是"现在没有这个缺陷"。而本批修完之后,七个 evaluator
    自己的阶梯都与它们产出的阻断一致了 —— 于是**兜底网没有可达输入**:
    把它的接线位置改回"最后统一过一遍",全套测试照样绿。

    那正是它下次被改坏的方式。`_no_done_while_blocking` 的存在理由是
    "下一个人写第八步时同一行会再手滑一次",所以这里直接扮演那个第八步:
    让一个 evaluator 返回 DONE + BLOCKING,再验 `evaluate()` 的三条不变量
    仍然成立。网挂在最后的话,`attribute_ready` 取的是降级前的值,
    下游会保持开着 —— 当场红。
    """
    real = flow_module._evaluate_attribute

    def sloppy(*args, **kwargs):
        """一个"手滑"的判定:算出阻断,却仍然报完成。"""
        result = real(*args, **kwargs)
        return replace(
            result,
            state=StepState.DONE,
            issues=result.issues
            + (
                Issue(
                    level=IssueLevel.BLOCKING,
                    code="SYNTHETIC_FOR_TEST",
                    message="扮演下一个人手滑写出的那一步",
                    target_step=FlowStep.ATTRIBUTE,
                ),
            ),
        )

    monkeypatch.setattr(flow_module, "_evaluate_attribute", sloppy)
    result = evaluate(_ready())
    state = {s.step: s.state for s in result.steps}

    assert state[FlowStep.ATTRIBUTE] is not StepState.DONE, "兜底网没有降级"
    for down in (FlowStep.PLAN, FlowStep.IMAGE_SET, FlowStep.COPY, FlowStep.DRAFT):
        assert state[down] is StepState.BLOCKED, (
            f"{down.name} 仍然开着 —— 降级没有传到 `upstream_ready`,"
            f"说明兜底网又被挪回了最后"
        )
    assert result.current_step is result.next_action.step


def test_a_colour_without_a_generation_plan_is_told_to_choose_one():
    """R-3(方案):`PLAN_MISSING` 是 BLOCKING,而按钮以前说「编排图片集」。

    运营照着按钮走,排出来的图没有模特与参数依据。
    """
    result = evaluate(_ready(plan=PlanFacts(has_active_plan=False)))

    assert result.current_step is FlowStep.PLAN
    assert result.next_action.code is NextActionCode.CHOOSE_PLAN
    assert result.blocking_count == 1


# ================================================================ 覆盖面自检


def test_the_sample_actually_exercises_every_step_state():
    """抽样本身要够宽。

    没有这一条,把 `SAMPLE_SIZE` 调成 1 也能让上面三条性质全绿 ——
    而那时它们证明的是"这一例没事",不是"这条性质成立"。
    """
    seen = {s.state for pf in _sample()[:2000] for s in evaluate(pf).steps}
    missing = [s.name for s in StepState if s not in seen]
    assert not missing, f"抽样没有覆盖到这些状态:{missing};事实空间需要补维度"


def test_the_fact_space_covers_every_evaluator_branch_input():
    """每一维都要真的有多个取值 —— 维度被写死成常量时当场红。"""
    dims = {
        "SETUPS": SETUPS,
        "MATERIALS": MATERIALS,
        "AUDIENCES": AUDIENCES,
        "ATTRIBUTES": ATTRIBUTES,
        "PLANS": PLANS,
        "IMAGE_SETS": IMAGE_SETS,
        "COPIES": COPIES,
        "DRAFTS": DRAFTS,
    }
    thin = [name for name, values in dims.items() if len(values) < 2]
    assert not thin, f"这些维度只剩一个取值,已经不再区分分支:{thin}"

    total = 1
    for values in dims.values():
        total *= len(values)
    assert total > SAMPLE_SIZE, (
        f"事实空间({total})比抽样量({SAMPLE_SIZE})还小,"
        f"应该改成 itertools.product 全量穷举而不是抽样"
    )
    assert itertools  # 上面那句话里提到的模块,留着给下一个人直接用
