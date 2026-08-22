"""修复策略与评分器契约(需求第九、十、十二、十七章)。"""
from __future__ import annotations

import asyncio

from app.core.enums import (
    Grade,
    HardFailCode,
    ProblemSeverity,
    RegenerationReason,
)
from app.core.errors import EvaluatorUnavailableError
from app.evaluators.base import SCORE_DIMENSIONS, ImageQualityEvaluator
from app.evaluators.mock import MockImageQualityEvaluator
from app.evaluators.registry import (
    available_names,
    describe_all,
    get_active_evaluator,
    get_evaluator,
)
from app.evaluators.repair import (
    FALLBACK_ACTION,
    REPAIR_TABLE,
    REPAIR_TABLE_DEFAULT,
    apply_prompt_additions,
    build_repair_plan,
    parse_repair_table,
)
from app.evaluators.rules import grade_candidate
from app.evaluators.scoring import parse_evaluator_payload
from app.evaluators.vision import VisionEvaluatorConfig, VisionModelImageQualityEvaluator
from app.providers.errors import NotConfiguredError
from tests.pure._helpers import expect_raises


def _run(coro):
    return asyncio.run(coro)


# ---------- 修复策略表 ----------

def test_every_hard_fail_code_has_a_repair_action():
    """需求第十一章的 15 个硬错误必须都能查到修复动作,否则只会得到兜底的"换 seed"。"""
    missing = [code.value for code in HardFailCode if code.value not in REPAIR_TABLE]
    assert not missing, f"这些硬错误没登记修复动作: {missing}"


def test_unknown_code_falls_back_instead_of_crashing():
    plan = build_repair_plan(grade=Grade.B, problem_codes=["SOMETHING_NEW"])
    assert plan.change_seed is True
    assert FALLBACK_ACTION.note in " ".join(plan.notes) or plan.triggered_by == ["SOMETHING_NEW"]


def test_repair_plan_is_deterministic():
    """需求第十二章原文是"确定性的修复参数"。"""
    first = build_repair_plan(
        grade=Grade.B, problem_codes=["STRAP_CHANGED", "LIGHTING_FLAT"]
    ).to_dict()
    for _ in range(20):
        assert build_repair_plan(
            grade=Grade.B, problem_codes=["STRAP_CHANGED", "LIGHTING_FLAT"]
        ).to_dict() == first


def test_repair_plan_ignores_input_order():
    left = build_repair_plan(grade=Grade.B, problem_codes=["A_CODE", "STRAP_CHANGED"])
    right = build_repair_plan(grade=Grade.B, problem_codes=["STRAP_CHANGED", "A_CODE"])
    assert left.to_dict() == right.to_dict()


def test_repair_plan_deduplicates_codes():
    plan = build_repair_plan(
        grade=Grade.B, problem_codes=["STRAP_CHANGED", "STRAP_CHANGED"]
    )
    assert plan.triggered_by == ["STRAP_CHANGED"]


def test_anatomy_error_changes_model_template_not_just_seed():
    """人体/面部错误靠换 seed 基本没用,必须换模特模板。"""
    plan = build_repair_plan(
        grade=Grade.D,
        problem_codes=[],
        hard_fail_codes=[HardFailCode.ANATOMY_HARD_ERROR.value],
    )
    assert plan.change_model_template is True
    assert plan.negative_prompt_additions


def test_garment_wrong_wants_a_different_provider():
    plan = build_repair_plan(
        grade=Grade.C, problem_codes=[], hard_fail_codes=[HardFailCode.GARMENT_WRONG.value]
    )
    assert plan.switch_provider is True


def test_provider_switch_can_be_disabled_by_rule_set():
    plan = build_repair_plan(
        grade=Grade.C,
        problem_codes=[],
        hard_fail_codes=[HardFailCode.GARMENT_WRONG.value],
        allow_provider_switch=False,
    )
    assert plan.switch_provider is False
    assert any("禁用" in note for note in plan.notes)


def test_c_grade_always_changes_model_template():
    """需求第十二章 C 档:更换 seed 或模特模板。换 seed 已经证明不够用了。"""
    assert build_repair_plan(grade=Grade.C, problem_codes=[]).change_model_template is True


def test_hard_fail_sets_hard_fail_reason():
    plan = build_repair_plan(
        grade=Grade.D, problem_codes=[], hard_fail_codes=[HardFailCode.COVERAGE_CHANGED.value]
    )
    assert plan.reason is RegenerationReason.HARD_FAIL


def test_low_score_without_hard_fail_sets_low_score_reason():
    assert build_repair_plan(grade=Grade.B, problem_codes=["LIGHTING_FLAT"]).reason \
        is RegenerationReason.LOW_SCORE


def test_pattern_distortion_adjusts_provider_params():
    plan = build_repair_plan(
        grade=Grade.B, problem_codes=[HardFailCode.PATTERN_DISTORTED.value]
    )
    assert plan.provider_param_overrides


def test_plan_serialises_to_plain_json_types():
    plan = build_repair_plan(grade=Grade.C, problem_codes=["STRAP_CHANGED"])
    data = plan.to_dict()
    import json

    assert json.loads(json.dumps(data)) == data


def test_versioned_repair_mapping_changes_the_real_plan():
    """补丁表不是只在页面保存：解析后的活动值必须进入实际重生计划。"""
    import json

    payload = json.loads(REPAIR_TABLE_DEFAULT)
    payload[HardFailCode.STRAP_CHANGED.value]["prompt_additions"] = ["活动版本里的肩带要求"]
    active = parse_repair_table(json.dumps(payload, ensure_ascii=False))
    plan = build_repair_plan(
        grade=Grade.B,
        problem_codes=[HardFailCode.STRAP_CHANGED.value],
        repair_table=active,
    )
    assert plan.prompt_additions == ["活动版本里的肩带要求"]


# ---------- 提示词合并 ----------

def test_prompt_additions_are_appended_once():
    merged = apply_prompt_additions("影棚白底", ["保持领口不变", "保持领口不变"])
    assert merged.count("保持领口不变") == 1
    assert merged.startswith("影棚白底")


def test_prompt_additions_do_not_accumulate_across_rounds():
    """多轮重生会反复合并,不去重会堆成一堵墙。"""
    prompt = "影棚白底"
    for _ in range(5):
        prompt = apply_prompt_additions(prompt, ["保持肩带不变"])
    assert prompt.count("保持肩带不变") == 1


def test_empty_additions_leave_prompt_untouched():
    assert apply_prompt_additions("原始", []) == "原始"
    assert apply_prompt_additions(None, []) is None


def test_additions_work_on_empty_base():
    assert apply_prompt_additions(None, ["新要求"]) == "新要求"


# ---------- Mock 评分器 ----------

def _evaluate(outcome="auto", round_number=1, image="candidates/ab/cd/fixed.jpg", quick=False):
    evaluator = MockImageQualityEvaluator()
    return _run(
        evaluator.evaluate(
            ["assets/aa/bb/front.jpg"],
            image,
            {"sku": "SKU-1", "garment_type": "BIKINI_SET"},
            {"mock_evaluator": {"outcome": outcome, "round_number": round_number,
                                "quick_only": quick}},
        )
    )


def test_mock_evaluator_is_always_configured():
    assert MockImageQualityEvaluator().is_configured() is True


def test_mock_evaluator_returns_all_dimensions():
    payload = _evaluate("a")
    assert set(payload["scores"]) == set(SCORE_DIMENSIONS)


def test_mock_evaluator_is_deterministic():
    """同一张图重跑必须得到同一个分数,否则"为什么判 C"永远说不清。"""
    first = _evaluate("auto")
    for _ in range(10):
        assert _evaluate("auto") == first


def test_mock_evaluator_scores_depend_on_the_image():
    left = _evaluate("auto", image="candidates/11/22/aaa.jpg")
    right = _evaluate("auto", image="candidates/33/44/bbb.jpg")
    assert left["scores"] != right["scores"]


def test_mock_evaluator_can_force_each_grade():
    """需求第十七章:模拟 A、B、C、D 各档评分。"""
    grades = {}
    for outcome in ("a", "b", "c", "d"):
        payload = _evaluate(outcome)
        result = parse_evaluator_payload(payload, evaluator="mock")
        grades[outcome] = grade_candidate(result).grade
    assert grades["a"] is Grade.A
    assert grades["b"] is Grade.B
    assert grades["c"] is Grade.C
    assert grades["d"] is Grade.D


def test_mock_evaluator_can_force_hard_fail():
    """需求第十七章:模拟硬错误。"""
    payload = _evaluate("hard_fail")
    assert payload["hard_fail"] is True
    assert payload["hard_fail_codes"]
    result = parse_evaluator_payload(payload, evaluator="mock")
    assert grade_candidate(result).grade is Grade.D


def test_mock_improving_mode_converges_to_a():
    """需求第十七章:模拟多轮重生。"""
    grades = [
        grade_candidate(
            parse_evaluator_payload(_evaluate("improving", round_number=r), evaluator="mock")
        ).grade
        for r in (1, 2, 3)
    ]
    assert grades == [Grade.C, Grade.B, Grade.A]


def test_mock_stuck_mode_never_passes():
    """需求第十七章:模拟最终进入人工审核。"""
    for round_number in (1, 2, 3, 4):
        result = parse_evaluator_payload(
            _evaluate("stuck", round_number=round_number), evaluator="mock"
        )
        assert grade_candidate(result).grade is Grade.D


def test_mock_reported_overall_differs_from_weighted_total():
    """内置探针:Mock 故意自报一个不同的总分,后端必须用自己算的那个。"""
    result = parse_evaluator_payload(_evaluate("b"), evaluator="mock")
    assert result.model_reported_overall != result.overall_score


def test_quick_mode_returns_fewer_dimensions():
    """需求第十三章:后两名只做快速硬错误检查。"""
    full = _evaluate("c", quick=False)
    quick = _evaluate("c", quick=True)
    assert len(quick["scores"]) < len(full["scores"])


def test_quick_mode_still_reports_hard_fails():
    payload = _evaluate("hard_fail", quick=True)
    assert payload["hard_fail"] is True


def test_mock_output_parses_cleanly_for_every_outcome():
    for outcome in ("a", "b", "c", "d", "hard_fail", "improving", "stuck", "auto"):
        result = parse_evaluator_payload(_evaluate(outcome), evaluator="mock")
        assert 0 <= result.overall_score <= 100


# ---------- 视觉大模型骨架 ----------

def test_unconfigured_vision_evaluator_reports_false_and_refuses_to_run():
    """未配置时:``is_configured()`` 为假,且 ``evaluate()`` 必须抛错。

    关键是**不能返回一个"看起来还行"的默认分** —— 那会让没被真正评过的图
    自动通过。有没有参考图不影响这条,所以两种入参一起在这里过一遍。

    (原先这件事被拆成四个用例、分散在两个文件里,断言完全一样。)
    """
    evaluator = VisionModelImageQualityEvaluator(VisionEvaluatorConfig())
    assert evaluator.is_configured() is False

    for references in ([], ["ref.jpg"]):
        expect_raises(
            NotConfiguredError,
            lambda refs=references: _run(evaluator.evaluate(refs, "x.jpg", {}, {})),
        )


def test_vision_prompt_lists_all_dimensions_and_hard_fail_codes():
    prompt = VisionModelImageQualityEvaluator(VisionEvaluatorConfig()).build_prompt({}, {})
    for dimension in SCORE_DIMENSIONS:
        assert dimension in prompt
    for code in HardFailCode:
        assert code.value in prompt


def test_vision_prompt_forbids_markdown_wrapping():
    prompt = VisionModelImageQualityEvaluator(VisionEvaluatorConfig()).build_prompt({}, {})
    assert "Markdown" in prompt or "markdown" in prompt


def test_vision_parser_is_shared_with_scoring_layer():
    parsed = VisionModelImageQualityEvaluator.parse_model_text('{"scores": {"composition": 80}}')
    assert parsed["scores"]["composition"] == 80


def test_vision_test_connection_never_raises():
    """连接自检在任何配置下都要能返回,后台页面不能因为它打不开。"""
    for config in (VisionEvaluatorConfig(), VisionEvaluatorConfig(base_url="https://x.invalid")):
        result = _run(VisionModelImageQualityEvaluator(config).test_connection())
        assert "configured" in result


# ---------- 注册表 ----------

def test_registry_exposes_mock_and_vision():
    assert set(available_names()) == {"mock", "vision"}


def test_unknown_evaluator_name_raises():
    from app.core.errors import ValidationError

    expect_raises(ValidationError, get_evaluator, "nope")


# 「显式选 Mock 始终可用」由下方 test_explicit_mock_still_works_in_any_environment
# 覆盖 —— 那个用例把环境固定成 production,是更严格的场景。


def test_unusable_evaluator_falls_back_to_mock_only_in_development():
    """原断言是「评分器没配好就退回 Mock」,理由写的是「判断力下降但闭环成立」。

    那条理由站不住:Mock 按文件指纹给分,真实商品图有相当比例会被判成 A 档,
    于是自动通过、自动出图、自动发布,运营端只看到一行「任务成功」。
    「判断力下降」的真实后果是**没被真正评过的图上了网站**。

    现在只有开发环境允许这种退回,其余环境必须抛错并转人工审核。
    """
    from app.evaluators import registry

    original = registry._mock_fallback_allowed
    try:
        registry._mock_fallback_allowed = lambda: True
        assert get_active_evaluator("vision").evaluator_name == "mock"
        assert get_active_evaluator("does-not-exist").evaluator_name == "mock"

        registry._mock_fallback_allowed = lambda: False
        expect_raises(EvaluatorUnavailableError, get_active_evaluator, "vision")
        expect_raises(EvaluatorUnavailableError, get_active_evaluator, "does-not-exist")
    finally:
        registry._mock_fallback_allowed = original


# 「vision 已实现」只从对外输出断言(见 test_describe_all_marks_vision_as_implemented),
# 不再直接检查 registry 内部的 IMPLEMENTED_EVALUATORS 集合 —— 对外输出才是稳定契约。
# implemented 与 configured 仍是两件事:is_usable 要求两者都成立,
# 这一条由 test_vision_is_usable_only_when_actually_configured 守着。


def test_vision_is_usable_only_when_actually_configured():
    """"已实现"不等于"能用" —— 少了 Key 就不该被选中去评分。"""
    from app.evaluators import registry

    original = registry.get_evaluator
    try:
        registry.get_evaluator = lambda name: VisionModelImageQualityEvaluator(
            VisionEvaluatorConfig(base_url="https://api.example.com/v1", model="m")
        )
        assert registry.is_usable("vision") is False  # 缺 Key

        registry.get_evaluator = lambda name: VisionModelImageQualityEvaluator(
            VisionEvaluatorConfig(base_url="https://api.example.com/v1", model="m", api_key="k")
        )
        assert registry.is_usable("vision") is True
    finally:
        registry.get_evaluator = original


def test_describe_all_never_leaks_secrets():
    for entry in describe_all():
        blob = repr(entry).lower()
        assert "api_key" not in blob and "secret" not in blob


def test_all_evaluators_implement_the_interface():
    for name in available_names():
        evaluator = get_evaluator(name)
        assert isinstance(evaluator, ImageQualityEvaluator)
        assert evaluator.evaluator_name == name


# ---------- 结构化包装 ----------

def test_evaluate_structured_produces_typed_result():
    evaluator = MockImageQualityEvaluator()
    result = _run(
        evaluator.evaluate_structured(
            ["a.jpg"], "candidates/ab/cd/x.jpg", {}, {"mock_evaluator": {"outcome": "c"}}
        )
    )
    assert result.evaluator == "mock"
    assert result.duration_ms is not None
    assert all(isinstance(p.severity, ProblemSeverity) for p in result.problems)


# ---------- 生产环境禁止静默回退 Mock ----------

def test_production_never_falls_back_to_mock_when_fail_closed():
    """这是本次改动的核心断言。

    退回 Mock 的真实后果不是"判断力下降",是**没被真正评过的图自动发上网站** ——
    Mock 按文件指纹给分,真实商品图有相当比例会被判成 A 档,于是自动通过、
    自动出图、自动发布,而运营端只看到一行「任务成功」。
    """
    from app.evaluators import registry

    original_env = registry._app_env
    original_flag = registry._fail_closed_configured
    try:
        registry._fail_closed_configured = lambda: True
        for env in ("production", "prod"):
            registry._app_env = lambda env=env: env
            assert registry._mock_fallback_allowed() is False, env
    finally:
        registry._app_env = original_env
        registry._fail_closed_configured = original_flag


def test_production_may_opt_out_of_fail_closed_explicitly():
    """关掉 VISION_MODEL_FAIL_CLOSED 是一个显式选择,不是默认值。"""
    from app.evaluators import registry

    original_env = registry._app_env
    original_flag = registry._fail_closed_configured
    try:
        registry._app_env = lambda: "production"
        registry._fail_closed_configured = lambda: False
        assert registry._mock_fallback_allowed() is False  # production 不在 LOCAL_ENVS 里
    finally:
        registry._app_env = original_env
        registry._fail_closed_configured = original_flag


def test_only_developer_machines_may_fall_back_to_mock():
    from app.evaluators import registry

    original_env = registry._app_env
    original_flag = registry._fail_closed_configured
    try:
        registry._fail_closed_configured = lambda: True
        for env in ("local", "dev", "development"):
            registry._app_env = lambda env=env: env
            assert registry._mock_fallback_allowed() is True, env
    finally:
        registry._app_env = original_env
        registry._fail_closed_configured = original_flag


def test_a_test_environment_may_not_silently_fall_back_to_mock():
    """"测试环境"通常是一台连着真实数据和真实 Key 的机器。

    视觉模型配错一个参数就静默退回 Mock,而 Mock 按文件指纹给分 ——
    真实商品图有相当比例会被判成 A 档,于是自动通过、自动出图,
    运营端只看到一行"任务成功"。要在这里用 Mock 必须是显式的决定
    (EVALUATOR_BACKEND=mock),不能是配错之后的默认行为。
    """
    from app.evaluators import registry

    original_env = registry._app_env
    original_flag = registry._fail_closed_configured
    try:
        registry._fail_closed_configured = lambda: True
        for env in ("test", "testing", "staging", "uat", "pre", ""):
            registry._app_env = lambda env=env: env
            assert registry._mock_fallback_allowed() is False, env
    finally:
        registry._app_env = original_env
        registry._fail_closed_configured = original_flag


def test_unreadable_config_defaults_to_fail_closed():
    """读不到配置时按最保守的来:默认安全,不默认方便。"""
    from app.evaluators import registry

    original = registry._app_env
    try:
        registry._app_env = lambda: ""
        assert registry._mock_fallback_allowed() is False
    finally:
        registry._app_env = original


def test_explicit_mock_still_works_in_any_environment():
    """显式选 Mock 是正当选择(离线演练、无 Key 演示),fail-closed 不该拦它。"""
    from app.evaluators import registry

    original = registry._app_env
    try:
        registry._app_env = lambda: "production"
        assert get_active_evaluator("mock").evaluator_name == "mock"
    finally:
        registry._app_env = original


def test_unusable_reason_names_the_missing_setting():
    """错误信息要能直接告诉运维少配了哪一项,而不是笼统的"未配置"。"""
    from app.evaluators import registry

    original = registry.get_evaluator
    try:
        registry.get_evaluator = lambda name: VisionModelImageQualityEvaluator(
            VisionEvaluatorConfig(base_url="https://api.example.com/v1")
        )
        assert "VISION_MODEL_NAME" in registry._why_unusable("vision")
    finally:
        registry.get_evaluator = original


def test_describe_all_marks_vision_as_implemented():
    entry = next(e for e in describe_all() if e["name"] == "vision")
    assert entry["implemented"] is True
