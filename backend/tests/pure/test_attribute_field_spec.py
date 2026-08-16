"""字段类型元数据的契约(BLOCK-11)。

## 为什么这批断言不写成 `"xxx" in src`

a30 复检报告点名 `test_blocking_fix_contracts.py` 的判据形状偏弱:
十几条里大半是源码字符串匹配,钉的是"这行字还在",不是"这个行为还对"。
那种断言在重构时会以两种方式失去意义 —— 把代码挪进辅助函数(断言变红,
但根因不是回归),或者同名字符串在别处多出现一次(断言恒绿,回归漏网)。

这个文件里的每一条都是**真的调用一次函数、看返回值**。
它们能这么写,是因为 `registry` 零三方依赖(只碰 `core/enums.py`)——
和 `workflows/` 放在 tests/pure 是同一个理由。
"""
from __future__ import annotations

from app.attributes.registry import REGISTRY, ValueType, field_spec_out


def test_every_registered_field_reports_a_spec():
    """注册表里的每个字段都要能给出类型 —— 前端按字段名逐行取,漏一个就退化成文本框。"""
    for name in REGISTRY:
        spec = field_spec_out(name)
        assert spec["value_type"], name
        assert isinstance(spec["multi_value"], bool), name
        assert isinstance(spec["options"], list), name
        assert isinstance(spec["option_labels"], dict), name


def test_multi_value_matches_the_registry_not_a_hand_kept_list():
    """`multi_value` 必须与注册表一致。

    它和 `value_type` 冗余是**故意的**(前端最常问的就是这一句),
    但冗余的代价是它可能和事实分叉 —— 所以在这里钉住。
    """
    for name, field in REGISTRY.items():
        assert field_spec_out(name)["multi_value"] is field.multi_value, name


def test_list_fields_are_actually_reported_as_lists():
    """这一条是 BLOCK-11 的核心:列表字段必须**被前端认出来是列表**。

    认不出来的后果不是显示不好看,是 `["RED","BLUE"]` 被纯文本框回传成
    字符串 `"RED / BLUE"`,库里那一列从此不再是数组,而下游按数组消费的
    地方开始逐字符遍历它。接口 200,界面显示得也对。
    """
    listy = [n for n, f in REGISTRY.items()
             if f.value_type in (ValueType.ENUM_LIST, ValueType.TEXT_LIST)]
    assert listy, "注册表里一个多值字段都没有,这条断言失去了对象"
    for name in listy:
        assert field_spec_out(name)["multi_value"] is True, name


def test_enum_fields_carry_their_allowed_values():
    """枚举字段要带上取值,否则前端只能给一个自由文本框,让人手打枚举名。"""
    for name, field in REGISTRY.items():
        options = field_spec_out(name)["options"]
        if field.enum is None:
            # 空列表而不是 None:"没有约束"和"约束读不到"在界面上是两种控件
            assert options == [], name
        else:
            assert options == [v.value for v in field.enum], name


def test_free_text_fields_have_no_options():
    """颜色是自由文本(平台词表各不相同)。给它一个下拉框等于凭空发明一个词表。"""
    assert field_spec_out("primary_color")["options"] == []
    assert field_spec_out("secondary_colors")["options"] == []


def test_unregistered_field_degrades_to_plain_text_instead_of_raising():
    """属性行是历史数据,字段可能在注册表里被删过。

    那时候界面该做的事是把值原样显示出来,不是整页读不出来 ——
    所以这里返回一个不带约束的文本规格,而不是抛 KeyError。
    """
    spec = field_spec_out("a_field_that_was_removed_long_ago")
    # `label` 回落成字段名本身:显示 `a_field_that_was_removed_long_ago`
    # 比显示空白强 —— 后者会让人以为这一行坏了
    assert spec == {
        "label": "a_field_that_was_removed_long_ago",
        "value_type": "TEXT",
        "multi_value": False,
        "options": [],
        "option_labels": {},
    }


def test_unknown_is_only_offered_where_the_enum_really_has_it():
    """前端的「未知」按钮按 `options` 决定在不在(BLOCK-11 的另一半)。

    钉住这个前提:枚举字段要么真的有 UNKNOWN 取值,要么前端就不该给按钮。
    这条测试不看前端源码 —— 它保证的是**前端据以判断的那份数据是真的**。
    """
    for name, field in REGISTRY.items():
        if field.enum is None:
            continue
        options = field_spec_out(name)["options"]
        has_unknown = "UNKNOWN" in options
        assert has_unknown == ("UNKNOWN" in [v.value for v in field.enum]), name


# ============================================ 界面用语:英文标识符不许上界面


def test_every_registered_field_has_a_chinese_label():
    """每个字段都要有中文名,而且**不能等于字段名本身**。

    字段名会在四个地方直接显示给运营:属性页表格的第一列、证据抽屉的标题、
    颜色维那格的「缺:…」、批量结果里的字段清单。少一个中文名的表现不是报错,
    是那几处里冒出一个 `waistband_type` —— 运营既读不懂,也没法转述给开发。

    `label` 在 dataclass 上没有默认值,所以"忘了写"会在导入期 TypeError;
    这一条补的是另一半:**写了但抄了字段名**。那种写法能过构造函数,
    而界面上的结果和没写一模一样。
    """
    for name, field in REGISTRY.items():
        assert field.label, f"{name}: 没有中文名"
        assert field.label != name, f"{name}: label 抄了字段名,等于没翻译"
        assert not field.label.isascii(), f"{name}: label 里一个中文都没有"


def test_the_spec_out_carries_the_label_so_the_frontend_need_not_keep_a_table():
    """`label` 必须跟着 `field_spec_out` 出去。

    前端自己维护一张 `primary_color -> 主色` 的表也能显示,而那张表会和
    注册表分叉 —— 分叉的表现是运营在属性页看到「主色」、在颜色维明细里
    看到 `primary_color`,同一个字段两个名字。
    """
    for name, field in REGISTRY.items():
        assert field_spec_out(name)["label"] == field.label, name


def test_every_enum_option_has_a_chinese_interface_label():
    """英文编码可以存库，但运营下拉框必须有完整中文词表。"""
    for name, field in REGISTRY.items():
        if field.enum is None:
            continue
        spec = field_spec_out(name)
        assert set(spec["option_labels"]) == set(spec["options"]), name
        for value, label in spec["option_labels"].items():
            assert label, f"{name}.{value}: 中文文案为空"
            assert not label.isascii(), f"{name}.{value}: 仍显示英文编码"
