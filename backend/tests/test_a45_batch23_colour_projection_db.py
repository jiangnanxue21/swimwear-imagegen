"""A45-batch23(阶段 5 批次 5-4):颜色投影的真库用例。

## 为什么非要真库

纯层验的是"给一个值,该写什么"。而这笔账的形状是一句**关于库里那一列**
的话:`ColorVariant.display_name` 后端 5 处读、前端 7 处显示,而**恒为空串**。

证明它不再恒空,只能去库里看。而且中间隔着三样纯层看不见的东西:

    `owner_for()` 算出来的 owner_id 到底是不是 `color_variants.id`
    `session.get(ColorVariant, ...)` 拿那个 owner_id 当主键查得到查不到
    投影跟不跟得上 `confirm` 的事务(分两步的话中间那个窗口显示旧名字)

第一条尤其要真库:纯层可以假设 owner_id 是 UUID,而真库会告诉你
`validation.owner_for()` 对没回填 `color_variant_id` 的商品行**当场抛错**。
"""
from __future__ import annotations

import uuid

import pytest

from app.attributes import colour_projection as cp
from app.attributes import service as attr_service
from app.attributes.validation import AttributeValueError
from app.core.enums import SellableStatus, SpuStatus
from app.models.product import Product
from app.models.spu import ColorVariant, Spu
from tests.conftest import requires_db

pytestmark = requires_db


def _spu(session) -> Spu:
    row = Spu(
        spu_code=f"CV{uuid.uuid4().hex[:6].upper()}",
        internal_name="颜色投影测试款",
        audience="WOMEN",
        base_category="swimwear",
        status=SpuStatus.DRAFT.value,
    )
    session.add(row)
    session.flush()
    return row


def _colour(session, spu: Spu, code: str) -> ColorVariant:
    row = ColorVariant(
        spu_id=spu.id,
        variant_code=code,
        working_name=f"供应商说的那个{code}",
        sellable_status=SellableStatus.ACTIVE.value,
    )
    session.add(row)
    session.flush()
    return row


def _product(session, spu: Spu, colour: ColorVariant, size: str = "S") -> Product:
    row = Product(
        spu=spu.spu_code,
        spu_id=spu.id,
        color_variant_id=colour.id,
        sku=f"{spu.spu_code}-{colour.variant_code}-{size}",
        name="测试款",
        category="swimwear",
        audience="WOMEN",
        size=size,
    )
    session.add(row)
    session.flush()
    return row


def _one(session):
    spu = _spu(session)
    colour = _colour(session, spu, "BLK")
    return spu, colour, _product(session, spu, colour)


# ---------------------------------------------------------------- 一、这笔账


def test_confirming_the_colour_fact_fills_the_projection_column(session):
    """**本文件的第一条,也是这笔账的全部内容。**

    `display_name` 从落列那天起恒为空串:后端 5 处读、前端 7 处显示。
    确认一次颜色事实之后,它应当有值。
    """
    spu, colour, product = _one(session)
    assert colour.display_name == "", "夹具的前提变了(这一列本该是空的)"

    attr_service.confirm(
        session,
        product=product,
        field_name=cp.SOURCE_FIELD,
        value="Midnight Black",
        actor="batch23",
    )
    session.flush()
    session.refresh(colour)

    assert colour.display_name == "Midnight Black", (
        "确认了颜色事实,而 `display_name` 还是空的 —— 投影没有接上"
    )


def test_the_projection_lands_in_the_same_transaction(session):
    """投影跟着 `confirm` 的事务走,不是稍后同步。

    分两步的话,中间那个窗口里界面显示的是旧名字,而没有任何东西说明为什么。
    这里的判据是:`confirm` 返回之后**还没有额外提交**,那一列已经是新值。
    """
    spu, colour, product = _one(session)
    attr_service.confirm(
        session,
        product=product,
        field_name=cp.SOURCE_FIELD,
        value="Ivory",
        actor="batch23",
    )
    # 刻意不 flush、不 commit —— confirm 内部已经把它写进这个事务了
    assert colour.display_name == "Ivory"


def test_a_second_colour_of_the_same_spu_is_untouched(session):
    """确认 A 色只写 A 色那一行。

    owner_id 算错(比如回落到 SPU 码)的表现正是这个 SPU 下**每个颜色**
    都被写成同一个名字,而界面上看起来"都有名字了",没有任何东西报错。
    """
    spu = _spu(session)
    black = _colour(session, spu, "BLK")
    white = _colour(session, spu, "WHT")
    black_product = _product(session, spu, black)

    attr_service.confirm(
        session,
        product=black_product,
        field_name=cp.SOURCE_FIELD,
        value="Jet Black",
        actor="batch23",
    )
    session.flush()
    session.refresh(black)
    session.refresh(white)

    assert black.display_name == "Jet Black"
    assert white.display_name == "", (
        "确认一个颜色把同 SPU 的另一个颜色也写了 —— owner_id 算错了"
    )


# ---------------------------------------------------------------- 二、不该写的时候不写


def test_confirming_another_field_leaves_the_colour_name_alone(session):
    """确认别的字段不碰这一列。

    按"字段名里有 color"模糊匹配的话,`secondary_colors`(一个列表)
    会被投影成一串逗号显示在颜色名的位置上。
    """
    spu, colour, product = _one(session)
    attr_service.confirm(
        session,
        product=product,
        field_name="secondary_colors",
        value=["Gold", "Silver"],
        actor="batch23",
    )
    session.flush()
    session.refresh(colour)
    assert colour.display_name == "", (
        f"确认 secondary_colors 写了颜色名:{colour.display_name!r}"
    )


def test_an_empty_colour_value_is_refused_before_it_reaches_the_projection(session):
    """空值**在校验层就被拒**,根本到不了投影。

    本文件第一版断言的是"投影没有擦掉旧名字",而那条断言其实验不到投影 ——
    `confirm()` 在更前面就抛了 `AttributeValueError`。记在这里是因为
    两种写法看起来一样对,而它们说的不是同一件事:

        校验层负责   空字符串不是一个合法的颜色事实
        投影层负责   拿到一个不提供颜色名的值时**不写**(而不是写空串)

    第二半由纯层的 `test_a_blank_fact_does_not_wipe_the_displayed_name` 穷举。
    在这里再验一次只会让人以为投影挡住了它。
    """
    spu, colour, product = _one(session)
    attr_service.confirm(
        session,
        product=product,
        field_name=cp.SOURCE_FIELD,
        value="Coral",
        actor="batch23",
    )
    session.flush()

    with pytest.raises(AttributeValueError):
        attr_service.confirm(
            session,
            product=product,
            field_name=cp.SOURCE_FIELD,
            value="   ",
            actor="batch23",
        )
    session.refresh(colour)
    assert colour.display_name == "Coral", "旧颜色名被一次失败的确认动过了"


def test_reconfirming_the_same_value_does_not_touch_the_row(session):
    """同值重复确认不写库 —— 否则 `updated_at` 每次都动,审计里看不出改过什么。"""
    spu, colour, product = _one(session)
    attr_service.confirm(
        session,
        product=product,
        field_name=cp.SOURCE_FIELD,
        value="Coral",
        actor="batch23",
    )
    session.flush()
    session.refresh(colour)
    first_touch = colour.updated_at

    attr_service.confirm(
        session,
        product=product,
        field_name=cp.SOURCE_FIELD,
        value="Coral",
        actor="batch23",
    )
    session.flush()
    session.refresh(colour)
    assert colour.updated_at == first_touch, (
        "同值重复确认仍然写了库 —— `updated_at` 会被无意义地推进"
    )


def test_an_over_long_name_is_refused_by_the_validator_not_truncated(session):
    """超长的值**在校验层就被拒**,不会以截断的形式落进这一列。

    这一条与 `colour_projection.MAX_LENGTH` 那条截断分支是一对:
    `validation.MAX_TEXT_LENGTH` 与它同为 128,所以投影的截断今天走不到。
    留着截断是为了守「两个数必须相等」那条不变量(纯层钉着),
    而**真实行为是拒绝** —— 这里验的是后者。

    合起来的意义:界面上显示的颜色名与事实值永远是同一个字符串。
    """
    spu, colour, product = _one(session)
    with pytest.raises(AttributeValueError):
        attr_service.confirm(
            session,
            product=product,
            field_name=cp.SOURCE_FIELD,
            value="x" * 400,
            actor="batch23",
        )
    session.refresh(colour)
    assert colour.display_name == "", "被拒的确认仍然写了投影列"


# ---------------------------------------------------------------- 三、来源追溯


def test_the_fact_row_still_carries_its_provenance(session):
    """投影**不取代**事实源:那条属性值仍然带着来源、确认人与指纹。

    这一条守的是「来源追溯」那半项交付。投影是反规范化副本,
    如果有人为了省事把颜色名只写进 `display_name`、不写属性值,
    「这个颜色名是谁定的、依据是什么」就永远答不出来了 ——
    而界面上一切正常。
    """
    spu, colour, product = _one(session)
    row = attr_service.confirm(
        session,
        product=product,
        field_name=cp.SOURCE_FIELD,
        value="Coral",
        actor="batch23",
    )
    session.flush()

    assert row.source == "MANUAL"
    assert row.status == "CONFIRMED"
    assert row.owner_id == str(colour.id), (
        "事实挂在一个不是 color_variants.id 的 owner 上 —— "
        "投影查不到那一行,而属性写入看起来成功了"
    )
    assert row.confirmed_by == "batch23", "确认人没落库 —— 来源追溯断在这里"
