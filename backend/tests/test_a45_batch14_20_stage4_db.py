"""A45-batch14-20:阶段 4 的真库用例。

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

    alembic upgrade head        # 从空库执行完整迁移链
    pytest tests/test_a45_batch14_20_stage4_db.py
    pytest -m requires_db       # 再跑全量

第一条失败时**先看是不是索引写法的问题**,不要去改模型让它变绿 ——
让它变绿最省事的做法正好是把 COALESCE 删掉。
"""
from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.core.enums import GenerationPlanStatus, SellableStatus, SpuStatus
from app.models.generation import GenerationTask
from app.models.generation_plan import GenerationPlan
from app.models.listing_image import ListingImageSet
from app.models.product import Product
from app.models.spu import ColorVariant, Spu
from app.services import generation_plan_service
from app.workflows import generation_plan as gp
from tests.conftest import requires_db

pytestmark = requires_db


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


def test_a_second_spu_default_plan_is_rejected_by_the_database(session):
    """**这一条是本文件的第一位。**

    NULL 互不相等,所以 `UNIQUE(spu_id, color_variant_id)` 挡不住它。
    挡得住的只有 `COALESCE(color_variant_id::text, '')` 那条表达式索引。
    """
    spu = _spu(session)
    session.add(_plan(spu))
    session.flush()

    session.add(_plan(spu))
    with pytest.raises(IntegrityError):
        session.flush()


def test_two_colours_can_each_have_their_own_override(session):
    """颜色覆盖之间互不影响 —— 唯一性是按 (spu, 颜色) 这一对算的。"""
    spu = _spu(session)
    red = _variant(session, spu, "RED")
    blue = _variant(session, spu, "BLU")
    session.add(_plan(spu))
    session.add(_plan(spu, color_variant_id=red.id))
    session.add(_plan(spu, color_variant_id=blue.id))
    session.flush()


def test_an_archived_plan_frees_the_slot(session):
    """归档之后可以再配一份。

    少了 `WHERE status <> 'ARCHIVED'`,一个 SPU 一辈子只能改一次方案 ——
    而旧方案又不能删(图片集过期判定要拿它的指纹比)。
    """
    spu = _spu(session)
    old = _plan(spu)
    session.add(old)
    session.flush()

    old.status = GenerationPlanStatus.ARCHIVED.value
    session.flush()

    session.add(_plan(spu))
    session.flush()  # 不该炸


def test_a_negative_budget_cap_is_rejected(session):
    """负上限的表现是"每次创建任务都被预算拦下",而提示写的是"预算不足"。"""
    spu = _spu(session)
    session.add(_plan(spu, budget_cap=-1))
    with pytest.raises(IntegrityError):
        session.flush()


# ---------------------------------------------------------------- 删除方向


def test_deleting_a_plan_keeps_the_task_and_clears_only_the_reference(session):
    """`generation_tasks.generation_plan_id` 是 SET NULL,不是 CASCADE。

    CASCADE 的话,删一份方案会连着删掉它出过的任务 —— 而那些任务的候选图
    可能已经批准、已经发布。指纹快照留在任务行上,所以"当时那份方案长什么样"
    仍然答得出来。
    """
    spu = _spu(session)
    plan = _plan(spu)
    session.add(plan)
    session.flush()
    plan_id = plan.id

    product = Product(
        spu_id=spu.id,
        spu=spu.spu_code,
        sku=f"{spu.spu_code}-BLK-S",
        name="阶段四外键测试商品",
        category="swimwear",
        audience="WOMEN",
    )
    session.add(product)
    session.flush()
    task = GenerationTask(
        product_id=product.id,
        mode="product_to_model",
        provider="mock",
        idempotency_key=f"stage4-delete-{uuid.uuid4().hex}",
        generation_plan_id=plan.id,
        plan_fingerprint="snapshot-survives-plan-deletion",
    )
    session.add(task)
    session.flush()
    task_id = task.id

    session.delete(plan)
    session.flush()

    row = session.execute(
        text(
            "SELECT generation_plan_id, plan_fingerprint "
            "FROM generation_tasks WHERE id = :tid"
        ),
        {"tid": str(task_id)},
    ).one()
    assert row[0] is None
    assert row[1] == "snapshot-survives-plan-deletion"
    assert session.get(GenerationTask, task_id) is not None
    assert session.get(GenerationPlan, plan_id) is None


def test_deleting_a_colour_takes_its_override_but_not_the_spu_default(session):
    """颜色没了,它的覆盖跟着走(CASCADE);SPU 默认那份留下。"""
    spu = _spu(session)
    red = _variant(session, spu, "RED")
    session.add(_plan(spu))
    session.add(_plan(spu, color_variant_id=red.id))
    session.flush()

    session.delete(red)
    session.flush()

    left = session.execute(
        text("SELECT count(*) FROM generation_plans WHERE spu_id = :sid"),
        {"sid": str(spu.id)},
    ).scalar_one()
    assert left == 1


# ---------------------------------------------------------------- §6.5 接线


def test_the_new_image_item_columns_have_the_conservative_database_shape(session):
    """`shared_opt_in` 默认必须是 false(§6.5「默认不混入」)。

    默认 true 在今天看不出差别(库里的 `variant_id` 全是 NULL),而颜色
    绑定入口一上线,每个颜色的附图位都会自动灌满所有通用图。
    """
    rows = session.execute(
        text(
            "SELECT column_name, column_default, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'listing_image_items' "
            "AND column_name IN ('shared_opt_in', 'angle')"
        )
    ).all()
    shape = {row[0]: (row[1], row[2]) for row in rows}
    assert set(shape) == {"shared_opt_in", "angle"}
    assert shape["shared_opt_in"][1] == "NO"
    assert "false" in str(shape["shared_opt_in"][0]).lower()
    assert shape["angle"] == (None, "YES")


def _approved_multicolour_image_set_count(session) -> int:
    """上线前盘点 SQL。生产执行时只读,测试里用造出的风险行反验。"""
    return session.execute(
        text(
            "SELECT count(*) FROM listing_image_sets s "
            "WHERE s.status = 'APPROVED' AND EXISTS ("
            "  SELECT 1 FROM spus p "
            "  JOIN color_variants v ON v.spu_id = p.id "
            "  WHERE p.spu_code = s.spu "
            "  GROUP BY p.id HAVING count(v.id) > 1)"
        )
    ).scalar_one()


def test_inventory_finds_an_approved_multicolour_set_with_only_shared_images(session):
    """盘点必须抓到「多颜色 SPU + 全部通用图」,不能靠 item 已经带颜色。

    §6.5 的门禁把"有通用图就算覆盖"那条放行拿掉了。如果库里真的存在
    多色 SPU 的已批准图片集,它们会在下一次校验时全部变成 BLOCKED ——
    那不是修复,是停产(`image_set_service.variant_coverage` 的注释里
    写过这个顾虑,本批是靠 §6.5 才敢动它)。

    测试夹具每次先清空 schema,所以它不能证明真实环境"没有存量"。这里
    反向造一条风险数据,证明上线前使用的只读盘点 SQL 不会得到平凡的零。
    """
    spu = _spu(session)
    _variant(session, spu, "RED")
    _variant(session, spu, "BLU")
    session.add(
        ListingImageSet(
            spu=spu.spu_code,
            version=1,
            status="APPROVED",
            approved_by="stage4-db-test",
        )
    )
    session.flush()

    assert _approved_multicolour_image_set_count(session) == 1


def test_save_plan_stores_the_fingerprint_the_pure_layer_computes(session):
    """存下来的指纹必须是纯层算出来的那一个。

    服务层自己拼一个的表现是:创建任务时用 A,判过期时用 B,于是每次
    创建任务都会顺带把自己的图片集判成过期 —— 一个只会多花钱、不会报错的洞。
    """
    spu = _spu(session)
    angles = gp.normalize_angles(["FRONT", "BACK"])
    expected = gp.plan_fingerprint(
        model_template_id=None,
        provider="mock",
        scene="studio",
        pose="STANDING_FRONT",
        angles=angles,
        budget_cap=None,
    )
    plan = generation_plan_service.save_plan(
        session,
        spu_id=spu.id,
        provider="mock",
        scene="studio",
        pose="STANDING_FRONT",
        angles=["FRONT", "BACK"],
        actor="stage4-db-test",
    )
    session.refresh(plan)

    assert plan.plan_fingerprint == expected
    assert plan.angles_json == gp.serialize_angles(angles)


def test_an_active_plan_can_still_be_replaced(session):
    """启用不是终点:新 DRAFT 与旧 ACTIVE 共存,再启用时旧方案归档。"""
    spu = _spu(session)
    first = generation_plan_service.save_plan(
        session,
        spu_id=spu.id,
        provider="mock",
        scene="studio",
        pose="STANDING_FRONT",
        angles=["FRONT"],
        actor="stage4-db-test",
    )
    generation_plan_service.activate(session, first.id, actor="stage4-db-test")

    replacement = generation_plan_service.save_plan(
        session,
        spu_id=spu.id,
        provider="mock",
        scene="beach",
        pose="STANDING_FRONT",
        angles=["FRONT", "BACK"],
        actor="stage4-db-test",
    )
    assert replacement.id != first.id
    assert first.status == GenerationPlanStatus.ACTIVE.value
    assert replacement.status == GenerationPlanStatus.DRAFT.value

    generation_plan_service.activate(
        session, replacement.id, actor="stage4-db-test"
    )
    assert first.status == GenerationPlanStatus.ARCHIVED.value
    assert replacement.status == GenerationPlanStatus.ACTIVE.value


def test_saving_the_same_plan_twice_is_idempotent(session):
    """双击不建第二份,也不多写一次版本。"""
    spu = _spu(session)
    kwargs = {
        "spu_id": spu.id,
        "provider": "mock",
        "scene": "studio",
        "pose": "STANDING_FRONT",
        "angles": ["FRONT"],
        "note": "same request",
        "actor": "stage4-db-test",
    }
    first = generation_plan_service.save_plan(session, **kwargs)
    version = first.row_version
    second = generation_plan_service.save_plan(session, **kwargs)

    assert second.id == first.id
    assert second.row_version == version
    count = session.scalar(
        select(func.count())
        .select_from(GenerationPlan)
        .where(GenerationPlan.spu_id == spu.id)
    )
    assert count == 1


def test_http_double_save_returns_the_same_plan(
    admin_client, session, monkeypatch
):
    """真实 POST 双击不该 500,两次响应必须指向同一份 DRAFT。"""
    from app.api import generation_plans as plan_api

    monkeypatch.setattr(
        plan_api.action_gate,
        "ensure_allowed_for_spu",
        lambda *args, **kwargs: None,
    )
    spu = _spu(session)
    payload = {
        "spu_id": str(spu.id),
        "provider": "mock",
        "scene": "studio",
        "pose": "STANDING_FRONT",
        "angles": [{"angle": "FRONT", "count": 1}],
        "note": "same HTTP request",
    }

    first = admin_client.post("/api/generation-plans", json=payload)
    second = admin_client.post("/api/generation-plans", json=payload)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["id"] == second.json()["id"]
    count = session.scalar(
        select(func.count())
        .select_from(GenerationPlan)
        .where(GenerationPlan.spu_id == spu.id)
    )
    assert count == 1


def test_editing_a_draft_updates_it_without_rewriting_an_active_plan(session):
    """同层已有 DRAFT 时保存是编辑草稿,不是覆盖 ACTIVE 历史。"""
    spu = _spu(session)
    draft = generation_plan_service.save_plan(
        session,
        spu_id=spu.id,
        provider="mock",
        scene="studio",
        pose="STANDING_FRONT",
        angles=["FRONT"],
        actor="stage4-db-test",
    )
    original_id = draft.id
    original_version = draft.row_version

    updated = generation_plan_service.save_plan(
        session,
        spu_id=spu.id,
        provider="mock",
        scene="beach",
        pose="STANDING_FRONT",
        angles=["FRONT", "BACK"],
        actor="stage4-db-test",
    )

    assert updated.id == original_id
    assert updated.scene == "beach"
    assert updated.row_version == original_version + 1


def test_two_sessions_racing_to_save_the_same_plan_reuse_one_winner(engine):
    """两个真事务并发保存:两个调用都成功,库里只有一个 DRAFT。"""
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as setup:
        spu = _spu(setup)
        setup.commit()
        spu_id = spu.id

    barrier = threading.Barrier(2)

    def save_once(actor: str):
        with factory() as worker:
            barrier.wait(timeout=10)
            row = generation_plan_service.save_plan(
                worker,
                spu_id=spu_id,
                provider="mock",
                scene="studio",
                pose="STANDING_FRONT",
                angles=["FRONT"],
                note="same concurrent request",
                actor=actor,
            )
            worker.commit()
            return row.id

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(save_once, ("session-a", "session-b")))

        assert ids[0] == ids[1]
        with factory() as check:
            count = check.scalar(
                select(func.count())
                .select_from(GenerationPlan)
                .where(
                    GenerationPlan.spu_id == spu_id,
                    GenerationPlan.status == GenerationPlanStatus.DRAFT.value,
                )
            )
            assert count == 1
    finally:
        with factory() as cleanup:
            row = cleanup.get(Spu, spu_id)
            if row is not None:
                cleanup.delete(row)
                cleanup.commit()
