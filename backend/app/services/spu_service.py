"""建档:一次请求落一个 SPU、若干颜色、以及它们展开出来的 SKU 行。

PRD v3.1 §6.1 步骤 1 与 §13 阶段 1 的第一条验收(「可构造三颜色九 SKU 的 SPU」)。

## 为什么三样东西必须在同一个事务里

分三次调用(建 SPU → 建颜色 → 铺 SKU)在接口上更"REST",但它把一个业务动作
拆成了三个可以各自失败的动作。第二步失败留下的是一个**没有颜色的 SPU**,
第三步失败留下的是**有颜色没有 SKU 的 SPU** —— 两种残骸在界面上都显示为
"建档中",而运营唯一的动作是再建一次,于是库里多一份残骸。

## 这里不 commit —— 但 `get_session` 也不 commit

事务归**调用方**所有(`services/publish_service.py` 顶部那段「事务边界」是
同一条规矩的详细版:「只 flush 不 commit,事务归调用方(API 或 use case)」)。
调用方在这里是路由:`api/spus.py` 的 `create_spu` 显式提交。

**不要把这件事记到 `api/deps.get_session` 头上。** 那个依赖的文档明写着它
不提交 —— §7.8 第一条禁止项就是请求级自动 commit 与 Service commit 混用,
那一行是上一轮专门摘掉的。照着"由依赖收口"去删掉路由里的 `session.commit()`,
建档会静默地什么都不落库,而**测试不会红**:`tests/conftest.py` 的 session
夹具跑在一个外层事务里(`join_transaction_mode="create_savepoint"`),
提交与否在同一个 session 内看不出差别。

## 视觉属性不在入参里 —— 这是阶段 1 的验收项之一

§13 阶段 1:「不填视觉属性即可建档」。`products` 上那 8 个投影列在建档接口上
**根本不存在**,不是"可选" —— 可选的字段会被前端填成空串,而空串和"还没识别"
在下游是两件事。它们的事实源是属性层,写入点只有
`attributes/service._project_to_legacy_column()` 一处(AST 守卫钉着)。
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core import audience as audience_rules
from app.core.enums import AuditAction, ProductStatus, SellableStatus, SpuStatus
from app.core.errors import (
    DuplicateError,
    ErrorCode,
    FieldProblem,
    NotFoundError,
    ValidationError,
)
from app.core.field_limits import limit_for
from app.listings import sku_matrix
from app.models.product import Product
from app.models.spu import ColorVariant, Spu
from app.services import audit

#: 纯层参数名 -> 接口字段名。**只有这一张表知道两边的对应关系。**
#:
#: `sku_matrix` 是零依赖的纯模块,它按**自己的形参**命名错误位置
#: (`expand(variant_codes=[...])` 抛的就是 `variant_codes[1]`)。
#: 那是对的:让纯层知道 HTTP 载荷长什么样,等于把接口形状漏进纯层。
#:
#: 但接口那一侧的字段叫 `color_variants`,而且每一项是个对象
#: (`{"variant_code": ..., "working_name": ...}`)。两边不做转换的话,
#: `loc` 指向一个表单里根本不存在的字段名 —— 前端拿它高亮不到任何一行,
#: 表现回到"只说了一句编码不合法"。
_LOC_PREFIX = {"variant_codes": "color_variants"}


def _api_loc(field: str) -> str:
    """把纯层的错误位置翻成前端能用的 `loc`。

    `variant_codes[1]` -> `color_variants[1].variant_code`
    `variant_codes`    -> `color_variants`
    其余(`spu_code` / `size_template`)两边同名,原样。
    """
    head, bracket, rest = field.partition("[")
    mapped = _LOC_PREFIX.get(head)
    if mapped is None:
        return field
    if not bracket:
        return mapped
    index = rest.rstrip("]")
    # 下钻到具体那一列:列表项是对象,只说第几行的话表单仍要在
    # variant_code / working_name 两个输入框之间猜
    return f"{mapped}[{index}].variant_code"


def _translate(error: sku_matrix.SkuPlanError) -> ValidationError:
    """展开规则的错误 → 接口错误。**保留 `field`**,前端要靠它高亮到具体那一行。

    ## 上一版这里的注释是错的(A45-batch14-6 修)

    它写着「`SkuPlanError.field` 是 `color_variants[0].variant_code` 这种
    点分路径 —— 它和 pydantic 的 `loc` 同源,所以原样进 `FieldProblem.loc`,
    **不做转换**」。

    而 `sku_matrix.expand` 抛的实际是 `variant_codes[1]`。也就是说
    "不做转换"这个决定建立在一个不成立的前提上:两边**从来就不同源**。
    结果 `loc` 指向一个表单里不存在的字段,前端高亮不到任何一行 ——
    正是这段注释自己说要避免的那件事。

    `test_an_illegal_variant_code_tells_the_form_which_row` 本来会当场抓住它,
    但那条用例属于"已写、一次都没跑过"的那批(见 STATUS.md),
    直到这台机器上第一次跑起真库 pytest 才露出来。
    """
    return ValidationError(
        error.message,
        code=ErrorCode.INPUT_INVALID,
        http_status=422,
        fields=[FieldProblem(loc=_api_loc(error.field), msg=error.message)],
    )


def get_spu(session: Session, spu_id: UUID) -> Spu:
    spu = session.get(Spu, spu_id)
    if spu is None:
        raise NotFoundError(f"SPU {spu_id} 不存在")
    return spu


def _code_taken(session: Session, spu_code: str) -> bool:
    return (
        session.scalar(select(Spu.id).where(Spu.spu_code == spu_code).limit(1))
        is not None
    )


def create_spu(session: Session, data: dict[str, Any], *, actor: str) -> Spu:
    """建档。返回建好的 SPU(颜色与 SKU 已经在 session 里 flush 过)。

    `data` 的形状见 `schemas/spu.SpuCreate`。这里再校验一次而不是信任 schema:
    展开规则(编码字符集、重复颜色、行数上限)住在 `listings/sku_matrix`,
    而 schema 只管字段类型 —— 把规则复制进 schema 会让它有两个版本,
    然后其中一个先过期。

    ## 受众在这里被读到,但不在这里被判定

    `audience_rules.coerce()` 负责把字符串变成枚举并对不认识的取值抛错。
    「这个受众配这个品类行不行」的判定仍然只有 `core/audience` 一处 ——
    §23.5 的那条规矩没有因为多了一张表而松动。
    """
    audience = audience_rules.coerce(data.get("audience"))
    if audience is None:
        # §4.2:SPU 层的受众**必填**,这里没有"待确认"这个状态。
        # 商品行上那个可空的 `audience` 是给旧路径留的,不是这里的先例
        raise ValidationError(
            "受众必填:它决定模特、提示词、槽位表、检查项、尺码表和平台类目,"
            "填错的话每一步单看都是'正常完成',错在最后才看得出来",
            code=ErrorCode.INPUT_INVALID,
            http_status=422,
        )

    try:
        spu_code = sku_matrix.normalize_spu_code(data.get("spu_code"))
        variants_in: list[dict[str, Any]] = list(data.get("color_variants") or [])
        planned = sku_matrix.expand(
            spu_code=spu_code,
            variant_codes=[v.get("variant_code", "") for v in variants_in],
            size_template=data.get("size_template", ""),
        )
    except sku_matrix.SkuPlanError as exc:
        raise _translate(exc) from None

    # 先查后插只挡得住"明显重复",挡不住并发 —— 真正的裁判是
    # `uq_spus_spu_code`。查询留作快速路径,好让错误信息说得清是哪个编码
    if _code_taken(session, spu_code):
        raise DuplicateError(f"SPU 编码 {spu_code} 已存在")

    spu = Spu(
        spu_code=spu_code,
        internal_name=(data.get("internal_name") or "").strip(),
        audience=audience.value,
        base_category=(data.get("base_category") or "swimwear").strip(),
        supplier_ref=(data.get("supplier_ref") or None),
        status=SpuStatus.DRAFT.value,
        created_by=actor,
        notes=data.get("notes"),
    )

    variants: dict[str, ColorVariant] = {}
    for index, payload in enumerate(variants_in):
        code = sku_matrix.normalize_code(
            payload.get("variant_code"),
            field=f"color_variants[{index}].variant_code",
            max_length=sku_matrix.MAX_VARIANT_CODE,
        )
        variants[code] = ColorVariant(
            spu=spu,
            variant_code=code,
            working_name=(payload.get("working_name") or "").strip(),
            # `display_name` 刻意不从入参取:它是投影列,唯一写入点是属性服务
            # 在 `standard_color_name` 被确认时(§4.3)。建档时填一个"正式名称"
            # 等于绕过那个写入点,而绕过它的值没有来源、没有证据、不会被复核
            supplier_color_code=(payload.get("supplier_color_code") or None),
            sort_order=index,
            sellable_status=SellableStatus.PLANNED.value,
        )

    # 保存点是必需的:约束冲突会让整个事务进 aborted 状态,不回滚就没法继续
    # (`product_service.create_product` 顶部记着同一件事)。
    savepoint = session.begin_nested()
    try:
        session.add(spu)
        for variant in variants.values():
            session.add(variant)
        # ---- 这一次 flush 不能省 ----
        #
        # `UUIDPrimaryKeyMixin` 的主键是 `default=uuid.uuid4`,而 SQLAlchemy 的
        # Python 侧 default **在 flush 时才求值**。在这之前 `spu.id` 是 None,
        # 于是"建档建出来的 SKU 全都 spu_id 为空"——而那一列本批次可空,
        # 数据库不会拦,列表页也照常显示。
        #
        # 颜色那边可以靠 relationship 赋值绕过这个问题(对象身份就够了),
        # SPU 这边不行:`Product.spu` 这个名字已经被反规范化字符串列占着,
        # 没有 relationship 可以指。所以这里老老实实先落一次。
        session.flush()

        for row in planned:
            product = Product(
                # 身份:两个外键是权威。`spu` 字符串同步写一份 —— 它是
                # 反规范化读列(§4.4),十来个域按它取数,本批次不动它们
                spu_id=spu.id,
                spu=spu_code,
                sku=row.sku,
                # `name` 是派生的显示值,不是身份 —— 截断安全,溢出不安全:
                # internal_name 上限 255,加上颜色(≤16)和尺码(≤8)最长 281,
                # 而列宽 255。不截的话 internal_name 超过 ~229 个字符时 flush 抛
                # **DataError**(StringDataRightTruncation 不是 IntegrityError,
                # 下面那个 except 接不住),运营拿到的是一句裸 500,不是字段报错。
                # 上限从 `field_limits` 读,和列宽、schema 同一份来源(A45-batch13-3 / R6)
                name=f"{spu.internal_name} {row.variant_code} {row.size}".strip()[
                    : limit_for("name")
                ],
                category=spu.base_category,
                # 受众:从 SPU 抄一份下去。**权威在 SPU**,这里是副本 ——
                # 抄的动作只发生在这一处,所以"同 SPU 各行受众必须一致"
                # 在新路径上是结构性成立的,不需要那道一致性检查(§4.2)
                audience=audience.value,
                size=row.size,
                size_group=row.size_group,
                status=ProductStatus.DRAFT.value,
            )
            product.color_variant = variants[row.variant_code]
            session.add(product)
        session.flush()
        savepoint.commit()
    except IntegrityError:
        savepoint.rollback()
        # 撞到的只可能是 `uq_spus_spu_code` 或 `uq_products_sku`:前者是并发,
        # 后者是**这个 SPU 编码下的 SKU 已经被别的路径建过**(老 CSV 导入
        # 也写 `products.sku`)。两种都是 409,信息里带上编码好让人去查
        raise DuplicateError(
            f"SPU 编码 {spu_code} 或它展开出来的 SKU 已经存在 —— "
            f"换一个编码,或先处理掉同名的存量行"
        ) from None

    audit.record(
        session,
        actor=actor,
        action=AuditAction.CREATE,
        entity_type="Spu",
        entity_id=spu.id,
        payload={
            "spu_code": spu_code,
            "audience": audience.value,
            "size_template": planned[0].size_group if planned else "",
            "variant_codes": sorted(variants),
            "sku_count": len(planned),
        },
    )
    return spu


def list_spus(session: Session, *, limit: int = 50, offset: int = 0) -> list[Spu]:
    """列表页的一页 SPU。**颜色一起预取。**

    `SpuOut` 带 `color_variants`,而那是个默认惰性的 relationship ——
    不预取的话序列化每一行都会补一次查询,一页 200 行就是 200 次。
    这个端点不在 `tests/pure/test_workbench_query_budget.py` 那条棘轮的
    覆盖范围里(它盯的是 `api.workbench_batch`),所以这里只能靠预取本身
    和 `test_a45_batch13_2_fixes.py` 里那条守卫。
    """
    return list(
        session.scalars(
            select(Spu)
            .options(selectinload(Spu.color_variants))
            .order_by(Spu.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )


def count_spus(session: Session) -> int:
    return int(session.scalar(select(func.count(Spu.id))) or 0)


def sku_counts_for(session: Session, spu_ids: list[UUID]) -> dict[UUID, int]:
    """这一页每个 SPU 底下有几行 SKU。**一次分组查询,不按行数。**

    存在的理由是硬规则 4:列表接口上那个 `sku_count` 原来是常量 0 ——
    形状对、数字假,而"这个 SPU 有几个 SKU"恰恰是列表页唯一能看出
    建档是否完整的信号。填常量的字段不会报错,只会一直说 0。

    按外键分组而不是按 `spu` 字符串(§4.4:反规范化列禁止作为查询权威)。
    没有 SKU 行的 SPU 不在结果里 —— 调用方用 `.get(id, 0)` 补,那个 0
    是**真的数出来是 0**,不是没算。
    """
    if not spu_ids:
        return {}
    rows = session.execute(
        select(Product.spu_id, func.count(Product.id))
        .where(Product.spu_id.in_(spu_ids))
        .group_by(Product.spu_id)
    ).all()
    return {spu_id: int(count) for spu_id, count in rows}


def skus_of(session: Session, spu_id: UUID) -> list[Product]:
    """这个 SPU 下面的 SKU 行。**按外键查,不按 `spu` 字符串查。**

    §4.4 那句"禁止作为查询权威"在这里第一次落到代码上:同样一句话,
    按字符串查会把改过名的行漏掉,按外键查不会。
    """
    return list(
        session.scalars(
            select(Product).where(Product.spu_id == spu_id).order_by(Product.sku)
        )
    )
