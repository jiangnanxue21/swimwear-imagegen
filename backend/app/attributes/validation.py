"""属性值校验与 owner 分层(A43 / BLOCK-03)。**纯函数,不碰数据库。**

## 这个模块补的是哪个洞

`schemas/attribute.py` 里那两行:

    class ConfirmItem(BaseModel):
        field_name: str
        value: Any

`Any` 的意思是**后端不看**。而服务层唯一的检查是 `get_field(field_name)` ——
它只证明字段名注册过,没有证明值符合那个字段的定义。于是客户端可以直接提交:

    ENUM_LIST 字段收到一个字符串
    TEXT 字段收到一个对象
    NUMBER 字段收到一个数组
    枚举字段收到一个不存在的取值
    单值字段收到一个几百 KB 的结构化 JSON

前端已经按 `FieldSpecOut` 用了正确的控件,所以正常点界面走不出这些形状。
但那不是防线:接口是公开的,而且**人工确认值被写成 `MANUAL + CONFIRMED`,
从此不被任何自动流程覆盖**。也就是说错误值一旦写进去,反而变成系统里
保护级别最高的那一类数据 —— 自动识别修不了它,只能再来一次人工确认。

## 为什么校验必须在后端,而且必须在**唯一写入点**上

`attributes/service.set_value()` 自称"唯一允许写 `product_attribute_values`
的入口"。既然如此,校验就该长在它身上,而不是长在 API 层:识别合并
(`apply_evidence`)也走这个函数,而模型输出恰恰是最不该被信任的那一路。
放在 API 层等于只拦人工那一条路,把机器那条路放过去。

## owner 分层

注册表里 `primary_color` / `secondary_colors` 声明的是 `OwnerType.VARIANT`,
其余是 `SPU`。但抽取和人工确认原来一律写成:

    owner_type=OwnerType.SPU, owner_id=str(product.id)

两个字段都不对:类型不对(变体属性写成了 SPU 层),id 也不对
(`product.id` 是一个 **SKU 行**的主键,不是 SPU 的标识)。合起来的实际语义是
"每个 Product UUID 一桶属性",而分层只存在于注册表和契约里。

`owner_for()` 让这件事只有一份定义:**owner_type 一律取注册表,不由调用方指定。**
调用方只提供三个坐标(spu / variant_id / sku),给哪一个由字段自己决定。

## 兼容:为什么保留 `legacy_owner_id`

存量数据全部写在 `(SPU, product.id)` 上。直接改口径会让所有已确认的属性
在读取时"消失" —— 那比不分层更糟。所以读取侧保留一条回落链
(见 `service.effective_map`),而这个模块只负责回答"新写入该落在哪"。
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from app.attributes.registry import REGISTRY, AttributeField, ValueType
from app.core.enums import OwnerType

#: 单个文本值的最大长度。与 `products.material` 的 VARCHAR(128) 同宽 ——
#: 投影列写不下的值,事实源也不该收
MAX_TEXT_LENGTH = 128

#: 列表类字段最多几项。取值与 `MAX_SECONDARY_COLORS` 一致:
#: 那是全仓唯一一个真实的多值字段,没有理由让别的列表字段更宽松
MAX_LIST_ITEMS = 10

#: 数值字段的合法区间。泳装类目今天没有 NUMBER 字段(克重要等材质那一组),
#: 但区间要先在这里立着 —— 加字段的人不该顺手决定"多少算合理"
NUMBER_MIN = -1_000_000.0
NUMBER_MAX = 1_000_000.0


class AttributeValueError(ValueError):
    """一个属性值不符合它的字段定义。

    带 `field_name`,因为接口层要把它翻译成 422 并指名是哪一项 ——
    一次确认可以提交多个字段,只说"格式不对"等于让人挨个试。
    """

    def __init__(self, field_name: str, reason: str) -> None:
        self.field_name = field_name
        self.reason = reason
        super().__init__(f"{field_name}: {reason}")


def validate_attribute_value(field_name: str, value: Any) -> Any:
    """校验并归一一个属性值。**返回可以直接落库的那个值。**

    返回归一后的值而不是只返回真假:归一("red" -> "RED"、去空白、去重)
    和校验是同一件事的两面,分开做的话总有一条路径只做了其中一半。

    `None` 一律放行,表示"这个字段目前没有值"。清空一个字段是一个合法动作,
    而把它伪装成空字符串会让 `TEXT` 和 `TEXT_LIST` 的空值长得不一样。
    """
    spec = REGISTRY.get(field_name)
    if spec is None:
        raise AttributeValueError(field_name, "未注册的属性字段")
    if value is None:
        return None

    handler = {
        ValueType.ENUM: _validate_enum,
        ValueType.ENUM_LIST: _validate_enum_list,
        ValueType.TEXT: _validate_text,
        ValueType.TEXT_LIST: _validate_text_list,
        ValueType.NUMBER: _validate_number,
        ValueType.BOOL: _validate_bool,
    }[spec.value_type]
    return handler(spec, value)


#: 变体 owner_id 的命名空间分隔符。见 `variant_owner_id()`。
_VARIANT_NS = "/"


def variant_owner_id(*, spu: str, variant_id: str) -> str:
    """VARIANT 层的 owner_id。**必须带 SPU 命名空间**(A44)。

    ## 不带命名空间时发生了什么

    `product_attribute_values` 的唯一索引是
    `(owner_type, owner_id, field_name) WHERE is_current` —— **没有 SPU**。

    A43 把 VARIANT 层的 owner_id 直接写成变体 id(取值是颜色名),于是
    全库只要有两个 SPU 都有\"黑色\"这个颜色,它们就共用同一行属性:

        给 SPU-SW-001 的黑色确认一次 `primary_color`
        → SPU-SW-002 的黑色跟着变,没有任何提示

    比\"属性不见了\"更糟:值还在,只是属于别人。而且它在单 SPU 的测试里
    永远复现不出来 —— 要两个 SPU 同色才会出现,而样例数据每个 SPU 一个颜色。

    ## 为什么带长度前缀

    直接拼 `spu + "/" + variant_id` 有一个真实的歧义:

        spu="A"    variant="B/C"   →  "A/B/C"
        spu="A/B"  variant="C"     →  "A/B/C"

    SPU 是自由文本(`schemas/product.py` 只校验长度),含斜杠不违法。
    两个不同的变体拼出同一个 owner_id,后果和上面那条一模一样。

    前缀写 SPU 的字符数,解析时按它精确切分,歧义在结构上不可能存在。
    多花 2~4 个字符换掉一整类静默串档,值。

        11:SPU-SW-001/black
    """
    spu = (spu or "").strip()
    variant_id = (variant_id or "").strip()
    return f"{len(spu)}:{spu}{_VARIANT_NS}{variant_id}"


def split_variant_owner_id(owner_id: str) -> tuple[str, str] | None:
    """把 owner_id 拆回 (spu, variant_id)。不是这个格式时返回 None。

    给巡检用:`orphaned_variant_owners()` 要回答\"这一行属性属于哪个 SPU\",
    而 A44 之前写下的行是裸变体 id,拆不出来 —— 返回 None 正是那个信号。
    """
    head, sep, rest = owner_id.partition(":")
    if not sep or not head.isdigit():
        return None
    length = int(head)
    spu, ns, variant_id = rest[:length], rest[length : length + 1], rest[length + 1 :]
    if len(spu) != length or ns != _VARIANT_NS:
        return None
    return spu, variant_id


def owner_for(
    field_name: str, *, spu: str, variant_id: str, sku: str
) -> tuple[OwnerType, str]:
    """这个字段的值该挂在哪一层、哪一个 id 上。

    **owner_type 只从注册表取。** 让调用方传进来,等于把"颜色属于变体"
    这句话复制到每一个调用点,而那正是它今天没有生效的原因。

    三个坐标都要求非空:回落到空字符串会让不同商品的属性挤进同一个 owner,
    而那种数据损坏在读取时看起来完全正常。

    VARIANT 层额外套一层 SPU 命名空间,理由见 `variant_owner_id()` ——
    简而言之:那张表的唯一索引里没有 SPU,不套的话同名颜色跨 SPU 串档。
    """
    spec = REGISTRY.get(field_name)
    if spec is None:
        raise AttributeValueError(field_name, "未注册的属性字段")

    mapping: dict[OwnerType, str] = {
        OwnerType.SPU: spu,
        OwnerType.VARIANT: variant_id,
        OwnerType.SKU: sku,
        # PRODUCT 是历史口径(一行商品一桶属性)。注册表里今天没有字段用它,
        # 留着是为了让存量数据读得出来 —— 落在 sku 上语义最接近
        OwnerType.PRODUCT: sku,
    }
    owner_id = (mapping.get(spec.owner_type) or "").strip()
    if not owner_id:
        raise AttributeValueError(
            field_name,
            f"{spec.owner_type.value} 层的 owner id 为空,无法确定这个值属于谁",
        )
    # 命名空间在**校验非空之后**套 —— 反过来的话空变体 id 会被
    # `"3:SPU/"` 这种\"看起来非空\"的字符串蒙混过去
    if spec.owner_type is OwnerType.VARIANT:
        if not (spu or "").strip():
            raise AttributeValueError(field_name, "变体属性缺少 SPU,无法确定它属于谁")
        owner_id = variant_owner_id(spu=spu, variant_id=owner_id)
    return spec.owner_type, owner_id


def owner_types_in_registry() -> tuple[OwnerType, ...]:
    """注册表实际用到的分层。给测试与文档用,免得再数一遍字段表。"""
    seen: dict[OwnerType, None] = {}
    for spec in REGISTRY.values():
        seen.setdefault(spec.owner_type, None)
    return tuple(seen)


# ---------------------------------------------------------------- 各类型


def _reject_containers(spec: AttributeField, value: Any) -> None:
    """单值字段收到容器时的统一拒绝。

    `dict` 一律拒绝,**不留例外**:注册表今天没有任何字段声明结构化 schema,
    而一个没有 schema 的对象进了 `value_json`,下游只能靠猜。
    要支持结构化字段,先在注册表里声明它的形状,再回来改这里。
    """
    if isinstance(value, dict):
        raise AttributeValueError(
            spec.name, "不接受对象;注册表没有为这个字段声明结构化 schema"
        )
    if isinstance(value, (list, tuple, set)):
        raise AttributeValueError(
            spec.name, f"{spec.value_type.value} 是单值字段,不接受列表"
        )


def _validate_enum(spec: AttributeField, value: Any) -> str:
    _reject_containers(spec, value)
    if isinstance(value, bool) or not isinstance(value, str):
        raise AttributeValueError(spec.name, "枚举字段只接受字符串")
    text = value.strip()
    if not text:
        raise AttributeValueError(spec.name, "枚举字段不接受空字符串;要清空请传 null")
    allowed = {v.value.upper(): v.value for v in spec.enum}  # type: ignore[union-attr]
    canonical = allowed.get(text.upper())
    if canonical is None:
        raise AttributeValueError(
            spec.name,
            f"不是合法取值:{text!r};允许的是 {sorted(allowed.values())}",
        )
    return canonical


def _validate_enum_list(spec: AttributeField, value: Any) -> list[str]:
    items = _as_list(spec, value)
    allowed = {v.value.upper(): v.value for v in spec.enum}  # type: ignore[union-attr]
    out: list[str] = []
    for index, raw in enumerate(items):
        if isinstance(raw, bool) or not isinstance(raw, str):
            raise AttributeValueError(spec.name, f"第 {index + 1} 项不是字符串")
        canonical = allowed.get(raw.strip().upper())
        if canonical is None:
            raise AttributeValueError(
                spec.name, f"第 {index + 1} 项不是合法取值:{raw!r}"
            )
        # 去重保序:同一个值提交两次不是错误,但存两份会让下游的
        # "有几个副色"变成一个取决于提交次数的数
        if canonical not in out:
            out.append(canonical)
    return out


def _validate_text(spec: AttributeField, value: Any) -> str:
    _reject_containers(spec, value)
    if isinstance(value, bool) or not isinstance(value, str):
        raise AttributeValueError(spec.name, "文本字段只接受字符串")
    text = value.strip()
    if not text:
        raise AttributeValueError(spec.name, "文本字段不接受空字符串;要清空请传 null")
    if len(text) > MAX_TEXT_LENGTH:
        raise AttributeValueError(
            spec.name, f"超过 {MAX_TEXT_LENGTH} 字符(实际 {len(text)})"
        )
    return text


def _validate_text_list(spec: AttributeField, value: Any) -> list[str]:
    items = _as_list(spec, value)
    out: list[str] = []
    for index, raw in enumerate(items):
        if isinstance(raw, bool) or not isinstance(raw, str):
            raise AttributeValueError(spec.name, f"第 {index + 1} 项不是字符串")
        text = raw.strip()
        if not text:
            # 静默丢弃而不是报错:界面上多按一次回车会留一个空项,
            # 那不是一次需要人来处理的错误
            continue
        if len(text) > MAX_TEXT_LENGTH:
            raise AttributeValueError(
                spec.name, f"第 {index + 1} 项超过 {MAX_TEXT_LENGTH} 字符"
            )
        if text not in out:
            out.append(text)
    return out


def _validate_number(spec: AttributeField, value: Any) -> float | int:
    _reject_containers(spec, value)
    # `bool` 是 `int` 的子类。不单独挡的话 True 会变成 1 悄悄存进去
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AttributeValueError(spec.name, "数值字段只接受数字")
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise AttributeValueError(spec.name, "数值必须是有限值")
    if not NUMBER_MIN <= value <= NUMBER_MAX:
        raise AttributeValueError(
            spec.name, f"超出允许区间 [{NUMBER_MIN}, {NUMBER_MAX}]"
        )
    return value


def _validate_bool(spec: AttributeField, value: Any) -> bool:
    _reject_containers(spec, value)
    # 只收真正的布尔值。收下 "true" / 1 会让"这个字段到底存的是什么类型"
    # 变成一个要看提交方是谁才能回答的问题
    if not isinstance(value, bool):
        raise AttributeValueError(spec.name, "布尔字段只接受 true / false")
    return value


def _as_list(spec: AttributeField, value: Any) -> Sequence[Any]:
    if isinstance(value, dict):
        raise AttributeValueError(spec.name, "多值字段不接受对象")
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        # **这一条是 BLOCK-11 那个真实事故的防线。**
        # `["RED","BLUE"]` 在界面上显示成 "RED / BLUE",原样提交回来就是
        # 一个字符串;收下它,库里那一列从此不再是数组,而下游按列表消费的
        # 地方(渠道层颜色映射)会拿到一个字符 'R'、一个字符 'E'……
        raise AttributeValueError(
            spec.name, f"{spec.value_type.value} 是多值字段,必须提交数组"
        )
    if len(value) > MAX_LIST_ITEMS:
        raise AttributeValueError(
            spec.name, f"最多 {MAX_LIST_ITEMS} 项(实际 {len(value)})"
        )
    return value
