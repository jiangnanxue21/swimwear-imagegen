"""A45-batch14-20:阶段 4 的**真库**用例。**一次都没跑过。**

## 为什么这几条非要真库

纯层守卫把判定验干净了,但阶段 4 有四样东西的正确性完全落在数据库上,
而**它们失效时都不报错**:

    一  `uq_generation_plans_scope` 是表达式唯一索引。写成 UniqueConstraint
        会因为 NULL 互不相等而挡不住第二份 SPU 默认方案 —— 那时
        `resolve_plan()` 每次按查询顺序挑一份,同一个 SPU 两次创建任务
        用了不同的参数。**纯层守卫看不见索引**
    二  `postgresql_where=status <> 'ARCHIVED'`。少了它,一个 SPU 一辈子
        只能改一次方案(归档那份会一直占着唯一位)。这是"改不动"型故障,
        它会以 500 的形式出现,但要等到第二次改方案
    三  §6.5 的门禁一旦接线,**存量图片集会不会集体无法批准**。§3.1 说
        系统尚未投入使用、没有存量数据,但那句话从来没有在真库上被验证过。
        这一条如果不成立,阶段 4 上线当天所有多色 SPU 停产
    四  `ck_generation_plans_budget_cap` 与两个外键的 ondelete 方向:
        删一份方案不该连着删掉它出过的图(SET NULL 而不是 CASCADE)

## 顺序

    alembic upgrade head        # 0040 / 0041 从未执行过
    pytest tests/test_a45_batch14_20_stage4_db.py
    pytest -m requires_db       # 再跑全量

第一条失败时**先看是不是索引写法的问题**,不要去改模型让它变绿 ——
让它变绿最省事的做法正好是把 COALESCE 删掉。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.enums import GenerationPlanStatus, SellableStatus, SpuStatus
from app.models.generation_plan import GenerationPlan
from app.models.spu import ColorVariant, Spu
from app.workflows import generation_plan as gp

pytestmark = pytest.mark.requires_db


def _spu(session) -> Spu:
    row = Spu(
        spu_code=f"SP{uuid.uuid4().hex[:6].upper()}",
        internal_name="阶段四测试款",
        audience="WOMEN",
        base_category="swimwear",
        status=SpuStatus.DRAFT.value,
    )
    session.add(row)
    session.flush()
    return row


def _variant(session, spu: Spu, code: str) -> ColorVariant:
    row = ColorVariant(
        spu_id=spu.id,
        variant_code=code,
        working_name=code,
        sellable_status=SellableStatus.ACTIVE.value,
    )
    session.add(row)
    session.flush()
    return row


def _plan(spu: Spu, **kw) -> GenerationPlan:
    base = dict(
        spu_id=spu.id,
        color_variant_id=None,
        provider="mock",
        scene="studio",
        pose="STANDING_FRONT",
        angles_json=gp.serialize_angles(gp.normalize_angles(["FRONT", "BACK"])),
        status=GenerationPlanStatus.ACTIVE.value,
        plan_fingerprint="",
    )
    base.update(kw)
    return GenerationPlan(**base)


# ---------------------------------------------------------------- 唯一性


def test_a_second_spu_default_plan_is_rejected_by_the_database(db_session):
    """**这一条是本文件的第一位。**

    NULL 互不相等,所以 `UNIQUE(spu_id, color_variant_id)` 挡不住它。
    挡得住的只有 `COALESCE(color_variant_id::text, '')` 那条表达式索引。
    """
    spu = _spu(db_session)
    db_session.add(_plan(spu))
    db_session.flush()

    db_session.add(_plan(spu))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_two_colours_can_each_have_their_own_override(db_session):
    """颜色覆盖之间互不影响 —— 唯一性是按 (spu, 颜色) 这一对算的。"""
    spu = _spu(db_session)
    red = _variant(db_session, spu, "RED")
    blue = _variant(db_session, spu, "BLU")
    db_session.add(_plan(spu))
    db_session.add(_plan(spu, color_variant_id=red.id))
    db_session.add(_plan(spu, color_variant_id=blue.id))
    db_session.flush()


def test_an_archived_plan_frees_the_slot(db_session):
    """归档之后可以再配一份。

    少了 `WHERE status <> 'ARCHIVED'`,一个 SPU 一辈子只能改一次方案 ——
    而旧方案又不能删(图片集过期判定要拿它的指纹比)。
    """
    spu = _spu(db_session)
    old = _plan(spu)
    db_session.add(old)
    db_session.flush()

    old.status = GenerationPlanStatus.ARCHIVED.value
    db_session.flush()

    db_session.add(_plan(spu))
    db_session.flush()  # 不该炸


def test_a_negative_budget_cap_is_rejected(db_session):
    """负上限的表现是"每次创建任务都被预算拦下",而提示写的是"预算不足"。"""
    spu = _spu(db_session)
    db_session.add(_plan(spu, budget_cap=-1))
    with pytest.raises(IntegrityError):
        db_session.flush()


# ---------------------------------------------------------------- 删除方向


def test_deleting_a_plan_does_not_take_the_pictures_with_it(db_session):
    """`generation_tasks.generation_plan_id` 是 SET NULL,不是 CASCADE。

    CASCADE 的话,删一份方案会连着删掉它出过的任务 —— 而那些任务的候选图
    可能已经批准、已经发布。指纹快照留在任务行上,所以"当时那份方案长什么样"
    仍然答得出来。
    """
    spu = _spu(db_session)
    plan = _plan(spu)
    db_session.add(plan)
    db_session.flush()
    plan_id = plan.id

    db_session.delete(plan)
    db_session.flush()

    remaining = db_session.execute(
        text("SELECT count(*) FROM generation_tasks WHERE generation_plan_id = :pid"),
        {"pid": str(plan_id)},
    ).scalar_one()
    assert remaining == 0


def test_deleting_a_colour_takes_its_override_but_not_the_spu_default(db_session):
    """颜色没了,它的覆盖跟着走(CASCADE);SPU 默认那份留下。"""
    spu = _spu(db_session)
    red = _variant(db_session, spu, "RED")
    db_session.add(_plan(spu))
    db_session.add(_plan(spu, color_variant_id=red.id))
    db_session.flush()

    db_session.delete(red)
    db_session.flush()

    left = db_session.execute(
        text("SELECT count(*) FROM generation_plans WHERE spu_id = :sid"),
        {"sid": str(spu.id)},
    ).scalar_one()
    assert left == 1


# ---------------------------------------------------------------- §6.5 接线


def test_the_new_image_item_columns_exist_with_the_conservative_default(db_session):
    """`shared_opt_in` 默认必须是 false(§6.5「默认不混入」)。

    默认 true 在今天看不出差别(库里的 `variant_id` 全是 NULL),而颜色
    绑定入口一上线,每个颜色的附图位都会自动灌满所有通用图。
    """
    row = db_session.execute(
        text(
            "SELECT column_default, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'listing_image_items' AND column_name = 'shared_opt_in'"
        )
    ).one()
    assert row[1] == "NO"
    assert "false" in str(row[0]).lower()


def test_no_existing_image_set_becomes_unapprovable(db_session):
    """**§3.1「系统尚未投入使用」这句话在这里被真的验一次。**

    §6.5 的门禁把"有通用图就算覆盖"那条放行拿掉了。如果库里真的存在
    多色 SPU 的已批准图片集,它们会在下一次校验时全部变成 BLOCKED ——
    那不是修复,是停产(`image_set_service.variant_coverage` 的注释里
    写过这个顾虑,本批是靠 §6.5 才敢动它)。

    这条断言的是**前提**而不是行为:库里没有已批准的多色图片集。
    它红了说明那句"没有存量数据"不成立,那时该做的是先做数据盘点,
    而不是把门禁调松。
    """
    stale = db_session.execute(
        text(
            "SELECT count(*) FROM listing_image_sets s "
            "WHERE s.status = 'APPROVED' AND EXISTS ("
            "  SELECT 1 FROM listing_image_items i "
            "  WHERE i.image_set_id = s.id AND i.variant_id IS NOT NULL)"
        )
    ).scalar_one()
    assert stale == 0, (
        "库里已经有绑定了颜色的已批准图片集 —— §6.5 的门禁会影响它们,"
        "先盘点再上线,不要调松门禁"
    )


def test_the_stored_fingerprint_matches_what_the_pure_layer_computes(db_session):
    """存下来的指纹必须是纯层算出来的那一个。

    服务层自己拼一个的表现是:创建任务时用 A,判过期时用 B,于是每次
    创建任务都会顺带把自己的图片集判成过期 —— 一个只会多花钱、不会报错的洞。
    """
    spu = _spu(db_session)
    angles = gp.normalize_angles(["FRONT", "BACK"])
    plan = _plan(
        spu,
        angles_json=gp.serialize_angles(angles),
        plan_fingerprint=gp.plan_fingerprint(
            model_template_id=None,
            provider="mock",
            scene="studio",
            pose="STANDING_FRONT",
            angles=angles,
            budget_cap=None,
        ),
    )
    db_session.add(plan)
    db_session.flush()
    db_session.refresh(plan)

    view = gp.PlanView(
        plan_id=str(plan.id),
        color_variant_id=None,
        model_template_id=None,
        provider=plan.provider,
        scene=plan.scene,
        pose=plan.pose,
        angles=gp.normalize_angles(plan.angles_json),
        budget_cap=plan.budget_cap,
        status=plan.status,
    )
    assert gp.fingerprint_of(view) == plan.plan_fingerprint
