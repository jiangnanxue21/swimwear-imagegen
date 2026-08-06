"""属性值校验与 owner 分层(A43 / BLOCK-03)。

判定部分是真调用真断言 —— `validation.py` 只 import 注册表和枚举,
没有数据库,所以这一组能在裸 python3 上跑完。

接线部分(校验长在 `set_value` 上、API 翻成 422)只能静态钉:
它们要 fastapi + sqlalchemy 才跑得起来。钉的方式是 AST,
理由和 `test_batch_lease.py` 开头那段一样。
"""
from __future__ import annotations

import ast

from app.attributes import validation as v
from app.attributes.registry import REGISTRY, ValueType
from app.core.enums import OwnerType
from tests.pure._helpers import BACKEND_ROOT, expect_raises

SERVICE = BACKEND_ROOT / "app" / "attributes" / "service.py"
API = BACKEND_ROOT / "app" / "api" / "attributes.py"


# ---------------------------------------------------------------- 类型


def test_a_list_field_refuses_the_string_it_used_to_accept():
    """**这一条钉的是 BLOCK-11 那个真实事故。**

    `["RED","BLUE"]` 在界面上显示成 `RED / BLUE`,原样提交回来就是一个
    字符串。后端收下它之后,库里那一列从此不再是数组,而下游按列表消费的
    地方(渠道层颜色映射)拿到的是一个字符 'R'、一个字符 'E'……
    """
    expect_raises(
        v.AttributeValueError, v.validate_attribute_value, "secondary_colors", "RED / BLUE"
    )
    assert v.validate_attribute_value("secondary_colors", ["Red", "blue"]) == [
        "Red",
        "blue",
    ]


def test_an_enum_field_refuses_a_value_that_is_not_in_the_enum():
    """自造的枚举值必须在这里被拦下,而不是流到平台字段映射。"""
    expect_raises(
        v.AttributeValueError, v.validate_attribute_value, "garment_type", "NOT_A_REAL_TYPE"
    )


def test_enum_values_come_back_canonical_not_as_typed():
    """校验和归一是同一件事的两面。分开做的话总有一条路只做了一半。"""
    field = next(
        name for name, spec in REGISTRY.items() if spec.value_type is ValueType.ENUM
    )
    allowed = [x.value for x in REGISTRY[field].enum]
    assert v.validate_attribute_value(field, allowed[0].lower()) == allowed[0]


def test_objects_are_refused_everywhere():
    """注册表今天没有任何字段声明结构化 schema,所以 dict 一律拒绝。

    要支持结构化字段,先在注册表里声明它的形状,再回来改这里 ——
    一个没有 schema 的对象进了 `value_json`,下游只能靠猜。
    """
    for field in ("garment_type", "material", "secondary_colors"):
        expect_raises(v.AttributeValueError, v.validate_attribute_value, field, {"a": 1})


def test_a_single_value_field_refuses_a_list():
    expect_raises(v.AttributeValueError, v.validate_attribute_value, "material", ["锦纶", "氨纶"])


def test_booleans_do_not_sneak_in_as_text_or_numbers():
    """`bool` 是 `int` 的子类。不单独挡的话 True 会变成 1 悄悄存进去。"""
    expect_raises(v.AttributeValueError, v.validate_attribute_value, "material", True)
    expect_raises(v.AttributeValueError, v.validate_attribute_value, "primary_color", False)


def test_lists_are_deduped_and_capped():
    """同一个值提交两次不是错误,但存两份会让"有几个副色"取决于提交次数。"""
    assert v.validate_attribute_value("secondary_colors", ["红", "红", "蓝"]) == [
        "红",
        "蓝",
    ]
    expect_raises(
        v.AttributeValueError,
        v.validate_attribute_value,
        "secondary_colors",
        [f"c{i}" for i in range(v.MAX_LIST_ITEMS + 1)],
    )


def test_text_is_trimmed_and_length_capped():
    assert v.validate_attribute_value("material", "  锦纶  ") == "锦纶"
    expect_raises(
        v.AttributeValueError, v.validate_attribute_value, "material", "x" * (v.MAX_TEXT_LENGTH + 1)
    )


def test_empty_string_is_refused_but_null_clears():
    """清空一个字段是合法动作;把它伪装成空字符串会让空值有两种长相。"""
    assert v.validate_attribute_value("material", None) is None
    expect_raises(v.AttributeValueError, v.validate_attribute_value, "material", " ")


def test_unregistered_fields_are_refused():
    expect_raises(v.AttributeValueError, v.validate_attribute_value, "no_such_field", "x")


def test_every_registered_field_has_a_validator():
    """注册表加一个新 value_type 而这边没跟上时,应该当场 KeyError。

    这条用例把全部字段跑一遍,是为了让"加了类型没加校验"在 CI 里红,
    而不是等到有人提交那个字段时才在生产上炸。
    """
    samples = {
        ValueType.ENUM: lambda spec: [x.value for x in spec.enum][0],
        ValueType.ENUM_LIST: lambda spec: [[x.value for x in spec.enum][0]],
        ValueType.TEXT: lambda spec: "x",
        ValueType.TEXT_LIST: lambda spec: ["x"],
        ValueType.NUMBER: lambda spec: 1,
        ValueType.BOOL: lambda spec: True,
    }
    for name, spec in REGISTRY.items():
        v.validate_attribute_value(name, samples[spec.value_type](spec))


# ---------------------------------------------------------------- owner 分层


def test_owner_type_comes_from_the_registry_not_from_the_caller():
    """**这是 BLOCK-03 后半段的全部内容。**

    原来抽取和人工确认都硬写 `owner_type=SPU, owner_id=str(product.id)`。
    两个字段都不对:颜色在注册表里声明的是 VARIANT,而 `product.id` 是一个
    SKU 行的主键、不是 SPU 的标识。合起来的实际语义是"每个 Product UUID
    一桶属性",分层只存在于契约里。

    ## A44:VARIANT 的 owner_id 不再是裸变体 id

    这一条原来断言 `(VARIANT, "RED")`。改成命名空间形式不是口味问题 ——
    那张表的唯一索引是 `(owner_type, owner_id, field_name)`,**没有 SPU**,
    于是"RED"这个 owner 是全库共用的:两个 SPU 都有红色时,给一个确认
    等于给另一个也确认了。见 `test_variant_key.py` 里那一组。

    SPU 层保持裸值:spu 本身就是全局唯一的,套一层只会让读写多一个
    对不齐的机会。
    """
    coords = {"spu": "SW-001", "variant_id": "RED", "sku": "SW-001-RED-M"}
    variant_owner = v.variant_owner_id(spu="SW-001", variant_id="RED")
    assert v.owner_for("primary_color", **coords) == (OwnerType.VARIANT, variant_owner)
    assert v.owner_for("secondary_colors", **coords) == (OwnerType.VARIANT, variant_owner)
    assert v.owner_for("garment_type", **coords) == (OwnerType.SPU, "SW-001")
    assert v.owner_for("material", **coords) == (OwnerType.SPU, "SW-001")
    assert v.split_variant_owner_id(variant_owner) == ("SW-001", "RED")


def test_owner_for_refuses_an_empty_coordinate():
    """回落到空字符串会让不同商品的属性挤进同一个 owner。

    那种数据损坏在读取时看起来完全正常 —— 所以必须在写入前拒绝。
    """
    expect_raises(
        v.AttributeValueError,
        v.owner_for,
        "primary_color",
        spu="SW-001",
        variant_id=" ",
        sku="SW-001-RED-M",
    )


def test_owner_for_signature_has_no_owner_type_parameter():
    """调用方不许指定层级。能指定就等于把"颜色属于变体"抄到每个调用点。"""
    import inspect

    params = set(inspect.signature(v.owner_for).parameters)
    assert "owner_type" not in params
    assert params == {"field_name", "spu", "variant_id", "sku"}


# ---------------------------------------------------------------- 接线


def test_validation_lives_on_the_single_write_entry():
    """校验必须长在 `set_value` 上,不是长在 API 层。

    `set_value` 自称"唯一允许写 product_attribute_values 的入口",而识别
    合并走的是同一个函数。只在 API 层拦,等于放过机器那条路 ——
    而模型输出恰恰是最不该被信任的那一路。
    """
    src = SERVICE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "set_value"
    )
    body = ast.get_source_segment(src, fn) or ""
    assert "validate_attribute_value(field_name, value)" in body, (
        "唯一写入点必须校验,否则识别合并那条路会绕过契约"
    )


def test_no_write_path_still_hardcodes_the_spu_owner():
    """`owner_type=OwnerType.SPU` 这个字面量不许再出现在写入调用里。"""
    src = SERVICE.read_text(encoding="utf-8")
    assert "owner_type=OwnerType.SPU,\n" not in src, (
        "写入方仍在硬指定层级 —— owner_type 必须由 owner_for() 从注册表取"
    )


def test_the_confirm_api_answers_422_and_names_the_field():
    """一次确认可以提交多个字段,只说"格式不对"等于让人挨个试。"""
    src = API.read_text(encoding="utf-8")
    assert "AttributeValueError" in src
    assert "http_status=422" in src
    assert "session.rollback()" in src, (
        "整批回滚 —— 写一半会留下一个谁也没打算要的中间状态"
    )
    assert '"field_name": exc.field_name' in src


def test_extraction_skips_a_bad_field_instead_of_failing_the_whole_run():
    """人工那条路必须 422,机器这条路不能。

    模型偶尔吐出一个不在枚举里的值是常态。让它炸掉整次识别意味着另外 7 个
    正常字段也写不进去,而运营看到的是"识别失败",指不到真正的原因。
    """
    src = SERVICE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "apply_evidence"
    )
    body = ast.get_source_segment(src, fn) or ""
    assert "except AttributeValueError" in body
    assert "continue" in body


def test_the_variant_layer_is_actually_assembled():
    """`CanonicalVariant.attrs` 不能再是一个永远为空的契约。

    渠道映射的 `_variant_attr_bucket()` 一直在读它,只是没有组装方填过 ——
    于是"这个颜色是波点、那个颜色是纯色"这类事实无论怎么确认都进不了导出。
    """
    src = SERVICE.read_text(encoding="utf-8")
    assert "CanonicalVariant(" in src, "build_canonical 必须组装变体层"
    wb = (BACKEND_ROOT / "app" / "workbench" / "service.py").read_text(encoding="utf-8")
    assert "variants=canonical_variants" in wb, (
        "SKU 展开必须把变体层带过去,否则导出那条路上它又被丢掉了"
    )
    assert "variant_attr_map(" in wb, (
        "多颜色 SPU 的变体属性要一条 SQL 查完 —— 每个变体调一次组装函数"
        "会把库读嵌进两层循环"
    )
