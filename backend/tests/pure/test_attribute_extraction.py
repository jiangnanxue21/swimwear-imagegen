"""M3:识别 Schema、归一化、置信度、决策表(零三方依赖)。

真库层的落库行为在 `tests/test_a45_batch14_extraction_db.py`。
(这里原来指向 `tests/test_api_attributes.py` —— 那个文件树里不存在,
从来没有人能顺着它走过去。)
这里测的是**规则**,而规则正是最需要能穷举、也最不需要数据库的部分。
"""
from __future__ import annotations

import ast
import re

from app.attributes.confidence import (
    MIN_SAMPLES_PER_BIN,
    MIN_SAMPLES_PER_FIELD,
    SOURCE_FACTOR,
    CalibrationBin,
    agreement_factor,
    calibrated,
    compute,
    quality_factor,
)
from app.attributes.registry import REGISTRY, extractable_fields
from app.core.enums import MediaSource
from app.extractors.mock import MockAttributeExtractor
from app.extractors.schema import (
    build_extraction_prompt,
    build_extraction_schema,
    normalize_value,
    schema_matches_registry,
)
from tests.pure._helpers import BACKEND_ROOT, expect_raises

APP_DIR = BACKEND_ROOT / "app"


# ---------------------------------------------------------------- Schema


def test_schema_is_generated_from_the_registry():
    """Schema 与注册表必须一致 —— 这是 M3 的出口条件之一。"""
    schema = build_extraction_schema()
    assert schema_matches_registry(schema)


def test_non_visual_fields_cannot_enter_the_schema():
    """看图看不出来的字段禁止出现在识别 Schema 里。

    显式点名一个不可识别的字段要**抛错**,不能静默过滤 ——
    悄悄忽略会让调用方以为识别过了。
    """
    assert "material" not in extractable_fields()
    expect_raises(ValueError, build_extraction_schema, ("material",))
    expect_raises(ValueError, build_extraction_schema, ("garment_type", "material"))


def test_unknown_fields_are_rejected():
    expect_raises(ValueError, build_extraction_schema, ("no_such_field",))


def test_schema_requires_unreadable_fields():
    """`unreadable_fields` 不是可选的。

    没有它,「看不清」只能表现为一个低置信度的猜测值,而那和
    「有把握但就是这个值」在数值上是同一个东西。
    """
    schema = build_extraction_schema()
    assert "unreadable_fields" in schema["required"]


def test_schema_never_asks_the_model_for_a_conclusion():
    """只要 confidence,不要等级、评分、是否合格。"""
    schema = build_extraction_schema()
    props = schema["properties"]["fields"]["items"]["properties"]
    assert set(props) == {"name", "value", "confidence", "evidence"}
    for banned in ("grade", "overall_score", "recommended_action", "pass"):
        assert banned not in props


def test_prompt_contains_no_anchoring_source():
    """提示词里不能有已有值、标题、供应商描述(§6.1)。

    模型看到 `{"primary_color": "NAVY"}` 之后就会把图判成 NAVY,
    所谓交叉验证只是复读我们喂进去的答案。
    """
    prompt = build_extraction_prompt()
    for anchor in ("known", "已有", "当前值", "商品标题", "供应商"):
        assert anchor not in prompt, f"提示词里出现了锚定源:{anchor}"
    assert "unreadable_fields" in prompt
    assert "不要猜" in prompt


def test_prompt_spells_out_the_json_object_envelope_and_array_shapes():
    """json_object 只保证 JSON 合法,不替我们执行 Schema。

    千问 VL 的一次真实返回把属性全部平铺在顶层,并把单个 missing 写成
    ``{"back_style": "NOT_VISUALLY_DETERMINABLE"}``。解析器正确地拒绝了它,
    但提示词当时从未告诉模型 fields/missing 的完整信封长什么样 ——
    原样重试只会再买一次同形坏响应。
    """
    prompt = build_extraction_prompt(("garment_type", "back_style"))

    assert '"fields":[' in prompt
    assert '"unreadable_fields":[]' in prompt
    assert '"missing":[' in prompt
    assert '"name":"字段名"' in prompt
    assert '"confidence":0.95' in prompt
    assert "missing 即使只有一项也必须是数组" in prompt
    assert "属性名直接放在 JSON 顶层" in prompt
    assert "禁止把 missing 写成字段到原因的对象" in prompt


def test_prompt_change_has_its_own_version_for_calibration_and_idempotency():
    """提示词变了必须换版本,不能继续借用旧提示词的校准证据。"""
    from app.extractors.vision import EXTRACTION_PROMPT_VERSION

    assert EXTRACTION_PROMPT_VERSION == "vision-2.1"


# ---------------------------------------------------------------- 归一化


def test_model_invented_enum_values_are_rejected():
    """模型自造的枚举值必须在归一化时被拦下。

    放过去的话它会一路流到平台字段映射,变成一个平台不认识的值 ——
    而那时候的错误信息通常毫无指向性。
    """
    assert normalize_value("neckline_type", "V_NECK") == "V_NECK"
    assert normalize_value("neckline_type", "v_neck") == "V_NECK"     # 大小写宽容
    assert normalize_value("neckline_type", "HEART_SHAPED_NECK") is None
    assert normalize_value("neckline_type", "") is None
    assert normalize_value("neckline_type", None) is None


def test_free_text_fields_pass_through():
    assert normalize_value("primary_color", "navy") == "navy"
    assert normalize_value("no_such_field", "x") is None


# ---------------------------------------------------------------- 置信度


def test_uncalibrated_returns_none_not_zero():
    """未校准 = None。**这是 fail closed 的前提。**

    返回 0 会让它看起来像「置信度很低」,于是被当成普通低分处理;
    实际含义是「这个数字根本没有意义」。
    """
    value, version = calibrated(0.95, [])
    assert value is None and version is None


def test_insufficient_samples_block_calibration():
    """样本不足同样返回 None。

    30 个样本估出来的准确率,置信区间宽到没法用。
    """
    thin = [CalibrationBin(0.9, 1.0, 0.97, MIN_SAMPLES_PER_BIN - 1, "v1")]
    assert calibrated(0.95, thin)[0] is None

    # 每箱够了,但字段合计不够
    per_bin_ok = [CalibrationBin(0.9, 1.0, 0.97, MIN_SAMPLES_PER_BIN, "v1")]
    assert MIN_SAMPLES_PER_BIN < MIN_SAMPLES_PER_FIELD
    assert calibrated(0.95, per_bin_ok)[0] is None

    enough = [
        CalibrationBin(0.0, 0.9, 0.70, MIN_SAMPLES_PER_FIELD, "v1"),
        CalibrationBin(0.9, 1.0, 0.97, MIN_SAMPLES_PER_BIN, "v1"),
    ]
    value, version = calibrated(0.95, enough)
    assert value == 0.97 and version == "v1"


def test_agreement_factor_zeroes_out_on_split():
    """过半分歧直接归零 —— 那种情况下"多数意见"没有意义,该转人工。"""
    assert agreement_factor(3, 3) == 1.0
    assert agreement_factor(2, 2) == 0.95
    assert agreement_factor(1, 1) == 0.85      # 单图没有交叉验证
    assert agreement_factor(3, 2) == 0.7
    assert agreement_factor(2, 1) == 0.0
    assert agreement_factor(0, 0) == 0.0


def test_quality_factor_penalises_unmeasured_images():
    """没测过的图不该享受"清晰"的待遇。"""
    assert quality_factor(None) == 0.85
    assert quality_factor({}) == 0.85
    assert quality_factor({"sharpness": 1.0}) == 1.0
    assert quality_factor({"sharpness": 1.0, "has_watermark": True}) < 1.0
    assert quality_factor({"sharpness": 0.1}) >= 0.6      # 有下限


def test_ai_generated_images_are_discounted():
    """AI 图只能是旁证(硬规矩 6)。

    它可能改变领型、颜色、遮盖度,对属性的证明力天然低于原始素材。
    """
    assert SOURCE_FACTOR[MediaSource.AI_GENERATED.value] < 1.0
    assert SOURCE_FACTOR[MediaSource.MANUAL_UPLOAD.value] == 1.0
    assert SOURCE_FACTOR[MediaSource.PLATFORM_SYNC.value] < 1.0


def test_compute_returns_none_when_uncalibrated():
    breakdown = compute(
        field_name="neckline_type",
        model_confidence=0.99,
        bins=[],
        total_images=3,
        agreeing_images=3,
        quality={"sharpness": 1.0},
        media_source=MediaSource.MANUAL_UPLOAD.value,
    )
    assert breakdown.value is None, "没有校准数据却算出了一个数"
    # 但因子本身要留下来:前端要能解释"为什么不能自动确认"
    assert breakdown.agreement_factor == 1.0
    assert breakdown.visibility_prior == REGISTRY["neckline_type"].visibility_prior


# ---------------------------------------------------------------- 决策表


def _decide(field, confidence, existing=None):
    from app.attributes.decision import decide_status

    return decide_status(
        field_name=field, system_confidence=confidence, existing=existing
    )


class _FakeValue:
    def __init__(self, source):
        self.source = source


def test_never_policy_fields_never_auto_confirm():
    """材质永不自动确认,哪怕置信度是 1.0。"""
    from app.core.enums import AttributeStatus

    assert _decide("material", 1.0) is AttributeStatus.CANDIDATE


def test_uncalibrated_lands_in_candidate():
    """未校准 -> CANDIDATE(留证据不采信)。"""
    from app.core.enums import AttributeStatus

    assert _decide("neckline_type", None) is AttributeStatus.CANDIDATE
    assert _decide("neckline_type", 0.4) is AttributeStatus.CANDIDATE


def test_threshold_boundaries():
    from app.core.enums import AttributeStatus

    # structure 组阈值 0.92
    assert _decide("neckline_type", 0.919) is AttributeStatus.SUGGESTED
    assert _decide("neckline_type", 0.92) is AttributeStatus.CONFIRMED
    # identity 组阈值 0.95 —— 颜色和品类错了直接上错货
    assert _decide("garment_type", 0.94) is AttributeStatus.SUGGESTED
    assert _decide("garment_type", 0.95) is AttributeStatus.CONFIRMED


def test_manual_value_is_never_overwritten():
    """人工值在场时一律 CONFLICT,不管置信度多高(§6.2 规则 4)。"""
    from app.core.enums import AttributeSource, AttributeStatus

    manual = _FakeValue(AttributeSource.MANUAL.value)
    assert _decide("neckline_type", 0.99, manual) is AttributeStatus.CONFLICT


def test_existing_non_manual_value_does_not_auto_confirm_again():
    """库里已有值(非人工)时不再自动确认,只出建议。"""
    from app.core.enums import AttributeSource, AttributeStatus

    extracted = _FakeValue(AttributeSource.EXTRACTED.value)
    assert _decide("neckline_type", 0.99, extracted) is AttributeStatus.SUGGESTED


# ---------------------------------------------------------------- Mock


def test_mock_is_deterministic():
    """同一张图每次识别得到同样的证据。

    不确定性会让合并与冲突的测试没法复现 —— 而那两块正是最需要
    能反复跑的部分。
    """
    ex = MockAttributeExtractor()
    fields = extractable_fields()
    enums = {n: () for n in fields}
    a = ex.extract(image="asset-1", target_fields=fields, enum_defs=enums, options={})
    b = ex.extract(image="asset-1", target_fields=fields, enum_defs=enums, options={})
    assert [(f.name, f.value, f.confidence) for f in a.fields] == \
           [(f.name, f.value, f.confidence) for f in b.fields]
    assert a.unreadable_fields == b.unreadable_fields


def test_mock_exercises_the_unreadable_branch():
    """Mock 必须能产出"看不清"。

    只在所有字段都识别成功时跑得通的链路,等于没测过失败分支。
    """
    ex = MockAttributeExtractor()
    fields = extractable_fields()
    enums = {n: () for n in fields}
    seen_unreadable = any(
        ex.extract(image=f"asset-{i}", target_fields=fields, enum_defs=enums, options={})
        .unreadable_fields
        for i in range(20)
    )
    assert seen_unreadable, "Mock 从来不报看不清,失败分支测不到"


def test_mock_never_returns_an_unregistered_field():
    ex = MockAttributeExtractor()
    fields = extractable_fields()
    result = ex.extract(
        image="x", target_fields=fields, enum_defs={n: () for n in fields}, options={}
    )
    for f in result.fields:
        assert f.name in REGISTRY


# ---------------------------------------------------------------- 结构约束


def test_extraction_never_passes_known_values_to_the_model():
    """编排层不能把已有值传给模型(§6.1)。"""
    tree = ast.parse((APP_DIR / "attributes" / "service.py").read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "run_extraction"
    )
    body = "\n".join(ast.unparse(s) for s in fn.body[1:])
    # 按词匹配,不按子串:`"unknown"` 里也有 known,
    # 而那是 getattr 的默认值,和锚定毫无关系
    assert not re.search(r"\bknown\b", body), "识别调用里出现了 known"
    assert "current_value" not in body, "识别阶段读了已有值,那就是锚定"
    assert "effective_map" not in body


def test_only_one_writer_for_attribute_values():
    """`product_attribute_values` 只能由 `set_value()` 写。

    多个写入点意味着"人工值永不被覆盖"这条规则要在每个点各查一遍,
    而漏掉一处的后果是人工确认过的值被一次自动识别冲掉。
    """
    writers = []
    for path in APP_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        # 排除类定义本身(`class ProductAttributeValue(Base, ...)`)
        if "ProductAttributeValue(" in text and "class ProductAttributeValue(" not in text:
            writers.append(path.relative_to(APP_DIR).as_posix())
    assert writers == ["attributes/service.py"], f"多处直接构造属性值:{writers}"
