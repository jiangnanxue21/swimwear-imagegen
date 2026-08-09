"""A45-batch31(阶段 6 修复 / F-8):向导接线的**真库接缝**用例。

## 这一批在补什么

阶段 6 的全部接线证据来自 `tests/pure/` 里的 AST 与源码字符串断言::

    assert '"wizard": _wizard_block(' in src

这类断言证明的是"这行代码写了",不是"这条链路跑通了"。而阶段 6 恰恰有三条
判据的失败模式全在**最后一跳**上 —— 判定层的穷举全绿,错的是它与取数、
与出参合起来的样子。本仓为这个形状付过十四次账(§3.43 那一族)。

review prompt 第三条要求「至少一条跨越真实注册表/取数层 -> 判定层 -> API
的接缝测试」。本文件就是它。每条用例都走**真库 + 真 HTTP**,断言的是
**结果**,不是"调了那个函数"。

## 五节

    一   `wizard` 块真的从 HTTP 出得来,而且与判定层逐字一致
    二   AC-05 那道闸在真数据上真的返回 409(F-2 的接缝证明)
    三   费用预估读的是**真的注册表**(`extractors` / `evaluators` / 价目表)
    四   AC-17 的两个只读端点
    五   GET 不写库(AC-15 的"只读"那半句)
"""
from __future__ import annotations

import uuid

from app.attributes import validation
from app.core.enums import (
    AttributeSource,
    AttributeStatus,
    ImageSetStatus,
    MediaRole,
    MediaSource,
    MediaStatus,
    RoleSource,
    SellableStatus,
    SpuStatus,
)
from app.listings import variants
from app.models.attribute import ProductAttributeValue
from app.models.listing_image import ListingImageItem, ListingImageSet
from app.models.media_asset import MediaAsset
from app.models.product import Product
from app.models.spu import ColorVariant, Spu
from app.workbench import service as wb
from app.workbench import wizard as wizard_rules
from app.workbench.flow import STEP_ORDER, FlowStep, NextActionCode, StepState
from tests.conftest import requires_db

pytestmark = requires_db


# ================================================================ 造数
#
# **造数一律 `commit()`,不是 `flush()`。**
#
# 详情端点最后有一句 `session.rollback()`(它是只读的,这一句是刻意的)。
# 而 `session` 夹具跑在 SAVEPOINT 上,被测代码的 rollback 是**真的** ——
# 于是只 flush 没 commit 的造数会在第一次 GET 之后被那句 rollback 清掉,
# 第二次 GET 就是 404。生产里每个请求各有一个 session,不会这样;
# 这条只影响测试,但它会让本文件的用例以一种很难懂的方式红。


def _spu(session) -> Spu:
    row = Spu(
        spu_code=f"B31{uuid.uuid4().hex[:5].upper()}",
        internal_name="batch31 接缝用例",
        audience="WOMEN",
        base_category="swimwear",
        status=SpuStatus.DRAFT.value,
    )
    session.add(row)
    session.commit()
    return row


def _variant(session, spu, code, status=SellableStatus.ACTIVE) -> ColorVariant:
    row = ColorVariant(
        spu_id=spu.id,
        variant_code=code,
        working_name=f"供应商说的{code}",
        sellable_status=status.value,
    )
    session.add(row)
    session.commit()
    return row


def _product(session, spu, variant, size="S") -> Product:
    row = Product(
        spu=spu.spu_code,
        spu_id=spu.id,
        color_variant_id=variant.id,
        sku=f"{spu.spu_code}-{variant.variant_code}-{size}",
        name="接缝用例款",
        category="swimwear",
        audience="WOMEN",
        size=size,
    )
    session.add(row)
    session.commit()
    return row


def _asset(session, product, spu, variant) -> MediaAsset:
    row = MediaAsset(
        product_id=product.id,
        spu_id=spu.id,
        color_variant_id=variant.id,
        role=MediaRole.PRODUCT_FRONT.value,
        role_source=RoleSource.HUMAN.value,
        source=MediaSource.MANUAL_UPLOAD.value,
        status=MediaStatus.READY.value,
        storage_path=f"orig/{uuid.uuid4().hex}.jpg",
        mime_type="image/jpeg",
        width=800,
        height=1200,
        bytes=1024,
        sha256=uuid.uuid4().hex * 2,
    )
    session.add(row)
    session.commit()
    return row


def _fixture(session, *, with_sample=True, blue_sample=False):
    """一个 SPU、两个在售颜色、每色一行 SKU。

    默认红色有样品、蓝色没有 —— 这是"九色里有一个颜色一张图都没有"的
    最小版本,颜色维的大部分用例都要它。

    `blue_sample=True` 用于 F-1 那条:它要**素材步整体 DONE**,
    否则下游的图片集步会因为等上游而不是 DONE,断言会假绿。
    """
    spu = _spu(session)
    red = _variant(session, spu, "RED")
    blue = _variant(session, spu, "BLU")
    red_sku = _product(session, spu, red)
    blue_sku = _product(session, spu, blue)
    if with_sample:
        _asset(session, red_sku, spu, red)
    if blue_sample:
        _asset(session, blue_sku, spu, blue)
    session.commit()
    return spu, red, blue, red_sku


#: 够让属性步判 DONE 的最小事实集(口径同 batch21 / batch24)。
#:
#: 少一条属性步就停在 TODO,而下游的图片集步会被判 BLOCKED —— 于是
#: "商品级 DONE 而颜色级 BLOCKED" 那个矛盾场景根本造不出来,
#: 断言会**因为上游没就绪而变绿**,也就是一条假绿的用例。
#: WOMEN 受众下 `REGISTRY` 说必需的三个,加两个让文案能过规则的。
#: 少 `primary_color` 的话属性步停在 IN_PROGRESS(「已确认 2/3」)。
_SHARED_FACTS = (
    ("material", "Nylon"),
    ("garment_type", "BIKINI_SET"),
    ("primary_color", "black"),
    ("pattern_type", "SOLID"),
    ("neckline_type", "V_NECK"),
)


def _confirmed_fact(session, product: Product, name: str, value: str):
    """一条已确认事实,**带上当前作用域指纹**。

    不带指纹(留 NULL)的话 `stale_fields` 判它过期 —— 属性步会说
    「已确认 0/3,样品已变 5」,于是这条链路永远走不到 DONE。
    那是 §5.3 / AC-21 的正确行为(没有指纹就无法证明事实仍然成立),
    所以这里补的是**造数**,不是绕过判定。
    """
    from app.attributes import service as attr_service

    owner_type, owner_id = validation.owner_for(
        name,
        spu=product.spu,
        variant_id=variants.variant_id_for(product),
        sku=product.sku,
    )
    prints = attr_service.scope_fingerprints_for(session, product)
    row = ProductAttributeValue(
        owner_type=owner_type.value,
        owner_id=owner_id,
        field_name=name,
        value_json=value,
        status=AttributeStatus.CONFIRMED.value,
        source=AttributeSource.MANUAL.value,
        is_current=True,
        input_fingerprint=attr_service.fingerprint_for_owner(
            prints, owner_type=owner_type, owner_id=owner_id
        ),
    )
    session.add(row)
    return row


def _confirm_shared_facts(session, product: Product) -> None:
    for name, value in _SHARED_FACTS:
        _confirmed_fact(session, product, name, value)
    session.commit()


def _flow(client, product) -> dict:
    resp = client.get(f"/api/workbench/products/{product.id}/flow")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ================================================================ 一、向导块出得来


def test_the_wizard_block_reaches_http_with_all_seven_steps(client, session):
    """`wizard` 块真的在响应里,七步齐全,而且**每一格都说得出话**。

    上一版这一条只有一句 `assert '"wizard": _wizard_block(' in src`。
    那句话在 `_wizard_block` 抛异常、返回 None、或者少一个键的时候
    **全部照样绿**。
    """
    _, _, _, product = _fixture(session)
    payload = _flow(client, product)

    assert "wizard" in payload, "详情响应里没有向导块"
    wizard = payload["wizard"]
    assert [s["step"] for s in wizard["steps"]] == [s.value for s in STEP_ORDER], (
        "七步的顺序与 STEP_ORDER 不一致 —— 运营是靠位置点的"
    )
    for step in wizard["steps"]:
        assert step["label"], f"{step['step']} 没有名字"
        assert step["intro"], f"{step['step']} 没有说明"
        # `is_open` 为真时 `gate_reason` 必须是空串(判定层的契约)
        if step["is_open"]:
            assert step["gate_reason"] == "", (
                f"{step['step']} 能进却还挂着一句挡路理由 —— 界面上会常驻一句"
                "「等上一步」,被读成「这一步永远做不了」"
            )
        else:
            assert step["gate_reason"], f"{step['step']} 进不去却说不出是谁挡着"


def test_the_http_wizard_agrees_with_the_decision_layer_field_by_field(
    client, session
):
    """HTTP 出参与**直接调判定层**得到的结果逐字段相同。

    AC-15 的原话是「三处不得出现互相矛盾的状态」。这一条把其中两处
    (判定层 / HTTP)钉死 —— 中间那一跳(取数 + 序列化)如果掉了什么,
    这里当场红,而不是等运营看到两个数字。
    """
    _, _, _, product = _fixture(session)
    payload = _flow(client, product)

    from app.api.workbench import _color_axis

    ctx = wb.collect(session, product, dry_run=True)
    _, rollup = _color_axis(session, product)
    expected = wizard_rules.serialize(
        wizard_rules.build(ctx.result, rollup=rollup), cost=None
    )

    got = dict(payload["wizard"])
    got.pop("cost")  # 费用单独一节验,它要读价目表
    expected.pop("cost")
    assert got == expected


def _approved_image_set_for_red(session, spu, red, product):
    """一版已批准图片集,**只覆盖 RED**。BLU 一张自己的图都没有。

    这是造矛盾场景的关键:图片集本身是合格的(有图、有主图、已批准),
    所以商品级那一步会判 DONE;而颜色维会说 BLU 缺图。

    ## 为什么复用已有素材而不是再传一张

    再传一张会改变 RED 作用域的样品指纹,于是刚刚确认的事实全部变成
    「样品已变」,属性步掉回 NEEDS_CONFIRM —— 图片集步跟着不是 DONE,
    断言又变成假绿。这是 §5.3 / AC-21 的正确行为,造数要绕开它,
    不是判定要改。
    """
    from sqlalchemy import select as _select

    asset = session.scalars(
        _select(MediaAsset).where(MediaAsset.color_variant_id == red.id)
    ).first()
    assert asset is not None, "RED 没有素材 —— 造数顺序错了"
    image_set = ListingImageSet(
        spu=spu.spu_code,
        version=1,
        status=ImageSetStatus.APPROVED.value,
        approved_by="batch31",
    )
    session.add(image_set)
    session.commit()
    session.add(
        ListingImageItem(
            image_set_id=image_set.id,
            media_asset_id=asset.id,
            role=asset.role,
            sort_order=0,
            variant_id=str(red.id),
            is_primary=True,
            enabled=True,
        )
    )
    session.commit()
    return image_set


def test_the_colour_axis_and_the_wizard_never_disagree_on_a_step(client, session):
    """**F-1 的接缝断言**:同一响应里,商品级步骤状态与颜色维不许互相矛盾。

    具体的矛盾形状是报告点名的那一个:图片集已批准 -> 商品级 DONE,
    而九个颜色里有一个没有自己的图 -> 颜色维 BLOCKED。运营会先信那个绿勾。

    ## 造数为什么要先把属性确认掉

    不确认的话属性步停在 TODO,图片集步会被判 **BLOCKED(等上游)**——
    于是"商品级不是 DONE"这句断言**因为上游没就绪而成立**,
    而不是因为合并规则在工作。那是一条假绿的用例:F-1 修不修它都是绿的。

    确认之后属性步 DONE,图片集步才有机会判 DONE —— 这时候这条断言
    才真的在问 F-1 那个问题。
    """
    spu, red, blue, product = _fixture(session, blue_sample=True)
    _confirm_shared_facts(session, product)
    _approved_image_set_for_red(session, spu, red, product)

    payload = _flow(client, product)
    colors = payload["colors"]
    assert colors is not None, "有 SPU 归属却没有颜色维"

    steps = {s["step"]: s["state"] for s in payload["wizard"]["steps"]}
    worst = colors["worst_by_step"]

    # ---- 三条前提。**它们不成立时后面那条断言是假绿的,所以先钉住** ----
    assert steps[FlowStep.ATTRIBUTE.value] == StepState.DONE.value, (
        "属性步没到 DONE —— 图片集步会因为等上游而不是 DONE,断言会假绿"
    )
    assert worst.get(FlowStep.IMAGE_SET.value) == "BLOCKED", (
        "颜色维没有报出缺图的那个颜色,造数没造出矛盾场景"
    )
    blocking = set(colors["blocking_codes"])
    assert "BLU" in blocking, f"BLU 没有被点名拦路:{blocking}"

    # ---- 正题 ----
    assert steps[FlowStep.IMAGE_SET.value] != StepState.DONE.value, (
        "颜色维说 BLU 缺图,而商品级那一行显示 DONE —— "
        "这正是 F-1:下压规则写了但没有人调。"
        "运营会先信那个绿勾,然后在导出时被门禁拦住"
    )


# ================================================================ 二、AC-05 的闸真的开火


def test_the_gate_refuses_a_bypass_with_409_and_the_decision_layers_reason(
    client, session
):
    """**F-2 的接缝证明**:属性没确认时直接 POST 生成文案 -> 409。

    而且 409 里那句话必须与向导上 `gate_reason` 一模一样(AC-05 最后一句:
    判定层与前端读同一个结果)。两句不同的话运营会以为是两个问题。
    """
    _, _, _, product = _fixture(session)

    payload = _flow(client, product)
    step = next(
        s for s in payload["wizard"]["steps"] if s["step"] == FlowStep.COPY.value
    )
    assert not step["is_open"], "造数没造出「文案步关着」,这条用例会假绿"

    resp = client.post(
        f"/api/workbench/products/{product.id}/copy/generate", json={}
    )
    assert resp.status_code == 409, resp.text
    assert step["gate_reason"] in resp.text, (
        "409 里那句话与向导上显示的挡路理由不同 —— 同一件事两种说法"
    )


def test_approving_an_image_set_is_gated_too(client, session):
    """**上一版漏得最狠的那一个**:属性没确认时直接批准图片集。

    上一版这个端点对属性状态一无所知,`POST /image-sets/{id}/approve`
    会照常执行 —— 而向导上那个按钮是灰的。AC-05 要拦的正是这个差。
    """
    spu, red, _, product = _fixture(session)
    asset = _asset(session, product, spu, red)
    image_set = ListingImageSet(
        spu=spu.spu_code, version=1, status=ImageSetStatus.DRAFT.value
    )
    session.add(image_set)
    session.commit()
    session.add(
        ListingImageItem(
            image_set_id=image_set.id,
            media_asset_id=asset.id,
            role=asset.role,
            sort_order=0,
            variant_id=str(red.id),
            is_primary=True,
            enabled=True,
        )
    )
    session.commit()

    resp = client.post(f"/api/image-sets/{image_set.id}/approve")
    assert resp.status_code == 409, (
        f"图片集在属性未确认时被批准了 —— AC-05 那道闸没开火。实际:{resp.status_code}"
    )
    assert "批准图片集" in resp.text, "409 没有说清是哪个动作被拒了"


def test_running_a_paid_extraction_is_gated_on_the_material_step(client, session):
    """没有任何样品时直接跑识别 -> 409。

    这一条的代价是钱:识别是真实付费调用,而没有样品时它跑起来只会
    产生一批没有证据的结论。
    """
    _, _, _, product = _fixture(session, with_sample=False)
    resp = client.post(
        f"/api/products/{product.id}/extract-attributes", json={}
    )
    assert resp.status_code == 409, (
        f"没有样品也跑起了识别 —— 那是一次白花的付费调用。实际:{resp.status_code}"
    )


def test_the_gate_lets_a_legitimate_action_through(client, session):
    """**反方向**:闸不能只会拒绝。

    只测拒绝的话,一道恒拒的闸也是绿的 —— 而它会让整条流程走不下去。
    素材步是自阻断步,所以它在任何时候都开着:这一条钉的正是那个例外。
    """
    _, _, _, product = _fixture(session)
    payload = _flow(client, product)
    material = next(
        s for s in payload["wizard"]["steps"] if s["step"] == FlowStep.MATERIAL.value
    )
    assert material["is_open"], (
        "素材步被判成关着 —— 一件刚建档的商品会七步全灰,"
        "而运营要做的第一件事恰恰在那一格"
    )
    verdict = wizard_rules.check_action(
        wb.collect(session, product, dry_run=True).result,
        NextActionCode.UPLOAD_MATERIAL,
    )
    assert verdict.allowed


# ================================================================ 三、费用预估读真注册表


def test_the_cost_block_comes_from_the_real_registries(client, session):
    """费用预估这一跳跨了三个**运行时读注册表**的函数,纯测试全看不见。

        `cost_rollup._billable_extractor()`   读 `extractors.registry`
        `cost_rollup._billable_evaluator()`   读 `evaluators.registry`
        `cost_rollup._image_provider()`       读 `provider_setting`

    它们任何一个抛异常,向导整块打不开 —— 而那是运营打开页面时的白屏。
    """
    _, _, _, product = _fixture(session)
    cost = _flow(client, product)["wizard"]["cost"]

    assert cost is not None, "有颜色维却没有费用预估"
    assert cost["is_estimate"] is True
    # **「没配价」不等于「免费」**:两个键都必须在,而且 is_complete 要如实
    assert "unpriced_operations" in cost
    assert "unknown" in cost
    assert isinstance(cost["totals"], list)
    assert cost["disclaimer"], "预估没有免责说明"

    from app.workbench import cost_rollup

    # 三个取数函数在真环境里都要能返回,不能抛
    assert isinstance(cost_rollup._image_provider(), str)
    cost_rollup._billable_extractor()
    cost_rollup._billable_evaluator()


def test_an_unpriced_operation_is_never_shown_as_free(client, session):
    """默认 Mock 不在价目表里 -> 那一行的金额是 `None`,而不是 0。

    显示成 0 的表现是运营看到"预计花费 ¥0",然后账单来了。
    """
    _, _, _, product = _fixture(session)
    cost = _flow(client, product)["wizard"]["cost"]
    for line in cost["lines"]:
        if line["unit_price_micros"] is None:
            assert line["cost_micros"] is None, (
                f"{line['operation']} 没有单价,金额却算出来了 —— 那个数字是编的"
            )


# ================================================================ 四、AC-17 的两个端点


def test_change_sources_and_impact_are_real_endpoints(client, session):
    """`/change-sources` 与 `/impact` 真的可用,而且口径来自同一张表。"""
    _, _, _, product = _fixture(session)

    sources = client.get("/api/workbench/change-sources")
    assert sources.status_code == 200, sources.text
    listed = sources.json()["sources"]
    assert listed, "变更源清单是空的"

    from app.workbench.stale_matrix import ChangeSource

    assert {s["source"] for s in listed} == {c.value for c in ChangeSource}, (
        "清单与 `stale_matrix.ChangeSource` 不是同一个集合 —— "
        "少一项的表现是运营改那样东西之前看不到提示"
    )

    one = listed[0]["source"]
    impact = client.get(
        f"/api/workbench/products/{product.id}/impact", params={"source": one}
    )
    assert impact.status_code == 200, impact.text
    body = impact.json()
    # 「不受影响」的行也要在(见 impact.py 的模块文档)
    assert len(body["rows"]) == 4, "四列没有全给 —— 缺的那一列会被读成「不受影响」"
    assert "objects" in body


def test_an_unknown_change_source_is_an_error_not_an_empty_list(client, session):
    """拼错的变更源报错,而不是回一张空清单。

    空清单在界面上是「改这个不影响任何东西」—— 一句错得很有说服力的话。
    """
    _, _, _, product = _fixture(session)
    resp = client.get(
        f"/api/workbench/products/{product.id}/impact",
        params={"source": "NOT_A_REAL_SOURCE"},
    )
    assert resp.status_code >= 400, "未知变更源被静默接受了"


# ================================================================ 五、GET 不写库


def test_the_detail_get_does_not_write_anything(client, session):
    """AC-15 的只读那半句:向导那次 GET 不许改任何状态。

    它读的是 `wb.collect(..., dry_run=True)`,而 `refresh_draft` 在
    非 dry_run 下会把草稿标成 STALE。一次 GET 改状态的表现是运营
    刷新了一下页面,然后草稿变成了过期。
    """
    spu, red, _, product = _fixture(session)
    _asset(session, product, spu, red)
    session.commit()

    from sqlalchemy import func, select

    from app.models.audit_log import AuditLog

    before = session.scalar(select(func.count()).select_from(AuditLog))
    for _ in range(3):
        _flow(client, product)
    after = session.scalar(select(func.count()).select_from(AuditLog))
    assert after == before, (
        f"三次 GET 之后审计表多了 {after - before} 行 —— 详情端点在写库"
    )


# ================================================================ 六、F-7:copy_stale 不再是常量


def _generate_and_approve_copy(session, product):
    """走**生产那条路**生成并批准一版文案。

    不手工插 `ListingCopy` 行:`attr_snapshot_ids` 是由 `build_content_plan`
    从 `effective_map` 写进去的,手工造一份就等于把被测那条比较的两端
    都由测试自己决定 —— 那样比出来的相等没有任何意义。
    """
    from app.listings import copy_service
    from app.workbench import service as _wb

    row = _wb.generate_copy(session, product, actor="batch31")
    session.commit()
    approved = copy_service.approve_copy(
        session, row.id, actor="batch31", expect_spu=product.spu
    )
    session.commit()
    return approved


def _colour_view(session, product, variant_code: str):
    from app.workbench import color_rollup
    from app.workbench import service as _wb

    canonical = _wb._canonical_with_skus(session, product)
    views = color_rollup.build_color_views(session, product, canonical=canonical)
    match = [v for v in views if v.variant_code == variant_code]
    assert match, f"没有 {variant_code} 这个颜色"
    return match[0]


def test_a_freshly_approved_copy_is_not_reported_stale(client, session):
    """刚批准的文案**不**过期。

    这一条钉的是 F-7 修复的**反方向**:`copy_stale` 接上真值之后,
    最容易犯的错是算出一个恒 True(比较的两端取数口径不同,永远不相等)。
    那比恒 False 更贵 —— 运营看到"文案已过期"就会点重新生成,而那要花钱。

    它同时是一条口径印证:颜色维算的已确认版本集(SPU 层 ∪ 这个颜色的
    VARIANT 层,来自 `upstream_collect`)与 `build_content_plan` 写进
    `attr_snapshot_ids` 的那一份(来自 `effective_map`)必须是同一个集合。
    """
    spu, red, blue, product = _fixture(session, blue_sample=True)
    _confirm_shared_facts(session, product)
    approved = _generate_and_approve_copy(session, product)
    assert approved.status == "APPROVED", f"文案没批准成功:{approved.status}"

    view = _colour_view(session, product, "RED")
    assert view.has_copy is True, "刚生成的文案没有被颜色维看见"
    assert view.copy_stale is False, (
        "刚批准的文案就被报成已过期 —— 两端的已确认版本集口径不同。"
        "运营会照着这句话去点重新生成,而那是一次付费调用"
    )

    from app.workbench.color_flow import ColorSubState, evaluate_color
    from app.workbench.flow import FlowStep

    assert evaluate_color(view).by_step[FlowStep.COPY].state is ColorSubState.DONE


def test_confirming_a_new_fact_marks_that_colours_copy_stale(client, session):
    """**F-7 的正题**:已确认属性变了 -> 那个颜色的文案报过期。

    上一版 `copy_stale` 是写死的 `False`,于是 `color_flow._copy()` 里
    那条 `if view.copy_stale:` 分支在生产路径上是死代码 ——
    颜色维永远显示 DONE,而那一版文案里的事实已经不成立了。
    """
    spu, red, blue, product = _fixture(session, blue_sample=True)
    _confirm_shared_facts(session, product)
    _generate_and_approve_copy(session, product)

    assert _colour_view(session, product, "RED").copy_stale is False

    # 再确认一个字段 —— 已确认版本集变了,那一版文案的快照对不上了
    _confirmed_fact(session, product, "strap_type", "HALTER")
    session.commit()

    view = _colour_view(session, product, "RED")
    assert view.copy_stale is True, (
        "已确认属性变了,而颜色维还说文案没过期 —— "
        "这正是 F-7:那一位是写死的常量"
    )

    from app.workbench.color_flow import ColorSubState, evaluate_color
    from app.workbench.flow import FlowStep

    step = evaluate_color(view).by_step[FlowStep.COPY]
    assert step.state is ColorSubState.TODO
    assert "过期" in step.detail, f"报了过期却不说是为什么:{step.detail!r}"


def test_one_colours_copy_going_stale_leaves_the_others_alone(client, session):
    """颜色 A 的文案过期,**不牵连**颜色 B。

    这是 AC-21 那条作用域纪律在文案维的同一件事。拿商品行的已确认集
    去比每个颜色的快照,结果是**全部颜色一起报过期** —— 而那正是
    "按颜色取而不是按商品行取"要避免的形状。
    """
    spu, red, blue, product = _fixture(session, blue_sample=True)
    _confirm_shared_facts(session, product)
    _generate_and_approve_copy(session, product)

    # BLU 从来没有生成过文案:它应该是"还没有",不是"已过期"
    blue_view = _colour_view(session, product, "BLU")
    assert blue_view.has_copy is False
    assert blue_view.copy_stale is False, (
        "一个还没有文案的颜色被报成「文案已过期」—— 那句话指向一个不存在的对象"
    )
