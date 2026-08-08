"""事实侧指纹与 `facts_stale` 派生 —— **真库那一半**(A45-batch14-21)。

## 这个文件在本轮一次都没跑过

打包这一批的机器上没有 PostgreSQL、没有 sqlalchemy。下面每一条都是**规格**,
不是成绩。

## 为什么这些不能改写成纯层守卫来凑数

纯层那 22 条钉的是**接线**:指纹取自谁、经不经过 `views()`、措辞落在哪个
问题码上。它们全部是 AST 断言 —— 证明得了"代码写成了那个样子",
证明不了"跑起来是那个结果"。

而 AC-21 的字面场景恰恰要一整条链跑通:

    传一张 A 色样品 -> 素材落库 -> 指纹变 -> 共享事实与 A 色事实判过期
                                          -> **B 色事实不受影响**

这条链跨了三张表、两个作用域、一次真实的唯一索引。把它写成纯层守卫
只能验到"某个函数被调用了",而本批要证明的正是那个函数**算出来的答案**
在真数据上是对的。

一条只验了「源码里出现过 facts_stale」的守卫,会让本批的 32/32 变成
一句谎话。

## 跑法

    docker compose up -d db
    cd backend && pytest tests/test_a45_batch14_21_facts_stale_db.py -v

## 先看这一条

`test_adding_an_asset_for_one_colour_leaves_the_other_colours_facts_alone`
是 **AC-21 本体**,也是最该先跑的。它红了说明双作用域这件事在真数据上
不成立 —— 而纯层那 22 条会全部绿着,因为接线确实接对了。
"""
from __future__ import annotations

import uuid

import pytest

from app.attributes import service as attr_service
from app.attributes import validation
from app.core.enums import (
    AttributeSource,
    AttributeStatus,
    MediaRole,
    MediaStatus,
    OwnerType,
    RoleSource,
)
from app.media import service as media_service
from app.models.media_asset import MediaAsset
from app.models.product import Product
from app.models.spu import ColorVariant, Spu

pytestmark = pytest.mark.requires_db


@pytest.fixture
def spu(session):
    row = Spu(
        spu_code=f"SPU-FS-{uuid.uuid4().hex[:6]}",
        internal_name="指纹用例款",
        audience="WOMEN",
        base_category="swimwear",
        created_by="test",
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def colours(session, spu):
    made = []
    for code, name in (("A", "黑"), ("B", "蓝")):
        row = ColorVariant(
            spu_id=spu.id, variant_code=code, working_name=name, display_name=name
        )
        session.add(row)
        made.append(row)
    session.flush()
    return made


@pytest.fixture
def products(session, spu, colours):
    """一个 SPU 两个颜色,各一件 SKU 行。**AC-21 要两个颜色才成立。**

    单颜色的夹具验不出 D1:那正是这个缺陷躲过 v3.0 的方式 —— 一个卖三个
    颜色的款给其中一个补图,另外两个的事实全部退回待确认,
    而每一条单色用例都是绿的。
    """
    made = []
    for variant in colours:
        row = Product(
            spu=spu.spu_code,
            sku=f"{spu.spu_code}-{variant.variant_code}",
            name=f"指纹用例商品 {variant.working_name}",
            category="swimwear",
            audience="WOMEN",
            spu_id=spu.id,
            color_variant_id=variant.id,
        )
        session.add(row)
        made.append(row)
    session.flush()
    return made


def _asset(session, product, *, colour=None, status=MediaStatus.READY):
    row = MediaAsset(
        product_id=product.id,
        spu_id=product.spu_id,
        spu=product.spu,
        source="MANUAL_UPLOAD",
        storage_path=f"products/{product.id}/{uuid.uuid4().hex[:8]}.jpg",
        mime_type="image/jpeg",
        width=800,
        height=1200,
        bytes=1024,
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        status=status.value,
        role=MediaRole.PRODUCT_FRONT.value,
        role_source=RoleSource.HUMAN.value,
        color_variant_id=colour.id if colour is not None else None,
    )
    session.add(row)
    session.flush()
    return row


def _confirm(session, product, field, value):
    return attr_service.confirm(
        session, product=product, field_name=field, value=value, actor="tester"
    )


# ---------------------------------------------------------------- 列本体


def test_the_column_exists_and_a_confirmed_fact_lands_a_fingerprint(
    session, products, colours
):
    """迁移 0042 那一列在,而且人工确认真的往里写了东西。

    **列在、但写入点漏了**是这一批最可能的失败形状,而它不报错:
    `facts_stale(stored=None)` 恒为 True,于是每个确认过的字段立刻又回到
    待确认。运营点多少次都一样,而库里那一列静静地全是 NULL。
    """
    product = products[0]
    _asset(session, product, colour=colours[0])
    row = _confirm(session, product, "material", "POLYESTER")
    assert row.input_fingerprint, (
        "确认写出来的事实没有指纹 —— 它永远满足不了 §6.3 的完成条件,"
        "而这是一个不报错的死循环"
    )
    assert len(row.input_fingerprint) == 64


def test_a_freshly_confirmed_fact_is_not_stale(session, products, colours):
    """刚确认完的事实不许显示过期。

    写入侧和读取侧要是用了两套证据集合,这一条当场红 —— 而那种漂移在
    别的地方都不报错,表现只是"确认完刷新一下又要确认"。
    """
    product = products[0]
    _asset(session, product, colour=colours[0])
    _confirm(session, product, "material", "POLYESTER")
    values = attr_service.effective_map(session, product.id, product=product)
    assert attr_service.stale_fields(session, product, values) == frozenset()


# ---------------------------------------------------------------- AC-21


def test_adding_an_asset_for_one_colour_leaves_the_other_colours_facts_alone(
    session, products, colours
):
    """**AC-21 本体。** 给颜色 A 补一张样品:

        共享事实   过期
        A 色事实   过期
        B 色事实   **不受影响**

    D1 那个缺陷的字面场景。它不成立时没有任何东西会报错 —— 只是运营
    每次给一个颜色补图,另外两个颜色已确认的事实全部退回待确认,
    而界面上看不出为什么。
    """
    a_product, b_product = products
    a_colour, b_colour = colours
    _asset(session, a_product, colour=a_colour)
    _asset(session, b_product, colour=b_colour)

    # 共享事实(SPU 层)+ 两个颜色各自的颜色事实
    _confirm(session, a_product, "material", "POLYESTER")
    _confirm(session, a_product, "primary_color", "BLACK")
    _confirm(session, b_product, "primary_color", "BLUE")

    b_values = attr_service.effective_map(session, b_product.id, product=b_product)
    assert attr_service.stale_fields(session, b_product, b_values) == frozenset()

    # 给 A 色补一张
    _asset(session, a_product, colour=a_colour)

    a_values = attr_service.effective_map(session, a_product.id, product=a_product)
    a_stale = attr_service.stale_fields(session, a_product, a_values)
    assert "material" in a_stale, "共享事实没有过期 —— 补图改变了共享集合"
    assert "primary_color" in a_stale, "A 色事实没有过期"

    b_values = attr_service.effective_map(session, b_product.id, product=b_product)
    b_stale = attr_service.stale_fields(session, b_product, b_values)
    assert "primary_color" not in b_stale, (
        "**B 色事实跟着过期了 —— 这就是 D1。** 一个卖三个颜色的款,"
        "给其中一个补图会让另外两个已确认的事实全部退回待确认"
    )


def test_a_shared_asset_stales_the_shared_facts_but_no_colour(
    session, products, colours
):
    """通用素材(不属于任何颜色)只动共享作用域。

    让它进**每一个**颜色子集是另一种写法,而那会让"补一张通用图"stale 掉
    全部颜色 —— D1 从后门原样回来。§6.3 也对得上:通用图不参与颜色事实的
    合并,那它就不该决定颜色事实什么时候过期。
    """
    a_product, b_product = products
    a_colour, b_colour = colours
    _asset(session, a_product, colour=a_colour)
    _asset(session, b_product, colour=b_colour)
    _confirm(session, a_product, "primary_color", "BLACK")
    _confirm(session, b_product, "primary_color", "BLUE")
    _confirm(session, a_product, "material", "POLYESTER")

    _asset(session, a_product, colour=None)  # 通用图

    a_values = attr_service.effective_map(session, a_product.id, product=a_product)
    a_stale = attr_service.stale_fields(session, a_product, a_values)
    assert "material" in a_stale, "共享事实没有跟着通用图过期"
    assert "primary_color" not in a_stale, (
        "通用图 stale 掉了颜色事实 —— D1 从后门回来了"
    )


def test_quarantining_an_asset_stales_the_facts_that_leaned_on_it(
    session, products, colours
):
    """隔离一张样品 = 它退出证据集合 = 依赖它的事实过期。

    §8.1 第 2 行那一格。不成立的话,一张已经被判定为不合规的图,
    它支撑起来的结论会继续带着"没变"的证明进导出。
    """
    product = products[0]
    asset = _asset(session, product, colour=colours[0])
    _confirm(session, product, "material", "POLYESTER")

    media_service.quarantine(session, asset.id, reason="水印", actor="tester")

    values = attr_service.effective_map(session, product.id, product=product)
    assert "material" in attr_service.stale_fields(session, product, values)


def test_the_audit_row_names_which_scopes_changed(session, products, colours):
    """审计里写得出"变的是哪几个作用域"。

    **这是 AC-21 在真环境里唯一能被直接观察的地方。** 派生比较证明得了
    事实过期了,证明不了是哪一次动作造成的 —— 而运营的问题恰恰是
    "为什么这堆字段突然要重新确认"。
    """
    from app.models.audit_log import AuditLog  # 延迟导入:纯层不该看见这张表

    a_product = products[0]
    asset = _asset(session, a_product, colour=colours[0])
    _asset(session, products[1], colour=colours[1])
    session.flush()

    media_service.quarantine(session, asset.id, reason="水印", actor="tester")
    session.flush()

    row = (
        session.query(AuditLog)
        .filter(AuditLog.entity_id == asset.id)
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    scopes = row.payload["changed_fingerprint_scopes"]
    assert "SHARED" in scopes, "共享作用域没被记下来"
    assert str(colours[0].id) in scopes, "A 色作用域没被记下来"
    assert str(colours[1].id) not in scopes, (
        "**B 色作用域也被记成变了** —— 审计会把 AC-21 说成不成立"
    )


# ---------------------------------------------------------------- 完成条件


def test_a_stale_required_fact_stops_the_product_from_looking_finished(
    session, products, colours
):
    """§6.3 的完成条件带上了指纹那一句。

    少了它,给一个颜色换掉全部样品之后,那个颜色的事实仍然带着旧结论
    进导出,而工作台显示"已完成"。
    """
    from app.workbench import service as wb

    product = products[0]
    _asset(session, product, colour=colours[0])
    # 属性阶段只有在所有声明颜色都具备必需素材角色时才展示字段级问题。
    _asset(session, products[1], colour=colours[1])
    _confirm(session, product, "material", "POLYESTER")

    before = wb.collect(session, product, dry_run=True)
    _asset(session, product, colour=colours[0])
    after = wb.collect(session, product, dry_run=True)

    assert "material" not in before.flow.attribute.stale_confirmed
    assert "material" in after.flow.attribute.stale_confirmed
    codes = {i.code for i in after.result.issues}
    assert "ATTR_FACT_STALE" in codes
    assert not any(
        issue.code == "ATTR_NOT_CONFIRMED" and issue.ref == "material"
        for issue in after.result.issues
    ), (
        "过期字段被报成「尚无可用值」—— 那句话会把运营送去跑一次付费识别,"
        "而识别产出 CANDIDATE,盖不掉人工值"
    )


def test_a_channel_fact_never_goes_stale_no_matter_how_many_assets_arrive(
    session, products, colours
):
    """CHANNEL 层不参与指纹比较。

    刊登属性来自平台契约,和样品图没有关系。让它参与的表现是一批永远
    消不掉的待确认 —— 而运营对它无能为力:补图只会让它再过期一次。
    """
    product = products[0]
    _asset(session, product, colour=colours[0])
    row = attr_service.set_value(
        session,
        owner_type=OwnerType.CHANNEL,
        owner_id=f"listing-{uuid.uuid4().hex[:8]}",
        field_name="material",
        value="POLYESTER",
        source=AttributeSource.MANUAL,
        status=AttributeStatus.CONFIRMED,
        actor="tester",
    )
    assert row.input_fingerprint is None, "CHANNEL 层不该存指纹"

    _asset(session, product, colour=colours[0])
    scope = validation.fingerprint_scope(OwnerType.CHANNEL, row.owner_id)
    assert scope.participates is False
    assert attr_service.stale_fields(
        session, product, {"material": row}, settled_only=False
    ) == frozenset()
