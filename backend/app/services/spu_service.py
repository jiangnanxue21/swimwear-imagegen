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
    ConcurrentTransition,
    DuplicateError,
    ErrorCode,
    FieldProblem,
    NotFoundError,
    ValidationError,
)
from app.core.field_limits import limit_for
from app.core.logging import get_logger
from app.listings import sku_matrix
from app.models.product import Product
from app.models.spu import ColorVariant, Spu
from app.services import audit, product_service

logger = get_logger(__name__)

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

    ## `msg` 归 `sku_matrix` 管,这里不再二次加工(A52)

    上一版 `normalize_code` 的消息都长成 `f"{field} ……"`,而 `field` 是那个
    模块自己的形参名 —— 运营看到的是「variant_codes[0] 含不允许的字符」:
    一个他在界面上根本找不到的词。前端 `help={problem}` 与
    「第 N 行颜色:{msg}」都是原样渲染,所以那串名字是直达运营的。

    修法在源头:`normalize_code` 现在同时收 `field`(机器定位)与
    `label`(人看的名字),消息里拼的是后者。这里**不做**"把开头那个词摘掉"
    的兜底 —— 兜底会让下一条按老样子写的消息静默地被修好,而
    `test_a52_...::test_no_message_repeats_its_own_locator` 会当场把它变红。
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
        raise NotFoundError(
            "找不到这个款,它可能已经被删除;请回到款式列表重新进入",
            detail={"spu_id": str(spu_id)},
        )
    return spu


def _code_taken(session: Session, spu_code: str) -> bool:
    return (
        session.scalar(select(Spu.id).where(Spu.spu_code == spu_code).limit(1))
        is not None
    )


def _by_request_key(session: Session, key: str) -> Spu | None:
    """按 `Idempotency-Key` 找已经建好的那个 SPU。"""
    return session.execute(
        select(Spu).where(Spu.request_key == key).limit(1)
    ).scalar_one_or_none()


def _reuse_or_conflict(spu: Spu, fingerprint: str) -> Spu:
    """同键重发:入参一样就复用,不一样就 409(§9.1)。

    与 `batch_service._reuse_or_conflict` 是同一套语义,刻意长得一样 ——
    两处都是 HTTP 层的 `Idempotency-Key`,做成两种形状只会让人以为
    它们的规则不同。
    """
    if spu.request_fingerprint == fingerprint:
        logger.info(
            "spu create reused an existing request key",
            extra={
                "extra_fields": {
                    "event": "spu.create_reused_request_key",
                    "spu_id": str(spu.id),
                    "spu_code": spu.spu_code,
                }
            },
        )
        # 瞬态标记,不落库:调用方必须知道这一次**没有建任何东西**。
        # 与批次那条同理由 —— 存进库等于把一个请求级事实钉在长期存在的行上
        spu.reused_request = True  # type: ignore[attr-defined]
        return spu
    raise ValidationError(
        "这把 Idempotency-Key 已经用在另一次建档上了。"
        f"它建的是 SPU {spu.spu_code} —— 要建一个不同的款请换一把键",
        code=ErrorCode.DUPLICATE_RESOURCE,
        http_status=409,
    )


def _preview_variants(raw: Any) -> list[dict[str, Any]]:
    """试算的颜色行:**整行空白的丢掉,其余原样保留。**

    第一步的表单里躺着一行 `emptyColour()` —— 三个字段全是空串。它表达的是
    "还没填",不是"填了个空的",所以试算不该为它报错。

    判据是**整行空白**而不是"编码为空":只看编码的话,一行填了工作名却漏了
    编码的输入会被静默丢掉,而那正是运营需要系统当场指出来的那种错。
    """
    rows: list[dict[str, Any]] = list(raw or [])
    return [
        row
        for row in rows
        if any(
            str(row.get(key) or "").strip()
            for key in ("variant_code", "working_name", "supplier_color_code")
        )
    ]


def preview_spu(session: Session, data: dict[str, Any]) -> dict[str, Any]:
    """建档试算。**一个字都不写库。**

    ## 它补的是哪一段

    建档表单三步走完才提交,而编码字符集、重复颜色、行数上限、编码已存在
    这四类失败**全部**只在提交那一刻才报得出来 —— 错在第二步的输入框上,
    人停在第三步,而第三步那句「建档没成功」底下只写着"规则由后端校验"。

    试算把同一份规则搬到"下一步"上跑一次。它不是第二份校验:

        编码规则   `sku_matrix.normalize_variant_codes` / `normalize_spu_code`
                   —— 与 `expand()` 内部**同一个函数**
        行数上限   `sku_matrix.expand()` —— 直接调它,不重算
        编码占用   `_code_taken()` —— 与 `create_spu` 同一个函数

    也就是说这里没有一条判定是新写的。新写的只有"什么时候问"。

    ## 失败走 422,与建档**同一条错误路径**

    不做成 `{ok: false, problems: [...]}`。理由是 AC-05 那条注释里的同一句:
    同一件事两种说法,运营会以为是两个问题。走 `_translate()` 之后,
    试算报的 `loc` 与提交报的 `loc` 逐字相同,前端一套高亮代码两处都能用。

    ## 编码占用是标志位,不是错误

    `code_taken` 回一个布尔。运营是边打字边触发试算的,`SW-0` 打到一半
    就红一次的话,他会先学会忽略这个提示,然后连真的那次也一起忽略。

    ## 尺码模板可空

    第二步还没选模板。空着时只跑颜色那一段,`sku_count` 回 `null`
    (**不是 0** —— 0 会被读成"这次建档一行都不产生")。第三步选完模板
    再试算一次,那一次才答得出行数上限。

    ## 颜色表也可空 —— 和"模板还没选"是同一类事

    第一步只填了 SPU 编码,颜色表是一行空编码。这一版之前那份载荷会先被
    pydantic 的 `min_length=1` 拒掉,于是**第一步的试算从来没跑通过**:
    `code_taken`(编码已被占用,错在第一步)这条四类失败里唯一能被提前的,
    实际上最早也要等到第二步试算成功之后才显示。

    现在"整列空白的颜色行"一律跳过,与"模板还没选"走同一条逻辑:
    答得出的照答(`spu_code` 合不合法、`code_taken`),答不出的回 `null`
    (`sku_count`)或空列表(`variant_codes`)。

    **跳过的判据是"整行空白",不是"编码为空"。** 只看编码的话,一行填了
    工作名却漏了编码的输入会被静默丢掉,而那是一个运营真的想让系统看见的错。
    """
    spu_code = ""
    try:
        spu_code = sku_matrix.normalize_spu_code(data.get("spu_code"))
        variants_in = _preview_variants(data.get("color_variants"))
        # 一个颜色都没填:跳过颜色段而不是抛。`normalize_variant_codes` 对
        # 空列表抛"至少要有一个颜色变体" —— 那句话在**提交**时是对的,
        # 在第一步的试算里不是:那时候还没轮到填颜色
        codes = (
            list(
                sku_matrix.normalize_variant_codes(
                    [v.get("variant_code", "") for v in variants_in]
                )
            )
            if variants_in
            else []
        )
        template = (data.get("size_template") or "").strip() or None
        # 没有颜色就没有行可展开。这一条挡的是"选了模板但还没填颜色":
        # 走进 `expand()` 的话它会抛那句"至少要有一个颜色变体",
        # 而这一步该回答的是"模板本身认不认识",不是"你颜色还没填"
        if not codes:
            template = None
        planned: tuple[sku_matrix.PlannedSku, ...] = ()
        sizes: list[str] = []
        if template is not None:
            planned = sku_matrix.expand(
                spu_code=spu_code, variant_codes=codes, size_template=template
            )
            sizes = list(sku_matrix.sizes_of(template))
    except sku_matrix.SkuPlanError as exc:
        raise _translate(exc) from None

    return {
        "spu_code": spu_code,
        "variant_codes": codes,
        "size_template": planned[0].size_group if planned else None,
        "sizes": sizes,
        "skus": [
            {
                "sku": row.sku,
                "variant_code": row.variant_code,
                "size": row.size,
                "size_group": row.size_group,
            }
            for row in planned
        ],
        # `None` 而不是 `len(planned)`:没模板时那个 0 是"算不出",不是"零行"
        "sku_count": len(planned) if template is not None else None,
        "code_taken": _code_taken(session, spu_code),
    }


def create_spu(
    session: Session,
    data: dict[str, Any],
    *,
    actor: str,
    request_key: str | None = None,
) -> Spu:
    """建档。返回建好的 SPU(颜色与 SKU 已经在 session 里 flush 过)。

    `data` 的形状见 `schemas/spu.SpuCreate`。这里再校验一次而不是信任 schema:
    展开规则(编码字符集、重复颜色、行数上限)住在 `listings/sku_matrix`,
    而 schema 只管字段类型 —— 把规则复制进 schema 会让它有两个版本,
    然后其中一个先过期。

    ## `request_key`:建档这条 HTTP 命令的幂等(PRD §9.1)

    不传时行为与从前一字不差。传了的话:

        同键 + 同入参   返回**原来那个 SPU**,不建第二个
        同键 + 不同入参 409 —— 客户端用同一把键提交了两个不同的款

    这一层与 `uq_spus_spu_code` 不重叠:那条约束挡的是"同一个编码被建了
    两次"(它是 §9.1 说的最终裁决),这一层挡的是"同一个建档请求被受理
    两遍"。双击、以及第一次响应在回程丢失后的客户端重发,走的都是这一层。

    **少了它,双击的表现是 `SPU 编码 X 已存在`** —— 一句在这个语境下是假的
    错误:运营没有建两个 SPU,他只是点了两下。建档是七步向导的第一步,
    这句假错误落在整条主流程的入口上。

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

    # ---- §9.1 幂等键 ----
    #
    # 顺序是刻意的:**先算完入参、再查键**。反过来的话,一次入参非法的重发
    # 会先命中键然后返回上一次的结果 —— 客户端拿到 200,而它这次提交的是
    # 一份错的数据,没有任何地方告诉它。
    #
    # 超长报错不截断,与 `batch_service` 同理由(截断后两把只有前缀相同的键
    # 会被当成同一把,而那种冲突报出来的错和真正的键冲突长得一模一样)
    key = (request_key or "").strip() or None
    if key is not None and len(key) > sku_matrix.MAX_IDEMPOTENCY_KEY:
        raise ValidationError(
            f"Idempotency-Key 最长 {sku_matrix.MAX_IDEMPOTENCY_KEY} 个字符。"
            "这一层不截断:截断之后两把只有前缀相同的键会被当成同一把,"
            "而那种冲突报出来的错和真正的键冲突长得一模一样",
            code=ErrorCode.INPUT_INVALID,
            http_status=422,
        )
    fingerprint: str | None = None
    if key is not None:
        fingerprint = sku_matrix.spu_request_fingerprint(
            spu_code=spu_code,
            internal_name=data.get("internal_name"),
            audience=audience.value,
            base_category=data.get("base_category") or "swimwear",
            supplier_ref=data.get("supplier_ref"),
            variant_codes=[v.get("variant_code", "") for v in variants_in],
            size_template=data.get("size_template", ""),
        )
        existing = _by_request_key(session, key)
        if existing is not None:
            return _reuse_or_conflict(existing, fingerprint)

    # 先查后插只挡得住"明显重复",挡不住并发 —— 真正的裁判是
    # `uq_spus_spu_code`。查询留作快速路径,好让错误信息说得清是哪个编码
    if _code_taken(session, spu_code):
        raise DuplicateError(f"SPU 编码 {spu_code} 已存在")

    spu = Spu(
        request_key=key,
        request_fingerprint=fingerprint,
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
            # 在 VARIANT 层 `primary_color` 被确认时(§4.3;那里原来写的
            # `standard_color_name` 是个不存在的字段名)。建档时填一个"正式名称"
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
        # ---- §9.1「并发均返回同一对象」----
        #
        # 带键时先看是不是**键**撞的:两个并发请求拿同一把键进来,一个插入
        # 成功、另一个撞 `uq_spus_request_key`。撞的那一路回查赢家并复用 ——
        # 与 `create_product` / 识别 run 的做法同构(先查后插只挡明显重复,
        # 数据库才是裁判)。
        #
        # 回查放在键分支里、而不是无条件回查:不带键的请求撞的一定是编码或
        # SKU,那时回查会查到 None 然后落到下面那句,多一次没有意义的查询
        if key is not None:
            winner = _by_request_key(session, key)
            if winner is not None:
                return _reuse_or_conflict(winner, fingerprint or "")
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


def _visible(stmt, include_disabled: bool):
    """列表默认不显示停用的款。**只有这一处定义"默认看得见什么"。**

    列表与计数各写一遍 `where` 的下场是分页错位:总数把停用的算进去、
    这一页没有,于是最后一页是空的,而空态写着"还没有商品"。
    `api/workbench.py` 的归档过滤踩过同一个形状。
    """
    return stmt if include_disabled else stmt.where(Spu.status != SpuStatus.DISABLED.value)


def list_spus(
    session: Session,
    *,
    limit: int = 50,
    offset: int = 0,
    include_disabled: bool = False,
) -> list[Spu]:
    """列表页的一页 SPU。**颜色一起预取。**

    `SpuOut` 带 `color_variants`,而那是个默认惰性的 relationship ——
    不预取的话序列化每一行都会补一次查询,一页 200 行就是 200 次。
    这个端点不在 `tests/pure/test_workbench_query_budget.py` 那条棘轮的
    覆盖范围里(它盯的是 `api.workbench_batch`),所以这里只能靠预取本身
    和 `test_a45_batch13_2_fixes.py` 里那条守卫。

    `include_disabled` 默认 False。**这不改变任何存量行的可见性** ——
    在停用端点存在之前,`spus.status` 一行都不可能是 DISABLED
    (全仓没有写入路径),所以默认过滤掉它对既有数据是恒等的。
    """
    return list(
        session.scalars(
            _visible(select(Spu), include_disabled)
            .options(selectinload(Spu.color_variants))
            .order_by(Spu.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )


def count_spus(session: Session, *, include_disabled: bool = False) -> int:
    """总数。**过滤条件必须和 `list_spus` 是同一份**,否则分页会错位。"""
    return int(
        session.scalar(_visible(select(func.count(Spu.id)), include_disabled)) or 0
    )


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


def rows_touching_spu(session: Session, spu: Spu) -> list[Product]:
    """这个款**可能**属于的全部 SKU 行:外键那批 ∪ 字符串码那批。

    ## 它和 `skus_of()` 刻意不同,而且不能合并

        skus_of()            回答「这个 SPU 的 SKU 是哪些」——**权威口径**。
                             §4.4:`products.spu` 那列禁止作为查询权威,
                             按它查会把改过名的行算进来
        rows_touching_spu()  回答「停用它之前,有没有可能漏掉一行」——**安全闸**

    两个问题对"错"的容忍方向是相反的。权威口径宁可窄:多算一行会让别的款的
    SKU 出现在这个款下面。安全闸宁可宽:**少算一行的代价是一件仍然挂在平台上
    的商品被静静放过**,而那正是 4.1 节 H 要防的结局。

    差出来的那一格是真实存在的:`Product.spu_id` 可空,老导入路径只写了
    `spu` 字符串。`disable_spu` 原来用 `skus_of()`,于是同一个款的存量行挂在
    平台上时,闸**放行**。

    宽出来的那部分(同码不同款的行)在这里只会让停用被拒,而拒绝会点名是
    哪几个 SKU 与哪个渠道 —— 运营看得见、查得清,代价是一次多余的核对;
    反过来那一边没有任何人会发现。
    """
    by_fk = select(Product).where(Product.spu_id == spu.id)
    rows = {row.id: row for row in session.scalars(by_fk)}
    if spu.spu_code:
        # 只补**没有外键**的那些行:有外键而外键指向别的款的,是那个款的事
        by_code = select(Product).where(
            Product.spu == spu.spu_code, Product.spu_id.is_(None)
        )
        for row in session.scalars(by_code):
            rows.setdefault(row.id, row)
    return sorted(rows.values(), key=lambda p: p.sku or "")


def _locked_spu(session: Session, spu_id: UUID) -> Spu:
    """取一行 SPU 并**持有行锁**。状态迁移(停用 / 恢复)一律走它。

    这两条都写 `row_version += 1`,而在此之前它们用的是无锁的 `get_spu()`,
    只有 `update_spu` 加了 `with_for_update()`。差出来的是两个真实的竞态:

        改字段 × 停用   `update_spu` 的行锁挡不住停用那一侧,两边各自
                        `row_version += 1`,版本号丢一格 —— 而版本号是
                        详情页那张编辑表单唯一的乐观锁
        停用 × 停用     两个请求都读到 ACTIVE,都写审计,都进一格

    幂等那一层(已经 DISABLED 就直接返回)挡不住它们:两个请求读到的都是
    停用**之前**的状态。
    """
    spu = session.scalar(select(Spu).where(Spu.id == spu_id).with_for_update())
    if spu is None:
        raise NotFoundError(
            "找不到这个款,它可能已经被删除;请回到款式列表重新进入",
            detail={"spu_id": str(spu_id)},
        )
    return spu


def update_spu(
    session: Session, spu_id: UUID, data: dict[str, Any], *, actor: str
) -> Spu:
    """以 `row_version` 做乐观锁，只允许改非身份元数据。"""
    expected = int(data.pop("expected_version"))
    spu = session.scalar(select(Spu).where(Spu.id == spu_id).with_for_update())
    if spu is None:
        raise NotFoundError(
            "找不到这个款,它可能已经被删除;请回到款式列表重新进入",
            detail={"spu_id": str(spu_id)},
        )
    if spu.row_version != expected:
        raise ConcurrentTransition(
            f"SPU 已被其他人更新（当前版本 {spu.row_version}），请刷新后重试"
        )
    changed: dict[str, Any] = {}
    for field in ("internal_name", "supplier_ref", "notes"):
        if field in data and getattr(spu, field) != data[field]:
            if field == "internal_name" and not str(data[field] or "").strip():
                raise ValidationError("内部名称不能为空", code=ErrorCode.INPUT_INVALID)
            setattr(spu, field, data[field])
            changed[field] = data[field]
    if changed:
        spu.row_version += 1
        audit.record(
            session,
            actor=actor,
            action=AuditAction.UPDATE,
            entity_type="Spu",
            entity_id=spu.id,
            payload={"changes": changed, "row_version": spu.row_version},
        )
    session.flush()
    return spu


def disable_spu(session: Session, spu_id: UUID, *, reason: str, actor: str) -> Spu:
    """停用一个款。**这是本系统里「删除 SPU」的全部含义。**

    ## 为什么不是 DELETE

    与 `product_service.archive_product` 同一笔账,只是更大:一个 SPU 底下
    挂着颜色变体、若干行 SKU,而每一行 SKU 又被九张表引着。硬删的两个方向
    都不能接受(RESTRICT 撞 500 / CASCADE 清空整条证据链),而 SPU 这一层
    还多一条:`SpuStatus.DISABLED` 这个取值**从落枚举那天就在**,注释写着
    「停掉了。**不删** —— 素材、事实、图片集都挂在它下面」,而在此之前
    全仓没有任何一条路径迁得到它。一个定义了却到不了的状态,和没有这个
    状态是一样的。

    ## 它**不**连带归档底下的 SKU

    刻意的。归档一行 SKU 有它自己的闸(平台还挂着就拒)、自己的理由、
    自己的审计记录;一个按钮背后连带迁移九行的话,那九条审计记录的理由
    全是同一句,而其中某一行可能正卡在平台侧驳回回流上。

    所以这一层的语义窄而清楚:**这个款不再接受新的颜色与 SKU,并从
    建档侧的列表里消失**。已经在生产动线上的那些行按它们自己的节奏走完。
    `add_color_variant` / `create_skus` 会拒绝停用中的 SPU —— 不拒的话
    "停用"就只是一个标签。

    ## 平台闸

    底下任何一行还挂在平台上就拒绝,并点名是哪几个 SKU 与哪个渠道站点。
    判据取 `product_service.live_listings_for()` —— **「还挂着」全仓只有
    那一个定义**,在这里自己写一遍 `notin_(...)` 的话,`_PUBLISH_SETTLED`
    就有了两个读者而只有一个会跟着改。

    幂等:已经停用的再停一次直接返回,不重复写审计。运营连点两下、
    或者请求超时后重试,不该拿到一个 409。

    ## 为什么不要 `expected_version`

    与 `archive_product` 同一条:它是**带闸的状态迁移**,不是字段编辑。
    要版本号的话,一次双击的第二下会撞 409「已被其他人更新」——
    而那句话在双击这个语境下是假的。字段编辑那一侧(`update_spu`)仍然要。
    """
    spu = _locked_spu(session, spu_id)
    if spu.status == SpuStatus.DISABLED.value:
        return spu

    # **平台闸用宽口径取数**(`rows_touching_spu`,不是 `skus_of`)。
    # 后者按外键查,而 `Product.spu_id` 可空、老导入路径只写 `spu` 字符串 ——
    # 漏掉的那一行如果正挂在平台上,闸会放行,而这道闸的全部意义就是不放行
    skus = rows_touching_spu(session, spu)
    live = product_service.live_listings_for(session, [row.id for row in skus])
    if live:
        by_id = {row.id: row for row in skus}
        where = ", ".join(sorted({f"{r.channel}/{r.site}" for r in live})[:5])
        which = ", ".join(
            sorted({by_id[r.product_id].sku for r in live if r.product_id in by_id})[:5]
        )
        raise ValidationError(
            f"这个款底下还有 SKU 挂在平台上({which} @ {where}),先在平台下架再停用",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
            detail={"live_listing_ids": [str(r.id) for r in live]},
        )

    previous = spu.status
    spu.status = SpuStatus.DISABLED.value
    # 版本号照样进一格:详情页上开着的那张编辑表单必须在下一次保存时
    # 撞 `ConcurrentTransition`,而不是把 status 之外的字段静静写回一个
    # 已经停用的款上
    spu.row_version += 1
    session.flush()
    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="Spu",
        entity_id=spu.id,
        payload={
            "action": "disable",
            # 理由进审计而不是新开一列,与归档同一条:低频动作,而「谁、
            # 什么时候、为什么」这三样审计表本来就答得出来
            "reason": (reason or "")[:500],
            "from_status": previous,
            "spu_code": spu.spu_code,
            "sku_count": len(skus),
        },
    )
    return spu


def restore_spu(session: Session, spu_id: UUID, *, actor: str) -> Spu:
    """把停用的款放回建档动线。

    回到 `DRAFT` 而不是停用前那一档,判据与 `restore_product` 逐字相同:
    停用期间上游可能变过,`ACTIVE` 是一句"这个款在做"的断言,而做出这句
    断言的依据在停用那一刻就停止更新了。回 DRAFT 让它重新走一遍。

    **不要理由。** 恢复是撤销,停用是决定 —— 前者不需要三个月后有人来问。

    与 `disable_spu` 一样走 `_locked_spu`:它同样写 `row_version += 1`,
    同样会和详情页那张编辑表单的保存撞车。
    """
    spu = _locked_spu(session, spu_id)
    if spu.status != SpuStatus.DISABLED.value:
        return spu
    spu.status = SpuStatus.DRAFT.value
    spu.row_version += 1
    session.flush()
    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="Spu",
        entity_id=spu.id,
        payload={"action": "restore", "to_status": spu.status, "spu_code": spu.spu_code},
    )
    return spu


def _assert_open_for_building(spu: Spu) -> None:
    """停用中的款不接受新的颜色与 SKU。

    不拒的话「停用」就只是一个标签:界面上写着"这个款不做了",而它照旧
    长出新的颜色和新的 SKU 行,没有任何地方会报错。

    放在服务层而不是接口层:批量导入(`workbench_batch`)也会落 SKU,
    而那条路径不经过 `api/spus.py`。
    """
    if spu.status == SpuStatus.DISABLED.value:
        raise ValidationError(
            f"SPU {spu.spu_code} 已停用,不能再加颜色或 SKU。要继续做这个款,先恢复它",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )


def add_color_variant(
    session: Session, spu_id: UUID, data: dict[str, Any], *, actor: str
) -> ColorVariant:
    """给既有 SPU 加颜色，并按明确或既有尺码模板原子铺开 SKU。"""
    spu = get_spu(session, spu_id)
    _assert_open_for_building(spu)
    existing = skus_of(session, spu_id)
    template = data.get("size_template")
    if template is None:
        groups = {row.size_group for row in existing if row.size_group}
        if len(groups) != 1:
            raise ValidationError(
                "无法从现有 SKU 唯一确定尺码模板，请显式传 size_template",
                code=ErrorCode.INPUT_INVALID,
                http_status=422,
            )
        template = groups.pop()
    try:
        code = sku_matrix.normalize_code(
            data.get("variant_code"), field="variant_code", max_length=sku_matrix.MAX_VARIANT_CODE
        )
        planned = sku_matrix.expand(
            spu_code=spu.spu_code, variant_codes=[code], size_template=template
        )
    except sku_matrix.SkuPlanError as exc:
        raise _translate(exc) from None
    if any(item.variant_code == code for item in spu.color_variants):
        raise DuplicateError(f"SPU {spu.spu_code} 已有颜色 {code}")
    if len(spu.color_variants) >= sku_matrix.MAX_VARIANTS_PER_SPU:
        raise ValidationError("颜色数量已达上限", code=ErrorCode.INPUT_INVALID)

    variant = ColorVariant(
        spu=spu,
        variant_code=code,
        working_name=(data.get("working_name") or "").strip(),
        supplier_color_code=data.get("supplier_color_code") or None,
        sort_order=len(spu.color_variants),
        sellable_status=SellableStatus.PLANNED.value,
    )
    session.add(variant)
    session.flush()
    for plan in planned:
        session.add(
            Product(
                spu_id=spu.id,
                color_variant_id=variant.id,
                spu=spu.spu_code,
                sku=plan.sku,
                name=f"{spu.internal_name} {code} {plan.size}"[: limit_for("name")],
                category=spu.base_category,
                audience=spu.audience,
                size=plan.size,
                size_group=plan.size_group,
                status=ProductStatus.DRAFT.value,
            )
        )
    try:
        session.flush()
    except IntegrityError:
        raise DuplicateError(f"颜色 {code} 展开的 SKU 已存在") from None
    audit.record(
        session,
        actor=actor,
        action=AuditAction.CREATE,
        entity_type="ColorVariant",
        entity_id=variant.id,
        payload={"spu_id": str(spu.id), "variant_code": code, "size_template": template},
    )
    return variant


def update_color_variant(
    session: Session, spu_id: UUID, variant_id: UUID, data: dict[str, Any], *, actor: str
) -> ColorVariant:
    expected = int(data.pop("expected_version"))
    row = session.scalar(
        select(ColorVariant)
        .where(ColorVariant.id == variant_id, ColorVariant.spu_id == spu_id)
        .with_for_update()
    )
    if row is None:
        raise NotFoundError("颜色变体不存在")
    if row.row_version != expected:
        raise ConcurrentTransition("颜色已被其他人更新，请刷新后重试")
    changed = False
    for field in ("working_name", "supplier_color_code", "sellable_status"):
        if field in data:
            value = getattr(data[field], "value", data[field])
            if getattr(row, field) != value:
                setattr(row, field, value)
                changed = True
    if changed:
        row.row_version += 1
        audit.record(
            session,
            actor=actor,
            action=AuditAction.UPDATE,
            entity_type="ColorVariant",
            entity_id=row.id,
            payload={"row_version": row.row_version},
        )
    session.flush()
    return row


def create_skus(
    session: Session, spu_id: UUID, items: list[dict[str, Any]], *, actor: str
) -> list[Product]:
    """在既有颜色下批量补 SKU；整批原子提交，不留下半批残骸。"""
    spu = get_spu(session, spu_id)
    _assert_open_for_building(spu)
    variants_by_id = {row.id: row for row in spu.color_variants}
    created: list[Product] = []
    seen: set[tuple[UUID, str]] = set()
    for item in items:
        variant = variants_by_id.get(item["color_variant_id"])
        if variant is None:
            raise ValidationError("颜色不属于这个 SPU", code=ErrorCode.INPUT_INVALID)
        size = str(item["size"]).strip().upper()
        key = (variant.id, size)
        if key in seen:
            raise ValidationError("同一批里颜色与尺码重复", code=ErrorCode.INPUT_INVALID)
        seen.add(key)
        sku = (item.get("sku") or sku_matrix.sku_code(
            spu_code=spu.spu_code, variant_code=variant.variant_code, size=size
        )).strip()
        row = Product(
            spu_id=spu.id,
            color_variant_id=variant.id,
            spu=spu.spu_code,
            sku=sku,
            name=f"{spu.internal_name} {variant.variant_code} {size}"[: limit_for("name")],
            category=spu.base_category,
            audience=spu.audience,
            size=size,
            status=ProductStatus.DRAFT.value,
            barcode=item.get("barcode"),
            price=item.get("price"),
            inventory=item.get("inventory"),
        )
        session.add(row)
        created.append(row)
    try:
        session.flush()
    except IntegrityError:
        raise DuplicateError("SKU 编码或颜色尺码组合已存在") from None
    audit.record(
        session,
        actor=actor,
        action=AuditAction.CREATE,
        entity_type="Product",
        payload={"spu_id": str(spu.id), "created": len(created), "batch": True},
    )
    return created
