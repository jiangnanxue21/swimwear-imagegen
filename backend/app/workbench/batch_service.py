"""批量执行 service(阶段 3,任务书 §6)。

分工延续 flow.py / service.py 的那条线:

    batch.py          判定。该不该执行、算不算成功、能不能重试(纯函数)
    batch_service.py  查库、真正执行动作、落库、发回执

## 执行器复用阶段 2 的单件路径

四个执行器全部调用阶段 2 已经验收过的函数(`attr_service.run_extraction`、
`wb.generate_copy`、`wb.build_draft`、`wb.export_gate`)。**没有一条批量专用的
业务逻辑。** 这不是为了省代码:批量路径如果自己算一遍"草稿过期了吗",
就会出现单件导出正确拒绝、批量导出照样写进文件的情况,而运营用的是批量。

## 每件一个保存点

`session.begin_nested()` 包住每一件。第 37 件炸了只回滚第 37 件,
前 36 件的结果留在库里 —— §6.3 第三条「单件失败不导致整个批量任务失败」
在数据库层面的含义就是这个。少了保存点,一次 `IntegrityError` 会把
整个事务标脏,后面每一件都跟着失败,而日志里只有第 37 件的错。
"""
from __future__ import annotations

import hashlib
import io
import os
import socket
import zipfile
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy import true as sa_true
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.attributes import service as attr_service
from app.channels import generic
from app.core.clock import as_naive_utc, iso_utc, utc_now
from app.core.enums import AuditAction, DraftStatus
from app.core.errors import ErrorCode, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.locks import try_advisory_xact_lock
from app.extractors.registry import get_extractor
from app.listings import export_writer, variants
from app.models.batch_job import BatchActionReceipt, BatchJob, BatchJobItem
from app.models.product import Product
from app.services import audit
from app.workbench import batch as rules
from app.workbench import service as wb
from app.workbench.batch import (
    ACTION_STEP,
    BATCH_ACTION_LABELS,
    BatchAction,
    BatchCounts,
    BatchErrorCode,
    BatchItemStatus,
    BatchJobStatus,
    Candidate,
    ExecResult,
    PlannedItem,
)
from app.workbench.flow import FlowStep, IssueLevel

logger = get_logger(__name__)

#: 一次批量最多多少件。阶段 3 的目标是 50 件(§6.2),留一倍余量。
#:
#: 上限存在的理由不是性能,是**误操作**:列表页"全选"再点批量识别,
#: 如果库里有 5000 件商品,那是 5000 次付费调用。
MAX_BATCH_SIZE = 100


# ==========================================================
# 候选与指纹
# ==========================================================


def _material_fingerprint(session: Session, product: Product) -> str:
    """参与识别的素材集合。**换了图才该重跑识别。**"""
    from app.core.enums import MediaStatus
    from app.models.media_asset import MediaAsset

    ids = sorted(
        str(i)
        for i in session.scalars(
            select(MediaAsset.id).where(
                MediaAsset.product_id == product.id,
                MediaAsset.status == MediaStatus.READY.value,
            )
        )
    )
    return "|".join(ids)


def _generator_fingerprint() -> str:
    """把当前生效的配置读出来,交给 `batch.generator_fingerprint` 算。

    判据本身是纯函数(见那边的说明);这里只负责"当前配置是什么"。
    切开是为了让"换了这个会不会让指纹变"能在纯测试里穷举 ——
    容器里没有 pydantic,读 settings 的代码进不了纯测试。
    """
    from app.core.config import settings
    from app.listings import copy_generator as cg
    from app.listings import copy_rules
    from app.providers._config import provider_setting

    # A45-#31 同类,而且**比它更要紧**:这是产出指纹。
    #
    # 指纹要回答的是"这次产出是用什么配置做的",而真正执行时用的是覆盖层的值
    # (`get_generator` / `_llm_from_settings` 都走 `provider_setting`)。
    # 指纹读启动期快照的话:运营在设置页换了模型 -> 执行用新模型、指纹不变
    # -> 幂等命中 -> **旧结果被当成新配置的产物复用**。
    #
    # 而这个函数的 docstring 写的正是"换了配置要重跑一遍最该生效的时候"。
    generator = (provider_setting("COPY_GENERATOR", settings.COPY_GENERATOR) or "").strip()
    is_llm = generator == cg.LLM_GENERATOR
    return rules.generator_fingerprint(
        generator=generator,
        model=provider_setting("TEXT_MODEL_NAME", settings.TEXT_MODEL_NAME)
        if is_llm
        else None,
        api_style=provider_setting("TEXT_MODEL_API_STYLE", settings.TEXT_MODEL_API_STYLE)
        if is_llm
        else None,
        prompt_version=cg.PROMPT_VERSION if is_llm else cg.TEMPLATE_VERSION,
        rules_version=copy_rules.RULES_VERSION,
    )


def _fingerprint(session: Session, action: BatchAction, ctx) -> str:
    """决定输出的上游状态。**改了它,结果会不一样。**

    判据与 `workflows/idempotency.py` 一致。取值刻意都是已经落库的版本号
    和状态,而不是时间戳 —— 时间戳每次都变,幂等就永远不命中,
    "不重复触发付费调用"会退化成一句空话。

    **上游事实之外,产出方式本身也要进指纹**(评审第 3 条)。原来三个
    action 只看上游数据:素材 id、属性版本、图片集与文案状态。于是换模型、
    换提示词、换校验规则、换平台 spec 之后,指纹一个字不变,旧结果被当成
    新配置的产物复用 —— 而这正是「换了配置要重跑一遍」最该生效的时候。
    """
    if action is BatchAction.EXTRACT:
        # 属性识别走的是视觉模型:模型名和提示词版本换了,同一批图也该重认
        from app.core.config import settings
        from app.providers._config import provider_setting

        # 同上:视觉模型也在设置页里可改,指纹必须跟着覆盖层走,
        # 否则换了模型之后同一批图会命中旧回执、不重认
        vision_model = provider_setting("VISION_MODEL_NAME", settings.VISION_MODEL_NAME)
        vision_style = provider_setting(
            "VISION_MODEL_API_STYLE", settings.VISION_MODEL_API_STYLE
        )
        return "|".join(
            [
                _material_fingerprint(session, ctx.product),
                f"vmodel:{vision_model or '?'}",
                f"vstyle:{vision_style or '?'}",
            ]
        )

    if action is BatchAction.GENERATE_COPY:
        # 已确认属性的版本 id 集合:属性一改,文案就该重生。
        #
        # **颜色不进这里**:它已经在 `scope_key` 里(0051 之后文案按
        # (SPU, 颜色) 分槽),而指纹回答的是"这个作用域的上游变没变"。
        # 两处都放的话,回执键会因为同一件事变两次 —— 不影响正确性,
        # 但"为什么这次没命中回执"会多出一个要排除的原因
        return "|".join(
            [
                "|".join(wb._confirmed_version_ids(session, ctx.product)),
                _generator_fingerprint(),
                f"locale:{generic.LOCALE}",
            ]
        )


    if action is BatchAction.VALIDATE_DRAFT:
        parts = [
            f"is:{ctx.image_set.version}:{ctx.image_set.status}" if ctx.image_set else "is:-",
            f"copy:{ctx.copy.version}:{ctx.copy.status}" if ctx.copy else "copy:-",
            f"map:{wb.MAPPING_VERSION}",
            # 平台 spec 换版会换一套必填规则和一套列头,而校验结果正是
            # 按那套规则算出来的。**按本商品的规则包取版**(阶段 0 修复):
            # 男装包与女装包各自换版,只重判自己那边的校验缓存
            f"spec:{generic.field_spec(category_id=generic.category_id_for(ctx.product)).spec_version}",
        ]
        return "|".join(parts)

    # EXPORT:草稿指纹本身就是"这份草稿是由哪些上游算出来的"
    if ctx.draft is not None:
        return f"draft:{ctx.draft.source_fingerprint or ''}"
    return "draft:-"


def _first_blocking_ref(result) -> str | None:
    """首个阻断问题的定位信息。异常列表与批次条目都用它一键跳转。"""
    for issue in result.issues:
        if issue.level is IssueLevel.BLOCKING and issue.ref:
            return issue.ref
    for issue in result.issues:
        if issue.level is IssueLevel.BLOCKING:
            return issue.code
    return None


def candidates(
    session: Session, action: BatchAction, product_ids: Sequence[UUID]
) -> tuple[list[Candidate], dict[str, Any]]:
    """把商品翻译成 `batch.Candidate`,顺带留下上下文供执行器复用。

    返回的第二项是 `{product_id: WorkbenchContext}` —— 计划阶段已经
    `collect()` 过一次,执行阶段没必要再查一遍;但**执行前会重新 collect**
    (见 `_execute`),因为前一件的执行可能改变了后一件的状态(同 SPU)。
    这里的 ctx 只用于算指纹和展示。
    """
    if not product_ids:
        return [], {}

    # A45-#14:**入口去重。**
    #
    # `ordered` 保序但不去重,于是同一个 product_id 传两次会落两条**同
    # `idempotency_key`** 的条目;而 `run_batch` 的 `by_product` / `tokens`
    # 按 product_id 建 dict,只剩一份 —— 另一行要么被覆盖、要么钉在 RUNNING
    # 等租约到期,而它一次都不会被执行。
    #
    # 选择去重而不是 422:重复通常来自前端多选时的一次误操作(比如同一件
    # 商品在两个分组里各选了一次),报错等于让运营自己去找哪一件重了,
    # 而系统完全知道答案。`dict.fromkeys` 保序,去重之后上限判定才对得上。
    product_ids = list(dict.fromkeys(product_ids))

    if len(product_ids) > MAX_BATCH_SIZE:
        raise ValidationError(
            f"一次批量最多 {MAX_BATCH_SIZE} 件,本次选中 {len(product_ids)} 件;"
            "请分批执行",
            code=ErrorCode.INPUT_INVALID,
        )

    rows = list(session.scalars(select(Product).where(Product.id.in_(product_ids))))
    found = {r.id for r in rows}
    missing = [str(p) for p in product_ids if p not in found]
    if missing:
        raise NotFoundError(f"商品不存在:{', '.join(missing[:5])}")

    # 保持调用方给的顺序:界面上勾选的顺序就是运营心里的顺序
    by_id = {r.id: r for r in rows}
    ordered = [by_id[p] for p in product_ids if p in by_id]

    out: list[Candidate] = []
    contexts: dict[str, Any] = {}
    for product in ordered:
        ctx = wb.collect(session, product)
        contexts[str(product.id)] = ctx
        result = ctx.result
        out.append(
            Candidate(
                product_id=str(product.id),
                sku=product.sku,
                spu=product.spu,
                next_action=result.next_action.code,
                next_action_label=result.next_action.label,
                current_step=result.current_step,
                blocking_count=result.blocking_count,
                pending_count=result.pending_count,
                first_blocking_ref=_first_blocking_ref(result),
                fingerprint=_fingerprint(session, action, ctx),
                # 文案的作用域键要用它(迁移 0051 / AC-11 第二句)。
                # 口径与属性 owner、图片映射键同源 —— `variant_id_for()`
                # 是「变体身份只有一份定义」的那一份,这里不重算
                variant_id=variants.variant_id_for(product),
            )
        )
    return out, contexts



def plan(
    session: Session,
    action: BatchAction,
    product_ids: Sequence[UUID],
    *,
    include_exported: bool = False,
) -> rules.BatchPlan:
    """FE-301 的"执行前显示可执行/不可执行数量"。

    ## A45-#17:这里原来写的是"**不写任何库**",而那句话不成立

    计划会走 `collect()`,而 `collect()` 的过期判定会把草稿改成 STALE ——
    `api.plan_batch` 里明确记着这件事并**刻意 commit**(那是评审第 19 条的
    反面:计划是 POST,它有资格落这次判定,列表页那条 GET 才不许)。

    准确的说法是:**它不写业务结果,但会落判定状态。** 两者的区别在回滚语义上
    是实的 —— 调用方不能假设"计划一下什么都没发生"。
    """
    cands, _ = candidates(session, action, product_ids)
    return rules.plan(action, cands, include_exported=include_exported)


# ==========================================================
# 回执:跨批次的幂等
# ==========================================================


def _receipt(session: Session, key: str) -> BatchActionReceipt | None:
    return session.scalars(
        select(BatchActionReceipt).where(BatchActionReceipt.idempotency_key == key)
    ).first()


def _claim_in_flight(
    *,
    key: str,
    action: BatchAction,
    scope_key: str,
    product_id: UUID,
    actor: str,
) -> bool:
    """付费调用**之前**,用一条独立事务把"我要开始了"记下来(评审第 5 条)。

    ## 为什么必须是独立会话

    业务事务里写一条 IN_FLIGHT 没有意义:进程在模型调用中途死掉时,那个
    事务连同标记一起回滚,库里什么都没有 —— 而钱可能已经花了。要让标记
    在崩溃后还在,它必须先于业务事务落盘。

    这不是分布式事务,也不试图做到精确:标记落了、调用没发出去,同样会
    留下一条 IN_FLIGHT。方向是刻意的 —— **宁可让人核对一次,不可让费用
    静默消失**。

    ## 为什么是 upsert,不是 insert

    原来这里是裸 INSERT,失败只记一条日志。而**重试时行已经存在**,那次
    INSERT 必然撞唯一约束 —— 于是"标记失败"在重试路径上是常态而不是异常,
    状态原地不动。配合下面 `_write_receipt` 只认 IN_FLIGHT 才更新的写法,
    结果是:`FAILED_BILLED` 的条目重试成功之后**回执永远停在
    FAILED_BILLED**,下一次重试再付一次费,循环不收敛。

    改成 `ON CONFLICT DO UPDATE`:同一条回执的生命周期由**状态迁移**表达,
    不再靠"再插一行"表达。`paid_calls` / `reuse_count` 是累计量,这里
    **一律不碰** —— 它们记的是历史上真的花过几次钱,重新开始一次执行
    不该把那段历史抹掉(台账上 `paid_calls > 1` 正是 §7.3 要人看见的信号)。

    ## 返回值不再可以忽略

    返回 False = 标记没落盘。此时**调用方必须停下,不许调用付费模型**:
    标记落不下去意味着崩溃后没人知道这次花没花钱,而那正是这条标记存在
    的全部理由。带着"记不上"继续调用,等于把它退化成一句日志。
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.db.session import SessionLocal

    now = utc_now()
    # 建会话本身也可能失败(库连不上)。它必须在 try 里 —— 放外面的话
    # 连不上库会直接把异常抛给调用方,而这个函数的契约是"不抛,返回 False"
    marker = None
    try:
        marker = SessionLocal()
        stmt = pg_insert(BatchActionReceipt.__table__).values(
            idempotency_key=key,
            action=action.value,
            scope_key=scope_key,
            product_id=product_id,
            status=rules.ReceiptStatus.IN_FLIGHT.value,
            paid_calls=0,
            reuse_count=0,
            # A-17:这里是"真的开跑了一次"唯一诚实的落点。走到这一句
            # 意味着幂等检查没拦住、马上就要调付费模型;而 `paid_calls`
            # 记的是**调了几张图**,两个数回答的是两个问题
            executions=1,
            result={},
            last_actor=actor,
            last_used_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            # C-16:按列名冲突,不按约束名。原来写死 `constraint=
            # "uq_batch_action_receipts_key"`,迁移里一改名这条 upsert 就
            # 恒抛 —— 而下面那个 `except Exception` 会把它吞成 False,
            # 于是**所有付费动作被拒**,日志只报异常类型不指向根因。
            # `index_elements` 由列推导,改名不影响
            index_elements=["idempotency_key"],
            set_={
                "status": rules.ReceiptStatus.IN_FLIGHT.value,
                # 累计,不覆盖。第二次认领同一把幂等键 = 这条作用域
                # 第二次真的开跑,那正是 §7.3 要人看见的信号
                "executions": BatchActionReceipt.__table__.c.executions + 1,
                # A45-batch12-2 / EX-02:**必须清掉。**
                #
                # 这一列回答的是"**这一次**执行有没有把请求发出去",不是
                # "这条作用域历史上发过没有"(后者看 `paid_calls`)。留着
                # 上一轮的时刻,新一次执行还没走到调用就已经带着标记,
                # 于是它崩溃时会被判成"可能已计费"—— 一个凭空的人工工单。
                #
                # 与 `paid_calls` / `reuse_count` 的"一律不碰"不冲突:
                # 那两个是累计量,这一个是本次执行的状态位
                "provider_call_at": None,
                "last_actor": actor,
                "last_used_at": now,
            },
        )
        marker.execute(stmt)
        marker.commit()
        return True
    except Exception as exc:  # noqa: BLE001 - 见 docstring 最后一段
        if marker is not None:
            marker.rollback()
        logger.warning(
            "could not write the in-flight marker",
            extra={
                "extra_fields": {
                    "event": "batch.in_flight_marker_failed",
                    "key": key[:32],
                    "error": type(exc).__name__,
                }
            },
        )
        return False
    finally:
        if marker is not None:
            marker.close()


def _mark_provider_call(*, key: str) -> bool:
    """付费调用**发出之前的最后一刻**,用一条独立事务记下时刻(EX-02)。

    ## 这一条与 `_claim_in_flight` 的分工

    认领回答"这条作用域开跑了吗",这一条回答"**这一次**开跑真的把钱花出去了吗"。
    两个问题原来压在同一个 `IN_FLIGHT` 上,于是回收之后只能靠猜:猜没花钱就
    自动重跑(可能重复计费),猜花了钱就一律转人工(基础设施抖一下就停一批)。
    `MAX_BILLED_EXECUTIONS` 那段"为什么是 2 而不是 1"就是这个猜法的产物。

    分开之后 `receipt_route` 不用猜了:

        没有这个标记   可证明请求没发出去 -> 免费重跑,不扣付费额度
        有这个标记     可能已受理并计费   -> 停下,等人对账

    ## 为什么同样必须是独立会话

    与 `_claim_in_flight` 完全同一条理由:业务事务会随崩溃一起回滚,而这个
    标记的全部意义就是**崩溃之后还读得到**。写在业务事务里等于没写。

    ## 返回 False 时**必须停下**(A45-batch12-3 更正)

    这一段原来写的是相反的结论,而那个结论建立在一句错话上:

    > 回执仍然是 IN_FLIGHT,`receipt_route` 拿到的 `call_dispatched`
    > 仍然是保守的 True(默认值),行为退回到"停下来等人对账"。

    默认值只保护**不传这个参数**的调用方。而唯一真的调用方 `_execute`
    是显式传的:`receipt.provider_call_at is not None`。标记写失败 →
    这一列是 NULL → `call_dispatched=False` → `receipt_route` 判 `EXECUTE`。
    也就是说"标记没写进去、但付费调用发出去了、然后崩溃"这条路,恰好会被
    下一次回收判成**可证明没花钱**,自动重跑一次。EX-02 要挡的就是这件事。

    而且那一支**刻意不消耗付费额度**(见 `receipt_route`)—— 在标记可信时
    这个判断是对的,标记不可信时它意味着这条路连 `MAX_BILLED_EXECUTIONS`
    都不再兜底,只剩 `MAX_ITEM_ATTEMPTS = 3`。修之前的上限是 2。**没有兜住,
    是退了一档。**

    发生概率也不像"数据库抖一下"那么远:`_claim_in_flight` 与这一条各开一个
    新的 `SessionLocal`,而业务会话早已 check out。50 件的批次撞上连接池耗尽
    时,先失败的恰恰是这两个新会话,业务会话照常把付费调用跑完 —— 两种失败
    并不同步。

    所以这里与认领**同一条处理**:记不上就不许调用付费模型。停下来的代价
    只是这一件要重试,而重试是安全的 —— 没有标记就说明也没有调用,
    `receipt_route` 会让它免费重跑。这正是这条修复自己的论证。
    """
    from app.db.session import SessionLocal

    marker = None
    try:
        marker = SessionLocal()
        result = marker.execute(
            update(BatchActionReceipt.__table__)
            .where(BatchActionReceipt.__table__.c.idempotency_key == key)
            .values(provider_call_at=utc_now())
        )
        # 没匹配到行 = 标记没落下,和 UPDATE 抛错是同一个结果。
        # 正常路径上 `_claim_in_flight` 已经保证这一行存在,所以走到这里
        # 说明有人绕过了认领、或者那一行被并发删掉了(对账脚本会删)。
        # 把"写成功"和"没匹配上"混成同一个 True,正是这个文件别处
        # (`usage_dict`、`_settle_unbilled_failure`)反复在拆的那种混淆
        if result.rowcount == 0:
            marker.rollback()
            logger.warning(
                "the provider-call marker has no matching row",
                extra={
                    "extra_fields": {
                        "event": "batch.provider_call_marker_missing_row",
                        "key": key[:32],
                    }
                },
            )
            return False
        marker.commit()
        return True
    except Exception as exc:  # noqa: BLE001 - 见 docstring 最后一段
        if marker is not None:
            marker.rollback()
        logger.warning(
            "could not write the provider-call marker",
            extra={
                "extra_fields": {
                    "event": "batch.provider_call_marker_failed",
                    "key": key[:32],
                    "error": type(exc).__name__,
                }
            },
        )
        return False
    finally:
        if marker is not None:
            marker.close()


def _write_receipt(
    session: Session,
    *,
    key: str,
    action: BatchAction,
    scope_key: str,
    product_id: UUID,
    paid_calls: int,
    result: Mapping[str, Any],
    actor: str,
    status: rules.ReceiptStatus = rules.ReceiptStatus.SUCCEEDED,
) -> None:
    """把回执推进到终态。

    ## 这里只做状态迁移,不用"再插一行"表达生命周期

    `_claim_in_flight` 已经保证行存在且处于 IN_FLIGHT,所以正常路径永远是
    UPDATE。原来这里的写法是"只有旧状态恰好是 IN_FLIGHT 才更新,否则改走
    INSERT",而 INSERT 必然撞唯一约束、`except` 分支又只加 `reuse_count`
    **不碰 status** —— 于是 `FAILED_BILLED` 重试成功之后回执停在
    `FAILED_BILLED`,下次重试再付一次费。

    ## 为什么必须 refresh

    IN_FLIGHT 是**独立会话**写的(见 `_claim_in_flight`)。业务会话在本次
    执行更早的时候已经把这一行读进过身份映射(`_execute` 里查回执那一步),
    拿到的是旧状态;不 refresh 的话这里看到的是 `FAILED_BILLED` 而不是
    `IN_FLIGHT` —— 上面那个缺陷真正的触发机制就在这里,而它只在**重试**
    路径上出现(首次执行时身份映射里没有这一行,那次查询会真的打到库)。

    ## paid_calls 累计,不覆盖

    它记的是这条作用域历史上真的调了几次付费模型。重试付了第二次,台账上
    就该是 2 —— §7.3 观测"重复触发付费模型调用"靠的正是这个数。
    """
    # A41:同 `create_batch` —— aware 的 now 赋给变量、用到的三处各自
    # `.replace(tzinfo=None)`,是 core/clock.py 点名的那个形状。这个函数里
    # 它的三处用法全是写库,归一到入口即可,语义不变
    now = utc_now()

    existing = _receipt(session, key)
    if existing is not None:
        # 独立会话刚改过这一行,业务会话身份映射里的是旧快照。
        # **诚实说明:这一句目前不是必需的** —— 变异验证里去掉它,本文件对应的
        # 测试全绿。因为认领刻意不碰 `paid_calls`,旧快照上的那个数与库里一致,
        # 而状态是直接赋值、不再读旧值。留着是防守:哪天认领开始改累计量,
        # 少这一句就会算出一个静默错误的费用数字,而费用错了没人会发现。
        session.refresh(existing)
        existing.status = status.value
        existing.paid_calls += paid_calls
        existing.result = dict(result)
        existing.last_actor = actor
        existing.last_used_at = now
        session.flush()
        return

    # 走到这里说明标记那一步没建成行(不付费的动作不记标记,属正常路径;
    # 付费动作走到这里则意味着有人绕过了 `_claim_in_flight`)。
    savepoint = session.begin_nested()
    try:
        session.add(
            BatchActionReceipt(
                idempotency_key=key,
                action=action.value,
                scope_key=scope_key,
                product_id=product_id,
                status=status.value,
                paid_calls=paid_calls,
                reuse_count=0,
                # A-17:这一行是现建的,说明 `_claim_in_flight` 没建成它
                # (不付费的动作不记标记,属正常路径)。执行确实发生过一次,
                # 记 1 —— 记 0 会让台账上出现"付过钱但没跑过"的行
                executions=1,
                result=dict(result),
                last_actor=actor,
                last_used_at=now,
            )
        )
        session.flush()
        savepoint.commit()
    except IntegrityError:
        # 唯一约束仍然是第二道防线:拿不到 advisory lock 的路径照样能写进来
        # (迁移脚本、手工 SQL、将来漏了那把锁的 worker)。撞上了不是"正常并发",
        # 是**一个信号** —— reuse_count 加一把它记下来。但状态必须照样推进,
        # 否则就退回成本函数开头描述的那个不收敛的循环
        savepoint.rollback()
        existing = _receipt(session, key)
        if existing is not None:
            session.refresh(existing)
            existing.status = status.value
            existing.paid_calls += paid_calls
            existing.reuse_count += 1
            existing.result = dict(result)
            existing.last_actor = actor
            existing.last_used_at = now
            session.flush()


def _reuse(session: Session, receipt: BatchActionReceipt, *, actor: str) -> ExecResult:
    receipt.reuse_count += 1
    receipt.last_actor = actor
    receipt.last_used_at = utc_now()
    session.flush()
    return ExecResult(
        ok=True,
        message="上游未变化,复用上次结果,未触发付费模型调用",
        paid_calls=0,
        reused=True,
        payload=dict(receipt.result or {}),
    )


def _revalidate_billed_failure(
    session: Session,
    receipt: BatchActionReceipt,
    product: Product,
    *,
    actor: str,
) -> ExecResult:
    """已付费但后处理失败的重试:**只重新校验,不再调模型**(评审第 5 条)。

    原来的行为是失败一律不写回执,于是文案模型明明已经生成了内容、只是
    合规校验没过,普通重试仍然会把同一个提示词再发一次 —— 钱花两遍,
    而第二次拿到的多半是同一段被拒的文本。

    现在模型产出连同违规项都已落库(`listing_copies`),这里读那一行、
    按**当前**规则重跑一遍校验。规则改过或人工改过文案,这次就能过;
    什么都没变,结论和上次一样,但**没有新的费用**。
    """
    from app.listings import copy_service

    receipt.reuse_count += 1
    receipt.last_actor = actor
    receipt.last_used_at = utc_now()

    row = wb._current_copy(session, product.spu)
    if row is None:
        # 回执说付过费,但文案行已经不在了(归档 / 手工清理)。
        # 没有东西可以重新校验,如实报失败,**不冒充复用**
        session.flush()
        return ExecResult(
            ok=False,
            code=BatchErrorCode.COPY_GENERATION_FAILED,
            message="上次已付费生成的文案行已不存在,无法离线重新校验;请重新生成",
            target_step=FlowStep.COPY,
            paid_calls=0,
        )

    copy_rules_set = copy_service.resolve_rules(generic.CHANNEL, generic.SITE)
    violations = copy_service.revalidate(session, row, rules=copy_rules_set)
    blocking = [v for v in violations if getattr(v.level, "value", str(v.level)) == "error"]
    session.flush()

    payload = {
        "copy_id": str(row.id),
        "version": row.version,
        "revalidated": True,
        "usage": rules.usage_dict(attempted_calls=0, billable_calls=0, unit="call"),
    }
    if blocking:
        first = blocking[0]
        return ExecResult(
            ok=False,
            code=BatchErrorCode.COPY_RULE_VIOLATION,
            message=(
                f"上次已付费生成的文案仍有 {len(blocking)} 处硬失败:"
                f"{getattr(first, 'message', '') or getattr(first, 'code', '')};"
                "本次只重新校验,未再次调用模型"
            ),
            target_step=FlowStep.COPY,
            target_ref=str(getattr(first, "location", "") or ""),
            paid_calls=0,
            payload=payload,
        )
    # 重新校验通过:这条回执从"已付费未通过"转成"已完成",
    # 后续重试就走正常的复用分支了
    receipt.status = rules.ReceiptStatus.SUCCEEDED.value
    receipt.result = dict(payload)
    session.flush()
    return ExecResult(
        ok=True,
        message=f"上次已付费的文案 v{row.version} 重新校验通过,未再次调用模型",
        paid_calls=0,
        reused=True,
        payload=payload,
    )


# ==========================================================
# 四个执行器
# ==========================================================


def _exec_extract(session: Session, product: Product, *, actor: str) -> ExecResult:
    """逐图识别。**成功数为 0 时这一件是失败的。**

    A2:`run_extraction` 逐图捕获异常、只把失败计进 `failed_count`,函数本身
    永远正常返回。这里以前无条件 `ok=True`,后果不止是界面上显示"识别完成:
    N 张图"——`ok=True` 会写幂等回执,此后普通重试直接命中回执、复用这次
    全失败的空结果,连重跑的机会都没有。

    口径:
        成功 0             -> FAILED,不写回执(沿用"失败不写回执"的既有约定)
        成功 >0 且有失败    -> 成功,但消息里说清几张没识别出来
        全部成功           -> 成功

    全失败时**不执行 `apply_evidence`**:没有任何证据可合并,跑一遍只会
    在审计里留下一次"什么都没改"的属性写入。
    """
    extraction = attr_service.run_extraction(
        session, product=product, extractor=get_extractor()
    )
    succeeded = extraction.succeeded_count or 0
    failed = extraction.failed_count or 0

    # 评审第 4 条:识别是**逐图**调用模型的,而这里原来无论几张图都记 1 次。
    # 一件 8 张图的商品,费用台账少算 7 次;批次的"节省金额"因此也是假的。
    #
    # attempted 取图片数:每一张都发起过请求。billable 取同一个数而不是
    # 只算成功的那些 —— 失败可能发生在响应解析阶段,那时厂商侧已经计费。
    # 宁可多算:多算会在台账上多出一个能被人核对掉的数字,少算则永远不会
    # 有人发现。
    # R1-39:`int(x or 0) or (a + b)` 里那个 `or` 有两层含义压在一起 ——
    # "None 当 0" 和 "0 当没测到,回落"。读的人分不出 `image_count == 0`
    # 走的是哪一条,而两条的账完全不同。拆开写,行为一字不变。
    images = int(extraction.image_count or 0)
    attempted = images if images > 0 else (succeeded + failed)
    usage = rules.usage_dict(
        attempted_calls=attempted,
        billable_calls=attempted,
        unit="image",
        images=attempted,
    )

    if succeeded == 0:
        return ExecResult(
            ok=False,
            code=BatchErrorCode.EXTRACTION_FAILED,
            message=f"{failed} 张图全部识别失败,没有产生任何属性证据;可重试",
            target_step=FlowStep.ATTRIBUTE,
            paid_calls=attempted,
            payload={
                "extraction_id": str(extraction.id),
                "image_count": extraction.image_count,
                "succeeded_count": succeeded,
                "failed_count": failed,
                "usage": usage,
            },
        )

    attr_service.apply_evidence(
        session, product=product, extraction=extraction, actor=actor
    )
    message = f"识别完成:{extraction.image_count} 张图"
    if failed:
        message += f"(其中 {failed} 张失败,可只重试失败图片)"
    return ExecResult(
        ok=True,
        message=message,
        paid_calls=attempted,
        payload={
            "extraction_id": str(extraction.id),
            "image_count": extraction.image_count,
            "succeeded_count": succeeded,
            "failed_count": failed,
            "usage": usage,
        },
    )


def _exec_generate_copy(
    session: Session, product: Product, *, actor: str
) -> ExecResult:
    """生成文案。**不批准。**

    批准是人对内容负责的动作。批量把 50 件文案直接批准等于取消审核这一步,
    而 §6.3 的通过标准里没有一条要求"自动批准" —— 它只要求批量把商品
    推到"等人批准",于是这里的成功态是 `NEEDS_HUMAN` 的前一步。
    """
    row = wb.generate_copy(session, product, actor=actor)

    # 评审第 4 条:模板生成器**没有任何远程调用**,而这里原来无条件记 1 次
    # 付费调用。默认配置下跑一个 50 件的批次,台账上凭空出现 50 次不存在的
    # 费用 —— 而"复用省下了多少钱"正是拿这个数算的。
    #
    # 判据取落库的 `model_name`(= 实际产出这一版的生成器名),不是当前
    # 配置:配置可能在生成之后被改过,那时算出来的是"现在会用哪个",
    # 不是"这一版是谁写的"。
    from app.listings import copy_generator as cg

    billable = 1 if (row.model_name or "") == cg.LLM_GENERATOR else 0
    usage = rules.usage_dict(
        attempted_calls=billable, billable_calls=billable, unit="call"
    )
    payload = {
        "copy_id": str(row.id),
        "version": row.version,
        "generator": row.model_name,
        "usage": usage,
    }

    blocking = [v for v in (row.violations or []) if str(v.get("level")) == "error"]
    if blocking:
        first = blocking[0]
        return ExecResult(
            ok=False,
            code=BatchErrorCode.COPY_RULE_VIOLATION,
            message=f"文案有 {len(blocking)} 处硬失败:{first.get('message') or first.get('code')}",
            target_step=FlowStep.COPY,
            target_ref=str(first.get("location") or first.get("code") or ""),
            paid_calls=billable,
            payload=payload,
        )
    return ExecResult(
        ok=True,
        message=f"已生成 v{row.version},等待人工批准",
        paid_calls=billable,
        payload=payload,
    )


def _exec_validate_draft(
    session: Session, product: Product, *, actor: str
) -> ExecResult:
    """生成/重建草稿并校验。手填字段不传 —— `build_draft` 会沿用上一版。"""
    row = wb.build_draft(session, product, manual=None, actor=actor)
    if row.status == DraftStatus.INVALID.value:
        errors = list(row.validation_errors or [])
        first = errors[0] if errors else {}
        return ExecResult(
            ok=False,
            code=BatchErrorCode.DRAFT_FIELD_INVALID,
            message=(
                f"草稿有 {len(errors)} 个字段不合格:"
                f"{first.get('field_key') or '?'} {first.get('message') or ''}"
            ).strip(),
            target_step=FlowStep.DRAFT,
            # 字段键就是"一键跳转到具体字段"的那个坐标(§6.3.1 第二条)
            target_ref=str(first.get("field_key") or ""),
            payload={"draft_id": str(row.id), "error_count": len(errors)},
        )
    return ExecResult(
        ok=True,
        message=f"草稿校验通过({row.status})",
        payload={
            "draft_id": str(row.id),
            "fingerprint": row.source_fingerprint,
            "spec_version": row.spec_version,
        },
    )


def _exec_export(
    session: Session, product: Product, *, actor: str, batch_id: str
) -> ExecResult:
    """走 `export_gate` 的三道闸。**只表示"校验通过",不表示"已导出"。**

    **不在这里生成文件。** 文件由 `batch_file` 在批次跑完后合并成一份工作簿
    (见那个函数的注释):50 件商品发 50 个附件不是运营要的东西。
    这里只负责"这一件到底能不能导",并把 mapped 的身份信息记进结果。

    A3:以前这里在校验通过的当下就 `record_export`(export_count +1、落导出
    审计),而此时**没有任何文件存在** —— 文件要等运营点下载时由 `batch_file`
    现场生成,届时草稿若已过期还会 409,审计里却已经记着"导出成功一次"。

    现在的分界:

        校验通过(export_gate 过闸)  ≠  文件已生成(字节已产出)
        文件已生成                  ≠  已上传平台(PlatformStatus 手工记录)

    导出留痕与 export_count 移到 `batch_file` 真正产出 zip 之后。
    单件路径 `export_draft` 本来就是对的(先生成字节、后 record_export),
    保持不动。
    """
    row, mapped = wb.export_gate(session, product)
    return ExecResult(
        ok=True,
        message="已通过导出校验;文件在下载时生成",
        payload={
            "draft_id": str(row.id),
            "spu": product.spu,
            "fingerprint": row.source_fingerprint,
            "spec_version": row.spec_version,
            "sku_count": len(mapped.rows),
            # 批次明细与审计据此区分两件事,前端不必再猜
            "gate_passed": True,
            "file_generated": False,
        },
    )


def _execute(
    session: Session,
    action: BatchAction,
    item: PlannedItem,
    *,
    actor: str,
    batch_id: str,
) -> ExecResult:
    """执行一件。先查回执,命中则复用(不调模型)。

    执行前**重新查一次商品与状态**:同 SPU 的前一件可能已经改了上游,
    而计划阶段的 ctx 是那之前的快照。用旧快照执行会写出一个基于过期事实的结果。
    """
    product = session.get(Product, UUID(item.product_id))
    if product is None:
        raise NotFoundError(f"商品 {item.product_id} 已不存在")

    # ---- §7.3 P0:重复触发付费模型调用 ----
    #
    # 下面三步是一个典型的 check-then-act:查回执 -> 调模型 -> 写回执。
    # 唯一约束只能保证第二条回执插不进去,**挡不住第二次模型调用** ——
    # 那时钱已经花了,回执只能把这件事记下来。
    #
    # 锁的键就是幂等键本身:动作 + 作用域 + 上游指纹。粒度不能更粗
    # (按 SPU 会让不同动作互相阻塞),也不能更细(再细就不是同一件事了)。
    #
    # 拿不到锁 = 另一个请求正在做这件事。此时**不执行、不调模型**,
    # 报一个可解释的失败让人重试(见 `try_advisory_xact_lock` 里
    # 为什么必须非阻塞)。
    if not try_advisory_xact_lock(session, "batch_receipt", item.idempotency_key):
        return ExecResult(
            ok=False,
            code=BatchErrorCode.CONCURRENT_IN_FLIGHT,
            message=(
                f"{BATCH_ACTION_LABELS[action]}:同一商品正被另一个请求执行,"
                "本次已跳过且未调用付费模型"
            ),
            target_step=ACTION_STEP[action],
            target_ref=item.scope_key,
            paid_calls=0,
        )

    # 拿到锁之后再查回执。顺序不能反:先查后锁的话,查与锁之间仍有窗口。
    #
    # 评审第 5 条:回执不再是"存在 = 做成了"。四种状态各有各的走法,
    # 分派逻辑在 `batch.receipt_route`(纯函数,可穷举测试),这里只执行。
    receipt = _receipt(session, item.idempotency_key)
    route = rules.receipt_route(
        None if receipt is None else receipt.status,
        action,
        # A45-batch12:执行次数从「事后信号」升级成「闸门」。
        # 缺列的历史行按 1 处理,与迁移 0028 的 server_default 同一条理由
        executions=1 if receipt is None else (receipt.executions or 1),
        # A45-batch12-2 / EX-02:这一次执行到底把请求发出去了没有。
        # 没有回执时这个值不参与判定(status is None 直接 EXECUTE);
        # 有回执但这一列为空 = 0031 之前的存量行或没走到调用,
        # 两种都由 `receipt_route` 按同一条规则处理
        call_dispatched=(
            True if receipt is None else receipt.provider_call_at is not None
        ),
    )
    if receipt is not None:
        if route is rules.ReceiptRoute.REUSE_SUCCESS:
            return _reuse(session, receipt, actor=actor)
        if route is rules.ReceiptRoute.REUSE_BILLED_FAILURE:
            return _revalidate_billed_failure(
                session, receipt, product, actor=actor
            )
        if route is rules.ReceiptRoute.NEEDS_RECONCILIATION:
            # **停在这里,不调付费模型。**
            #
            # 上一次的外部调用可能已经成功并计费,只是结果没能落库;而这一件
            # 已经自动重跑过一次仍然没落地。再跑一次就是第三次可能的付费,
            # 而且发起者是回收器 —— 全程没有人决定过要不要再花这笔钱。
            #
            # 生成链路对同一种不确定性的处理就是停下来等对账
            # (`SUBMIT_RESULT_UNKNOWN` + force 必填对账结论)。这一条把批量
            # 侧对齐到同一个标准。解除通道见 `tools/resolve_billed_unknown.py`。
            logger.error(
                "the previous call was billed but its result is unknown; refusing a paid retry",
                extra={"extra_fields": {"event": "batch.billed_result_unknown_refusing_paid_retry",
                    "key": item.idempotency_key[:32],
                    "action": action.value,
                    "executions": receipt.executions,
                    "paid_calls": receipt.paid_calls,
                    "scope_key": item.scope_key,
                }},
            )
            # 两种成因,给运营的话不一样。混成一句的代价是让人去查一个
            # 不存在的时刻,或者反过来漏掉一个已知的时刻(A45-batch12-2)
            if receipt.provider_call_at is not None:
                detail = (
                    f"上一次执行已于 {receipt.provider_call_at:%Y-%m-%d %H:%M:%S} "
                    "发出付费调用,但结果没能落库。请照这个时刻到模型服务商后台对账"
                )
            else:
                detail = (
                    f"上一次执行可能已经计费但结果没落库,且已自动重跑 "
                    f"{receipt.executions} 次仍未落地。请到模型服务商后台对账"
                )
            return ExecResult(
                ok=False,
                code=BatchErrorCode.BILLED_RESULT_UNKNOWN,
                message=(
                    f"{BATCH_ACTION_LABELS[action]}:{detail}。"
                    "为避免重复计费已停下,本次未调用付费模型"
                ),
                target_step=ACTION_STEP[action],
                target_ref=item.scope_key,
                paid_calls=0,
            )
        if route is rules.ReceiptRoute.EXECUTE_AGAIN_BILLED:
            # 上次执行中途死掉(IN_FLIGHT),或已付费失败但没有可离线复校的
            # 原始结果(识别全失败:一条证据都没落)。只能重跑,**费用会累计**——
            # 台账上这条的 paid_calls > 1 正是要人看见的那个信号。
            # 这条路最多再走一次,之后由上面那个分支接管
            logger.warning(
                "the receipt says this item needs one more paid retry",
                extra={"extra_fields": {"event": "batch.receipt_requires_paid_retry",
                    "key": item.idempotency_key[:32],
                    "status": receipt.status,
                    "action": action.value,
                    "executions": receipt.executions,
                }},
            )

    # 付费动作在真正调用之前先落一条独立事务的 IN_FLIGHT 标记。
    # 校验与导出不调外部模型,不需要 —— 给它们也记一条只会让台账变吵
    if action in (BatchAction.EXTRACT, BatchAction.GENERATE_COPY):
        if not _claim_in_flight(
            key=item.idempotency_key,
            action=action,
            scope_key=item.scope_key,
            product_id=product.id,
            actor=actor,
        ):
            # 标记没落盘就**不许调用付费模型**(评审第 3 条)。原来这里是
            # 记一条日志继续往下走 —— 而标记存在的全部理由就是"崩溃后还能
            # 知道这次花没花钱";带着记不上继续调用,等于把它退化成日志,
            # 而代价是一笔谁也对不上的费用。
            #
            # 停下来的代价只是这一件要重试,而重试是安全的:没有标记就说明
            # 也没有调用。
            logger.error(
                "could not claim the in-flight marker; refusing to make a paid call",
                extra={"extra_fields": {"event": "batch.in_flight_claim_failed_refusing_paid_call",
                    "key": item.idempotency_key[:32],
                    "action": action.value,
                }},
            )
            return ExecResult(
                ok=False,
                code=BatchErrorCode.CONCURRENT_IN_FLIGHT,
                message=(
                    f"{BATCH_ACTION_LABELS[action]}:未能记录执行标记,"
                    "为避免产生无法对账的费用已中止,未调用付费模型。请重试"
                ),
                target_step=ACTION_STEP[action],
                target_ref=item.scope_key,
                paid_calls=0,
            )

    # **最后一刻的标记(EX-02)。** 放在这里而不是 `_claim_in_flight` 旁边:
    # 两者之间的语句越少,"有标记 = 请求真的发出去过"这句话越准。
    # 两个付费执行器的第一件事都是调模型,所以这里已经贴到不能更近。
    #
    # A45-batch12-3:返回值不再可以丢。原来这两句是
    # `_mark_provider_call(...)` 裸调,而标记写失败之后紧接着的付费调用
    # 会留下一条"没有标记的 IN_FLIGHT" —— 下一次回收把它读成「可证明没花钱」,
    # 自动重跑一次。理由写在 `_mark_provider_call` 的 docstring 里。
    if action in (BatchAction.EXTRACT, BatchAction.GENERATE_COPY):
        if not _mark_provider_call(key=item.idempotency_key):
            logger.error(
                "could not write the provider-call marker; refusing to make a paid call",
                extra={
                    "extra_fields": {
                        "event": "batch.provider_call_marker_failed_refusing_paid_call",
                        "key": item.idempotency_key[:32],
                        "action": action.value,
                    }
                },
            )
            # 落成"确认没花钱"。留着 IN_FLIGHT 也能免费重跑
            # (没有标记 -> `receipt_route` 判 EXECUTE),但这一行会被
            # `resolve_billed_unknown.py` 的第二个条件(executions >= 上限)
            # 捞出来变成一条假的对账工单 —— 而我们**知道**这次没调用,
            # 那就该说出来,而不是留一条"费用不明"让人去查
            _settle_unbilled_failure(session, key=item.idempotency_key)
            return ExecResult(
                ok=False,
                code=BatchErrorCode.CONCURRENT_IN_FLIGHT,
                message=(
                    f"{BATCH_ACTION_LABELS[action]}:未能记录付费调用标记,"
                    "为避免产生无法对账的费用已中止,未调用付费模型。请重试"
                ),
                target_step=ACTION_STEP[action],
                target_ref=item.scope_key,
                paid_calls=0,
            )

    if action is BatchAction.EXTRACT:
        result = _exec_extract(session, product, actor=actor)
    elif action is BatchAction.GENERATE_COPY:
        result = _exec_generate_copy(session, product, actor=actor)
    elif action is BatchAction.VALIDATE_DRAFT:
        result = _exec_validate_draft(session, product, actor=actor)
    elif action is BatchAction.EXPORT:
        result = _exec_export(session, product, actor=actor, batch_id=batch_id)
    else:  # pragma: no cover - 枚举已穷举
        raise ValidationError(f"不支持的批量动作 {action}")

    # 回执落终态。**"未计费的失败"仍然不写回执** —— 那条约定是对的:
    # 它的意思是"这件事等于没做过",重试该真的重跑,FE-304 靠它成立。
    #
    # 变的是另一半(评审第 5 条):**已经计费的失败也要留痕**。原来这里
    # 只有 `if result.ok`,于是文案模型已经出了内容、只是合规校验没过时,
    # 回执一个字不写,普通重试把同一个提示词再发一次 —— 钱花两遍。
    if result.ok:
        _write_receipt(
            session,
            key=item.idempotency_key,
            action=action,
            scope_key=item.scope_key,
            product_id=product.id,
            paid_calls=result.paid_calls,
            result=result.payload,
            actor=actor,
            status=rules.ReceiptStatus.SUCCEEDED,
        )
    elif result.paid_calls > 0:
        _write_receipt(
            session,
            key=item.idempotency_key,
            action=action,
            scope_key=item.scope_key,
            product_id=product.id,
            paid_calls=result.paid_calls,
            result=result.payload,
            actor=actor,
            status=rules.ReceiptStatus.FAILED_BILLED,
        )
    else:
        # 没花钱的失败:把 IN_FLIGHT 标记落成 FAILED_UNBILLED,
        # 不留一条"费用不明"在台账上等人核对
        _settle_unbilled_failure(session, key=item.idempotency_key)
    return result


def _settle_unbilled_failure(session: Session, *, key: str) -> None:
    """把 `_claim_in_flight` 留下的标记落成"确认没花钱"。

    标记是独立事务写的,这里的清理走业务事务 —— 万一业务事务回滚,
    标记留在 IN_FLIGHT,下次重试会按"费用不明"处理。方向仍然是宁可多核对。
    """
    row = _receipt(session, key)
    if row is not None and row.status == rules.ReceiptStatus.IN_FLIGHT.value:
        # 历史上花过钱就**不能降级成"没花钱"**:FAILED_UNBILLED 的语义是
        # "等价于没做过",而 paid_calls > 0 说明做过、而且付过费。把它抹成 0
        # 会让台账少算,而少算的数字永远不会有人发现(A15 第一节的老教训)。
        # 这一次没花钱,但这条作用域历史上花过 —— 那仍然是已付费的失败。
        if row.paid_calls > 0:
            row.status = rules.ReceiptStatus.FAILED_BILLED.value
        else:
            row.status = rules.ReceiptStatus.FAILED_UNBILLED.value
        session.flush()


# ==========================================================
# 批次:创建、执行、重试
# ==========================================================


def _by_request_key(session: Session, key: str) -> BatchJob | None:
    return session.scalars(
        select(BatchJob).where(BatchJob.request_key == key)
    ).first()


def _same_request_or_conflict(job: BatchJob, fingerprint: str | None) -> BatchJob:
    """同一把请求键再来一次:是重发,还是两件不同的事(EX-06)。

    指纹一致 = 重发,返回原批次,客户端拿到的 `batch_id` 与第一次相同。
    这正是双击和"响应回程丢失后重试"要的结果。

    指纹不一致 = **必须报错,不能悄悄返回原批次。** 客户端用同一把键提交了
    两件不同的事;返回其中任意一个,它都会以为另一件也做了 —— 那比多建一个
    批次更危险,因为它是静默的:界面上一切正常,只是有一批商品从来没被处理过。

    409 而不是 400:请求本身没毛病,是它和已经存在的那条记录冲突。
    """
    if job.request_fingerprint == fingerprint:
        logger.info(
            "batch create reused an existing request key",
            extra={
                "extra_fields": {
                    "event": "batch.create_reused_request_key",
                    "batch_id": str(job.id),
                }
            },
        )
        # 瞬态标记,不落库。调用方必须知道这一次**没有创建任何东西** ——
        # 不知道的话它会照常执行一遍,而那正是这条幂等要挡的重复执行。
        # 不加一列的理由:这件事只在本次响应里有意义,存进库等于把一个
        # 请求级事实钉在一个长期存在的行上,下一次读到它的人会误解
        job.reused_request = True  # type: ignore[attr-defined]
        return job
    raise ValidationError(
        "这把 Idempotency-Key 已经用在另一组入参上了。"
        "同一把键只能对应同一次请求 —— 要新建一个不同的批次请换一把键",
        code=ErrorCode.DUPLICATE_RESOURCE,
        http_status=409,
    )


def create_batch(
    session: Session,
    *,
    action: BatchAction,
    product_ids: Sequence[UUID],
    actor: str,
    label: str | None = None,
    include_exported: bool = False,
    request_key: str | None = None,
) -> BatchJob:
    """建批次并落全部条目。计划快照进 `params`,事后可与执行结果对照。

    ## `request_key`:创建这条命令本身的幂等(A45-batch12-2 / EX-06)

    不传时行为与从前一字不差。传了的话:

        同键 + 同入参   返回**原来那个批次**,不建第二个
        同键 + 不同入参 409 —— 客户端用同一把键提交了两件不同的事,
                        返回其中任意一个都会让它以为另一件也做了

    这一层与 `batch_action_receipts` 不重叠:那一层挡的是"同一商品同一动作
    被跑两遍"(批次内部),这一层挡的是"同一个创建请求被受理两遍"。
    双击、以及第一次响应在回程丢失后的客户端重发,走的都是这一层。
    """
    fingerprint: str | None = None
    # A45-batch12-3:超长**报错,不截断**。截到 64 之后,两把只有前 64 字符
    # 相同的键会变成同一把 —— 表现是第二次提交拿到一个 409,而那个 409 说的是
    # "这把键已经用在另一组入参上了",没有任何人能从这句话推出"你的键太长了"。
    # 静默改写调用方给的标识符,和 EX-06 反对的静默返回原批次是同一类事
    key = (request_key or "").strip() or None
    if key is not None and len(key) > 64:
        raise ValidationError(
            "Idempotency-Key 最长 64 个字符。这一层不截断:截断之后两把只有"
            "前缀相同的键会被当成同一把,而那种冲突报出来的错和真正的键冲突"
            "长得一模一样",
            code=ErrorCode.INPUT_INVALID,
            http_status=422,
        )
    if key is not None:
        fingerprint = rules.batch_request_fingerprint(
            action,
            [str(pid) for pid in product_ids],
            include_exported=include_exported,
            label=label,
        )
        existing = _by_request_key(session, key)
        if existing is not None:
            return _same_request_or_conflict(existing, fingerprint)

    plan_result = plan(
        session, action, product_ids, include_exported=include_exported
    )
    # A41:原来是 aware 的 `datetime.now(UTC)` 赋给变量、51 行之后才
    # `.replace(tzinfo=None)`。中间隔着整个 job 构造 —— 那个形状今天是对的,
    # 但下一个人在中间新加一处 `now` 的用法就会静默错一个时区
    # (见 core/clock.py「收敛没有做完」)
    now = utc_now()

    job = BatchJob(
        action=action.value,
        status=BatchJobStatus.QUEUED.value,
        label=label or None,
        created_by=actor,
        request_key=key,
        request_fingerprint=fingerprint,
        params={
            "include_exported": include_exported,
            "selected": len(product_ids),
            # FE-301 的验收结果:执行前显示的两个数字。存下来,验收时可回看
            "planned_executable": plan_result.executable_count,
            "planned_blocked": plan_result.blocked_count,
            "planned_by_code": plan_result.by_code,
        },
    )
    job.reused_request = False  # type: ignore[attr-defined]
    # ---- A45-batch12-2 / EX-06:输给并发的那一方回读,不报错 ----
    #
    # 上面那句"查一下有没有"是 check-then-act:双击产生的两个请求会双双查空。
    # 裁决只能由唯一约束做出,而这里的职责是**认输之后把赢家读回来** ——
    # 与 `generation_service` 处理生成幂等键冲突是同一个套路。
    #
    # 保存点是必须的:不套的话这次 INSERT 失败会把整个会话打成不可用状态,
    # 接下来那次回读查询根本发不出去。
    #
    # ---- A45-batch12-3 / REG-04:`add()` 必须在保存点**里面** ----
    #
    # 上一版是 `session.add(job)` 在外、`begin_nested()` 在内。而
    # `begin_nested()` 会先把当前 pending 对象 flush 一次,**那次 flush 发生在
    # SAVEPOINT 语句之前**:唯一键冲突于是在 `begin_nested()` 这一句抛出来,
    # 而它在 `try` 之外 —— `IntegrityError` 直接冒到接口层,第二个并发请求
    # 收到 500,而不是回读到第一个批次。
    #
    # 数据库那条唯一约束仍然拦住了第二条 BatchJob,所以这不是重复建批次;
    # 但"双击之后一个 500"和 EX-06 想要的"双击之后两次都拿到同一个批次"
    # 差着一整条用户路径:运营看到 500 会再点一次。
    #
    # 把 `add` 和 `flush` 一起关进保存点之后,进入 `begin_nested()` 时会话里
    # 没有这条 pending 的 job,那次预 flush 无事可做,冲突只可能由块内的
    # `flush()` 抛出 —— 也就是 `except IntegrityError` 真的盖得住的地方。
    if key is not None:
        try:
            with session.begin_nested():
                session.add(job)
                session.flush()
        except IntegrityError:
            # `with` 退出时保存点已经回滚,会话可用,回读发得出去
            winner = _by_request_key(session, key)
            if winner is None:
                # 撞了唯一约束却读不到赢家:不是这条约束造成的冲突。
                # 咽下去会把一个别的问题伪装成"重复请求",原样抛出去
                raise
            logger.info(
                "lost the batch idempotency race; reusing the winner",
                extra={
                    "extra_fields": {
                        "event": "batch.create_lost_idempotency_race",
                        "key": key[:32],
                        "batch_id": str(winner.id),
                    }
                },
            )
            return _same_request_or_conflict(winner, fingerprint)
    else:
        session.add(job)
        session.flush()

    for item in plan_result.items:
        session.add(
            BatchJobItem(
                batch_id=job.id,
                product_id=UUID(item.product_id),
                sku=item.sku,
                spu=item.spu,
                scope_key=item.scope_key,
                status=(
                    BatchItemStatus.PENDING.value
                    if item.executable
                    else rules.status_for(
                        item.code or BatchErrorCode.PRECHECK_MISMATCH
                    ).value
                ),
                error_code=None if item.executable else str(item.code),
                error_message=None if item.executable else item.reason,
                target_step=(
                    None
                    if item.executable or item.target_step is None
                    else item.target_step.value
                ),
                target_ref=None if item.executable else item.target_ref,
                idempotency_key=item.idempotency_key,
                finished_at=None if item.executable else now,
            )
        )
    session.flush()

    audit.record(
        session,
        actor=actor,
        action=AuditAction.CREATE,
        entity_type="BatchJob",
        entity_id=job.id,
        payload={
            "action": action.value,
            "executable": plan_result.executable_count,
            "blocked": plan_result.blocked_count,
        },
    )
    return job


#: 一次领几件。**值和理由都在 `batch.CLAIM_CHUNK`,这里只是转出来。**
#:
#: 它搬走了,因为它参与租约不变量(`ITEM_LEASE_SECONDS > CLAIM_CHUNK ×
#: 单件最长合法耗时`),而那条 assert 在判定模块里 —— 常量留在这边的话
#: 那条 assert 就看不见它,只能退化成单件断言,而**单件断言正是原来那一版
#: 漏掉这个缺陷的原因**。留下这个名字是为了不改两个调用点,
#: 也为了从这边找过去的人能看到它去哪了。
CLAIM_CHUNK = rules.CLAIM_CHUNK


def _worker_id() -> str:
    """领取者标识。**只用于排查,不参与任何判定。**

    判定只看 `lease_until`,理由见 `models/batch_job.py` 里那两列的注释:
    拿 owner 判定就得先有一份"哪些 worker 还活着"的名单,而系统里没有。
    """
    return f"{socket.gethostname()[:40]}:{os.getpid()}"


def claim_items(
    session: Session,
    job: BatchJob,
    *,
    owner: str,
    limit: int = CLAIM_CHUNK,
    now: datetime | None = None,
    exclude_ids: Collection[UUID] | None = None,
) -> list[BatchJobItem]:
    """原子领取一小批条目并盖上租约(任务 17)。**调用方负责提交。**

    ## `FOR UPDATE SKIP LOCKED`

    和 `publish_service.claim_due` / `poll_service` 同一个理由:两个 worker
    同时跑同一个批次时各自拿到互不相交的一批,而不是排队等同一批。
    没有它时两边会捞到同一批 PENDING 行、同时置 RUNNING、同时开跑,
    最后靠 `_execute` 里的 advisory lock 挡下来 —— 挡是挡住了,但代价是
    一半的条目落成 `CONCURRENT_IN_FLIGHT`,运营看到的是"跑了一半失败了"。

    这也是**重复投递安全**的真正来源:批次消息可以放心地投第二次
    (`redispatch_stalled_batches` 就靠这一条),因为第二个 worker 要么
    领到别的条目,要么什么都领不到然后退出。

    ## 为什么把过期的 RUNNING 也一起领了

    worker 死在半路时,它领走的那几件停在 RUNNING。只捞 PENDING 的话
    那几件谁都碰不到 —— 这正是本轮之前的行为。租约过期就说明"没人在跑了",
    此时它和 PENDING 是同一件事,一起领即可。

    `lease_until IS NULL` 必须显式放行:SQL 里 `NULL <= now` 求值是 NULL
    不是真,漏掉这一条,0025 迁移之前留下的 RUNNING 残骸就永远领不到。
    判定口径与 `batch.lease_expired()` 一致,那里有完整理由。

    ## `exclude_ids`:本轮已经处理过的,**不要领**(A-23)

    调用方原来是领完之后在 Python 里 `if row.id not in seen` 过滤掉。
    可那时这一行**已经被这条 SQL 改过了**:置 RUNNING、盖新租约、
    `attempts + 1`。于是被过滤掉的行本轮既不执行也不释放,却白烧了一次
    尝试次数 —— 烧满 `MAX_ITEM_ATTEMPTS`(=3)之后它落 `WORKER_LOST`,
    而它一次都没真正跑过。

    过滤必须发生在 `UPDATE` **之前**,也就是这里。
    """
    moment = now or utc_now()
    deadline = moment + timedelta(seconds=rules.ITEM_LEASE_SECONDS)
    stmt = (
        select(BatchJobItem)
        .where(
            BatchJobItem.batch_id == job.id,
            (BatchJobItem.status == BatchItemStatus.PENDING.value)
            | (
                (BatchJobItem.status == BatchItemStatus.RUNNING.value)
                & (
                    (BatchJobItem.lease_until.is_(None))
                    | (BatchJobItem.lease_until <= moment)
                )
            ),
        )
        .where(
            # 空集合时不加条件:`NOT IN ()` 在不同方言下的行为不一致,
            # 而这是最常见的那一路(第一轮 seen 是空的)
            sa_true() if not exclude_ids else BatchJobItem.id.notin_(list(exclude_ids))
        )
        .order_by(BatchJobItem.created_at, BatchJobItem.sku)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = list(session.scalars(stmt))
    for row in rows:
        row.status = BatchItemStatus.RUNNING.value
        row.attempts = (row.attempts or 0) + 1
        row.lease_owner = owner
        row.lease_until = deadline
        # A43 / BLOCK-02:每一次领取生成一个新令牌。**上一任的令牌从此作废**,
        # 它的落库会打在 `WHERE lease_token = :token` 上,影响 0 行。
        # 令牌由领取方直接生成而不是读一次再加一:"读-改-写"在两个 worker
        # 之间正是这里要防的那件事
        row.lease_token = uuid4()
        row.started_at = row.started_at or moment
    session.flush()
    return rows


def renew_lease(
    session: Session,
    item_id: UUID,
    *,
    owner: str,
    token: UUID,
    now: datetime | None = None,
) -> bool:
    """把这一件的租约往后推。**返回是否还持有执行权。**

    A43 / BLOCK-02。调用点在**付费调用之前** —— 这是整个链路上唯一一个
    "已经知道自己失去执行权、但还没花钱"的时刻。返回 False 时必须停下来,
    继续跑下去只会产生一个注定要被丢弃的结果,而钱是真花了的。

    条件更新的四个条件与 `rules.lease_still_held()` 一一对应,
    判定口径必须只有一份:那边是纯函数(可穷举测试),这边是 SQL(可并发验证)。
    两边分叉的表现会是"单机测试全绿、真库下偶尔覆盖" —— 最难查的那一类。

    `lease_until` **不在**条件里,理由见 `lease_still_held()` 的注释:
    交接由令牌吊销定义,不由时钟定义。一件跑超了但还没被抢走的条目,
    仍然有权把自己的结果落库,也仍然有权续租。
    """
    moment = now or utc_now()
    deadline = moment + timedelta(seconds=rules.ITEM_LEASE_SECONDS)
    result = session.execute(
        update(BatchJobItem)
        .where(
            BatchJobItem.id == item_id,
            BatchJobItem.status == BatchItemStatus.RUNNING.value,
            BatchJobItem.lease_owner == owner,
            BatchJobItem.lease_token == token,
        )
        .values(lease_until=deadline)
        .execution_options(synchronize_session=False)
    )
    held = result.rowcount == 1
    if not held:
        logger.warning(
            "the item lease was lost before execution started; skipping",
            extra={
                "extra_fields": {"event": "batch.lease_lost_before_execution",
                    "item_id": str(item_id),
                    "owner": owner,
                    "phase": "renew",
                }
            },
        )
    return held


def lease_budget_shortfall() -> str | None:
    """**部署的配置**下,租约还够不够长。够就返回 None(A45-batch18 / P2-1)。

    `batch.py` 里那条模块级 `if` 用的是三项配置的**默认值**,它挡得住
    "有人改了常量却忘了改租约",挡不住"运维在设置页把单张超时从 60 调到
    120、把图片上限从 12 调到 20"。后者一样会让租约短于单件合法耗时,
    而后果是一样的:回收器从一个正在正常工作的 worker 手里抢走条目,
    那次付费调用的结果被 fencing 丢弃。

    做成"返回一句话"而不是直接抛:这个函数在**启动**时被调用,
    而一份让服务起不来的配置错误,和一份让批次偶尔重复付费的配置错误,
    严重程度不是一个量级。调用方(`main.lifespan`)按 error 记日志,
    人看得见、也改得动。

    读配置用 `provider_int` / `provider_float` 那一套,和识别层
    (`extractors/vision.ExtractorModelConfig.from_settings`)同一个来源 ——
    自己再读一遍 `settings` 的话,数据库覆盖那一层会被跳过,而设置页改的
    正是那一层。
    """
    from app.extractors import call_budget
    from app.providers._config import provider_float, provider_int

    budget = rules.longest_legal_item_seconds(
        timeout_seconds=provider_float("EXTRACTOR_MODEL_TIMEOUT_SECONDS", 60.0),
        max_retries=provider_int("EXTRACTOR_MODEL_MAX_RETRIES", 2),
        max_images=call_budget.effective_ceiling(
            provider_int(
                "EXTRACTOR_MAX_IMAGES_PER_RUN", call_budget.DEFAULT_MAX_IMAGES_PER_RUN
            )
        ),
    )
    covered = rules.ITEM_LEASE_SECONDS
    if covered > rules.CLAIM_CHUNK * budget:
        return None
    return (
        f"当前配置下单件 EXTRACT 最长合法耗时 {budget} 秒"
        f"(超时 × (1+重试) × 单次图片上限),而条目租约只有 {covered} 秒。"
        "一件正在正常跑的条目会被回收器判过期并被另一个 worker 抢走,"
        "旧 worker 那次**已经付过费**的调用结果随后被 fencing 丢弃。"
        "请调低 EXTRACTOR_MODEL_TIMEOUT_SECONDS / EXTRACTOR_MAX_IMAGES_PER_RUN,"
        "或调高 workbench/batch.ITEM_LEASE_SECONDS"
    )


def run_batch(
    session: Session,
    job: BatchJob,
    *,
    actor: str,
    commit_each: bool = True,
    owner: str | None = None,
) -> BatchCounts:
    """执行批次里的待办条目,**每次领一小批**。单件失败不终止批次。

    循环结构在 `batch.run_plan`(纯函数,可穷举测试);这里只提供
    "执行一件"和"落一件"两个回调。

    ## 每件一个**事务**(评审第 2 条),不只是一个保存点

    保存点解决的是"第 37 件炸了别回滚前 36 件",它在同一个事务内成立。
    但整批仍然是一个长事务,于是:

        请求断开     外部调用可能已经计费,而事务整个回滚 —— 钱花了,
                     回执和结果一行都没留下
        锁占用       advisory lock 与行锁一直持有到最后一件跑完
        超时         50 件模型调用挤在一个 HTTP 请求 / 一个事务里

    现在每落完一件就提交一次:已完成的条目、回执、审计立刻落盘,
    后面发生什么都不再影响它们。`commit_each=False` 留给已经自己
    管着事务边界的调用方(测试夹具),默认必须是 True。

    ## 为什么改成分批领(任务 17)

    原来是"一次把全部 PENDING 读进来,然后慢慢跑"。那一读没有加锁,
    于是同一个批次被投递两次时,两个 worker 读到的是同一份清单。
    现在每一小批都经过 `claim_items` 的 `FOR UPDATE SKIP LOCKED`,
    并在开跑前把租约提交出去 —— 租约不落库就等于没有,别的 worker
    看不见内存里的对象。

    `commit_each=False` 时租约仍然会写,但不单独提交:那条路径是测试夹具,
    它本来就只有一个 session,没有"别的 worker"要看见这件事。
    """
    action = BatchAction(job.action)
    worker = owner or _worker_id()
    outcomes: list[rules.ItemOutcome] = []
    #: 执行权已经丢了才跑完的结果。它们进了审计,没进业务行(见 apply_outcome)。
    #: 收集起来是为了在批次结束时报一次数 —— 一条 stale outcome 意味着
    #: 有一件商品被跑了两次,那不是正常运行的一部分
    stale: list[rules.ItemOutcome] = []
    #: 本轮已经处理过的条目。正常情况下用不到 —— 每一件都会落到终态,
    #: 下一轮自然领不到。它防的是"落库回调本身出错导致状态没推进",
    #: 那种情况下没有这个集合就是一个**无限循环**,而且每一圈都可能
    #: 是一次付费调用
    seen: set[UUID] = set()

    while True:
        # A-23:`seen` 下推到 SQL。留在 Python 里的话,被过滤掉的那些行
        # 已经被 `claim_items` 置成 RUNNING 且 `attempts + 1` 了 ——
        # 本轮既不跑也不放,白烧一次尝试次数
        claimed = claim_items(
            session, job, owner=worker, limit=CLAIM_CHUNK, exclude_ids=seen
        )
        if not claimed:
            break
        seen.update(row.id for row in claimed)

        if job.status != BatchJobStatus.RUNNING.value:
            job.status = BatchJobStatus.RUNNING.value
        # A34 把这里从被点名禁止的 `datetime.now(UTC).replace(tzinfo=None)`
        # 收敛成 `utc_now()`,是全仓最后一个原样写法;分批领之后它挪进了循环,
        # 语义不变(仍然只在第一轮真正赋值)
        job.started_at = job.started_at or utc_now()
        session.flush()
        if commit_each:
            # 租约与 RUNNING 必须在开跑**之前**落库。晚一步的后果是:
            # 第一件跑到一半时另一个 worker 进来,库里还看不到租约,
            # 于是它把同一批又领了一遍
            session.commit()

        planned = rules.BatchPlan(
            action=action,
            items=tuple(
                PlannedItem(
                    product_id=str(row.product_id),
                    sku=row.sku,
                    spu=row.spu,
                    scope_key=row.scope_key,
                    executable=True,
                    idempotency_key=row.idempotency_key,
                )
                for row in claimed
            ),
        )
        by_product = {str(row.product_id): row for row in claimed}
        # 领取时拿到的令牌。**必须在这里存下来** —— `row.lease_token` 在
        # 落库之后会被清空,而条件更新的 WHERE 要用的是"我领的时候是哪个"
        tokens = {str(row.product_id): row.lease_token for row in claimed}

        # `by_product` / `tokens` 每轮循环重新赋值,而这两个闭包必须看到
        # **本轮**那一份。今天它们在同一轮里就被 `run_plan` 消费掉,所以
        # 自由变量取值是对的 —— 但那是时序上的巧合,不是这段代码说清楚的事。
        # 哪天有人把 `execute` / `persist` 攒起来延后跑(批量提交、线程池、
        # 重试队列都会导致这个),它们会静默地拿到**最后一轮**的行和令牌:
        # 条件更新的 WHERE 用错令牌,表现是"这一件的执行权莫名其妙不在手上"。
        #
        # `persist` 原来只绑了 `by_product` 一个、漏了 `tokens`,`execute`
        # 两个都没绑 —— 有人看见过这个坑,但只补了三分之一。两个闭包、
        # 两个名字,一次绑齐(ruff B023)。
        def execute(
            item: PlannedItem,
            _by_product: dict = by_product,
            _tokens: dict = tokens,
        ) -> ExecResult:
            row = _by_product[item.product_id]
            # A43 / BLOCK-02:**花钱之前先确认自己还有执行权。**
            #
            # 这是整条链路上唯一一个"已经知道自己出局、但还没花钱"的时刻。
            # 续租同时把 `lease_until` 从"领取时刻 + 30 分钟"推到
            # "开跑时刻 + 30 分钟" —— 领取与开跑之间的排队时间不再吃掉租约。
            if not renew_lease(
                session, row.id, owner=worker, token=_tokens[item.product_id]
            ):
                raise rules.LeaseLostError(
                    "这一件的执行权已经不在本 worker 手上(租约被回收或已被重试重置),"
                    "本次不再执行"
                )
            if commit_each:
                # **续租必须在付费调用之前变成别的会话看得见的事实**
                # (A45-batch18 / P2-1)。
                #
                # 原来这条 UPDATE 留在外层事务里,而外层事务要等 `_execute()`
                # 跑完、结果保存完才提交。也就是说:整个付费调用期间,
                # `reap_expired_leases` 在它自己的会话里读到的仍然是**领取时**
                # 那个 `lease_until`。续租等于没续 —— 它挡住的只是"我已经
                # 出局了"这一种情况,挡不住"我正在跑却被判过期"。
                #
                # 提交在这里是安全的:上一件已经在 `persist` 里提交过,
                # 本轮 `claim_items` 之后也提交过,此刻事务里只有这一条
                # UPDATE,没有任何半成品业务写入会被一起提交出去。
                #
                # `commit_each=False` 那条路径是测试夹具,它只有一个 session,
                # 本来就不存在"别的 worker 看不看得见"这个问题
                session.commit()
            savepoint = session.begin_nested()
            try:
                result = _execute(
                    session, action, item, actor=actor, batch_id=str(job.id)
                )
                savepoint.commit()
            except Exception:
                savepoint.rollback()
                raise
            return result

        def persist(
            outcome: rules.ItemOutcome,
            _by_product: dict = by_product,
            _tokens: dict = tokens,
        ) -> None:
            row = _by_product[outcome.item.product_id]
            applied = apply_outcome(
                session,
                row,
                outcome,
                owner=worker,
                token=_tokens[outcome.item.product_id],
                actor=actor,
            )
            if not applied:
                stale.append(outcome)
            if commit_each:
                # 事务边界落在**每一件之后**。提交的同时释放这一件的
                # advisory lock —— 锁的作用域本来就只有"执行这一件",
                # 持有到整批结束纯属副作用(评审第 2 条)
                session.commit()

        outcomes.extend(rules.run_plan(planned, execute, on_outcome=persist))

    session.flush()

    counts = rules.count_rows(_item_dicts(session, job.id))
    settle(session, job, counts)
    if commit_each:
        session.commit()

    if stale:
        # 这一条比 unexplained 更要紧:它意味着**钱花了两次**。
        # 阈值不设 —— 出现一次就该有人看一眼租约时长与单件耗时的关系
        logger.warning(
            "this settle pass carried outcomes from an expired lease",
            extra={
                "extra_fields": {"event": "batch.stale_outcomes",
                    "batch_id": str(job.id),
                    "count": len(stale),
                    "skus": [o.item.sku for o in stale[:10]],
                }
            },
        )

    unexplained = [o for o in outcomes if not o.explainable]
    if unexplained:
        # §6.3.2:不可解释失败数为 0 才能签字。它是一个异常码缺口,
        # 所以要在日志里点名,而不是只在界面上显示一个数字
        logger.warning(
            "some failed items carry no failure reason",
            extra={"extra_fields": {"event": "batch.unexplained_failures",
                "batch_id": str(job.id),
                "count": len(unexplained),
                "messages": [o.message[:200] for o in unexplained[:5]],
            }},
        )
    return counts


def settle(session: Session, job: BatchJob, counts: BatchCounts) -> BatchJobStatus:
    """条件结算(§7.6「条件结算」)。**还有在途条目时不许写完成时间。**

    `rules.job_status()` 早就是条件式的(有 pending / running 就返回 RUNNING),
    但 `finished_at` 以前是无条件盖的 —— 一个 worker 跑完自己领的那几件就
    给整个批次盖上完成时间,而另一个 worker 手里还有活。分批领之后这不再是
    假设:两个 worker 跑同一个批次是被允许的常态。

    反向也要写:`finished_at = None`。回收器把一件放回 PENDING 时,批次从
    "已完成"退回"执行中",而那个旧的完成时间如果留着,列表页会显示
    "10:05 完成"却带着一个转圈的状态。**结算是可撤销的**,所以两个方向
    都必须写,不能只在终态时赋值。
    """
    status = rules.job_status(counts)
    job.status = status.value
    if status in {BatchJobStatus.COMPLETED, BatchJobStatus.COMPLETED_WITH_ERRORS}:
        job.finished_at = job.finished_at or utc_now()
    else:
        job.finished_at = None
    session.flush()
    return status


def _outcome_values(outcome: rules.ItemOutcome) -> dict[str, Any]:
    """一次落库要写的全部列。**从 `_apply_outcome` 拆出来的,没有新逻辑。**

    拆开是因为它现在有两个消费者:条件 UPDATE 需要一个 dict,
    而"落到终态就还回租约"这句话在两种写法下必须完全一样。
    """
    return {
        "status": outcome.status.value,
        # 落到终态就还回租约。不还的话这一行会带着一个未来时刻的 `lease_until`
        # 躺在库里 —— 判定上无害(回收器只看 RUNNING),但排查时它会让人以为
        # 还有 worker 持有它。
        #
        # `lease_token` 也要清:这一件已经跑完了,任何还拿着旧令牌的执行体
        # 都不该再写它
        "lease_owner": None,
        "lease_until": None,
        "lease_token": None,
        "error_code": None if outcome.code is None else str(outcome.code),
        "error_message": outcome.message or None,
        "target_step": (
            None if outcome.target_step is None else outcome.target_step.value
        ),
        "target_ref": outcome.target_ref or None,
        "reused": outcome.reused,
        "result": dict(outcome.payload) or None,
        "finished_at": utc_now(),
    }


def apply_outcome(
    session: Session,
    row: BatchJobItem,
    outcome: rules.ItemOutcome,
    *,
    owner: str,
    token: UUID | None,
    actor: str = "system",
) -> bool:
    """把一次执行的结果落库。**带归属校验(A43 / BLOCK-02),返回是否落成。**

    ## 这个函数原来没有 WHERE

    它是一个纯赋值函数,谁调谁写。于是 A42 的真库并发用例里有一条断言写着
    "旧 worker 最终可以覆盖状态",并注明那是一个**已知缺口**:

        1. worker A 领走,租约 30 分钟
        2. 这一件真的跑了 31 分钟
        3. 回收器只看 lease_until,把它放回 PENDING
        4. worker B 领走,跑完,落 SUCCEEDED
        5. A 也跑完了,无条件覆写 —— B 的结果消失

    覆盖掉的不只是一个状态字符串:`paid_calls` 二次累加、`result` 指向
    A 那一次生成的版本(B 生成的那一版从此没有引用)、`finished_at` 变成
    A 的时刻。一件跑了两次、花了两份钱的条目,在库里看起来和跑了一次一样。

    ## 现在的形状

    四个条件写进 WHERE,与 `rules.lease_still_held()` 一一对应。
    `rowcount != 1` 说明执行权已经不在自己手上,此时**结果只进审计,
    不进这一行** —— 它是一份 stale outcome:钱花掉了,记录要留,
    但它不能改变一个已经由别人决定了的状态。

    `paid_calls` 用 SQL 表达式自增而不是先读后写:先读后写在两个 worker
    之间正是要防的那件事,而且读到的那个值本身可能已经过期。
    """
    values = _outcome_values(outcome)
    values["paid_calls"] = BatchJobItem.paid_calls + outcome.paid_calls
    result = session.execute(
        update(BatchJobItem)
        .where(
            BatchJobItem.id == row.id,
            BatchJobItem.status == BatchItemStatus.RUNNING.value,
            BatchJobItem.lease_owner == owner,
            BatchJobItem.lease_token == token,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    # ORM 里那份内存副本现在是旧的(UPDATE 绕过了它)。不过期的话,
    # 紧接着的 `count_rows` 会按内存里的 PENDING/RUNNING 算,
    # 而库里已经是终态 —— 批次状态会停在"执行中"直到下一次查询
    session.expire(row)
    if result.rowcount == 1:
        return True

    _record_stale_outcome(session, row, outcome, owner=owner, actor=actor)
    return False


def _record_stale_outcome(
    session: Session,
    row: BatchJobItem,
    outcome: rules.ItemOutcome,
    *,
    owner: str,
    actor: str,
) -> None:
    """执行权已经丢了,但这次执行确实发生过。**记录,不覆盖。**

    进审计而不是只打日志:`paid_calls` 这一列现在**不会**被这次执行加上去
    (那是 BLOCK-02 验收标准明写的),于是"这笔钱花在哪了"在业务表里
    没有落脚点。审计是追加的,它是唯一还能回答这个问题的地方。

    日志里同时点名,因为这件事意味着有一件商品被跑了两次 —— 它不是
    正常运行的一部分,值得有人去看一眼租约时长和单件耗时的关系。
    """
    payload = {
        "action": "stale_outcome",
        "sku": row.sku,
        "owner": owner,
        "status": outcome.status.value,
        "paid_calls": outcome.paid_calls,
        "reused": outcome.reused,
        "message": (outcome.message or "")[:500],
    }
    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="BatchJobItem",
        entity_id=row.id,
        payload=payload,
    )
    logger.warning(
        "discarded a batch outcome that no longer matches the current lease",
        extra={
            "extra_fields": {
                "event": "batch.stale_outcome_discarded",
                "item_id": str(row.id),
                **(payload),
            }
        },
    )


def _apply_outcome(row: BatchJobItem, outcome: rules.ItemOutcome) -> None:
    """**已废弃:不带归属校验的落库。** 保留只为让旧调用点立刻可见。

    新代码一律走 `apply_outcome()`。这个函数不做 WHERE,任何还在用它的
    路径都等于把 BLOCK-02 那个缺口重新打开一次。
    """
    for key, value in _outcome_values(outcome).items():
        setattr(row, key, value)
    row.paid_calls = (row.paid_calls or 0) + outcome.paid_calls


def reset_items_for_retry(
    session: Session,
    job: BatchJob,
    *,
    actor: str,
    item_ids: Sequence[UUID] | None = None,
) -> int:
    """把可重试的条目打回 PENDING,**不执行**。返回打回了几条。

    从 `retry_items()` 里拆出来的(BLOCK-03)。原来那个函数最后一行直接
    `return run_batch(...)`,于是 `BATCH_EXECUTION_MODE=celery` 时:
    创建批次走 worker、重试批次却在 HTTP 请求里同步跑完 —— 同一个批次的
    两条路径行为不一致,而重试恰恰是"上一次跑挂了"之后走的那条,
    条目往往更多、更慢。请求超时后运营看到的是失败,后台却在继续跑。

    拆开之后"重置"和"执行"可以分别落在不同的进程里:接口负责重置并提交,
    worker 负责执行。同步模式下调用方接着调 `run_batch()` 即可,行为不变。
    """
    rows = _items(session, job.id)
    wanted = None if item_ids is None else {str(i) for i in item_ids}
    dicts = _item_dicts(session, job.id)
    retryable = set(rules.retryable_rows(dicts))

    targets = [
        row
        for row in rows
        if str(row.id) in retryable and (wanted is None or str(row.id) in wanted)
    ]
    if not targets:
        raise ValidationError(
            "没有可重试的条目。成功项不重跑;「需人工」项要先按提示处理,"
            "重试不会改变结果",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )

    for row in targets:
        row.status = BatchItemStatus.PENDING.value
        row.error_code = None
        row.error_message = None
        row.target_ref = None
        # A-40:`target_step` 也要清。上面三个都清了、单单漏下它,表现是
        # 一件 PENDING 的条目仍然带着上一次失败的"卡在哪一步",而详情页
        # 的下一步文案正是照它渲染的 —— 重试之后界面还停在旧的卡点上
        row.target_step = None
        # 人工重试同样是一次交接(A43 / BLOCK-02)。一件正卡在 RUNNING 的
        # 条目被人点了重试之后,原 worker 如果还活着,它跑完的结果必须落不进来
        row.lease_owner = None
        row.lease_until = None
        row.lease_token = None
        # **`finished_at` 也要清。** 这一行马上要被当成"没跑过"重新领,
        # 和 `reap_expired_leases()` 的 REQUEUE 分支是同一个状态转换,
        # 两处的行结构必须一样。
        #
        # 留着它不只是脏数据:`_liveness_out()` 把**全部行**的
        # `max(finished_at)` 当作批次心跳。重置之后 `settle()` 会把
        # `job.status` 拉回 RUNNING,于是活性判定走的是"开跑之后"那一支,
        # 心跳读到的是**上一轮的完成时刻** —— 只要上一轮结束距今超过
        # `BATCH_PROGRESS_STALL_SECONDS`,刚点完重试的批次会立刻被报成
        # STALLED,提示"worker 中途退出了"。`requeued_at` 那条退化链只在
        # `COMPLETED*` 分支生效,救不了这条路。
        row.finished_at = None
    session.flush()

    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="BatchJob",
        entity_id=job.id,
        payload={"action": "retry", "items": len(targets)},
    )
    return len(targets)


def retry_items(
    session: Session,
    job: BatchJob,
    *,
    actor: str,
    item_ids: Sequence[UUID] | None = None,
) -> BatchCounts:
    """重试(FE-304)。**不新建条目、不重复触发付费调用。**

    三条约束都在这里:

        复用同一行         `attempts` 加一,不 INSERT 新行 —— 于是
                           "不重复创建任务"是结构保证的
        只重试可重试的     `batch.retryable_rows`:SUCCEEDED 不动,
                           NEEDS_HUMAN 不动(重试一百次结果相同)
        先查回执           上游没变就复用上次结果,`paid_calls` 记 0

    `item_ids` 不传时重试全部可重试项;传了则只重试选中的,同样过一遍
    可重试判定 —— 界面可以让运营勾一条 NEEDS_HUMAN,但后端不会真的去跑它。

    **同步执行版。** 异步模式下接口层改调 `reset_items_for_retry()` + 投递,
    执行由 worker 那边的 `run_batch()` 完成(BLOCK-03)。
    """
    reset_items_for_retry(session, job, actor=actor, item_ids=item_ids)
    return run_batch(session, job, actor=actor)


# ==========================================================
# 异常恢复(任务 18)
# ==========================================================
#
# 任务 18 在方案 12.1 里叫「Batch Outbox 与异常恢复」。**这里没有新建
# outbox 表**,理由不是省事,是 `dispatch_service.send_batch` 顶部已经
# 定过的一条:批次的条目本身就是意图记录 —— `batch_job_items` 里那批
# PENDING 行在业务事务里落库,消息投不出去它们照样在。再叠一张表是把
# 同一件事记两遍,而两份记录迟早会不一致。
#
# 缺的从来不是"意图存不存得住",是**没有人重投、也没有人回收**:
#
#     消息丢了     批次停在 QUEUED,条目全是 PENDING,等一个人来点重试
#     worker 死了  它领走的几件停在 RUNNING,谁都碰不到(见 batch.py
#                  「条目租约与回收」一节)
#
# 下面两个函数补的正是这两半。它们都靠任务 17 的租约才敢做:没有
# `FOR UPDATE SKIP LOCKED` + 租约的话,"重投一次"等于"再跑一遍",
# 而那意味着重复的付费调用。


def reap_expired_leases(
    session: Session, *, now: datetime | None = None, limit: int = 200
) -> dict[str, int]:
    """回收租约过期的条目。**跨批次全表扫描,调用方提交。**

    不按批次扫的原因很直接:死掉的 worker 不会告诉我们它领的是哪个批次。
    取数条件就是 `ix_batch_job_items_lease` 那个索引的两列。

    `FOR UPDATE SKIP LOCKED`:回收器可能有多个实例(beat + 手工跑
    `requeue_stranded`),跳过锁住的行比排队等它便宜,而且这一轮漏掉的
    下一轮自然会捞到。

    判定全部在 `batch.reclaim_verdict()`:放回队列还是交给人,取决于
    这一件已经执行过几次。**回收器自己不判定**,它只执行判定的结果。
    """
    moment = now or utc_now()
    rows = list(
        session.scalars(
            select(BatchJobItem)
            .where(
                BatchJobItem.status == BatchItemStatus.RUNNING.value,
                (BatchJobItem.lease_until.is_(None))
                | (BatchJobItem.lease_until <= moment),
            )
            # NULL 既然当作"早就过期",排序上就该排在最前:它们是 0025
            # 之前留下的存量残骸,等得最久
            .order_by(BatchJobItem.lease_until.nulls_first())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    if not rows:
        return {"scanned": 0, "requeued": 0, "gave_up": 0, "batches": 0}

    requeued = gave_up = 0
    touched: dict[UUID, list[str]] = {}
    # 先把涉及的批次查出来:`WORKER_LOST` 的类别要按批次动作覆盖
    # (同 `CONCURRENT_IN_FLIGHT`,注册表里那个 DRAFT 只是兜底值),
    # 而"这一件属于哪个动作"只有批次那一行知道
    jobs = {
        job.id: job
        for job in session.scalars(
            select(BatchJob).where(BatchJob.id.in_({row.batch_id for row in rows}))
        )
    }
    for row in rows:
        verdict = rules.reclaim_verdict(attempts=row.attempts or 0)
        row.lease_owner = None
        row.lease_until = None
        # **吊销令牌就是这次交接本身(A43 / BLOCK-02)。**
        #
        # 清 owner 和 until 只是让这一行看起来没人持有;真正让原 worker
        # 写不进来的是这一句 —— 它之后任何带旧令牌的条件更新都影响 0 行。
        # 交接的时刻由这一行定义,不由时钟定义
        row.lease_token = None
        touched.setdefault(row.batch_id, []).append(str(row.id))
        if verdict.action is rules.ReclaimAction.REQUEUE:
            row.status = BatchItemStatus.PENDING.value
            # 上一次的失败痕迹要清干净:这一行马上要被当成"没跑过"重新领,
            # 留着旧的错误码会让详情页显示一件 PENDING 却带着红字的条目
            row.error_code = None
            row.error_message = None
            row.target_ref = None
            # A-40:同上。这一支下面的 else 分支**会写** target_step,
            # 两支不对称的话,一行被 REQUEUE 的条目会留着上一轮的卡点
            row.target_step = None
            row.finished_at = None
            requeued += 1
        else:
            row.status = BatchItemStatus.FAILED.value
            row.error_code = BatchErrorCode.WORKER_LOST.value
            row.error_message = verdict.reason
            row.finished_at = moment
            job = jobs.get(row.batch_id)
            step = None if job is None else ACTION_STEP.get(BatchAction(job.action))
            if step is not None:
                row.target_step = step.value
            gave_up += 1
    session.flush()

    # 结算必须跟着走:回收把条目从 RUNNING 挪走,批次的整体状态因此可能
    # 变化(最后一件被放弃 -> COMPLETED_WITH_ERRORS;或者反过来,一个
    # 已经"完成"的批次因为放回一件而退回 RUNNING)。不重算的话批次状态
    # 与它自己的条目对不上,而列表页读的是批次那一行
    for batch_id, item_ids in touched.items():
        job = jobs.get(batch_id)
        if job is None:
            continue
        settle(session, job, rules.count_rows(_item_dicts(session, batch_id)))
        audit.record(
            session,
            actor="system",
            action=AuditAction.UPDATE,
            entity_type="BatchJob",
            entity_id=batch_id,
            payload={"action": "reap_expired_leases", "items": item_ids[:50]},
        )
    session.flush()

    logger.warning(
        "reaped expired batch item leases",
        extra={
            "extra_fields": {"event": "batch.leases_reaped",
                "scanned": len(rows),
                "requeued": requeued,
                "gave_up": gave_up,
                "batches": len(touched),
            }
        },
    )
    return {
        "scanned": len(rows),
        "requeued": requeued,
        "gave_up": gave_up,
        "batches": len(touched),
    }


def redispatch_stalled_batches(
    session: Session,
    *,
    actor: str = "system",
    now: datetime | None = None,
    limit: int = 20,
    grace_seconds: int = rules.BATCH_PICKUP_STALL_SECONDS,
) -> dict[str, int]:
    """把「有活但没人干」的批次重新投一次。**这是任务 18 的 outbox 那一半。**

    判据是两条**库里读得到的事实**,不是猜:

        还有 PENDING 条目          活确实没干完
        `updated_at` 超过宽限期     而且已经很久没有任何进展

    宽限期取 `BATCH_PICKUP_STALL_SECONDS` —— 与界面上说"可能没派出去"
    用的是同一个阈值。两处用不同的数会让运营先看到提示、又等不到自愈,
    或者反过来,自愈完了提示才亮。

    ## 重投为什么安全

    这一条全靠任务 17:第二个 worker 进来时,`claim_items` 的
    `FOR UPDATE SKIP LOCKED` 保证它要么领到别的条目、要么什么都领不到
    然后退出;真的抢在同一行上,回执表还挡着重复的付费调用。
    **没有租约的话这个函数就是一个花钱的 bug**,这也是任务表把 18 排在
    17 后面的原因。

    ## 为什么不看"有没有 worker 活着"

    因为系统里没有那份名单(同 `lease_owner` 不参与判定)。投重了的代价
    是一条被立刻丢弃的消息,漏投的代价是一个永远转圈的批次 ——
    所以这里取多投的一侧。
    """
    moment = now or utc_now()
    cutoff = moment - timedelta(seconds=grace_seconds)
    has_pending = (
        select(BatchJobItem.id)
        .where(
            BatchJobItem.batch_id == BatchJob.id,
            BatchJobItem.status == BatchItemStatus.PENDING.value,
        )
        .exists()
    )
    # A-18:心跳取**条目**,不取 job 行。
    #
    # 原来判据只有 `BatchJob.updated_at <= cutoff`,而那一列是 `db/base.py`
    # 的 `onupdate=func.now()` —— 只在这一行真被 UPDATE 时才推进。`run_batch`
    # 只在**第一轮** chunk 写 job 行(status→RUNNING、started_at),之后每件
    # 只写条目行与租约,job 行再也不 UPDATE。阈值是 300 秒,于是:
    #
    #     **任何健康运行超 5 分钟且仍有 PENDING 的批次,每个 beat 都会被重投。**
    #
    # 租约兜住了正确性(`claim_items` 的 FOR UPDATE SKIP LOCKED),代价是
    # 并行执行、日志噪声,以及 "stalled" 这个词在告警里彻底失真 ——
    # 一个天天喊狼来了的信号,真卡住的时候没人会看。
    #
    # 批次自己没有心跳列,但它的条目有:`finished_at` 是"又落地了一件",
    # `updated_at` 是"租约续过 / 被重置回 PENDING"。取两者的最大值,与
    # job 行自己的 `updated_at` 一起取 GREATEST —— 这与 `_liveness_out`
    # 给判定层喂 `last_progress_at` 的口径是同一份事实,两处不该分叉。
    last_item_progress = (
        select(
            func.max(
                # 条目的 `updated_at` 是 `nullable=False`(db/base.TimestampMixin),
                # 所以这里不需要 COALESCE 兜底:GREATEST 在 PostgreSQL 里忽略
                # NULL 参数,`finished_at` 还没落地时结果就是 `updated_at`
                func.greatest(BatchJobItem.finished_at, BatchJobItem.updated_at)
            )
        )
        .where(BatchJobItem.batch_id == BatchJob.id)
        .correlate(BatchJob)
        .scalar_subquery()
    )
    # 这一层的 COALESCE 兜的是**另一件事**:一个条目都没有的批次,子查询
    # 整体是 NULL。此时退回 job 行自己的 updated_at —— 也就是退回旧行为,
    # 而不是让 GREATEST 吃到一个 NULL 之后行为依后端而异
    heartbeat = func.greatest(
        BatchJob.updated_at, func.coalesce(last_item_progress, BatchJob.updated_at)
    )
    jobs = list(
        session.scalars(
            select(BatchJob)
            .where(
                BatchJob.status.in_(
                    [BatchJobStatus.QUEUED.value, BatchJobStatus.RUNNING.value]
                ),
                heartbeat <= cutoff,
                has_pending,
            )
            .order_by(BatchJob.updated_at)
            .limit(limit)
        )
    )
    if not jobs:
        return {"candidates": 0, "dispatched": 0, "failed": 0}

    # 局部 import:`dispatch_service` -> `tasks.batch_tasks` -> 本模块,
    # 顶层引会成环。全仓只允许 dispatch_service 碰队列(那条契约由
    # `test_only_dispatch_service_talks_to_the_queue` 钉着),所以这里
    # 只能经它,不能直接 `.delay()`
    from app.services import dispatch_service

    dispatched = failed = 0
    for job in jobs:
        if dispatch_service.send_batch(str(job.id), actor):
            dispatched += 1
        else:
            failed += 1
    if dispatched or failed:
        logger.warning(
            "redispatched stalled batches",
            extra={
                "extra_fields": {"event": "batch.redispatched",
                    "candidates": len(jobs),
                    "dispatched": dispatched,
                    "failed": failed,
                }
            },
        )
    return {"candidates": len(jobs), "dispatched": dispatched, "failed": failed}


# ==========================================================
# 批量导出文件
# ==========================================================


#: 批量导出文件在存储层的前缀。**不能落在公开前缀下**(见 storage.PUBLIC_PREFIXES)——
#: 上架文件里有价格、库存和尚未发布的文案。
EXPORT_STORAGE_PREFIX = "batch-exports"


def _lock_job_row(session: Session, job: BatchJob) -> None:
    """锁住这个批次在 `batch_jobs` 里的那一行,并把身份四列读成最新的。

    两步缺一不可:

        FOR UPDATE      让并发的第二个请求**等**在这里,而不是并行往下跑
        refresh         等到了之后,身份映射里还是进门时那份旧快照 ——
                        不 refresh 的话它照样看到 `file_path` 是空的,
                        锁等于白上

    只锁不查数据(`select(BatchJob.id)`):这一行的内容随后由 `refresh()`
    重读,`FOR UPDATE` 在这里的唯一职责是排队。
    """
    session.execute(
        select(BatchJob.id).where(BatchJob.id == job.id).with_for_update()
    )
    session.refresh(job)


def ensure_batch_export(
    session: Session, job: BatchJob, *, actor: str, storage
) -> dict[str, Any]:
    """确保这个批次的导出文件存在,返回它的身份。**幂等。**

    评审第 6 条的核心:导出文件**生成一次、之后不可变**。

    原来下载是一个 GET,而那个 GET 每次都重新拼 XLSX + ZIP,并对每件商品
    调一次 `record_export()`。后果是浏览器重试、双击、预取、监控探活
    全都会改数据库状态并累加 `export_count` —— 而 `export_count` 是
    「这份文件导出过几次」这个审计问题的答案,它因此不再可信。

    现在生成与下载是两件事,各自有自己的动词:

        POST .../file   生成(一次)。落存储、写四列身份、记导出留痕
        GET  .../file   下载(任意次)。只读字节,不写任何东西

    已经生成过就直接返回旧的身份,**不重新生成** —— 平台驳回要定位到
    "运营当时传上去的那一份",而重新生成出来的是"现在的内容"。
    两者不是同一个文件,哪怕字节碰巧一样。

    需要一份新的时,走 `regenerate_batch_export()`(admin),不是再点一次这里。

    ## A-16:幂等靠的是锁,不是那句 if

    原来这里是 `if job.file_path:` 打头的**无锁 check-then-act**,而上面
    那段"生成一次、之后不可变"的承诺全部压在它身上。两个并发 POST
    (双击、前端超时后重试、监控预取)会双双读到 `file_path` 为空,
    于是各自跑完整个 `batch_file()`:

        export_count      每件商品被 `record_export()` 记两次 —— 而它正是
                          "这份文件导出过几次"这个审计问题的答案
        file_sha256       后写的覆盖先写的,身份四列描述的是**第二份**字节,
                          而运营手上拿到的可能是第一份
        存储对象          两次生成之间内容若有变化(某草稿刚过期),
                          按 `file_hash` 落盘就会留下一个没人引用的孤儿

    `client.ts` 的全局 `timeout: 30_000` 比 nginx 给 `/api/` 的
    `proxy_read_timeout 320s` 短一个数量级(A-33),所以"前端已报超时、
    后端还在生成、运营又点了一次"是这条竞态最现实的触发路径,不是理论值。

    修法是把顺序倒过来:**先锁住这一行,再判断**。`SELECT ... FOR UPDATE`
    会一直等到持锁的那个事务提交,此时 `refresh()` 读到的就是它写下的
    `file_path` —— 第二个请求于是走"已生成"分支,拿到第一份的身份。

    用行锁而不是 advisory lock:这里要串行的对象**就是** `batch_jobs`
    的这一行,它已经存在(`get_job` 刚查过),不存在 `db/locks.py` 说的
    "第一步是查还不存在的那一行"那个问题。SQLite 夹具上 SQLAlchemy 的
    方言会把 `FOR UPDATE` 编译成空串,所以这句在测试环境是安全的空操作
    (与 `output_service` / `review_service` 里的写法一致)。
    """
    _lock_job_row(session, job)
    if job.file_path:
        return _export_file_meta(job, regenerated=False)

    payload, filename, mime = batch_file(session, job, actor=actor)
    digest = hashlib.sha256(payload).hexdigest()
    stored = storage.save(
        payload,
        file_hash=digest,
        extension="zip",
        prefix=EXPORT_STORAGE_PREFIX,
    )
    now = utc_now()
    job.file_path = stored.storage_path
    job.file_name = filename
    job.file_sha256 = digest
    job.file_size = len(payload)
    job.file_generated_at = now
    job.file_generated_by = actor
    session.flush()
    return {**_export_file_meta(job, regenerated=True), "mime": mime}


def regenerate_batch_export(
    session: Session, job: BatchJob, *, actor: str, storage, reason: str
) -> dict[str, Any]:
    """作废旧的上架文件,重新生成一份。**这是 A-21 那两个 409 的出口。**

    ## 为什么必须有这个入口

    `read_batch_file()` 有两处 409 都写着"请重新生成":文件被存储清理掉了、
    以及字节的 sha 与生成时对不上。但 `ensure_batch_export()` 见 `file_path`
    非空就返回旧身份,而全仓当时只有 `/reviews/{id}/regenerate`(另一业务)
    —— **批量导出文件根本没有再生成入口**,运营照着提示做,只会撞回同一个
    409。修 A-16 加上行锁之后这条死路更硬了:并发也不再"意外"帮他生成一份。

    ## 为什么是 admin,且必须给理由

    重新生成会换掉 `file_sha256`,而那一列是平台驳回时"我当时传的是这一版"
    的**唯一物证**。换掉它是一次有代价的动作,不该和"点一下下载"共用门槛。
    `reason` 会连同 `old_sha → new_sha` 一起进审计:事后回看必须能回答
    "这份文件为什么换过一版"。

    ## 旧对象不删

    `storage.save()` 按内容哈希落盘,旧字节仍留在 `batch-exports/` 下。
    这里**刻意不删**:删了之后万一新的这份也有问题,连对照物都没有了。
    代价是孤儿对象会累积,回收由 C-17 单独处理(`cleanup_service` 今天
    完全不认识这个前缀)。
    """
    _lock_job_row(session, job)
    if not job.file_path:
        # 没生成过就不叫"重新"生成。走生成那条路,审计动词才对得上
        raise ValidationError(
            "这个批次还没有生成过上架文件,请先点「生成上架文件」",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )
    text = (reason or "").strip()
    if not text:
        raise ValidationError(
            "重新生成会作废旧文件的校验和,请说明原因",
            code=ErrorCode.INPUT_INVALID,
        )

    old_sha = job.file_sha256
    old_path = job.file_path
    old_name = job.file_name
    old_generated_at = iso_utc(job.file_generated_at)

    # 先清身份,再生成。反过来的话中途抛异常会留下"旧身份 + 新字节"的
    # 组合,而那正是 `read_batch_file` 的 sha 校验要拦的那种不一致
    job.file_path = None
    job.file_name = None
    job.file_sha256 = None
    job.file_size = None
    job.file_generated_at = None
    job.file_generated_by = None
    session.flush()

    payload, filename, mime = batch_file(session, job, actor=actor)
    digest = hashlib.sha256(payload).hexdigest()
    stored = storage.save(
        payload,
        file_hash=digest,
        extension="zip",
        prefix=EXPORT_STORAGE_PREFIX,
    )
    now = utc_now()
    job.file_path = stored.storage_path
    job.file_name = filename
    job.file_sha256 = digest
    job.file_size = len(payload)
    job.file_generated_at = now
    job.file_generated_by = actor
    session.flush()

    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="BatchJob",
        entity_id=job.id,
        payload={
            "action": "regenerate_export_file",
            "reason": text,
            # 平台驳回定位靠的就是这两个数对不对得上。它们必须在同一条
            # 审计行里,分两条记的话中间插进别的记录就再也串不起来
            "old_sha256": old_sha,
            "new_sha256": digest,
            "old_file_name": old_name,
            "old_generated_at": old_generated_at,
            # 旧对象没删(见 docstring),留下路径才有可能去捞回来
            "old_storage_path": old_path,
            "bytes_changed": old_sha != digest,
        },
    )
    return {
        **_export_file_meta(job, regenerated=True),
        "mime": mime,
        "previous_sha256": old_sha,
        # 字节没变说明当初那个 409 的成因不在内容上(多半是存储被清了),
        # 前端据此可以说一句更准的话
        "bytes_changed": old_sha != digest,
    }


def _export_file_meta(job: BatchJob, *, regenerated: bool) -> dict[str, Any]:
    return {
        "generated": True,
        "regenerated": regenerated,
        "file_name": job.file_name,
        "file_sha256": job.file_sha256,
        "file_size": job.file_size,
        "generated_at": (
            iso_utc(job.file_generated_at)
        ),
        "generated_by": job.file_generated_by,
        "mime": "application/zip",
    }


def read_batch_file(session: Session, job: BatchJob, *, storage) -> tuple[bytes, str, str]:
    """读已生成的导出文件。**不写库、不生成、不改 export_count。**

    没生成过时报 409 而不是顺手生成一个:一个 GET 悄悄产生副作用,
    正是第 6 条要修掉的那件事。前端应当先 POST 生成、再 GET 下载。

    ## A-21:下面两条错误文案指向的动作,现在真的存在了

    它们原来都写着"请重新生成",而 `ensure_batch_export` 见 `file_path`
    非空就返回旧身份 —— 全仓没有任何再生成入口,运营照做只会撞回同一个
    409。现在文案点名 `regenerate_batch_export()` 对应的那个 admin 端点,
    并说清它的代价(换掉 sha = 换掉驳回定位的物证)。
    """
    if not job.file_path:
        raise ValidationError(
            "这个批次还没有生成上架文件;请先点「生成上架文件」再下载",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )
    try:
        payload = storage.read(job.file_path)
    except Exception as exc:  # noqa: BLE001 - 存储层各实现抛的异常不统一
        raise NotFoundError(
            "上架文件已不在存储中(可能被清理过)。这份文件的校验和是平台驳回时"
            "「我传的是这一版」的物证,系统不会自动换掉它 —— "
            "需要一份新的请找管理员用「重新生成上架文件」,并说明原因"
        ) from exc

    # 校验字节没被换过。改一个字节就换一个 sha —— 而这份文件是驳回定位的
    # 参照物,"我传的是这一版"必须能自证
    digest = hashlib.sha256(payload).hexdigest()
    if job.file_sha256 and digest != job.file_sha256:
        raise ValidationError(
            "上架文件的校验和与生成时不一致,已拒绝下载 —— 存储里这份字节"
            "已经不是当初生成的那一份。请找管理员用「重新生成上架文件」"
            "(会记录 旧sha→新sha),再把新的那份传平台",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )
    return payload, job.file_name or f"batch-{str(job.id)[:8]}.zip", "application/zip"


def batch_file(
    session: Session, job: BatchJob, *, actor: str
) -> tuple[bytes, str, str]:
    """把批次里导出成功的商品合并成一个 zip:上架文件 + 清单。

    **只由 `ensure_batch_export` 调用一次。** 直接调它等于绕过第 6 条的
    不可变约定 —— 字节会重新产出、`record_export` 会再记一次。

    ## 为什么是 zip

    平台要解析的那份 Excel 必须保持和单件导出**一模一样的形状**(见
    `export_writer.to_batch_xlsx` 的注释),所以批次号和草稿指纹不能加进去。
    但 §6.3 要求"批量导出结果可定位到具体商品和原始批次"——
    于是对照关系放进同一个压缩包里的 `manifest.csv`。

    一份干净的上架文件 + 一份清单,比一份带了两列私有字段、上传时被平台
    拒掉的文件有用。
    """
    if BatchAction(job.action) is not BatchAction.EXPORT:
        raise ValidationError(
            f"这个批次的动作是{rules.BATCH_ACTION_LABELS[BatchAction(job.action)]},"
            "没有可下载的上架文件",
            code=ErrorCode.INPUT_INVALID,
        )

    succeeded = [
        row
        for row in _items(session, job.id)
        if row.status == BatchItemStatus.SUCCEEDED.value
    ]
    if not succeeded:
        raise NotFoundError("这个批次没有导出成功的商品,无文件可下")

    # spec 在收集完成后按批次唯一类目加载(见下方 category_ids 收敛检查);
    # 这里先不取 —— 取默认常量正是 §0.2 那条缺陷在批量路径上的分身。
    #
    # **这一处刻意仍然是 aware,不要顺手改成 `utc_now()`。**
    #
    # 它喂的是 `ExportMeta.exported_at`,而 `listings/export_writer.py` 直接对它
    # `.isoformat()` 写进导出文件 —— 改成 naive 之后文件里那一行会**丢掉**
    # `+00:00`,而接口侧现在统一走 `core/clock.iso_utc()`(永远带偏移)。
    # 两边就又对不上了,只是方向反过来。
    #
    # 真要收敛它,得连 `export_writer` 一起改成过 `iso_utc`,而那会改变已经
    # 发出去的导出文件的字节 —— 需要带真库跑 `test_listing_mapping_export.py`,
    # 是一次单独的改动(同 core/clock.py 末尾那段)。
    now = datetime.now(UTC)
    entries: list[export_writer.BatchEntry] = []
    spec_versions: set[str] = set()

    # 一个 SPU 只收一次:同 SPU 的多个 SKU 已在计划阶段去重,
    # 但重试路径理论上可能让两条同 scope 的行都成功
    seen: set[str] = set()
    #: A3:补记导出留痕用。(product, draft) 就是这一轮真正写进 zip 的那些
    exported: list[tuple[Product, Any]] = []
    #: 批次内出现过的规则包(受众 × 品类)。多于一个则拒绝合并导出,见下
    category_ids: set[str] = set()
    #: A-24:过不了导出闸的那些。(spu, sku, 原因),攒齐了一次报
    blocked: list[tuple[str, str, str]] = []
    for row in succeeded:
        if row.spu in seen:
            continue
        seen.add(row.spu)
        product = session.get(Product, row.product_id)
        if product is None:
            continue
        # A-24:**先把整批过一遍,再一次性报出来。**
        #
        # 原来这里直接让 `export_gate` 的异常穿出去:任何一件的草稿过期,
        # 整包 409,而 detail 里只有那一件的过期原因、**不点名是哪个 SPU**。
        # 界面同时还是一排绿色的 SUCCEEDED(导出条目确实成功了,过期的是
        # 之后才被检查的草稿)—— 运营看到的是"全绿 + 一句没有主语的报错",
        # 只能一件件点开找。50 件的批次要点 50 次。
        #
        # 闸门本身一个字没松:仍然是任何一件不合格就整批拒绝(§4.5.1)。
        # 变的只是**报错里说清楚是哪几件、各自卡在什么上**。
        try:
            draft, mapped = wb.export_gate(session, product)
        except (ValidationError, NotFoundError) as exc:
            blocked.append((product.spu, product.sku, str(exc)))
            continue
        exported.append((product, draft))
        spec_versions.add(str(draft.spec_version))
        category_ids.add(str(draft.category_id))
        entries.append(
            export_writer.BatchEntry(
                spu=product.spu,
                mapped=mapped,
                draft_id=str(draft.id),
                fingerprint=draft.source_fingerprint,
                sku_count=len(mapped.rows),
            )
        )

    if blocked:
        # 最多点名 5 件:再多的话错误条会长到没人读,而"还有 N 件"这个数
        # 才是运营要的下一个动作(去列表页按状态筛)
        heads = "、".join(f"{spu}/{sku}({why})" for spu, sku, why in blocked[:5])
        more = f";另有 {len(blocked) - 5} 件同样不能导出" if len(blocked) > 5 else ""
        raise ValidationError(
            f"{len(blocked)} 件商品不能导出,整批已停下:{heads}{more}。"
            "请重新生成这几件的草稿后再合并导出",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
            detail={
                "blocked": [
                    {"spu": spu, "sku": sku, "reason": why} for spu, sku, why in blocked
                ]
            },
        )

    if len(spec_versions) > 1:
        # §4.5 第 5 行:模板换版会让全部草稿过期。真出现混版说明有草稿
        # 是在换版前生成的,合并出来的文件会半份新半份旧
        raise ValidationError(
            "批次内导出模板版本不一致("
            + "、".join(sorted(spec_versions))
            + ");请整批重新生成草稿后再合并导出",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )

    if len(category_ids) > 1:
        # 与混版同一个理由的受众版(PRD v2):男装与女装的导出模板**列头
        # 不同**(领型 vs 腰头),合并进一个工作簿等于半份列头对不上值。
        # 先按受众/类目分两个批次导,不做"自动拆成多 sheet"—— 那会让
        # "一个批次一个文件"的对账口径悄悄变掉
        raise ValidationError(
            "批次内商品分属不同规则包("
            + "、".join(sorted(category_ids))
            + ");男装与女装的导出列不同,请按受众分成两个批次分别导出",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )
    batch_category = next(iter(category_ids), generic.CATEGORY_ID)
    spec = generic.field_spec(category_id=batch_category)

    meta = export_writer.ExportMeta(
        spu=f"batch-{str(job.id)[:8]}",
        channel=generic.CHANNEL,
        site=generic.SITE,
        # 批次唯一类目(上面刚收敛过);可追溯到批内商品,不来自模块常量
        category_id=batch_category,
        spec_version=next(iter(spec_versions), ""),
        exported_at=now,
        exported_by=actor,
    )
    workbook = export_writer.to_batch_xlsx(entries, spec, meta)
    manifest = export_writer.manifest_csv(entries, meta, batch_id=str(job.id))

    stamp = now.strftime("%Y%m%d-%H%M%S")
    base = f"batch-{str(job.id)[:8]}-{stamp}"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{base}.xlsx", workbook)
        # utf-8-sig:清单也是给人双击打开看的
        archive.writestr("manifest.csv", manifest.encode("utf-8-sig"))

    payload_bytes = buffer.getvalue()

    # A3:导出留痕在这里补记 —— 字节已经产出,这一刻"导出"才是真的。
    # 文件生成失败(上面任何一步抛异常)不会走到这里,也就不会有
    # "已导出一次"却没有文件的审计。
    # 与单件 `export_draft` 同一口径:export_count 数的是**文件生成次数**,
    # 重复下载就是重复生成,各记一次。
    for product, draft in exported:
        wb.record_export(
            session,
            product,
            draft,
            actor=actor,
            fmt="xlsx",
            now=now,
            batch_id=str(job.id),
        )

    session.flush()
    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="BatchJob",
        entity_id=job.id,
        payload={
            # 评审第 6 条:这条审计记的是**生成**。下载是另一件事,
            # 由 API 层单独记一条 —— 两者原来共用 "download" 一个词,
            # 于是审计里数不出"这份文件被下过几次"
            "action": "generate_export_file",
            "spu_count": len(entries),
            "sku_count": sum(e.sku_count for e in entries),
            "file_generated": True,
        },
    )
    return payload_bytes, f"{base}.zip", "application/zip"


# ==========================================================
# 读取
# ==========================================================


def _items(session: Session, batch_id: UUID) -> list[BatchJobItem]:
    return list(
        session.scalars(
            select(BatchJobItem)
            .where(BatchJobItem.batch_id == batch_id)
            .order_by(BatchJobItem.created_at, BatchJobItem.sku)
        )
    )


def item_dict(row: BatchJobItem) -> dict[str, Any]:
    """条目 -> 出参 / 计数输入。`batch.count_rows` 认这个形状。"""
    meta = None if row.error_code is None else rules.error_meta(row.error_code)
    return {
        "id": str(row.id),
        "batch_id": str(row.batch_id),
        "product_id": str(row.product_id),
        "sku": row.sku,
        "spu": row.spu,
        "scope_key": row.scope_key,
        "status": row.status,
        "status_label": rules.BATCH_ITEM_STATUS_LABELS.get(
            BatchItemStatus(row.status), row.status
        )
        if row.status in {s.value for s in BatchItemStatus}
        else row.status,
        "error_code": row.error_code,
        "error_label": None if meta is None else meta.label,
        "error_message": row.error_message,
        "advice": None if meta is None else meta.advice,
        "explainable": rules.is_explainable(row.error_code),
        "retryable": bool(meta and meta.retryable),
        "target_step": row.target_step,
        "target_ref": row.target_ref,
        "attempts": row.attempts,
        "paid_calls": row.paid_calls,
        "reused": row.reused,
        "result": row.result or {},
        "finished_at": iso_utc(row.finished_at),
    }


def _item_dicts(session: Session, batch_id: UUID) -> list[dict[str, Any]]:
    return [item_dict(row) for row in _items(session, batch_id)]


def counts_out(counts: BatchCounts) -> dict[str, Any]:
    """FE-302 要显示的那一行数字 + §6.3 的两条比率。"""
    return {
        "total": counts.total,
        "pending": counts.pending,
        "running": counts.running,
        "succeeded": counts.succeeded,
        "failed": counts.failed,
        "needs_human": counts.needs_human,
        "skipped": counts.skipped,
        "unexplained": counts.unexplained,
        "attempted": counts.attempted,
        "paid_calls": counts.paid_calls,
        "reused": counts.reused,
        "success_rate": counts.success_rate,
        "explainable_rate": counts.explainable_rate,
    }


def _items_by_batch(
    session: Session, batch_ids: Sequence[UUID]
) -> dict[UUID, list[BatchJobItem]]:
    """一次查完多个批次的条目(A-32)。**一条 SQL,不是一个循环。**

    列表端点原来逐批次调 `job_out`,而 `job_out` 里有一次 `_items()` ——
    30 个批次就是 30 次全表条目查询。计数与活性判定确实需要每一行
    (`count_rows` 的 `unexplained` 要看每行的 `error_code`,活性要看
    `finished_at` 的最大值),所以省不掉行,能省的是**往返次数**。

    排序与 `_items()` 逐字一致:同一批次的条目在两条路径上必须同序,
    否则详情页和列表页的"第一件是哪件"会不一样。
    """
    if not batch_ids:
        return {}
    rows = session.scalars(
        select(BatchJobItem)
        .where(BatchJobItem.batch_id.in_(list(batch_ids)))
        .order_by(BatchJobItem.created_at, BatchJobItem.sku)
    )
    grouped: dict[UUID, list[BatchJobItem]] = {batch_id: [] for batch_id in batch_ids}
    for row in rows:
        grouped.setdefault(row.batch_id, []).append(row)
    return grouped


def jobs_out(session: Session, jobs: Sequence[BatchJob]) -> list[dict[str, Any]]:
    """列表端点的出参。**条目只查一次。**(A-32)

    判定一个字都没改:每个批次仍然走同一个 `job_out`,只是行由外面
    一次取好传进去 —— 与 A-08 修发布清单时用的是同一套办法。
    """
    grouped = _items_by_batch(session, [job.id for job in jobs])
    return [job_out(session, job, rows=grouped.get(job.id, [])) for job in jobs]


def job_out(
    session: Session,
    job: BatchJob,
    *,
    with_items: bool = False,
    rows: Sequence[BatchJobItem] | None = None,
) -> dict[str, Any]:
    # 一次查询,两个用途:出参 dict 与活性判定。原来活性那一半从 dict 里
    # 把 ISO 字符串再解析回 datetime,而那是"绕开重构"的代价,不是必要开销
    #
    # A-32:`rows` 由调用方预取时不再自己查。默认仍然自己查 —— 详情端点
    # 只有一个批次,给它加一层预取只会让调用点更绕
    rows = list(rows) if rows is not None else _items(session, job.id)
    items = [item_dict(row) for row in rows]
    counts = rules.count_rows(items)
    action = BatchAction(job.action)
    out: dict[str, Any] = {
        "id": str(job.id),
        "action": job.action,
        "action_label": rules.BATCH_ACTION_LABELS[action],
        "status": job.status,
        "status_label": rules.BATCH_JOB_STATUS_LABELS.get(
            BatchJobStatus(job.status), job.status
        )
        if job.status in {s.value for s in BatchJobStatus}
        else job.status,
        "label": job.label,
        "created_by": job.created_by,
        # 一律走 `iso_utc`:`expire_on_commit=False` 让"刚写完"和"重新查"
        # 序列化出两种形状(见 core/clock.iso_utc)
        "created_at": iso_utc(job.created_at),
        "started_at": iso_utc(job.started_at),
        "finished_at": iso_utc(job.finished_at),
        "params": job.params or {},
        "counts": counts_out(counts),
        "downloadable": action is BatchAction.EXPORT and counts.succeeded > 0,
        # A-21:文件身份是批次状态的一部分,不是只有下载那一刻才存在的东西。
        # 前端要用它区分"还没生成"和"生成过了" —— 只有后者才谈得上「重新
        # 生成」。`downloadable` 回答不了这个:它说的是"有成功条目、可以去
        # 生成",不是"已经生成过"
        "file_generated": bool(job.file_path),
        "file_sha256": job.file_sha256,
        "file_name": job.file_name,
        "file_generated_at": iso_utc(job.file_generated_at),
        "file_generated_by": job.file_generated_by,
        "retryable_count": len(rules.retryable_rows(items)),
        **_liveness_out(job, rows),
    }
    if with_items:
        out["items"] = items
    return out


def _epoch(value: datetime | None) -> float | None:
    """datetime -> epoch 秒。`rules.liveness` 刻意不认识时区,归一在这一层做。

    归一走 `core/clock.as_naive_utc` 而不是自己写 —— 从 ORM 读回来的
    `timestamptz` 是 aware 的,`utc_now()` 是 naive 的,而"这两者怎么对齐"
    全仓只有那一份答案。在这里再写一遍就是第二份,而它们分叉的表现是
    判定整体偏一个时区,并且在 UTC 机器上测不出来。
    """
    naive = as_naive_utc(value)
    if naive is None:
        return None
    return naive.replace(tzinfo=UTC).timestamp()


#: 判定用的那份清单在 `workbench/batch.py`(纯模块,取值可以被真的验一遍)。
#: 这里只做 enum -> str 的归一,不重新定义"什么算没跑完"。
_UNFINISHED_ITEM_STATUSES: frozenset[str] = frozenset(
    s.value for s in rules.UNFINISHED_STATUSES
)


def _liveness_out(job: BatchJob, rows: Sequence[BatchJobItem]) -> dict[str, Any]:
    """批次还会不会自己往前走 —— 前端据此决定轮询节拍(硬规则第 4 条)。

    心跳取**最后一个条目落地的时刻**:批次表自己没有心跳列,但条目有
    `finished_at`。一个都没落地时 `liveness()` 会退回 `started_at`。

    收 ORM 行而不是 `item_dict()` 的产物。原来收 dict,于是要把 `finished_at`
    从 ISO 字符串解析回 datetime,而那句注释把「我没重构」写成了「重构没用」:
    真正的原因是 `job_out` 当时只拿得到 dict。现在 `job_out` 查一次
    `_items()` 并把行和 dict 一起分发,两边都不必再解析字符串。

    未完成条目数与重排时刻也从这里取。前者让终态 status 不再单方面决定
    SETTLED,后者给"重试之后一直没人接手"一个比 `created_at` 新的参照
    —— 两者都是真实列(`status` / `updated_at`),不是为了凑参数造的值。
    """
    last_progress: float | None = None
    unfinished = 0
    requeued: float | None = None
    for row in rows:
        if row.status in _UNFINISHED_ITEM_STATUSES:
            unfinished += 1
            # 重置为 PENDING 的那次 UPDATE 会推进 `updated_at`,所以未完成
            # 条目里最新的那个 updated_at 就是"这批活最近一次被排上队"。
            # 新建批次时它等于 created_at,retry 之后它等于重试时刻
            stamp = _epoch(row.updated_at)
            if stamp is not None and (requeued is None or stamp > requeued):
                requeued = stamp
        stamp = _epoch(row.finished_at)
        if stamp is not None and (last_progress is None or stamp > last_progress):
            last_progress = stamp

    verdict = rules.liveness(
        status=job.status,
        created_at=_epoch(job.created_at),
        started_at=_epoch(job.started_at),
        last_progress_at=last_progress,
        now=utc_now().replace(tzinfo=UTC).timestamp(),  # utc_now 是 naive UTC,见 core/clock
        unfinished_items=unfinished,
        requeued_at=requeued,
    )
    return {
        "liveness": verdict.state.value,
        "liveness_label": verdict.label,
        "liveness_advice": verdict.advice,
        "stalled_seconds": verdict.stalled_seconds,
    }


def file_audit(
    session: Session, job: BatchJob, *, dry_run: bool = False
) -> dict[str, Any]:
    """OPS-REVIEW P4:这个批次导出的文件现在还能不能传平台。

    只对导出批次有意义。非导出批次返回一个空结论而**不报错** ——
    前端对每个打开的批次都问一次,让它先判断动作类型等于把
    `BatchAction` 的语义抄到前端去,那是下一次不一致的种子。

    按 SPU 去重:导出是 SPU 级动作,同 SPU 的多个 SKU 指向同一份草稿,
    重复列出会让"3 件过期"变成"7 件过期"(§3.4 的两个数字)。
    """
    if BatchAction(job.action) is not BatchAction.EXPORT:
        return {
            "applicable": False,
            "banner": None,
            "total": 0,
            "stale_count": 0,
            "unknown_count": 0,
            "usable": False,
            "blocks_redownload": False,
            "items": [],
        }

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in _items(session, job.id):
        if item.status != BatchItemStatus.SUCCEEDED.value:
            continue  # 没导成功的条目本来就不在压缩包里
        if item.spu in seen:
            continue
        seen.add(item.spu)
        product = session.get(Product, item.product_id)
        if product is None:
            # 商品被删了。草稿多半也随之没了,但结论要照实给,不静默跳过 ——
            # 压缩包里确实有它那几行
            rows.append(
                {
                    "product_id": str(item.product_id),
                    "sku": item.sku,
                    "spu": item.spu,
                    "exported_fingerprint": (item.result or {}).get("fingerprint"),
                    "current_fingerprint": None,
                    "draft_exists": False,
                    "draft_stale": False,
                }
            )
            continue
        # A6:只读接口传 dry_run=True,判定逻辑不变(仍是 refresh_draft 那一份),
        # 只是不把 STALE 结论落库
        facts = wb.draft_file_facts(session, product, dry_run=dry_run)
        rows.append(
            {
                "product_id": str(item.product_id),
                "sku": item.sku,
                "spu": item.spu,
                "exported_fingerprint": (item.result or {}).get("fingerprint"),
                **facts,
            }
        )

    audit_result = rules.audit_file_states(rows)
    return {
        "applicable": True,
        "banner": rules.file_audit_banner(audit_result),
        "total": audit_result.total,
        "stale_count": audit_result.stale_count,
        "unknown_count": audit_result.unknown_count,
        "usable": audit_result.usable,
        "blocks_redownload": audit_result.blocks_redownload,
        "items": [
            {
                "product_id": entry.product_id,
                "sku": entry.sku,
                "spu": entry.spu,
                "state": entry.state.value,
                "state_label": rules.FILE_STATE_LABELS[entry.state],
                "advice": rules.FILE_STATE_ADVICE[entry.state],
                "exported_fingerprint": (entry.exported_fingerprint or "")[:12] or None,
                "current_fingerprint": (entry.current_fingerprint or "")[:12] or None,
            }
            for entry in audit_result.entries
            # 有效的条目不进表:一张 50 行全绿的表没人看,而过期的那 3 行
            # 恰恰要被看见。计数里仍然包含它们(total)
            if entry.state is not rules.FileState.FRESH
        ],
    }


def list_jobs(
    session: Session, *, limit: int = 30, action: str | None = None
) -> list[BatchJob]:
    stmt = select(BatchJob).order_by(BatchJob.created_at.desc()).limit(limit)
    if action:
        stmt = stmt.where(BatchJob.action == action)
    return list(session.scalars(stmt))


def get_job(session: Session, batch_id: UUID) -> BatchJob:
    row = session.get(BatchJob, batch_id)
    if row is None:
        raise NotFoundError("批次不存在")
    return row


def recent_failures(
    session: Session, *, limit: int = 200
) -> list[dict[str, Any]]:
    """最近的批次失败与需人工项。异常列表把它作为第二个来源(FE-303)。"""
    rows = list(
        session.scalars(
            select(BatchJobItem)
            .where(
                BatchJobItem.status.in_(
                    [
                        BatchItemStatus.FAILED.value,
                        BatchItemStatus.NEEDS_HUMAN.value,
                    ]
                )
            )
            .order_by(BatchJobItem.finished_at.desc().nulls_last())
            .limit(limit)
        )
    )
    return [item_dict(row) for row in rows]


def receipt_ledger(
    session: Session, *, limit: int = 50
) -> list[dict[str, Any]]:
    """回执台账。§7.3 注里"核对同一商品同一动作的调用次数"看这张表。

    ## A-17:异常判的是 `executions`,不是 `paid_calls`

    原来这里写 `paid_calls > 1`。而评审第 4 条把属性识别改成了**逐图计费**
    (`_exec_extract` 里 `attempted = image_count`,成功失败两个分支都
    `paid_calls=attempted`)—— 一件 4 图商品**首次**执行完 `paid_calls` 就是 4。
    于是台账把每一个多图商品都标成 §7.3 的 P0"重复付费",而真正的重复付费
    淹没在里面看不出来。**信号被自身正确的计费语义噪声化了。**

    两个数分工:

        paid_calls    这条作用域一共花了多少次调用(逐图,4 图就是 4)
        executions    这条作用域被真的开跑过几次(`_claim_in_flight` +1)

    "同一动作、同一作用域、同一上游指纹被执行了两次"只能由后者回答。
    `paid_calls` 保持逐图真实计费语义**不动** —— 它没有错,错的是拿它当次数用。
    """
    rows = list(
        session.scalars(
            select(BatchActionReceipt)
            .order_by(BatchActionReceipt.last_used_at.desc().nulls_last())
            .limit(limit)
        )
    )
    return [
        {
            "action": r.action,
            "action_label": rules.BATCH_ACTION_LABELS.get(
                BatchAction(r.action), r.action
            )
            if r.action in {a.value for a in BatchAction}
            else r.action,
            "scope_key": r.scope_key,
            # A45-#5:**前端需要一个真正唯一的键。**
            #
            # 回执按 `idempotency_key`(含上游指纹)一行一条,上游一变,同一个
            # `(action, scope_key)` 就存在多行,而这张表按 `last_used_at` 取前 50
            # —— 两行完全可能同屏。前端原来拿 `${action}-${scope_key}` 当 rowKey,
            # 那在这种时候是重复的,React 会警告、行选中与展开会串。
            #
            # 出参给**前 16 位**:它是 sha 派生的,16 位足以在一页 50 行里唯一,
            # 而完整值是幂等键本身,没有理由整串暴露在只读台账上。
            "idempotency_key": (r.idempotency_key or "")[:16],
            "paid_calls": r.paid_calls,
            "executions": r.executions,
            "reuse_count": r.reuse_count,
            "last_actor": r.last_actor,
            "last_used_at": iso_utc(r.last_used_at),
            # 两个条件都要:跑过两趟才叫重复,而没花过钱的重复不是费用问题
            # (不付费的动作不走 `_claim_in_flight`,executions 恒为 1)
            "paid_call_anomaly": r.executions > 1 and r.paid_calls > 0,
        }
        for r in rows
    ]


def iter_flow_contexts(
    session: Session, products: Iterable[Product], *, dry_run: bool = False
):
    """给异常列表 / SPU 聚合用的批量 collect。

    单独一个函数是为了让两处调用点都走同一份 `wb.collect` ——
    异常列表里的阻断数与列表页的阻断数因此不可能是两个数字(§3.4)。

    `dry_run` 原样转给 `collect`:两个调用点都挂在 GET 上,都传 True
    (评审第 19 条)。
    """
    for product in products:
        yield product, wb.collect(session, product, dry_run=dry_run)
