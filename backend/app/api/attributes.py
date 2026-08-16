"""属性识别接口(M3,§6.6)。

    POST /api/products/{id}/extract-attributes   触发识别(可指定只识别新增素材)
    GET  /api/products/{id}/attributes           当前采信值 + 两个置信度 + 待确认项
    POST /api/products/{id}/attributes/confirm   批量确认建议值 / 解决冲突
    GET  /api/extractions/{id}                   单次识别详情:逐图证据

注意 `GET /attributes` 返回的每一项都带 `confidence_breakdown`。
那不是调试信息 —— 运营需要知道「0.71 是因为只有一张图」还是「因为图太糊」,
这两种情况他要做的事完全不同(补一张图 vs 重拍)。
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api import action_gate
from app.api.deps import current_actor, db_session, require_operator
from app.attributes import missing_summary, run_state
from app.attributes import service as attr_service
from app.attributes.registry import field_spec_out, fields_for
from app.attributes.validation import AttributeValueError
from app.core import audience as audience_rules
from app.core.errors import ErrorCode, NotFoundError, ValidationError
from app.extractors.registry import get_extractor
from app.models.product import Product
from app.schemas.attribute import (
    AttributeValueOut,
    ConfirmRequest,
    EvidenceOut,
    ExtractionDetailOut,
    ExtractionOut,
    ExtractRequest,
    FieldSpecOut,
    UnresolvedMissingOut,
)
from app.services import dispatch_service, spu_service

router = APIRouter(tags=["attributes"], dependencies=[Depends(require_operator)])


def _out(row, *, stale: frozenset[str]) -> AttributeValueOut:
    """属性行 + 它的字段类型(BLOCK-11)+ 它过没过期(§5.3)。

    **两个返回属性值的端点都必须走这里。** 只在 list 上带 spec、confirm 上
    不带的话,前端在确认之后会拿到一条没有类型的行,于是那一行的编辑控件
    退化回纯文本 —— 缺陷会以"确认过一次之后才复现"的形状回来,
    而那是最难被测出来的形状。

    ## `stale` 是必传的,没有默认值

    同一个理由的第二次应用。给它一个默认值(无论 True 还是 False)的话,
    第三个端点写出来时会**忘记传**而不报错:默认 False 让全库事实显示正常,
    默认 True 让全库事实显示过期,两个方向都是一句没人算过的话。
    必传的话,忘记传是一个 TypeError,在写的那一刻就红。

    传集合而不是布尔:调用方一次拿到多行,逐行算一次等于逐行取一次数
    (`stale_fields` 内部要查素材),而**两次取数之间素材可能变** ——
    同一个响应里的几行分属不同的输入快照,一个查不出来的不一致。
    """
    out = AttributeValueOut.model_validate(row)
    out.spec = FieldSpecOut(**field_spec_out(row.field_name))
    out.facts_stale = row.field_name in stale
    return out


def _product(session: Session, product_id: UUID) -> Product:
    product = session.get(Product, product_id)
    if product is None:
        raise NotFoundError("商品不存在")
    return product


def _extraction_out(row) -> ExtractionOut:
    out = ExtractionOut.model_validate(row)
    out.can_cancel = run_state.can_cancel(row.status)
    out.request_disposition = getattr(row, "_request_disposition", None)
    return out


def _placeholder_out(field_name: str) -> AttributeValueOut:
    """注册表字段尚无事实行时，仍给属性页一个可人工填写的入口。"""
    return AttributeValueOut(
        field_name=field_name,
        source="NONE",
        status="MISSING",
        is_placeholder=True,
        spec=FieldSpecOut(**field_spec_out(field_name)),
        facts_stale=False,
    )


def _dispatch_if_queued(row) -> None:
    if row.status == "QUEUED":
        dispatch_service.send_attribute_extraction(row.id)


@router.post("/products/{product_id}/extract-attributes", response_model=ExtractionOut)
def extract_attributes(
    product_id: UUID,
    payload: ExtractRequest,
    request: Request,
    session: Session = Depends(db_session),
) -> ExtractionOut:
    """兼容 SKU 入口：只排队并立即返回，执行由 Celery 完成。"""
    product = _product(session, product_id)
    # AC-05(F-2):识别属于属性步,前置是素材就绪。绕过它直接 POST 的表现是
    # 一次真实付费调用跑在一个连样品都没有的商品上 —— 而向导上那个按钮是灰的
    action_gate.ensure_allowed(
        session, product, action_gate.NextActionCode.RUN_EXTRACTION
    )
    extraction = attr_service.queue_extraction(
        session,
        product=product,
        extractor=get_extractor(),
        fields=tuple(payload.fields) if payload.fields else None,
        only_media_ids=payload.media_asset_ids,
        # §11 第一行的另一半:上一次 run 的 `retry_scope` 算出了该重跑哪几个
        # 颜色,这里是它回来的那一跳。在此之前那个答案回给了调用方,
        # 而调用方没有任何入参能表达它 —— 只能人工挑图 id,而挑漏一张的
        # 表现是那个颜色重试完仍然缺证据,于是再付一次钱重试一次
        only_variant_ids=payload.color_variant_ids,
        force_rerun=payload.force_rerun,
        actor=current_actor(request),
    )
    session.commit()
    _dispatch_if_queued(extraction)
    return _extraction_out(extraction)


@router.post(
    "/spus/{spu_id}/attribute-extraction-runs", response_model=ExtractionOut
)
def create_spu_extraction_run(
    spu_id: UUID,
    payload: ExtractRequest,
    request: Request,
    session: Session = Depends(db_session),
) -> ExtractionOut:
    """PRD §12.3 的 SPU 级异步入口。输入取整个 SPU 的证据素材快照。"""
    spu_service.get_spu(session, spu_id)
    products = spu_service.skus_of(session, spu_id)
    if not products:
        raise ValidationError(
            "SPU 还没有 SKU，无法创建识别 run",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )
    action_gate.ensure_allowed_for_spu(
        session,
        action_gate.NextActionCode.RUN_EXTRACTION,
        spu_id=spu_id,
    )
    extraction = attr_service.queue_extraction(
        session,
        product=products[0],
        extractor=get_extractor(),
        fields=tuple(payload.fields) if payload.fields else None,
        only_media_ids=payload.media_asset_ids,
        only_variant_ids=payload.color_variant_ids,
        spu_scope_id=spu_id,
        force_rerun=payload.force_rerun,
        actor=current_actor(request),
    )
    session.commit()
    _dispatch_if_queued(extraction)
    return _extraction_out(extraction)


@router.get("/products/{product_id}/attributes", response_model=list[AttributeValueOut])
def list_attributes(
    product_id: UUID, session: Session = Depends(db_session)
) -> list[AttributeValueOut]:
    product = _product(session, product_id)
    # 带 `product=` 才读得到分层后的值(SPU / VARIANT / SKU 三个 owner),
    # 不带只读 legacy 那一处。原来这里不带 —— 而作用域判定要 owner,
    # 拿 legacy 行去问"该比哪个颜色的指纹"会一律得到共享作用域,
    # 于是颜色事实的过期判断静静地退化成 SPU 级(D1 从后门回来)
    values = attr_service.effective_map(session, product_id, product=product)
    # 全都看,不只看已确认的(`settled_only=False`)。接口回答的是
    # "库里这一行的指纹对不对得上",而工作台问的是"该不该催人" ——
    # 后者才需要只看已确认的那一档,理由在 `stale_fields` 的文档里
    stale = attr_service.stale_fields(session, product, values, settled_only=False)
    # 注册表的 `fields_for()` 明确是属性页的列清单。旧实现只遍历 values，
    # 结果恰好把最需要人工填写的字段（material 等 visual=False 字段）藏掉：
    # 没值 -> 没行 -> 没输入框，而阻断提示却仍叫人“人工填写”。
    ordered = fields_for(audience_rules.coerce(product.audience))
    result = [
        _out(values[name], stale=stale) if name in values else _placeholder_out(name)
        for name in ordered
    ]
    # 受众变更前留下的历史事实不能静默消失；不再适用的行排在适用字段之后。
    result.extend(
        _out(values[name], stale=stale)
        for name in sorted(set(values) - set(ordered))
    )
    return result


@router.post(
    "/products/{product_id}/attributes/confirm", response_model=list[AttributeValueOut]
)
def confirm_attributes(
    product_id: UUID,
    payload: ConfirmRequest,
    request: Request,
    session: Session = Depends(db_session),
) -> list[AttributeValueOut]:
    """人工确认。写入 `MANUAL`,**从此不被任何自动流程覆盖**。"""
    product = _product(session, product_id)
    # AC-05(F-2):确认属性属于属性步。人工填写与解决冲突走的也是这个端点,
    # 三者同一道闸 —— 它们在 `ACTION_STEP` 里同属 ATTRIBUTE
    action_gate.ensure_allowed(
        session, product, action_gate.NextActionCode.CONFIRM_ATTRIBUTES
    )
    actor = current_actor(request)
    written = []
    for item in payload.items:
        try:
            written.append(
                attr_service.confirm(
                    session,
                    product=product,
                    field_name=item.field_name,
                    value=item.value,
                    actor=actor,
                )
            )
        except AttributeValueError as exc:
            # 422 而不是 400(A43 / BLOCK-03):请求语法没问题,是**语义**不合契约。
            # 整批回滚而不是写一半:一次确认里 3 个字段,第 2 个类型错时
            # 只写进去第 1 个,页面刷新后显示的是一个谁也没打算要的中间状态。
            #
            # 指名字段:一次可以提交多个,只说"格式不对"等于让人挨个试
            session.rollback()
            raise ValidationError(
                f"属性值不合字段定义 —— {exc}",
                code=ErrorCode.INPUT_INVALID,
                http_status=422,
                detail={"field_name": exc.field_name, "reason": exc.reason},
            ) from exc
    # **确认完照样算一次,不假设"刚写的一定不过期"。**
    #
    # 多数情况下它确实是 False。但有一种情况不是:这个字段落在一个今天
    # 没有任何证据素材的颜色上 —— `fingerprint_for_owner` 查不到那个键,
    # 存进去的就是 NULL,而 NULL 判过期。硬写 False 的话,界面会说
    # "已确认",刷新之后工作台说"样品已变",两句话都来自同一次动作。
    stale = attr_service.stale_fields(
        session,
        product,
        {row.field_name: row for row in written},
        settled_only=False,
    )
    session.commit()
    return [_out(v, stale=stale) for v in written]


@router.get("/extractions/{extraction_id}", response_model=ExtractionDetailOut)
@router.get(
    "/attribute-extraction-runs/{extraction_id}", response_model=ExtractionDetailOut
)
def get_extraction(
    extraction_id: UUID, session: Session = Depends(db_session)
) -> ExtractionDetailOut:
    """单次识别详情:逐图证据。

    证据里带 `normalized_value` 为 None 的项 —— 那是模型自造了一个
    枚举值、被归一化拦下来的。**要显示出来**:它是提示词需要改进的信号。
    """
    row = attr_service.get_extraction(session, extraction_id)
    detail = ExtractionDetailOut.model_validate(row)
    detail.can_cancel = run_state.can_cancel(row.status)
    evidence = attr_service.list_evidence(session, row.id)
    detail.evidence = [EvidenceOut.model_validate(e) for e in evidence]
    # RUNNING 时后续图片还可能给出值,不能把中间快照宣布成最终缺失。
    detail.unresolved_missing = (
        []
        if detail.can_cancel
        else [
            UnresolvedMissingOut(**item.as_dict())
            for item in missing_summary.unresolved_missing(evidence)
        ]
    )
    return detail


@router.post(
    "/attribute-extraction-runs/{extraction_id}/cancel",
    response_model=ExtractionOut,
)
def cancel_extraction(
    extraction_id: UUID,
    request: Request,
    session: Session = Depends(db_session),
) -> ExtractionOut:
    row = attr_service.request_cancellation(
        session, extraction_id, actor=current_actor(request)
    )
    session.commit()
    return _extraction_out(row)


@router.get("/attributes/calibration")
def calibration_status(session: Session = Depends(db_session)) -> dict:
    """各字段的校准状态与当前生效阈值(§6.6,运维可见)。

    没有这一页的话,「为什么所有属性都是 CANDIDATE」这个问题
    只能靠翻代码回答。而答案通常是「校准数据还没灌」——
    那是一个运维动作,不是 bug。
    """
    from sqlalchemy import select as _select

    from app.attributes.calibration import evaluate_readiness
    from app.attributes.registry import REGISTRY
    from app.models.attribute import AttributeCalibration

    rows = list(session.scalars(_select(AttributeCalibration)))
    grouped: dict[tuple[str, str, str], list] = {}
    for r in rows:
        grouped.setdefault((r.field_name, r.model_name, r.prompt_version), []).append(r)

    from app.attributes.confidence import CalibrationBin

    combos = []
    for (field_name, model_name, prompt_version), items in sorted(grouped.items()):
        bins = [
            CalibrationBin(
                bin_lower=float(i.bin_lower),
                bin_upper=float(i.bin_upper),
                observed_accuracy=float(i.observed_accuracy),
                sample_count=i.sample_count,
                calibration_version=i.calibration_version,
            )
            for i in items
        ]
        status = evaluate_readiness(
            field_name=field_name,
            model_name=model_name,
            prompt_version=prompt_version,
            bins=bins,
        )
        combos.append(
            {
                "field_name": status.field_name,
                "model_name": status.model_name,
                "prompt_version": status.prompt_version,
                "total_samples": status.total_samples,
                "usable_bins": status.usable_bins,
                "thin_bins": status.thin_bins,
                "ready": status.ready,
                "reason": status.reason,
            }
        )

    return {
        "combinations": combos,
        # 当前生效的阈值。运维要能对照着看"这个字段要多高才自动确认"
        "thresholds": {
            name: spec.auto_confirm_threshold for name, spec in REGISTRY.items()
        },
        "note": "没有任何组合 ready 时,全部字段只出建议 —— 这是 fail closed,不是故障",
    }


@router.get("/attributes/metrics")
def attribute_metrics(
    session: Session = Depends(db_session),
    # 原来无下界,负数会被服务层内部修正成 1,但响应里仍然回显负数
    # (报告 6.6)—— 于是"最近 -5 天的自动确认率"是一个自洽的荒谬答案。
    # 在入参层挡住,比在响应里解释清楚更省事
    days: int = Query(30, ge=1, le=365),
) -> dict:
    """自动确认率与人工推翻率(M4 出口条件)。

    **推翻率 > 2% 就要收紧阈值**(§6.5)。返回值里带 `overturn_alert`,
    不让读的人自己记这个判据。
    """
    from app.attributes import service as _svc

    return _svc.confirmation_metrics(session, days=days)


@router.get("/attributes/projection-check")
def projection_check(
    session: Session = Depends(db_session),
    limit: int = Query(200, ge=1, le=2000),
) -> dict:
    """事实源与 `products` 投影列的一致性巡检(§5.4 第二条防线)。

    只报告,**不自动修复** —— 和素材层的双读对账同一个理由:
    自动改数据会掩盖"为什么会不一致"这个问题本身。

    ## `checked_products` 是真的检查了几件(报告 6.6)

    原来它直接返回 `limit`。库里只有 30 件商品时,这个接口会说
    "检查了 200 件,0 处不一致" —— 一份看起来覆盖全库的巡检报告,
    而它的分母是假的。巡检报告的全部价值就在于那个分母可信。

    `limit` 也补上了上下界:原来无界,一个 `limit=99999999` 会把全库
    拉进内存逐件比对。
    """
    from app.attributes import service as _svc

    rows, checked = _svc.projection_mismatches(session, limit=limit)
    return {
        "mismatches": rows,
        "count": len(rows),
        "checked_products": checked,
        # 检查到上限就说出来,否则"0 处不一致"会被读成"全库干净"
        "truncated": checked >= limit,
    }
