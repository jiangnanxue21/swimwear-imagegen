"""A45-batch13:建档链路的真库断言(阶段 1 第一批,PRD v3.1 §13)。

`tests/pure/test_a45_batch13_stage1_identity.py` 钉的是**结构**:那条 flush 在不在、
受众列可不可空、SKU 按哪一列查、投影列有没有被写。这一批钉的是**行为**,而且
每一条都对着一个纯测试**够不着**的东西:

    外键真的落进去了吗        纯测试只能看到源码里写了 `spu_id=spu.id`,
                              看不到 `spu.id` 在那一刻是不是 None
    约束真的拦得住吗          `uq_spus_spu_code` / `uq_products_variant_size` /
                              两条 RESTRICT 外键,在库里才有意义
    失败之后有没有残骸        "一个事务"这句话只有回滚过才算数

## 这些用例一次都没跑过

和 12-4 的 6 条、12-5 的 7 条、12-7 的 11 条同一个池子:**已写、未执行**
(整改环境无 PostgreSQL)。写出来只是把阶段 1 的第一条验收从「欠用例」
推进到「欠执行」,不要读成「建档跑通了」。

batch13-2 在末尾加了 6 条(本文件共 18 条),全部站在**读取侧**:同一个 SPU,
属性层 / 图片集 / 列表页各自看见几个变体、显示什么名字、报不报诊断。
加它们的直接原因是 F1 —— 建档侧 12 条用例全绿,而九行 SKU 在读取方眼里
是九个颜色,一条用例都没问过读取方。

跑起来需要:`make up` 之后 `pytest tests/test_a45_batch13_spu_db.py`,
或在 CI 的 backend job 里(那里有 PostgreSQL + Redis)。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.enums import Audience, SellableStatus, SpuStatus
from app.core.errors import DuplicateError, ValidationError
from app.models.product import Product
from app.models.spu import ColorVariant, Spu
from app.services import spu_service
from tests.conftest import requires_db

pytestmark = requires_db


def _payload(**overrides) -> dict:
    payload = {
        "spu_code": "SPU-SW-011",
        "internal_name": "三色高腰连体",
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


# ================================================================ 一、验收本身


def test_three_colors_and_three_sizes_land_nine_rows(session):
    """§13 阶段 1 第一条验收:可构造三颜色九 SKU 的 SPU。"""
    spu = spu_service.create_spu(session, _payload(), actor="tester")
    session.commit()

    assert spu.status == SpuStatus.DRAFT.value
    variants = session.scalars(
        select(ColorVariant).where(ColorVariant.spu_id == spu.id)
    ).all()
    assert len(variants) == 3
    assert {v.sellable_status for v in variants} == {SellableStatus.PLANNED.value}

    skus = spu_service.skus_of(session, spu.id)
    assert len(skus) == 9
    assert {s.sku for s in skus} == {
        f"SPU-SW-011-{color}-{size}"
        for color in ("BLK", "NVY", "COR")
        for size in ("S", "M", "L")
    }


def test_every_row_carries_both_foreign_keys(session):
    """这一条是纯测试够不着的那一半。

    主键是 `default=uuid.uuid4`,而 Python 侧 default 在 flush 时才求值 ——
    少了中间那次 flush,九行 SKU 的 `spu_id` 会**整批为空**,而这一版那列可空,
    数据库不拦、列表页照常显示。源码断言看不到这件事,这里看得到。
    """
    spu = spu_service.create_spu(session, _payload(), actor="tester")
    session.commit()

    rows = session.scalars(select(Product).where(Product.spu == "SPU-SW-011")).all()
    assert len(rows) == 9
    assert all(row.spu_id == spu.id for row in rows), "有行没挂上 SPU"
    assert all(row.color_variant_id is not None for row in rows), "有行没挂上颜色"

    by_variant: dict[str, set[str]] = {}
    for row in rows:
        by_variant.setdefault(str(row.color_variant_id), set()).add(row.size)
    assert len(by_variant) == 3, "三个颜色应该各自有一组行"
    assert all(sizes == {"S", "M", "L"} for sizes in by_variant.values())


def test_no_visual_attribute_is_required_to_archive(session):
    """§13 阶段 1 验收:不填视觉属性即可建档。

    落下来的行在那 8 个投影列上是**列默认值**,不是"识别过的空值" ——
    区别体现在属性层:建档不产生任何 `product_attribute_values` 行。
    """
    spu_service.create_spu(session, _payload(), actor="tester")
    session.commit()

    row = session.scalars(select(Product).where(Product.spu == "SPU-SW-011")).first()
    assert row is not None
    assert row.primary_color == ""
    assert row.material == ""


def test_the_audience_is_copied_down_from_the_spu(session):
    """受众的权威在 SPU;SKU 行上那一列是副本,而且抄的动作只有一处。

    于是"同 SPU 各行受众必须一致"在新路径上是结构性成立的 —— 那道一致性检查
    在阶段 1 收尾时可以删掉,前提是老路径也切过来(§4.2)。
    """
    spu = spu_service.create_spu(
        session, _payload(audience=Audience.UNISEX.value), actor="tester"
    )
    session.commit()

    assert spu.audience == Audience.UNISEX.value
    rows = spu_service.skus_of(session, spu.id)
    assert {row.audience for row in rows} == {Audience.UNISEX.value}


def test_size_group_records_the_template(session):
    spu = spu_service.create_spu(session, _payload(), actor="tester")
    session.commit()
    assert {row.size_group for row in spu_service.skus_of(session, spu.id)} == {
        "ALPHA_3"
    }


def test_one_size_lands_a_real_value_not_null(session):
    """均码走模板,落库是 "OS"。

    这不是洁癖:`uq_products_variant_size` 对 NULL 不生效(NULL 彼此不相等),
    尺码留空的行可以在同一个颜色下重复任意多次,而唯一约束一声不吭。
    """
    spu = spu_service.create_spu(
        session,
        _payload(
            spu_code="SPU-SW-012",
            size_template="ONE_SIZE",
            color_variants=[{"variant_code": "BLK", "working_name": "黑"}],
        ),
        actor="tester",
    )
    session.commit()

    rows = spu_service.skus_of(session, spu.id)
    assert len(rows) == 1
    assert rows[0].size == "OS"


# ================================================================ 二、约束


def test_a_duplicate_spu_code_is_rejected(session):
    spu_service.create_spu(session, _payload(), actor="tester")
    session.commit()

    with pytest.raises(DuplicateError):
        spu_service.create_spu(session, _payload(), actor="tester")


def test_the_same_color_code_twice_never_reaches_the_database(session):
    """`UNIQUE(spu_id, variant_code)` 是最终裁判,但它报不出是第几个重复了。"""
    with pytest.raises(ValidationError):
        spu_service.create_spu(
            session,
            _payload(
                color_variants=[
                    {"variant_code": "BLK"},
                    {"variant_code": "NVY"},
                    {"variant_code": "blk"},  # 归一化之后和第一个撞
                ]
            ),
            actor="tester",
        )


def test_a_missing_audience_is_refused(session):
    with pytest.raises(ValidationError):
        spu_service.create_spu(session, _payload(audience=None), actor="tester")
    assert session.scalars(select(Spu)).all() == []


def test_a_color_with_skus_cannot_be_deleted(session):
    """外键是 RESTRICT:删一个颜色不该连带删掉它下面的 SKU 行。

    CASCADE 之下这个动作会顺手删掉挂在那些行上的素材、生成任务、候选图、
    图片集、文案、草稿和发布记录,而发出动作的人在界面上看到的只是一个颜色。
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


# ================================================================ 三、事务


def test_a_failure_halfway_leaves_no_debris(session):
    """SPU、颜色、SKU 行必须一起成或一起不成。

    构造方式:先建一个 SPU 占掉某个 SKU 编码,再用**另一个 SPU 编码**建档,
    但让它展开出同一个 SKU —— 撞 `uq_products_sku`,而 SPU 那一行本身合法。
    残骸的形状是"库里多了一个没有 SKU 的 SPU",而它在界面上显示为"建档中",
    运营唯一的动作是再建一次。
    """
    session.add(
        Product(
            spu="SPU-SW-013",
            sku="SPU-SW-013-BLK-S",
            name="占位",
            category="swimwear",
        )
    )
    session.commit()

    with pytest.raises(DuplicateError):
        spu_service.create_spu(
            session,
            _payload(
                spu_code="SPU-SW-013",
                color_variants=[{"variant_code": "BLK"}],
            ),
            actor="tester",
        )
    session.rollback()

    assert session.scalars(
        select(Spu).where(Spu.spu_code == "SPU-SW-013")
    ).all() == [], "失败之后留下了一个没有 SKU 的 SPU"
    assert session.scalars(select(ColorVariant)).all() == []


def test_archiving_writes_one_audit_row(session):
    """建档是一个业务动作,审计里就该是一条 —— 不是 1 + 3 + 9 条。"""
    from app.models.audit_log import AuditLog

    spu = spu_service.create_spu(session, _payload(), actor="operator-a")
    session.commit()

    rows = session.scalars(
        select(AuditLog).where(AuditLog.entity_type == "Spu")
    ).all()
    assert len(rows) == 1
    assert rows[0].actor == "operator-a"
    assert rows[0].payload["sku_count"] == 9
    assert rows[0].payload["variant_codes"] == ["BLK", "COR", "NVY"]
    assert str(rows[0].entity_id) == str(spu.id)


# ================================================================ 四、batch13-2:读取侧
#
# 上面那些用例都站在**建档**这一侧数行数、看外键。F1 之所以能带着 42 条纯守卫
# 和 12 条真库用例发出去,是因为没有一条站在**读取**这一侧问过:同一个 SPU,
# 属性层、图片集、导出这三个消费者各自看见几个变体。


def test_the_nine_rows_are_three_variants_to_the_readers(session):
    """建档建出九行,读取方必须只看见三个变体。

    纯测试里这一条是拿构造出来的 `VariantRow` 断言的,够不着两件事:
    `color_variant_id` 在 flush 那一刻真的落进去了没有,以及
    `variant_id_for()` 从 ORM 对象上读出来的到底是什么。
    """
    from app.listings import variants as variant_service

    spu = spu_service.create_spu(session, _payload(), actor="tester")
    session.commit()

    rows = spu_service.skus_of(session, spu.id)
    assert len(rows) == 9
    ids = {variant_service.variant_id_for(row) for row in rows}
    assert len(ids) == 3, f"九行 SKU 被读成了 {len(ids)} 个变体:{sorted(ids)}"
    assert ids == {str(v.id) for v in spu.color_variants}, (
        "变体身份不是颜色变体的主键 —— 回落到 variant_key 或种子了"
    )


def test_the_image_set_coverage_asks_for_three_variants_not_nine(session):
    """图片集的变体覆盖要求三张图,不是九张。

    这是 F1 三个后果里最贵的一个:要求九个变体各有一张图,而运营只会
    拍三个颜色,于是这一版**永远批准不了**,且错误信息指不到原因。
    """
    from app.listings import variants as variant_service

    spu = spu_service.create_spu(session, _payload(), actor="tester")
    session.commit()

    required = variant_service.required_variant_ids(session, spu.spu_code)
    assert len(required) == 3, f"图片覆盖要求 {len(required)} 个变体:{required}"


def test_the_variant_directory_shows_names_not_uuids(session):
    """建档刚做完、识别还没跑的那一刻,目录里显示的是颜色的名字。

    此时 `primary_color` 全是空的,而身份已经是 UUID。少了
    `label_for_row()` 的第二级回落,图片绑定界面上三个变体会显示成
    三串十六进制,运营认不出哪个是黑色。
    """
    from app.listings import variants as variant_service

    spu = spu_service.create_spu(session, _payload(), actor="tester")
    session.commit()

    labels = {ref.label for ref in variant_service.variant_directory(session, spu.spu_code)}
    assert labels == {"供应商说的那个黑", "藏青", "珊瑚"}, labels


def test_a_freshly_archived_spu_has_a_clean_drift_report(session):
    """新建档的 SPU 不该在任何一张诊断表上。

    `unassigned` 的旧判据是"没有 variant_key",而新路径**不写** key ——
    不改判据的话,建档建出来的每一行都会永远挂在那张表上。恒报等于不报。
    """
    from app.listings import variants as variant_service

    spu = spu_service.create_spu(session, _payload(), actor="tester")
    session.commit()

    report = variant_service.drift_report(session, spu.spu_code)
    assert all(not rows for rows in report.values()), f"新建档的 SPU 就有诊断:{report}"


def test_the_list_endpoint_reports_the_real_sku_count(client):
    """`GET /spus` 的 `sku_count` 是数出来的,不是常量 0。"""
    created = client.post("/api/spus", json=_payload())
    assert created.status_code == 201, created.text

    listed = client.get("/api/spus")
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert items, "列表里一个 SPU 都没有"
    assert items[0]["sku_count"] == 9, f"列表页报了 {items[0]['sku_count']} 个 SKU"


def test_an_illegal_variant_code_tells_the_form_which_row(client):
    """422 的错误体里带着出问题的那一行。

    前端靠 `error.fields[].loc` 高亮 —— 拿不到的话,一个有三个颜色的
    建档表单只会整体弹一句"编码不合法",运营要自己一行行试。
    """
    bad = _payload(
        spu_code="SPU-SW-014",
        color_variants=[
            {"variant_code": "BLK", "working_name": "黑"},
            {"variant_code": "N-VY", "working_name": "藏青"},
        ],
    )
    response = client.post("/api/spus", json=bad)
    assert response.status_code == 422, response.text
    fields = response.json()["error"].get("fields")
    assert fields, f"422 里没有字段位置:{response.json()}"
    assert "color_variants[1]" in fields[0]["loc"], fields
