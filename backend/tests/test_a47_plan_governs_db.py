"""a47 §5.5:生成方案真的控制了出图吗 —— 四条等式与 override 分支。

## 为什么这几条非要真库

`tests/pure/test_a47_plan_governs_generation.py` 把判定与接线验干净了,
但它验的是**源码形状**:"`provider` 是从 `plan_view` 取的"。
形状对了不等于跑出来对 —— 中间隔着方案解析、Provider 注册表、幂等键
和一次真实落库,而这四样里任何一样出岔子,表现都是"任务建出来了,
参数不是方案的",没有任何一处报错。

## 四条等式(PRD §5.5)

同一商品 + 颜色,SPU 有 ACTIVE 方案时::

    GenerationPlan.provider          == GenerationTask.provider
    GenerationPlan.model_template_id == GenerationTask.model_template_id
    gp.required_angles(方案)          == 实际出图请求覆盖的角度集合
    GenerationTask.plan_fingerprint  != NULL

第三条在这个基线上有一处**必须说清的边界**:`GenerationTask` 没有角度列
(本轮禁止加列),`GenerationRequest` 也没有角度参数 —— 出图 Provider
的接口里根本没有这个概念。所以"实际出图覆盖的角度集合"落在**提示词**上,
这里验的是 `task.prompt` 逐个角度都提到了,以及 `candidate_count` 等于
`gp.total_candidates(方案)`。要验到"生成出来的图真的是那几个角度",
需要给候选图加角度标注 —— 那是另一轮的事,记在 `docs/DECISIONS.md`。

## 顺序

    alembic upgrade head
    pytest tests/test_a47_plan_governs_db.py
    pytest -m requires_db
"""
from __future__ import annotations

import uuid

import pytest

from app.core.config import settings
from app.core.enums import (
    AuditAction,
    GenerationPlanStatus,
    SellableStatus,
    SpuStatus,
)
from app.core.errors import ValidationError
from app.models.audit_log import AuditLog
from app.models.generation_plan import GenerationPlan
from app.models.model_template import ModelTemplate
from app.models.product import Product
from app.models.product_asset import ProductAsset
from app.models.spu import ColorVariant, Spu
from app.services import generation_service as gs
from app.workflows import generation_plan as gp
from tests.conftest import requires_db

pytestmark = requires_db


# ---------------------------------------------------------------- 夹具


def _spu(session) -> Spu:
    row = Spu(
        spu_code=f"SP{uuid.uuid4().hex[:6].upper()}",
        internal_name="a47 方案控制出图",
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


def _product(session, spu: Spu, variant: ColorVariant) -> Product:
    row = Product(
        spu_id=spu.id,
        spu=spu.spu_code,
        sku=f"{spu.spu_code}-{variant.variant_code}-S",
        name="a47 测试商品",
        category="swimwear",
        audience="WOMEN",
        color_variant_id=variant.id,
    )
    session.add(row)
    session.flush()
    # 素材是建任务的前置(`create_task` 里那句"该商品还没有可用素材")
    file_hash = uuid.uuid4().hex
    session.add(
        ProductAsset(
            product_id=row.id,
            asset_type="GARMENT_FRONT",
            file_hash=file_hash,
            mime_type="image/jpeg",
            file_size=1024,
            width=800,
            height=1200,
            storage_path=f"uploads/aa/aa/{file_hash}.jpg",
            original_filename="front.jpg",
            check_passed=True,
        )
    )
    session.flush()
    return row


def _active_plan(session, spu: Spu, **kw) -> GenerationPlan:
    base = dict(
        spu_id=spu.id,
        color_variant_id=None,
        provider="mock",
        scene="海边日光",
        pose="站姿",
        angles_json=gp.serialize_angles(gp.normalize_angles({"FRONT": 2, "BACK": 1})),
        status=GenerationPlanStatus.ACTIVE.value,
    )
    base.update(kw)
    row = GenerationPlan(**base)
    row.plan_fingerprint = gp.fingerprint_of(
        gp.PlanView(
            plan_id="pending",
            color_variant_id=str(base["color_variant_id"]) if base["color_variant_id"] else None,
            model_template_id=str(base.get("model_template_id") or "") or None,
            provider=base["provider"],
            scene=base["scene"],
            pose=base["pose"],
            angles=gp.normalize_angles(base["angles_json"]),
            budget_cap=base.get("budget_cap"),
        )
    )
    session.add(row)
    session.flush()
    return row


def _model_template(session) -> ModelTemplate:
    file_hash = uuid.uuid4().hex
    row = ModelTemplate(
        name="a47 方案回归模特",
        storage_path=f"uploads/models/{file_hash}.jpg",
        file_hash=file_hash,
        mime_type="image/jpeg",
        width=800,
        height=1200,
        audience="WOMEN",
        enabled=True,
        license_status="UNVERIFIED",
    )
    session.add(row)
    session.flush()
    return row


def _scene(session):
    spu = _spu(session)
    variant = _variant(session, spu, "RED")
    product = _product(session, spu, variant)
    template = _model_template(session)
    plan = _active_plan(session, spu, model_template_id=template.id)
    return spu, variant, product, plan


# ---------------------------------------------------------------- 四条等式


def test_the_provider_on_the_task_is_the_one_the_plan_names(session):
    """第一条等式。**调用方传的被忽略,而且是有意被忽略的。**

    这里刻意传一个与方案不同的 provider —— 改之前它会原样落到任务上,
    而方案指纹照记不误。
    """
    _, _, product, plan = _scene(session)

    task, _ = gs.create_task(
        session, product_id=product.id, mode="virtual_try_on", provider="mock"
    )

    assert task.provider == plan.provider


def test_the_model_template_on_the_task_is_the_plan_s(session):
    """第二条等式：虚拟试穿的模特由生效方案决定。"""
    _, _, product, plan = _scene(session)
    assert plan.model_template_id is not None

    task, _ = gs.create_task(
        session, product_id=product.id, mode="virtual_try_on", provider="mock"
    )

    assert task.model_template_id == plan.model_template_id


def test_the_round_covers_exactly_the_angles_the_plan_requires(session):
    """第三条等式 —— 见模块文档里那段边界说明。"""
    _, _, product, plan = _scene(session)
    wanted = gp.normalize_angles(plan.angles_json)

    task, _ = gs.create_task(
        session, product_id=product.id, mode="virtual_try_on", provider="mock",
        candidate_count=1,
    )

    # 张数:方案说了算,不是弹窗里那个 1
    assert task.candidate_count == gp.total_candidates(wanted) == 3
    # 角度集合:逐个都进了送给 Provider 的那句话
    for angle in gp.required_angles(wanted):
        assert angle in (task.prompt or ""), f"提示词里没有 {angle}"
    # 场景与姿势同理(§5.2「scene / pose 参与 prompt 构建」)
    assert plan.scene in (task.prompt or "")
    assert plan.pose in (task.prompt or "")


def test_the_fingerprint_column_finally_has_a_writer_that_means_something(session):
    """第四条等式。

    这一列以前**有索引、没有读者**,而且记的是一份并没有被执行的方案。
    现在它记的是真的被执行的那一份。
    """
    _, _, product, plan = _scene(session)

    task, _ = gs.create_task(
        session, product_id=product.id, mode="virtual_try_on", provider="mock"
    )

    assert task.plan_fingerprint == plan.plan_fingerprint
    assert task.generation_plan_id == plan.id


# ---------------------------------------------------------------- 幂等键


def test_two_requests_that_differ_only_in_provider_hit_the_same_task(session):
    """§5.2 硬约束 2:幂等键用**解析后**的值。

    用入参的话这两次会建出两个任务,而它们最终跑的是同一组参数 ——
    两次付费调用,拿回两组几乎一样的图。
    """
    _, _, product, _ = _scene(session)

    first, dup_first = gs.create_task(
        session, product_id=product.id, mode="virtual_try_on", provider="mock"
    )
    second, dup_second = gs.create_task(
        session, product_id=product.id, mode="virtual_try_on", provider="fashn"
    )

    assert dup_first is False and dup_second is True
    assert second.id == first.id


# ---------------------------------------------------------------- 冲突留痕


def test_the_audit_says_which_one_was_applied(session):
    """§5.2 硬约束 3。

    没有这一条,排障的人会对着请求体里的 `fashn` 和任务上的 `mock`
    百思不得其解 —— 而两个值都是对的,只是有一条规则没写在任何地方。
    """
    _, _, product, plan = _scene(session)

    task, _ = gs.create_task(
        session, product_id=product.id, mode="virtual_try_on", provider="fashn"
    )
    session.flush()

    entry = (
        session.query(AuditLog)
        .filter(
            AuditLog.entity_type == "GenerationTask",
            AuditLog.entity_id == task.id,
            AuditLog.action == AuditAction.CREATE.value,
        )
        .one()
    )
    overrides = entry.payload["plan_overrides"]

    assert overrides == [
        {"field": "provider", "requested": "fashn", "plan": plan.provider, "applied": "plan"}
    ]
    assert entry.payload["plan_bypassed"] is False


def test_no_conflict_leaves_an_empty_list_not_a_missing_key(session):
    """空列表与缺席是两件事。

    缺席意味着这一版代码还不记这件事,空列表意味着这次真的没有冲突 ——
    读审计的人要分得出。
    """
    _, _, product, plan = _scene(session)

    task, _ = gs.create_task(
        session, product_id=product.id, mode="virtual_try_on", provider=plan.provider
    )
    session.flush()

    entry = (
        session.query(AuditLog)
        .filter(AuditLog.entity_type == "GenerationTask", AuditLog.entity_id == task.id)
        .one()
    )
    assert entry.payload["plan_overrides"] == []


# ---------------------------------------------------------------- override 分支


def test_override_runs_the_caller_s_parameters_and_records_no_plan(session):
    """§5.3 的要害:**绕过了方案就不许再记这份方案。**

    记着方案而按别的参数跑,正是本轮要修的那个错位的镜像版本 ——
    而且更隐蔽:那一行任务看起来完全正常。
    """
    _, _, product, plan = _scene(session)

    task, _ = gs.create_task(
        session,
        product_id=product.id,
        mode="virtual_try_on",
        provider="mock",
        candidate_count=2,
        prompt="只按我说的来",
        model_template_id=plan.model_template_id,
        override_plan=True,
    )

    assert task.candidate_count == 2
    assert task.prompt == "只按我说的来"
    assert task.generation_plan_id is None
    assert task.plan_fingerprint is None


def test_override_and_plan_governed_are_two_different_tasks(session):
    """两者的幂等键必须分得开。

    合成一个键的话,管理员一次排障会把运营那条正常动线的任务顶掉 ——
    而运营看到的是"提交成功",拿到的是排障参数出的图。
    """
    _, _, product, plan = _scene(session)

    governed, _ = gs.create_task(
        session, product_id=product.id, mode="virtual_try_on", provider="mock"
    )
    bypassed, deduplicated = gs.create_task(
        session,
        product_id=product.id,
        mode="virtual_try_on",
        provider="mock",
        candidate_count=3,
        model_template_id=plan.model_template_id,
        override_plan=True,
    )

    assert deduplicated is False
    assert bypassed.id != governed.id


def test_override_still_pays_attention_to_the_budget(session, monkeypatch):
    """预算是花钱的闸,不是出图参数。

    绕过方案不等于绕过预算 —— 否则 override 会成为超预算出图的后门,
    而它只有管理员能用,查起来最没人怀疑。
    """
    spu = _spu(session)
    variant = _variant(session, spu, "BLU")
    product = _product(session, spu, variant)
    template = _model_template(session)
    # Mock 未配价时按设计是 0 元，预算 0 不应拦住免费调用。
    # 这条用例要验的是“override 绕不过预算”，所以必须先让
    # 被测调用确实有正价格，否则它只是在断言 0 > 0。
    monkeypatch.setattr(
        settings,
        "PROVIDER_PRICE_BOOK",
        '{"mock":{"submit":{"micros":1000000,"currency":"USD"}}}',
    )
    # 上限为 0:任何一次预估都会把它顶破(`gp.budget_verdict` 的 EXCEEDED)
    _active_plan(session, spu, model_template_id=template.id, budget_cap=0)

    with pytest.raises(ValidationError):
        gs.create_task(
            session,
            product_id=product.id,
            mode="virtual_try_on",
            provider="mock",
            model_template_id=template.id,
            override_plan=True,
        )


# ---------------------------------------------------------------- 端点的身份判定


def test_an_operator_asking_to_override_gets_403_not_a_silent_ignore(
    session_operator_client, session
):
    """§5.3:非管理员传它一律 403。

    静默忽略的代价写在 `_assert_may_override_plan` 的文档里:
    **一个没有生效的开关比没有这个开关更贵。**
    """
    _, _, product, _ = _scene(session)
    session.commit()

    response = session_operator_client.post(
        "/api/generation-tasks",
        json={
            "product_id": str(product.id),
            "mode": "virtual_try_on",
            "provider": "mock",
            "override_plan": True,
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_FORBIDDEN"


def test_an_admin_may_override(session_admin_client, session):
    _, _, product, plan = _scene(session)
    session.commit()

    response = session_admin_client.post(
        "/api/generation-tasks",
        json={
            "product_id": str(product.id),
            "mode": "virtual_try_on",
            "provider": "mock",
            "model_template_id": str(plan.model_template_id),
            "override_plan": True,
        },
    )

    assert response.status_code == 201
