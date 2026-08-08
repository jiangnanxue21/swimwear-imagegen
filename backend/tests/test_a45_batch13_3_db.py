"""A45-batch13-3:走读修复的真库断言。

纯守卫(`tests/pure/test_a45_batch13_3_fixes.py`)钉的是形状:那个 kwarg 在不在、
那道 If 长什么样。这一批钉行为,而且每一条都对着一个 AST **够不着**的东西:

    R1  `passive_deletes="all"` 之后,ORM 删颜色真的撞 RESTRICT 吗?
        撞完之后九行 SKU 的外键**原样还在**吗?(旧行为是先置 NULL ——
        IntegrityError 抛不抛只是半个问题,另外半个是子行有没有被动过)
    R2  改一行 SKU 的受众,`spus.audience` 权威真的跟着动吗?
        SPU 管理的行清空受众真的在入口被拒吗?老路径真的没被误伤吗?
    R3  `info={"identity": True}` 真的让 `update_product` 拒改外键吗?
    R5  不填 working_name 的颜色,目录里显示的真是编码不是 UUID 吗?
    R6  255 字符的 internal_name 真的建得进去吗?(旧代码在 flush 抛
        DataError —— 那个类型 except IntegrityError 接不住)

## 这些用例一次都没跑过

和 12-4 / 12-5 / 12-7 / 13 的那些同一个池子:**已写、未执行**(整改环境无
PostgreSQL)。跑起来需要 `make up` 之后
`pytest tests/test_a45_batch13_3_db.py`,或 CI 的 backend job。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.enums import Audience
from app.core.errors import ValidationError
from app.models.product import Product
from app.models.spu import ColorVariant, Spu
from app.services import product_service, spu_service
from tests.conftest import requires_db

pytestmark = requires_db


def _payload(**overrides) -> dict:
    payload = {
        "spu_code": "SPU-SW-021",
        "internal_name": "三色平角泳裤",
        "audience": Audience.WOMEN.value,
        "base_category": "swimwear",
        "supplier_ref": None,
        "notes": None,
        "size_template": "ALPHA_3",
        "color_variants": [
            {"variant_code": "BLK", "working_name": "供应商说的那个黑"},
            {"variant_code": "NVY", "working_name": "藏青"},
            {"variant_code": "COR", "working_name": "珊瑚"},
        ],
    }
    payload.update(overrides)
    return payload


# ================================================================ R1 删颜色


def test_deleting_a_colour_is_refused_and_the_foreign_keys_survive(session):
    """RESTRICT 必须在 **ORM 路径**上生效,而且子行一个字节都不许被动。

    `passive_deletes="all"` 之前的行为:flush 先发
    `UPDATE products SET color_variant_id = NULL`,再删父行 —— 列可空,
    两步都成功,"删颜色"拿到 200,九行 SKU 悄悄孤儿化。所以这条断言
    分两半:IntegrityError 要抛,**并且**回滚后每一行外键都原样还在 ——
    只断言前一半的话,"先置 NULL、删父行时撞别的约束"也能绿。
    """
    from sqlalchemy.exc import IntegrityError

    spu = spu_service.create_spu(session, _payload(), actor="tester")
    session.commit()

    variant = session.scalars(
        select(ColorVariant).where(ColorVariant.spu_id == spu.id)
    ).first()
    session.delete(variant)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()

    rows = session.scalars(select(Product).where(Product.spu_id == spu.id)).all()
    assert len(rows) == 9
    assert all(row.color_variant_id is not None for row in rows), (
        "有 SKU 行的外键被置空了 —— ORM 在 RESTRICT 响之前动了子行"
    )
    assert (
        session.scalar(
            select(ColorVariant.id).where(ColorVariant.id == variant.id)
        )
        is not None
    ), "颜色行被删掉了 —— RESTRICT 没拦住"


# ================================================================ R2 受众权威


def test_changing_the_audience_on_one_sku_moves_the_spu_authority(session):
    """副本怎么改,权威就怎么改,同一个事务。

    不同步的话:SPU 详情页说 WOMEN、九行 SKU 说 MEN,没有任何诊断;
    阶段 1 第二批把读取方切到权威列的那一天,这次修改被静默撤销。
    """
    spu = spu_service.create_spu(session, _payload(spu_code="SPU-SW-022"), actor="t")
    session.commit()

    row = session.scalars(select(Product).where(Product.spu_id == spu.id)).first()
    product_service.update_product(
        session, row.id, {"audience": Audience.MEN}, actor="tester"
    )
    session.commit()

    session.expire_all()
    assert session.get(Spu, spu.id).audience == Audience.MEN.value, (
        "spus.audience 权威没有跟着改 —— §4.2 的唯一权威被标准操作流分叉了"
    )
    audiences = {
        p.audience
        for p in session.scalars(select(Product).where(Product.spu_id == spu.id))
    }
    assert audiences == {Audience.MEN.value}, f"九行副本没有全部跟上:{audiences}"


def test_clearing_the_audience_on_a_spu_managed_row_is_refused(session):
    """SPU 层没有"待确认受众"(§4.2,权威列 NOT NULL)。

    放行的两种结局都是错:不同步 —— 权威 WOMEN、副本 NULL,静默分叉;
    同步 —— flush 撞 NOT NULL,500。所以在入口拒,而且拒完什么都不许变。
    """
    spu = spu_service.create_spu(session, _payload(spu_code="SPU-SW-023"), actor="t")
    session.commit()

    row = session.scalars(select(Product).where(Product.spu_id == spu.id)).first()
    with pytest.raises(ValidationError):
        product_service.update_product(session, row.id, {"audience": None}, actor="t")
    session.rollback()

    session.expire_all()
    assert session.get(Spu, spu.id).audience == Audience.WOMEN.value
    assert session.get(Product, row.id).audience == Audience.WOMEN.value


def test_the_legacy_path_can_still_clear_its_audience(session):
    """负向对照:老路径(没有 spu_id 的行)的 NULL 仍然是合法的"待确认受众"。

    那道闸只该拦 SPU 管理的行 —— 拦宽了,导入的存量商品从此改不回
    "待确认",而 §3.4 规则 3 靠的就是这个状态。
    """
    # 新的公开建档入口已经要求先有 SPU；这里直接摆一条历史遗留行，
    # 验证的仍是 update 对真实存量 ``spu_id IS NULL`` 的兼容。
    product = Product(
        spu="LEGACY-SW-001",
        sku="LEGACY-SW-001-BLK-S",
        name="老路径行",
        category="swimwear",
        audience=Audience.WOMEN.value,
    )
    session.add(product)
    session.commit()

    product_service.update_product(session, product.id, {"audience": None}, actor="t")
    session.commit()
    session.expire_all()
    assert session.get(Product, product.id).audience is None


# ================================================================ R3 身份禁改


def test_identity_foreign_keys_reject_edits_at_the_service_gate(session):
    """两个外键在 `update_product` 上必须和 `variant_key` 同罪。

    schema 今天不含这两个字段,但那是**另一层** —— 这条测的是模型标记
    那层真的生效:任何直接调服务的路径(脚本、将来的批量接口)都会被拦。
    """
    from uuid import uuid4

    spu = spu_service.create_spu(session, _payload(spu_code="SPU-SW-024"), actor="t")
    session.commit()
    row = session.scalars(select(Product).where(Product.spu_id == spu.id)).first()

    for column in ("spu_id", "color_variant_id"):
        with pytest.raises(ValidationError):
            product_service.update_product(
                session, row.id, {column: uuid4()}, actor="tester"
            )
        session.rollback()


# ================================================================ R5 目录名


def test_an_empty_working_name_shows_the_variant_code_not_a_uuid(session):
    """只填编码建档(working_name 走 schema 默认空串),目录显示编码。

    13-2 的用例每次都填了名字,恰好没盖住这条 —— 而 schema 上它就是可选的。
    """
    from app.listings import variants as variant_service

    spu = spu_service.create_spu(
        session,
        _payload(
            spu_code="SPU-SW-025",
            color_variants=[
                {"variant_code": "BLK"},
                {"variant_code": "NVY"},
                {"variant_code": "COR"},
            ],
        ),
        actor="tester",
    )
    session.commit()

    labels = {
        ref.label for ref in variant_service.variant_directory(session, spu.spu_code)
    }
    assert labels == {"BLK", "NVY", "COR"}, (
        f"目录显示的不是编码:{labels} —— 多半又掉回身份(UUID)了"
    )


# ================================================================ R6 name 列宽


def test_a_maximal_internal_name_still_archives(session):
    """internal_name 顶格(schema 上限 255)也建得进去。

    旧代码拼出来 281 字符,flush 抛 DataError —— 不是 IntegrityError,
    except 接不住,一路裸 500。修完之后:建档成功,name 截到列宽。
    """
    spu = spu_service.create_spu(
        session,
        _payload(spu_code="SPU-SW-026", internal_name="长" * 255),
        actor="tester",
    )
    session.commit()

    rows = session.scalars(select(Product).where(Product.spu_id == spu.id)).all()
    assert len(rows) == 9
    assert all(len(row.name) <= 255 for row in rows)
    # 截的是派生显示值,身份一个字符都不许动
    assert all(row.sku.startswith("SPU-SW-026-") for row in rows)
