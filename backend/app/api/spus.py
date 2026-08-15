"""建档 REST 接口(PRD v3.1 §12.1)。

对应建档向导第一步的几次交互:

    GET  /spus/size-templates   表单第二步的下拉项 —— **前端不许自己内置一份**
    GET  /spus/code-rules       编码规则(字符集、分隔符、各段上限)—— 同上
    POST /spus/preview          试算:同一份规则,提交之前跑一次,不写库
    POST /spus                  一次落 SPU + 颜色 + SKU 行(同一个事务)
    GET  /spus/{id}             建完之后回读,含展开出来的 SKU

`GET /spus` 列表也在这里,但它只是给联调和后续列表页留的入口 ——
建档向导本身用不到。

## 试算与规则这两个端点是一起加的,补的是同一段缺口

编码字符集、重复颜色、行数上限、编码已存在这四类失败,在此之前**全部**
只在提交那一刻报得出来 —— 而错在表单第二步的输入框上。前端不能抄一份
规则过去(抄了就有两个版本,先过期的那一个会让合法输入在界面上被拒),
所以补的方向是**把同一份规则提前问一次**,而不是复制它。

规则端点只喂提示文案与输入归一化,判定仍然只有 `listings/sku_matrix` 一处。

## 停用不是删除

`POST /spus/{id}/disable` 是本系统里「删除一个款」的全部含义,理由
(九张外键表、RESTRICT 与 CASCADE 各自的后果)写在
`spu_service.disable_spu` 的 docstring 里。全仓只有一个 `DELETE` 路由,
在生成任务上。
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.orm import Session

from app.api.deps import current_actor, db_session, require_operator
from app.listings.sku_matrix import SIZE_TEMPLATE_LABELS
from app.schemas.common import Page
from app.schemas.spu import (
    CodeRulesOut,
    ColorVariantAdd,
    ColorVariantOut,
    ColorVariantPatch,
    SizeTemplateOut,
    SkuBatchCreate,
    SpuCreate,
    SpuDetailOut,
    SpuDisableIn,
    SpuOut,
    SpuPatch,
    SpuPreviewIn,
    SpuPreviewOut,
    SpuSkuOut,
    code_rules_out,
    size_template_options,
)
from app.services import spu_service

# 与 products 一致:整个路由挂 require_operator,读接口也要
# (`deps.require_operator` 顶部解释了为什么读也要 —— "不改状态"和
#  "可以给任何人看"是两件事)
router = APIRouter(
    prefix="/spus",
    tags=["spus"],
    dependencies=[Depends(require_operator)],
)


def _to_out(spu, sku_count: int) -> SpuOut:
    """**`sku_count` 没有默认值,这是故意的。**

    上一版签名是 `sku_count: int = 0`,而列表路径一个参数都没传 —— 于是
    `GET /spus` 里每一个 SPU 都报 0 个 SKU,详情页却是对的。硬规则 4 点名
    的就是这个形状:为了让接口形状完整而填的常量,不是从真实来源推出来的。

    去掉默认值之后,新增一条列表路径而忘了数 SKU 会是 TypeError,
    不再是一个安静的 0。
    """
    out = SpuOut.model_validate(spu)
    out.sku_count = sku_count
    return out


def _to_detail(spu, skus) -> SpuDetailOut:
    out = SpuDetailOut.model_validate(spu)
    out.sku_count = len(skus)
    out.skus = [SpuSkuOut.model_validate(row) for row in skus]
    return out


# 这条路由必须排在 `/{spu_id}` **前面**。反过来的话 FastAPI 会先匹配到
# 动态段,拿 "size-templates" 去 parse UUID,于是一个静态端点变成 422
@router.get("/size-templates", response_model=list[SizeTemplateOut])
def list_size_templates() -> list[SizeTemplateOut]:
    """可选尺码模板。

    放在后端是硬规则 4 的同一条道理:前端内置一份的话,加一个模板要改两个仓库,
    而漏改的那一侧不会报错 —— 它只会少一个选项。
    """
    return size_template_options(SIZE_TEMPLATE_LABELS)


# 与上面那条同理由:静态段必须排在 `/{spu_id}` 前面
@router.get("/code-rules", response_model=CodeRulesOut)
def get_code_rules() -> CodeRulesOut:
    """编码规则:字符集、分隔符、各段长度上限、颜色与行数上限。

    给建档表单显示提示、并在失焦时做与后端**同一个**归一化(去空白 + 转大写)。
    它**不是校验器** —— 判定走 `POST /spus/preview`,那条调的是
    `listings/sku_matrix` 本身。

    放在后端是硬规则 4 的同一条:前端内置一份的话,改一次上限要改两个仓库,
    而漏改的那一侧不报错 —— 它只会安静地按旧数字提示,然后运营照着提示填,
    被后端拒。
    """
    return code_rules_out()


@router.post("/preview", response_model=SpuPreviewOut)
def preview_spu(
    payload: SpuPreviewIn,
    session: Session = Depends(db_session),
) -> SpuPreviewOut:
    """建档试算。**一个字都不写库,也不需要 `Idempotency-Key`。**

    POST 只是因为入参(一整张颜色表)放不进 query string —— 与
    `generation_plans:preview_activation` / `workbench_batch:plan_batch`
    同一类,它们在 `action_gate.UNGATED_ENDPOINTS` 里的理由也是同一句。

    非法入参走 **422,与建档同一条错误路径**(`spu_service._translate`),
    于是试算报的 `loc` 与提交报的 `loc` 逐字相同,前端一套高亮两处都能用。
    做成 `{ok: false, problems: [...]}` 的话,同一件事就有了两种说法。
    """
    return SpuPreviewOut.model_validate(
        spu_service.preview_spu(session, payload.model_dump())
    )


@router.post("", response_model=SpuDetailOut, status_code=status.HTTP_201_CREATED)
def create_spu(
    payload: SpuCreate,
    request: Request,
    session: Session = Depends(db_session),
) -> SpuDetailOut:
    """建档。

    ## `Idempotency-Key`(PRD §9.1 / §12.1)

    读**头**而不是读 body:这是 HTTP 层的语义(RFC draft 的
    `Idempotency-Key`),与 `api/workbench_batch.py` 那条同源 ——
    同一件事在两个端点上一个读头一个读 body,调用方会以为它们是两回事。

    带键时:同键同入参返回原来那个 SPU(不建第二个),同键不同入参 409。
    不带键时行为与从前一字不差。

    **不带键的建档仍然可能双击。** 那时兜底的是 `uq_spus_spu_code`,
    而它给出的错误是「SPU 编码 X 已存在」—— 在双击这个语境下那句话是假的。
    所以前端应当带键;这里不强制,是因为 CSV 导入与联调脚本没有键也该能用。
    """
    spu = spu_service.create_spu(
        session,
        payload.model_dump(),
        actor=current_actor(request),
        request_key=request.headers.get("Idempotency-Key"),
    )
    skus = spu_service.skus_of(session, spu.id)
    # **这一行不能删。** 事务归调用方所有,而 `deps.get_session` 明确不提交
    # (§7.8 禁止请求级自动 commit 与 Service commit 混用)。删掉它建档会
    # 静默地什么都不落库,而测试不会红 —— conftest 的 session 夹具跑在外层
    # 事务里,同一个 session 内看不出提交与否。守在
    # `test_a45_batch13_2_fixes.py::test_the_archiving_route_commits_because_nothing_else_will`
    session.commit()
    return _to_detail(spu, skus)


@router.get("", response_model=Page[SpuOut])
def list_spus(
    session: Session = Depends(db_session),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    #: 显示已停用的款。默认不显示,与工作台列表的 `include_archived` 同一口径。
    #: **对存量数据是恒等的** —— 停用端点存在之前没有任何路径写得出 DISABLED
    include_disabled: bool = Query(False),
) -> Page[SpuOut]:
    rows = spu_service.list_spus(
        session,
        limit=page_size,
        offset=(page - 1) * page_size,
        include_disabled=include_disabled,
    )
    # 一次分组查询数完这一页,不在下面的推导式里按行查 —— 那是 A-06 那条
    # 「聚合的循环里不许有库读」的同一个形状,只是换了一个端点
    counts = spu_service.sku_counts_for(session, [row.id for row in rows])
    return Page[SpuOut](
        items=[_to_out(row, counts.get(row.id, 0)) for row in rows],
        # 总数与列表必须用同一个过滤条件,否则最后一页是空的而空态
        # 写着"还没有商品"
        total=spu_service.count_spus(session, include_disabled=include_disabled),
        page=page,
        page_size=page_size,
    )


@router.get("/{spu_id}", response_model=SpuDetailOut)
def get_spu(
    spu_id: UUID,
    session: Session = Depends(db_session),
) -> SpuDetailOut:
    spu = spu_service.get_spu(session, spu_id)
    return _to_detail(spu, spu_service.skus_of(session, spu.id))


@router.patch("/{spu_id}", response_model=SpuDetailOut)
def update_spu(
    spu_id: UUID,
    payload: SpuPatch,
    request: Request,
    session: Session = Depends(db_session),
) -> SpuDetailOut:
    spu = spu_service.update_spu(
        session,
        spu_id,
        payload.model_dump(exclude_unset=True),
        actor=current_actor(request),
    )
    session.commit()
    return _to_detail(spu, spu_service.skus_of(session, spu.id))


@router.post("/{spu_id}/disable", response_model=SpuDetailOut)
def disable_spu(
    spu_id: UUID,
    payload: SpuDisableIn,
    request: Request,
    session: Session = Depends(db_session),
) -> SpuDetailOut:
    """停用一个款。**这是本系统里「删除 SPU」的全部含义。**

    没有 `DELETE /spus/{id}`,而且不该有 —— 理由(SPU 底下挂着颜色、
    SKU,而每一行 SKU 又被九张表引着)写在 `spu_service.disable_spu` 里。

    停用之后这个款从建档侧的列表消失(`GET /spus` 默认过滤),不再接受
    新的颜色与 SKU;**已经在生产动线上的那些 SKU 行不受影响** ——
    归档一行 SKU 是它自己的动作,有自己的闸和自己的理由。
    """
    spu = spu_service.disable_spu(
        session, spu_id, reason=payload.reason, actor=current_actor(request)
    )
    session.commit()
    return _to_detail(spu, spu_service.skus_of(session, spu.id))


@router.post("/{spu_id}/restore", response_model=SpuDetailOut)
def restore_spu(
    spu_id: UUID,
    request: Request,
    session: Session = Depends(db_session),
) -> SpuDetailOut:
    """把停用的款放回建档动线(恢复到 DRAFT,理由见服务层)。"""
    spu = spu_service.restore_spu(session, spu_id, actor=current_actor(request))
    session.commit()
    return _to_detail(spu, spu_service.skus_of(session, spu.id))


@router.post("/{spu_id}/color-variants", response_model=ColorVariantOut, status_code=201)
def add_color_variant(
    spu_id: UUID,
    payload: ColorVariantAdd,
    request: Request,
    session: Session = Depends(db_session),
) -> ColorVariantOut:
    row = spu_service.add_color_variant(
        session, spu_id, payload.model_dump(), actor=current_actor(request)
    )
    session.commit()
    return ColorVariantOut.model_validate(row)


@router.patch("/{spu_id}/color-variants/{variant_id}", response_model=ColorVariantOut)
def update_color_variant(
    spu_id: UUID,
    variant_id: UUID,
    payload: ColorVariantPatch,
    request: Request,
    session: Session = Depends(db_session),
) -> ColorVariantOut:
    row = spu_service.update_color_variant(
        session,
        spu_id,
        variant_id,
        payload.model_dump(exclude_unset=True),
        actor=current_actor(request),
    )
    session.commit()
    return ColorVariantOut.model_validate(row)


@router.post("/{spu_id}/skus:batch", response_model=list[SpuSkuOut], status_code=201)
def create_skus(
    spu_id: UUID,
    payload: SkuBatchCreate,
    request: Request,
    session: Session = Depends(db_session),
) -> list[SpuSkuOut]:
    rows = spu_service.create_skus(
        session,
        spu_id,
        [item.model_dump() for item in payload.items],
        actor=current_actor(request),
    )
    session.commit()
    return [SpuSkuOut.model_validate(row) for row in rows]
