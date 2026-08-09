"""建档 REST 接口(PRD v3.1 §12.1)。

三个端点,对应建档向导第一步的三次交互:

    GET  /spus/size-templates   表单第二步的下拉项 —— **前端不许自己内置一份**
    POST /spus                  一次落 SPU + 颜色 + SKU 行(同一个事务)
    GET  /spus/{id}             建完之后回读,含展开出来的 SKU

`GET /spus` 列表也在这里,但它只是给联调和后续列表页留的入口 ——
建档向导本身用不到。
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.orm import Session

from app.api.deps import current_actor, db_session, require_operator
from app.listings.sku_matrix import SIZE_TEMPLATE_LABELS
from app.schemas.common import Page
from app.schemas.spu import (
    ColorVariantAdd,
    ColorVariantOut,
    ColorVariantPatch,
    SizeTemplateOut,
    SkuBatchCreate,
    SpuCreate,
    SpuDetailOut,
    SpuOut,
    SpuPatch,
    SpuSkuOut,
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
) -> Page[SpuOut]:
    rows = spu_service.list_spus(
        session, limit=page_size, offset=(page - 1) * page_size
    )
    # 一次分组查询数完这一页,不在下面的推导式里按行查 —— 那是 A-06 那条
    # 「聚合的循环里不许有库读」的同一个形状,只是换了一个端点
    counts = spu_service.sku_counts_for(session, [row.id for row in rows])
    return Page[SpuOut](
        items=[_to_out(row, counts.get(row.id, 0)) for row in rows],
        total=spu_service.count_spus(session),
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
