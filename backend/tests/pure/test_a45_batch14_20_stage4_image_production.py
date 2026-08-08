"""A45-batch14-20:PRD 阶段 4「多颜色图片生产」的纯层守卫。**零依赖。**

分五段,对应阶段 4 的五项交付:

    一  方案指纹与作用域解析      §4.7 / §6.4(修复风险表 D2)
    二  幂等键接颜色与双指纹      §6.5「绑定 color_variant_id + plan + 双指纹」
    三  §6.5 颜色图片规则          BLOCK-02 挂了几版才等到的那个业务决定
    四  事实一致性维度组一票否决    §5.2 / §6.5
    五  失效矩阵扩展                §8.1

**这一批验不到的东西写在第六段末尾**,不写在文档里就没人会去看。
"""
from __future__ import annotations

import ast

from app.core.enums import HardFailCode, ImageAngle
from app.evaluators import fact_consistency as fc
from app.evaluators.base import SCORE_DIMENSIONS
from app.evaluators.rules import grade_candidate
from app.evaluators.scoring import parse_evaluator_payload
from app.listings import image_set_rules as isr
from app.workbench import stale_matrix as sm
from app.workflows import generation_plan as gp
from app.workflows.idempotency import build_idempotency_key
from tests.pure._helpers import BACKEND_ROOT


def _plan(**kw) -> gp.PlanView:
    base = dict(
        plan_id="p1",
        color_variant_id=None,
        model_template_id="tpl-1",
        provider="fashn",
        scene="studio",
        pose="STANDING_FRONT",
        angles=gp.normalize_angles(["FRONT", "BACK"]),
        budget_cap=None,
        status="ACTIVE",
    )
    base.update(kw)
    return gp.PlanView(**base)


def _item(asset_id="a", **kw) -> isr.ItemView:
    base = dict(role="PRODUCT_FRONT", sort_order=0, variant_id=None)
    base.update(kw)
    return isr.ItemView(media_asset_id=asset_id, **base)


def _set(items) -> isr.SetView:
    return isr.SetView("set-1", "SPU-1", 1, "DRAFT", items=tuple(items))


# ================================================================ 一、方案


def test_the_fingerprint_does_not_care_which_colour_the_plan_belongs_to():
    """**归属不进指纹。**

    给两个颜色配完全相同的参数应该得到同一个指纹。把 `color_variant_id`
    编进去的后果很具体:§8.1「更换生成方案」那一行会在**什么都没换**的
    时候判两个颜色的图片集都过期,于是两批图白重出一遍。
    """
    assert gp.fingerprint_of(_plan(color_variant_id=None)) == gp.fingerprint_of(
        _plan(color_variant_id="RED")
    )


def test_every_parameter_that_changes_the_picture_changes_the_fingerprint():
    """改任意一个参数,指纹必须变。逐个字段行使一遍,不挑代表。"""
    base = gp.fingerprint_of(_plan())
    assert gp.fingerprint_of(_plan(model_template_id="tpl-2")) != base
    assert gp.fingerprint_of(_plan(provider="mock")) != base
    assert gp.fingerprint_of(_plan(scene="beach")) != base
    assert gp.fingerprint_of(_plan(pose="WALKING")) != base
    assert gp.fingerprint_of(_plan(angles=gp.normalize_angles(["FRONT"]))) != base
    # 张数变了也算变了 —— 同一组角度、每角度 1 张与 2 张是两份方案
    assert (
        gp.fingerprint_of(_plan(angles=gp.normalize_angles({"FRONT": 2, "BACK": 1})))
        != base
    )


def test_the_budget_cap_is_in_the_fingerprint_even_though_it_does_not_change_the_picture():
    """预算不影响图长什么样,但它决定**能不能出**。

    不进指纹的话,把上限从 10 元改到 100 元之后重新提交,会静默命中那个
    被预算拦下的旧任务 —— 运营看到"提交成功"而什么都没发生。
    """
    assert gp.fingerprint_of(_plan(budget_cap="10")) != gp.fingerprint_of(_plan())


def test_the_same_money_written_three_ways_is_one_fingerprint():
    """`10` / `10.0` / `"10.00"` 必须编成同一个串。

    不归一的话,同一份方案在"从接口传进来"和"从库里读回来"两条路上会算出
    两个指纹 —— 而那两条路恰好是创建任务与判过期各走一条,于是每次创建
    任务都会顺带把自己的图片集判成过期。
    """
    keys = {gp.fingerprint_of(_plan(budget_cap=v)) for v in (10, 10.0, "10.00", "10")}
    assert len(keys) == 1


def test_angles_are_a_set_not_an_order():
    """角度的**呈现顺序**不进指纹。

    前端调一下角度的展示顺序就换一个幂等键的话,拖一下排序等于重出一批图。
    """
    assert gp.normalize_angles(["BACK", "FRONT"]) == gp.normalize_angles(
        ["FRONT", "BACK"]
    )
    assert gp.fingerprint_of(
        _plan(angles=gp.normalize_angles(["BACK", "FRONT"]))
    ) == gp.fingerprint_of(_plan(angles=gp.normalize_angles(["FRONT", "BACK"])))


def test_unknown_angles_are_dropped_but_the_dropping_is_reported():
    """未知角度静默丢掉,**而"丢了几条"必须能被报出来**。

    只丢不报的话,"方案里写了 BACK 却拼成 BAKC"的表现是背面图永远不缺 ——
    §6.5 的角度验收对这个方案恒为绿,而没有任何地方会说为什么。
    """
    angles = gp.normalize_angles(["FRONT", "BAKC", "BACK"])
    assert gp.required_angles(angles) == frozenset({"FRONT", "BACK"})
    codes = {p.code for p in gp.plan_problems(_plan(angles=angles), raw_angle_count=3)}
    assert gp.PLAN_UNKNOWN_ANGLE_DROPPED in codes


def test_a_plan_cannot_ask_for_more_candidates_than_one_round_allows():
    """一轮出不了那么多张时要当场说,不要留到批准那一步。"""
    angles = gp.normalize_angles({a.value: 3 for a in ImageAngle})
    codes = {p.code for p in gp.plan_problems(_plan(angles=angles))}
    assert gp.PLAN_TOO_MANY_CANDIDATES in codes
    assert gp.total_candidates(angles) > gp.MAX_PLAN_CANDIDATES


def test_the_ceiling_comes_from_the_single_source_of_candidate_count():
    """上限必须与 `MAX_CANDIDATE_COUNT` 同源。

    抄一个数字过来的表现是:调大候选上限之后方案侧仍然按旧值拦,
    而两处都不报错 —— 与 `phase_budget` 那条同一个理由。
    """
    from app.core.field_limits import MAX_CANDIDATE_COUNT

    assert gp.MAX_PLAN_CANDIDATES == MAX_CANDIDATE_COUNT
    # 值相等还不够 —— **抄一个恰好相等的字面量也能让上面那行绿**。
    # 要钉的是"它引用的是那个名字",所以看源码形状(与 `batch.py` 那条
    # CLAIM_CHUNK 断言同一套办法)。
    source = (BACKEND_ROOT / "app" / "workflows" / "generation_plan.py").read_text(
        encoding="utf-8"
    )
    assert "MAX_PLAN_CANDIDATES = MAX_CANDIDATE_COUNT" in source


def test_a_colour_override_wins_and_a_missing_one_falls_back_to_the_spu_default():
    default = _plan(plan_id="d", color_variant_id=None)
    red = _plan(plan_id="r", color_variant_id="RED", scene="beach")
    assert gp.resolve_plan([default, red], "RED").plan_id == "r"
    assert gp.resolve_plan([default, red], "BLUE").plan_id == "d"
    assert gp.resolve_plan([red], "BLUE") is None


def test_a_draft_plan_never_gets_used_for_a_real_task():
    """DRAFT 是编辑中的那一份。拿它创建任务等于让改到一半的参数直接花钱。"""
    assert gp.resolve_plan([_plan(status="DRAFT")], None) is None
    assert gp.resolve_plan([_plan(status="ARCHIVED")], None) is None


def test_changing_the_spu_default_only_stales_the_colours_without_an_override():
    """**这是本批最值钱的一条。**

    改 SPU 默认方案时,配了颜色覆盖的那些颜色一个字都没变。按"方案变了就
    全部过期"写的话,一个九色 SPU 调一次默认角度会重出八个颜色的全部候选,
    每一张都是真实付费调用。
    """
    variants = ["RED", "BLUE", "GREEN"]
    before = [_plan(plan_id="d"), _plan(plan_id="r", color_variant_id="RED")]
    after = [_plan(plan_id="d", scene="beach"), _plan(plan_id="r", color_variant_id="RED")]

    changed = gp.image_set_stale_scopes(
        gp.effective_fingerprints(before, variants),
        gp.effective_fingerprints(after, variants),
    )
    assert changed == frozenset({"BLUE", "GREEN"}), "RED 有覆盖,不该被默认方案的改动波及"


def test_adding_an_override_stales_exactly_that_colour():
    variants = ["RED", "BLUE"]
    before = [_plan(plan_id="d")]
    after = [_plan(plan_id="d"), _plan(plan_id="r", color_variant_id="RED", pose="SITTING")]
    changed = gp.image_set_stale_scopes(
        gp.effective_fingerprints(before, variants),
        gp.effective_fingerprints(after, variants),
    )
    assert changed == frozenset({"RED"})


def test_deleting_the_last_usable_plan_still_counts_as_a_change():
    """比并集不比交集。

    只比交集的话,"某个颜色的方案没了"会因为那个键在 after 里不存在而被
    漏掉 —— 而那恰恰是最该重出图的一种变化。与
    `scope_fingerprint.changed_scopes` 是同一个坑。
    """
    variants = ["RED"]
    before = gp.effective_fingerprints([_plan(plan_id="d")], variants)
    after = gp.effective_fingerprints([], variants)
    assert gp.image_set_stale_scopes(before, after) == frozenset({"RED"})


def test_the_budget_gate_fails_closed_when_the_ledger_cannot_be_read():
    """算不出来就不放行。与校准 fail-closed、`facts_stale` 同一条口径。"""
    assert gp.budget_verdict(budget_cap="10", spent=None) is gp.BudgetVerdict.UNKNOWN
    assert gp.budget_blocks(gp.BudgetVerdict.UNKNOWN)
    # 没设上限时读不到花费也放行 —— 没有上限就没有要证明的事
    assert gp.budget_verdict(budget_cap=None, spent=None) is gp.BudgetVerdict.ALLOWED


def test_the_budget_gate_counts_this_run_too():
    """比的是"已花 + 本次预估",不是"已花"。

    只比已花的话,上限永远会在**超出之后**才生效 —— 那时钱已经花掉了。
    """
    assert gp.budget_verdict(budget_cap="10", spent="9.5", estimated="1") is (
        gp.BudgetVerdict.EXCEEDED
    )
    assert gp.budget_verdict(budget_cap="10", spent="9.5", estimated="0.5") is (
        gp.BudgetVerdict.ALLOWED
    )


# ================================================================ 二、幂等键


def test_the_three_stage_four_inputs_each_change_the_key_on_their_own():
    """三个输入互相独立,不能只留一个。"""
    base = dict(product_id="p", mode="m", provider="v")
    plain = build_idempotency_key(**base)
    assert build_idempotency_key(**base, color_variant_id="RED") != plain
    assert build_idempotency_key(**base, plan_fingerprint="fp") != plain
    assert build_idempotency_key(**base, sample_fingerprint="sfp") != plain
    # 两两之间也不许撞
    keys = {
        build_idempotency_key(**base, color_variant_id="RED"),
        build_idempotency_key(**base, plan_fingerprint="RED"),
        build_idempotency_key(**base, sample_fingerprint="RED"),
    }
    assert len(keys) == 3


def test_not_passing_and_passing_empty_are_the_same_key():
    """"没传"和"传了空"必须是同一个键。

    否则接口两条调用路径(前端不传 / 服务层算出空)会给出两个不同的任务,
    而它们的参数完全一样 —— 那是一次白花的付费调用。
    """
    base = dict(product_id="p", mode="m", provider="v")
    assert build_idempotency_key(**base) == build_idempotency_key(
        **base, color_variant_id=None, plan_fingerprint="", sample_fingerprint=None
    )


def test_the_truncation_length_matches_the_column_width():
    """截断长度与列宽同源。

    截断是幂等最坏的失效方式:两份不同的显式键被截成同一个,第二次提交
    命中第一次的任务,**而两次的参数不同**。0041 把列扩到 256,
    截断值必须跟着走 —— 所以它现在有名字,而不是一个字面量。
    """
    from app.workflows.idempotency import MAX_IDEMPOTENCY_KEY_CHARS

    assert MAX_IDEMPOTENCY_KEY_CHARS == 256
    long_key = "x" * 400
    assert (
        len(build_idempotency_key(product_id="p", mode="m", provider="v", explicit_key=long_key))
        == MAX_IDEMPOTENCY_KEY_CHARS
    )
    migration = (
        BACKEND_ROOT / "migrations" / "versions" / "0041_generation_plan_and_variant_scope.py"
    ).read_text(encoding="utf-8")
    assert "sa.String(256)" in migration, "列没扩到 256,截断长度就不能放宽"


# ================================================================ 三、§6.5


def test_a_shared_image_can_never_be_a_colour_primary():
    """§6.5:「颜色主图只能来自 `variant_id = 该颜色` 的条目」。"""
    image_set = _set(
        [
            _item("shared", is_primary=True, variant_id=None),
            _item("red", variant_id="RED", sort_order=1, angle="FRONT"),
        ]
    )
    codes = {v.code for v in isr.validate_set(image_set, variant_ids=["RED"])}
    assert isr.ImageSetViolation.SHARED_IMAGE_AS_PRIMARY in codes


def test_the_primary_no_longer_falls_back_to_the_spu_level():
    """**这里改过一次行为。**

    原来颜色没有专属主图时会回落到通用图。§6.5 把 BLOCK-02 那个业务决定
    定死了:不得回退使用其他颜色的图片,缺图就是缺图。回退的具体后果是
    红色 SKU 挂着黑色主图上架 —— 因为颜色绑定入口上线之前,通用图就是
    第一个颜色的图。
    """
    image_set = _set([_item("shared", is_primary=True, variant_id=None)])
    assert isr.primary_for(image_set, "RED") is None
    # SPU 级展示位仍然取得到通用主图 —— 单色 SPU 走的是这条
    assert isr.primary_for(image_set, None).media_asset_id == "shared"


def test_an_unmarked_shared_image_does_not_drift_into_a_colour():
    """§6.5:「必须由运营明确标记"通用",默认不混入」。"""
    unmarked = _set(
        [
            _item("red", is_primary=True, variant_id="RED", angle="FRONT"),
            _item("shared", variant_id=None, sort_order=1),
        ]
    )
    codes = {v.code for v in isr.validate_set(unmarked, variant_ids=["RED"])}
    assert isr.ImageSetViolation.UNMARKED_SHARED_IMAGE in codes

    marked = _set(
        [
            _item("red", is_primary=True, variant_id="RED", angle="FRONT"),
            _item("shared", variant_id=None, sort_order=1, shared_opt_in=True),
        ]
    )
    codes = {v.code for v in isr.validate_set(marked, variant_ids=["RED"])}
    assert isr.ImageSetViolation.UNMARKED_SHARED_IMAGE not in codes


def test_a_marked_shared_image_shows_up_in_the_colour_but_never_first():
    """标记过的通用图只进附图位,排序上永远在主图之后。"""
    image_set = _set(
        [
            _item("shared", variant_id=None, sort_order=0, shared_opt_in=True),
            _item("red", variant_id="RED", sort_order=5, is_primary=True),
        ]
    )
    order = [i.media_asset_id for i in isr.ordered_items(image_set, "RED")]
    assert order == ["red", "shared"]


def test_an_unmarked_shared_image_is_not_in_the_colour_order_at_all():
    image_set = _set(
        [
            _item("shared", variant_id=None, sort_order=0),
            _item("red", variant_id="RED", sort_order=5, is_primary=True),
        ]
    )
    assert [i.media_asset_id for i in isr.ordered_items(image_set, "RED")] == ["red"]


def test_a_shared_image_no_longer_counts_as_covering_a_colour():
    """**这是把 variant_coverage 从诊断变成门禁的那一刀。**

    原来那条放行是「只要集里存在通用图,所有变体都算被覆盖」,于是一个
    九色 SPU 只要有一张通用图就永远"覆盖完整"。
    """
    image_set = _set(
        [_item("shared", is_primary=False, variant_id=None, shared_opt_in=True),
         _item("red", is_primary=True, variant_id="RED", angle="FRONT")]
    )
    problems = isr.validate_set(image_set, variant_ids=["RED", "BLUE"])
    missing = [v for v in problems if v.code is isr.ImageSetViolation.MISSING_IMAGE_FOR_VARIANT]
    assert [v.variant_id for v in missing] == ["BLUE"]


def test_a_colour_tag_that_does_not_belong_to_this_spu_is_blocking_now():
    """原来只在 `unknown_tags` 里报,是诊断不是门禁。

    一个指向已删除商品的标签会让覆盖率算出假的绿。
    """
    image_set = _set(
        [
            _item("red", is_primary=True, variant_id="RED", angle="FRONT"),
            _item("ghost", variant_id="PURPLE", sort_order=1),
        ]
    )
    codes = {v.code for v in isr.validate_set(image_set, variant_ids=["RED"])}
    assert isr.ImageSetViolation.FOREIGN_VARIANT_IMAGE in codes


def test_angles_are_verified_per_colour_not_by_counting_pictures():
    """§6.5:「必要角度按 `angles_json` 配置验收,**不是只数总张数**」。

    数总张数在多颜色下会给出错误的绿灯:一个颜色出了 4 张正面图,总数达标,
    而背面一张都没有。
    """
    four_fronts = _set(
        [
            _item("r1", is_primary=True, variant_id="RED", angle="FRONT"),
            _item("r2", variant_id="RED", sort_order=1, angle="FRONT"),
            _item("r3", variant_id="RED", sort_order=2, angle="FRONT"),
            _item("r4", variant_id="RED", sort_order=3, angle="FRONT"),
        ]
    )
    problems = isr.validate_set(
        four_fronts, variant_ids=["RED"], required_angles={"RED": ["FRONT", "BACK"]}
    )
    missing = [v for v in problems if v.code is isr.ImageSetViolation.MISSING_REQUIRED_ANGLE]
    assert len(missing) == 1 and "BACK" in missing[0].message


def test_an_unlabelled_picture_covers_no_angle():
    """没标注角度**不算覆盖**。

    反过来(没标注就算通过)会让角度验收在接线完成之前一直是绿的 ——
    而那正是它要防的。
    """
    image_set = _set([_item("r1", is_primary=True, variant_id="RED", angle=None)])
    cover = isr.coverage(image_set, variant_ids=["RED"], required_angles={"RED": ["FRONT"]})
    assert cover["RED"].missing_angles == frozenset({"FRONT"})


def test_section_6_5_stays_switched_off_for_single_colour_spus():
    """`variant_ids` 传空 = 不做颜色级检查。

    §6.5 的混排规则在没有"别的颜色"时没有意义,而单色 SPU 与颜色绑定
    入口尚未接线的调用点都走这条路。少一个维度的检查必须能被显式表达,
    否则接线未完成的阶段会被迫在"全查"和"全不查"之间二选一。
    """
    image_set = _set([_item("shared", is_primary=True, variant_id=None)])
    assert isr.validate_set(image_set) == []


def test_the_coverage_used_by_the_gate_is_the_one_the_diagnostics_show():
    """门禁与诊断读同一份覆盖。

    两处各算一次的表现是:界面说缺一个颜色、批准却通过了,而两边都不报错。
    """
    source = (BACKEND_ROOT / "app" / "listings" / "image_set_rules.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    section = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_section_6_5"
    )
    calls = {
        n.func.id
        for n in ast.walk(section)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "coverage" in calls, "§6.5 的判定必须走 coverage(),不许自己再推一遍"


# ================================================================ 四、事实一致性


def _payload(**extra):
    body = {
        "hard_fail": False,
        "hard_fail_codes": [],
        "scores": {d: 95 for d in SCORE_DIMENSIONS},
        "uncertain_items": [],
        "problems": [],
    }
    body.update(extra)
    return body


FACTS = {
    "primary_color": "BLACK",
    "secondary_color": "NONE",
    "pattern_type": "SOLID",
    "strap_type": "DOUBLE",
    "neckline_type": "V_NECK",
    "back_style": "CROSS",
    "coverage_level": "MODERATE",
    "garment_type": "ONE_PIECE",
}


def test_a_fact_mismatch_goes_through_the_existing_single_veto_not_a_second_one():
    """产物是**硬错误代码**,否决权仍在 `grade_candidate` 第一条线上。

    另建一条"事实不一致就拒绝"的分支,系统就有两个地方能把一张图判死,
    而两者漂移时"这张图为什么是 D"会有两个都说得通的答案。
    """
    result = parse_evaluator_payload(
        _payload(
            fact_consistency=[{"key": "strap", "verdict": "MISMATCH", "observed": "单肩"}]
        ),
        evaluator="t",
        confirmed_facts=FACTS,
    )
    assert result.hard_fail and result.hard_fail_codes == ["STRAP_CHANGED"]
    decision = grade_candidate(result)
    assert decision.grade.value == "D" and not decision.is_auto_approvable


def test_a_high_score_does_not_survive_a_fact_mismatch():
    """总分 95 也不行 —— 这一条是"接现有一票否决机制"的字面意思。"""
    result = parse_evaluator_payload(
        _payload(
            fact_consistency=[{"key": "color", "verdict": "MISMATCH", "observed": "红"}]
        ),
        evaluator="t",
        confirmed_facts=FACTS,
    )
    assert result.overall_score >= 90
    assert grade_candidate(result).grade.value == "D"


def test_an_unconfirmed_fact_never_produces_a_veto():
    """**这一条写反了是致命的。**

    默认判不一致会让整批图在事实确认之前全部被否,而运营看到的是
    "模型说所有图都错",查不到是自己少确认了一个字段。
    """
    result = parse_evaluator_payload(
        _payload(
            fact_consistency=[{"key": "strap", "verdict": "MISMATCH", "observed": "单肩"}]
        ),
        evaluator="t",
        confirmed_facts={},
    )
    assert not result.hard_fail and result.hard_fail_codes == []
    assert any("事实未确认" in note for note in result.uncertain_items)


def test_uncertain_is_neither_match_nor_mismatch():
    """看不清只记一笔。并成不一致会让一张被水花挡住背部的好图被判死。"""
    result = parse_evaluator_payload(
        _payload(
            fact_consistency=[{"key": "back_style", "verdict": "UNCERTAIN", "observed": "被遮挡"}]
        ),
        evaluator="t",
        confirmed_facts=FACTS,
    )
    assert not result.hard_fail
    assert any("看不清" in note for note in result.uncertain_items)


def test_a_verdict_the_model_invented_is_treated_as_uncertain():
    """认不出来的词本质就是没答,而"没答"已经有一档了。"""
    result = parse_evaluator_payload(
        _payload(
            fact_consistency=[{"key": "color", "verdict": "PROBABLY_OK", "observed": "黑"}]
        ),
        evaluator="t",
        confirmed_facts=FACTS,
    )
    assert not result.hard_fail
    # **不放行也不判死,而是留一笔。**只断言"没被否决"的话,把未知词
    # 当成 MATCH 也能让它绿 —— 而那是"模型拼错一个词 = 放行"。
    assert any("看不清" in note for note in result.uncertain_items)


def test_a_check_that_was_not_run_is_distinguishable_from_one_that_passed():
    """"查了没问题"与"没查"必须分得开。

    一份看起来全绿的报告如果实际只查了两项,审核的人会误以为其余七项都过了。
    """
    findings = fc.evaluate(
        [{"key": "color", "verdict": "MATCH", "observed": "黑"}], confirmed_facts=FACTS
    )
    summary = fc.coverage(findings)
    assert summary["judged"] == 1 and summary["skipped"] == len(fc.FACT_CHECKS) - 1


def test_a_mismatch_is_always_critical():
    """严重度必须是 CRITICAL。

    今天写 MAJOR 也不会放行(`hard_fail` 那条线先拦下),所以这个字段看起来
    无所谓 —— 直到有人调了 `SEVERITY_GRADE_CAP` 或阈值,它才开始悄悄放行。
    这类"今天没差别、改天要命"的字段必须单独钉,不能靠端到端行为兜住。
    """
    findings = fc.evaluate(
        [{"key": "strap", "verdict": "MISMATCH", "observed": "单肩"}], confirmed_facts=FACTS
    )
    problems = fc.problem_dicts(findings)
    assert problems and all(p["severity"] == "CRITICAL" for p in problems)
    assert all(p["hard_fail"] for p in problems)


def test_the_codes_and_the_problems_come_from_one_derivation():
    """`problem_dicts` 必须从 `hard_fail_codes` 派生。

    两处各判一次的话,短路掉其中一处**否决照样发生**,于是那一处变成
    一段没人走的死代码 —— 而它登记在 `WIRED_MODULES` 里,看起来被守着。
    """
    source = (BACKEND_ROOT / "app" / "evaluators" / "fact_consistency.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "problem_dicts"
    )
    calls = {
        n.func.id for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "hard_fail_codes" in calls


def test_the_rule_pack_decides_which_checks_run():
    """§14.4:枚举是全集,规则包决定这一次用哪些。

    把女装的肩带项摆在男装泳裤的清单里,模型迟早会挑一个用。
    """
    mens = {c.key for c in fc.enabled_checks(["COLOR_VARIANT_WRONG", "WAISTBAND_CHANGED"])}
    assert mens == {"color"}, "男装包里不该出现女装的结构项"
    assert fc.enabled_checks(None) == fc.FACT_CHECKS, "没有启用清单时退回全集"


def test_every_checklist_item_maps_to_a_real_hard_fail_code():
    """九项各自点名一个真实存在的码。

    拼错一个字母的表现是那一项静默失效,而清单看上去毫无问题 ——
    与规则包那条守卫同源。
    """
    assert len(fc.FACT_CHECKS) == 9
    known = set(HardFailCode)
    assert all(c.code in known for c in fc.FACT_CHECKS)
    assert len({c.key for c in fc.FACT_CHECKS}) == 9


def test_the_two_new_codes_are_enabled_in_all_three_rule_packs():
    """不对称与融合缺陷描述的是"渲染塌了",与受众无关。

    少配一份包的表现是那个受众的这两项永远返不出来 —— Schema 允许、
    提示词也列了,只是模型选不到。
    """
    spec_dir = BACKEND_ROOT / "app" / "channels" / "generic" / "spec"
    for name in ("women_swimwear", "men_swimwear", "unisex_swimwear"):
        text = (spec_dir / f"{name}.yaml").read_text(encoding="utf-8")
        assert "ASYMMETRY_INTRODUCED" in text and "FUSION_DEFECT" in text, name


def test_the_checklist_is_written_once_and_the_prompt_only_renders_it():
    """提示词不许手抄一份清单。

    手抄的话,加一项时抄的那份不会跟着变 —— 模型只回答提示词里说了的那几项,
    Schema 允许的其余项永远是空,而那种缺失不报错。
    """
    source = (BACKEND_ROOT / "app" / "evaluators" / "vision_schema.py").read_text(
        encoding="utf-8"
    )
    # **不能拿裸串比对**:`DEFAULT_SYSTEM_PROMPT` 里本来就写着"图案"、"领口"
    # 这些词(那是受众前时代的检查项清单,与本模块无关)。比对会误报,
    # 而误报最省事的解决办法是把断言删掉。
    #
    # 要钉的是形状:排版那一行必须**遍历 `checks`**,不能是字面量。
    tree = ast.parse(source)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "build_user_prompt"
    )
    rendered = [
        node for node in ast.walk(fn)
        if isinstance(node, ast.Assign)
        and any(getattr(t_, "id", "") == "check_lines" for t_ in node.targets)
    ]
    assert rendered, "提示词里没有渲染事实一致性清单那一行"
    names = {
        n.id for node in rendered for n in ast.walk(node) if isinstance(n, ast.Name)
    }
    assert "checks" in names, (
        "提示词里的事实一致性清单是手抄的 —— 清单只能有 "
        "fact_consistency.FACT_CHECKS 一份,这里必须遍历它"
    )
    assert "enabled_checks(" in source


# ================================================================ 五、失效矩阵


def test_the_matrix_grew_by_exactly_two_rows_and_stayed_closed():
    assert len(sm.MATRIX) == 32 and sm.is_exhaustive()
    assert set(sm.TRIGGERS) == set(sm.ChangeSource)


def test_replacing_an_approved_set_is_a_new_version_not_a_reset():
    """与第 3 行(重新编排 → 重置为待批准)是两件事。

    那边是已批准集被改动、必须重新走审批;这边是新一版**已经**批准了、
    旧版自动退位。合并这两格会让"批准一版新图之后该做什么"失去答案。
    """
    replaced = sm.cell(sm.ChangeSource.APPROVED_IMAGE_SET_REPLACED, sm.Target.IMAGE_SET)
    rearranged = sm.cell(sm.ChangeSource.APPROVED_IMAGE_SET_REARRANGED, sm.Target.IMAGE_SET)
    assert replaced.effect is sm.Effect.NEW_VERSION
    assert rearranged.effect is sm.Effect.RESET_TO_PENDING


def test_the_plan_row_points_at_the_scope_aware_mechanism():
    """点名的必须是按作用域算的那个函数。

    点一个"方案变了就全过期"的函数也能让守卫变绿,而代价是九色 SPU
    改一次默认角度重出八个颜色 —— 那正是这一格要防的。
    """
    cell = sm.cell(sm.ChangeSource.GENERATION_PLAN_CHANGED, sm.Target.IMAGE_SET)
    assert cell.effect is sm.Effect.STALE
    assert cell.mechanism == "app.workflows.generation_plan.image_set_stale_scopes"


def test_a_plan_change_does_not_touch_the_facts():
    """方案决定这件衣服怎么被拍出来,不决定这件衣服是什么。"""
    cell = sm.cell(sm.ChangeSource.GENERATION_PLAN_CHANGED, sm.Target.FACTS)
    assert cell.effect is sm.Effect.NOT_AFFECTED and cell.note and not cell.mechanism


# ================================================================ 六、本批验不到什么


def test_what_this_batch_cannot_verify_is_written_down():
    """**这条守卫记的是边界,不是成绩。**

    阶段 4 有三样东西在这台机器上一个字都证明不了:

        一  迁移 0041 从未执行 —— 表达式唯一索引、CHECK、外键方向
        二  §6.5 的门禁一旦接线,**存量图片集会不会集体无法批准**
            (§3.1 说没有存量数据,但那句话没有在真库上被验证过)
        三  §6.5 门禁接线之后,`variant_coverage()` 在真实数据形状上的表现

    **原稿第三条写的是"角度继承接线点在服务层,而服务层要 sqlalchemy"。
    合并复审时删掉了:那不是"验不到",是"没写"。**两者混在一起会让下一个人
    以为等一台有依赖的机器就行。没写这件事另有守卫记账,见本文件末尾
    `test_the_two_new_item_columns_have_no_writer_yet_and_this_is_the_ledger`。

    真验证在 `tests/test_a45_batch14_20_stage4_db.py`(已写、一次都没跑过)。
    这条守卫在于:**那份文件必须存在**。删掉它、或者把欠账悄悄清掉时,
    这里会红。
    """
    db_suite = BACKEND_ROOT / "tests" / "test_a45_batch14_20_stage4_db.py"
    assert db_suite.exists(), "阶段 4 的真库用例不见了 —— 那是本批唯一的真验证"
    text = db_suite.read_text(encoding="utf-8")
    assert "requires_db" in text


# 「方案面板写完了但进不去任何路由」那条欠账守卫**在本处退役**(A45-batch18)。
# 它自己的文档字符串写着接线那天该怎么办:删掉它,把"进得去这个页面吗"交给
# Playwright(见 test_frontend_contract.py 顶部那张分工表)。按 14-24 的自净
# 规矩删掉而不是注释掉 —— 留着会在下次清点里以"已有覆盖"的身份被数进去。
# 继任者是 tests/pure/test_a45_batch18_plan_host.py 第三节(四段可达性断言)。