"""生成任务业务逻辑。

编排职责在 app/tasks/generation_tasks.py,这里只管建/查/改状态,
保证 API 层不直接碰状态机。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.attributes import scope_fingerprint
from app.core import audience as audience_rules
from app.core.clock import utc_now
from app.core.enums import (
    GARMENT_ROLES,
    PROVIDER_RESULT_PENDING_CODE,
    SOURCE_ASSET_BROKEN_CODES,
    SUBMIT_UNKNOWN_CODE,
    WORKER_STALLED_CODE,
    AuditAction,
    CandidateStatus,
    RoutingMode,
    TaskStatus,
    UnitsSource,
)
from app.core.errors import (
    ConcurrentTransition,
    ErrorCode,
    NotFoundError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.sorting import normalize_sort
from app.db import locks
from app.media import service as media_service
from app.models.generation import (
    GenerationAttempt,
    GenerationCandidate,
    GenerationTask,
    ProviderUsageRecord,
)
from app.models.model_template import ModelTemplate
from app.models.product import Product
from app.models.product_asset import ProductAsset
from app.prompts.versioning import content_version
from app.providers._config import provider_setting
from app.providers.base import GenerationMode
from app.providers.registry import resolve
from app.services import (
    audit,
    generation_plan_service,
    model_template_service,
    prompt_service,
    spend,
)
from app.services.sorting import apply_order
from app.workflows import generation_plan as gp
from app.workflows import state_machine as sm
from app.workflows.idempotency import build_idempotency_key
from app.workflows.phase_budget import stall_threshold_seconds


def get_task(
    session: Session, task_id: UUID, *, for_update: bool = False
) -> GenerationTask:
    """取任务。``for_update=True`` 时锁住这一行。

    人工审核的三个决策(通过 / 驳回 / 重生)必须加锁:它们要读一堆字段
    (状态、轮次上限、候选图归属)再据此改一堆字段,而两个人同时在审核页上
    点不同按钮是完全正常的操作。条件 UPDATE 能保住状态那一列不被覆盖,
    但保不住"我据以做决定的那些值在我写回去时还成立"。

    只在**决策路径**上加锁,不在列表和详情上加:那些是只读的,
    锁它们只会让审核页互相卡住。
    """
    if for_update:
        # 先锁再取:session.get 可能直接从身份映射返回,连 SQL 都不发,
        # 那样锁根本没加上
        session.execute(
            select(GenerationTask.id).where(GenerationTask.id == task_id).with_for_update()
        ).one_or_none()
    task = session.get(GenerationTask, task_id)
    if task is None:
        raise NotFoundError("生成任务不存在")
    if for_update:
        # 锁之前身份映射里那份可能已经过期了,强制读一次最新值
        session.refresh(task)
    return task


logger = get_logger(__name__)



#: 进入这些状态之前**不需要**确认「没人要求取消」。
#:
#: CANCELLED 本身就是取消的落点;FAILED 是失败的落点,一个已经被要求取消的
#: 任务照样可以失败(而且必须能 —— 否则崩在半路的任务会永远停在进行中状态)。
#: 其余所有目标状态都属于"继续往下跑",而继续往下跑的前提是没人喊停。
_CANCEL_EXEMPT_TARGETS: frozenset[TaskStatus] = frozenset(
    {TaskStatus.CANCELLED, TaskStatus.FAILED}
)


def transition(
    session: Session,
    task: GenerationTask,
    target: TaskStatus,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
    expected_status: str | TaskStatus | None = None,
) -> GenerationTask:
    """唯一允许修改任务状态的入口。非法跳转直接抛错。

    ## 为什么是一条带条件的 UPDATE,而不是改对象再 flush

    以前这里先按会话里那个 ORM 对象的 ``status`` 做合法性校验,再发一条
    **只按主键定位**的 UPDATE。两步之间没有任何东西保证那一行还是刚才那个样子,
    而中间往往隔着一次几十秒到几分钟的外部调用:

        worker 读到 SUBMITTING          用户点了取消 -> 落库 CANCELLED
        worker 校验 SUBMITTING -> PROVIDER_RUNNING(在它眼里合法)
        worker 发 UPDATE ... WHERE id=?  -> **把 CANCELLED 覆盖成 PROVIDER_RUNNING**

    取消在界面上显示成功了,任务却继续跑完、继续计费。并发的批准、驳回、
    重生也是同一个形状。

    现在把「我以为它是什么状态」写进 WHERE:数据库只在这一行确实还是那个样子
    时才改它,否则改到 0 行,抛 `ConcurrentTransition`。判定和写入合并成
    一条语句之后,中间那道缝就不存在了。

    ``expected_status`` 给少数需要显式指定前置状态的调用方用;不传就以会话里
    这个对象当前的状态为准 —— 那正是调用方刚刚据以做判断的那个值。
    """
    expected = TaskStatus(expected_status if expected_status is not None else task.status)
    sm.assert_can(expected, target)

    now = utc_now()
    values: dict[str, Any] = {"status": TaskStatus(target).value, "updated_at": now}

    if target is TaskStatus.PREPROCESSING:
        # 首轮记录开工时间,重生轮次保留最初那一次。用 coalesce 让数据库判断,
        # 而不是读一个可能已经过期的 task.started_at
        values["started_at"] = func.coalesce(GenerationTask.started_at, now)
    if sm.is_terminal(target):
        values["finished_at"] = now
    if error_code is not None:
        values["error_code"] = error_code
    if error_message is not None:
        # 只存我们自己生成的说明,不塞第三方异常原文(可能含 URL 里的凭据)
        values["error_message"] = error_message[:2000]
    if target is not TaskStatus.FAILED and error_code is None:
        values["error_code"] = None
        values["error_message"] = None

    # 先把这个对象上其它待写字段(provider、max_rounds、cancel_requested……)
    # 刷出去。它们必须在条件 UPDATE **之前**落库 —— 比如 retry_task 会先把
    # cancel_requested 置回 False,而下面的 WHERE 正要读这一列。
    session.flush()

    stmt = update(GenerationTask).where(
        GenerationTask.id == task.id,
        GenerationTask.status == expected.value,
    )
    if target not in _CANCEL_EXEMPT_TARGETS:
        # 「继续往下跑」还要额外确认没人喊停。只看 status 是不够的:
        # 取消先打标记再改状态,两者之间存在一个 cancel_requested 已经为真、
        # 状态还没变的瞬间,而 worker 恰好可能在那一刻推进状态。
        stmt = stmt.where(GenerationTask.cancel_requested.is_(False))

    changed = session.scalar(
        stmt.values(**values)
        .returning(GenerationTask.id)
        .execution_options(synchronize_session=False)
    )
    if changed is None:
        # 身份映射里那份已经不可信了,让下一次访问重新读库 ——
        # 调用方拿到异常之后往往要看一眼"那它现在到底是什么状态"
        session.expire(task)
        raise ConcurrentTransition(
            f"任务状态已被其它操作改变,{expected.value} -> {target.value} 未生效;"
            "请刷新后重试",
            detail={
                "task_id": str(task.id),
                "expected": expected.value,
                "target": target.value,
            },
        )

    # 走的是 Core UPDATE,身份映射里的对象还是旧值。不过期的话,
    # 同一个会话后面读到的会是转移前的样子。
    session.expire(task)
    return task



#: 能当「商品图」用的素材角色。详情图、背景图、模特图都不行 ——
#: 拿一张吊牌特写去做试穿,Provider 会认认真真给你生成一个穿着吊牌的模特。
def _assert_assets_are_usable(
    session: Session,
    assets: list[ProductAsset],
    generation_mode: GenerationMode,
    model_template_id: UUID | None,
    product: Product | None = None,
) -> None:
    """在**创建任务时**就把「这批素材能不能跑」判掉。

    以前这些检查散落在 worker 里,而且缺了两条:显式指定的素材不看 check_passed,
    找不到正面图/抠图时退回 assets[0]。后果是用户提交时一切正常,
    十几秒后任务变成 FAILED,或者更糟 —— 用一张详情图当商品图把钱花掉了。
    创建时挡下来,错误信息还能直接告诉他缺什么。
    """
    unchecked = [a for a in assets if not a.check_passed]
    if unchecked:
        names = ", ".join(sorted({a.original_filename or str(a.id) for a in unchecked})[:3])
        raise ValidationError(
            f"这些素材没有通过图片检查,不能用于生成:{names}",
            code=ErrorCode.INPUT_INVALID,
        )

    roles = {a.asset_type for a in assets}
    if not roles & set(GARMENT_ROLES):
        raise ValidationError(
            "缺少商品图:请至少选择一张「正面图」或「抠图」",
            code=ErrorCode.INPUT_INVALID,
        )

    if generation_mode is GenerationMode.VIRTUAL_TRY_ON:
        if model_template_id is not None:
            template = session.get(ModelTemplate, model_template_id)
            if template is None:
                raise ValidationError("模特模板不存在", code=ErrorCode.INPUT_INVALID)
            if not template.enabled:
                raise ValidationError("选中的模特模板已停用", code=ErrorCode.INPUT_INVALID)

            # ---- §12.4 生成前硬阻断:受众错配,不可绕过 ----
            #
            # 正常情况下走不到 —— §10.5 已经让不匹配的模特不出现在候选列表里。
            # 但**它必须独立成立,不能依赖前端筛选**:接口是公开的,批量路径
            # 也走这里,而错配图一旦生成出来,后面两道关都不可靠 ——
            # 评分器比对的是商品与参考图是否一致,**它不检查穿的人是谁**;
            # 人工审阅时这类图看起来就是一张合格的商品图。
            # 所以拦在创建任务这一刻,连费用都不产生。
            block = audience_rules.generation_block_reason(
                getattr(product, "audience", None), template.audience
            )
            if block is not None:
                raise ValidationError(block, code=ErrorCode.INPUT_INVALID)

            # ---- §11.3 授权失效:禁止创建新任务 ----
            # 历史商品与历史图片不受影响,这条只管"新任务"。
            # 传品类是为了让授权里的"禁用品类"判得了 —— 它按规则包键
            # (受众 + 品类族)比对,与草稿用的是同一个派生
            from app.channels import generic

            category_id = None
            if product is not None:
                try:
                    category_id = generic.category_id_for(product)
                except Exception:  # noqa: BLE001
                    # 受众未确认等情况派生不出规则包。授权的其余三条照常判,
                    # 只有"禁用品类"这一条跳过 —— 不因为派生不出就整个放行
                    category_id = None
            model_template_service.assert_usable(template, category_id=category_id)
            return
        # ---- C-10 关闭:自由上传的模特参考图**不再绕过**合规闸 ----
        #
        # 2026-08-11 评审。这条分支原来直接 `return`,于是走它的任务跳过
        # §10.5 受众、§11 授权 / 年龄 / AI 换装 / 禁用品类的**全部**检查,
        # 只在日志里留一行 warning。而 PRD §6.4 明写自由上传模特图不得绕过
        # ModelTemplate 校验 —— 也就是说这不是"还没做的功能",是一条与
        # 明文要求相反的执行路径。
        #
        # ## 为什么是拒绝,而不是"解析到授权主体再同等校验"
        #
        # 评审给的方向是 `MODEL_REFERENCE -> provenance -> 对应授权主体 ->
        # 同等校验`,**并注明解析不了就 fail-closed**。今天解析不了,而且
        # 缺的不是一句代码:
        #
        #     ProductAsset          没有任何指向 ModelTemplate 或授权记录的列
        #     MediaAsset.consent_id 列在(§12.1),但**全仓没有一个写入点** ——
        #                           `ingest(consent_id=...)` 的实参从来没人传
        #     MediaConsent          有 subject_type / scope / 有效期,没有受众,
        #                           所以连 §10.5 那一条都判不了
        #
        # 在这三样齐之前写一条"解析成功就放行"的分支,等于把一个永远走不到的
        # 放行路径摆在闸门上 —— 本仓库硬规则第 4 条反复点名的正是这种形状
        # (一个看起来完整、实际不是从真实来源推出来的判定)。
        #
        # ## 运营的出路是现成的,不是被堵死
        #
        # 把那张模特图登记成 ModelTemplate(带受众与 §11 授权字段)再选它。
        # 那条路径今天就能走,而且是**唯一**能执行四道检查的路径。
        # 措辞按 §5.1:只说"要走模特库",不吐 C-10 之类的编号。
        #
        # 重开这条路的条件写在上面那三行里。谁补齐了它们,就在这里加分支。
        if "MODEL_REFERENCE" in roles:
            logger.warning(
                "refused a virtual try-on task that relies on a free-uploaded "
                "MODEL_REFERENCE asset; there is no license subject to check "
                "against (PRD 6.4 forbids bypassing ModelTemplate validation)",
                extra={
                    "extra_fields": {"event": "gen.license_subject_missing",
                        "product_id": str(getattr(product, "id", "")),
                        "audience": getattr(product, "audience", None),
                    }
                },
            )
            raise ValidationError(
                "商品自带的「模特参考图」不能直接用于虚拟试穿:那张图上没有受众、"
                "年龄确认与授权范围的记录,合规检查无从执行。请先把这张模特图"
                "登记到模特库(填写受众与授权信息),再在这里选中它。",
                code=ErrorCode.INPUT_INVALID,
            )
        raise ValidationError(
            "虚拟试穿需要模特:请在模特库里选择一个启用中的模特模板",
            code=ErrorCode.INPUT_INVALID,
        )


def _resolved_prompt_version(prompt: str | None, requested: str | None) -> str:
    """出图链路的 prompt_version(PRD BE-306)。

    上一版是函数默认参数 `"v1"` —— 拼装的提示词(compose_prompt 把场景/姿势/
    角度并进去)怎么变,落库的版本号都不动,「这次调用到底用了哪段文本」
    答不出来。现在:调用方显式给了就尊重;没给且有提示词,对**拼装后的最终
    字符串**取内容哈希 —— 它就是 AC-30b 要求的"已定型的最终字符串";
    没提示词的任务保持 `"v1"`,存量幂等身份不漂移。
    """
    if requested:
        return requested
    if prompt:
        return content_version(prompt)
    return "v1"


def create_task(
    session: Session,
    *,
    product_id: UUID,
    mode: str,
    provider: str | None,
    routing_mode: str = RoutingMode.MANUAL.value,
    input_asset_ids: list[UUID] | None = None,
    model_template_id: UUID | None = None,
    candidate_count: int = 4,
    max_rounds: int = 3,
    output_width: int | None = None,
    output_height: int | None = None,
    base_seed: int | None = None,
    prompt: str | None = None,
    negative_prompt: str | None = None,
    prompt_version: str | None = None,
    provider_params: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    color_variant_id: UUID | None = None,
    override_plan: bool = False,
    actor: str = "system",
) -> tuple[GenerationTask, bool]:
    """创建任务。返回 (任务, 是否命中幂等)。

    必须立即返回(需求第十六章),因此这里只落库,真正的生成交给 Celery。

    ## 阶段 4:方案与颜色作用域(§6.4 / §6.5)

    `color_variant_id` 不传时由 `product.color_variant_id` 推出 —— 一行 SKU
    只属于一个颜色,让调用方再传一遍等于给这件事造第二个答案。

    方案由 `generation_plan_service.resolve_for()` **解析**出来,不由调用方
    指定:指定的写法会让"给红色配了覆盖方案"这件事取决于前端记不记得带上
    那个 id,而 §4.7 的整套唯一性约束正是为了让它有唯一答案。

    ## a47:方案解析出来之后**由这里决定最终参数**,调用方只表达意图

    ### 改之前的形状:按方案验收,不按方案生成

    `GenerationPlan` 有六个生成参数,而建任务时只有 `budget_cap` 生效 ——
    `provider` / `model_template_id` 取的是调用方传的,`scene` / `pose` /
    `angles_json` 三个字段在建任务与执行链路里**一次都没有被读过**。

    而它们在**验收**链路里被读了:`workbench/upstream_collect.py` 用
    `gp.required_angles()` 算出每个颜色"要求哪些角度"进上游快照,
    §6.5 据此判图片集完不完整。于是系统的实际行为是:

        运营配方案:要求 正面 / 30° / 背面
        出图:按弹窗里手填的参数跑,方案的角度没人告诉生成器
        验收:按方案检查 —— 缺背面,判定图片集不完整
        运营看到"不完整",而他并没有做错任何事

    **这是本轮唯一一处业务正确性修复**,可以在 Mock 下当场复现。

    ### 三条硬约束

    1. **解析不到方案不拦。** 存量商品与单色 SPU 都没有方案,拦下来
       等于让它们集体无法出图(这条从阶段 4 起就成立,原样保留)。
    2. **幂等键用解析后的值。** 用入参的话,同一份方案下前端传两个不同的
       `provider` 会生成两个任务,而它们最终跑的是同一组参数 —— 幂等这道
       防线在最需要它的场景(重试、批量、网络抖动)下直接失效。
    3. **冲突不静默。** 按方案执行,并在审计 payload 里记
       「请求 X,方案 Y,按 Y 执行」。静默覆盖会让排障的人对着请求体
       百思不得其解。清单在 `gp.PLAN_GOVERNED_FIELDS` 一处。

    ### 不给 `generation_tasks` 加列

    `scene` / `pose` 进提示词(`gp.compose_prompt`),`angles` 决定角度集合
    与一轮张数。事后追溯靠已有的 `generation_plan_id` + `plan_fingerprint`
    两列 —— 那正是它们该有的用途,也顺带让 `plan_fingerprint`
    从"算出来但没有读者"变成"有读者"。

    ### `override_plan` 是管理员排障出口,不是默认路径的放宽

    非管理员传它由**接口层** 403(端点挂的是 `require_operator`,
    没有"require_admin 的调用点"这回事)。这里只认它一件事:
    绕过之后 `generation_plan_id` 与 `plan_fingerprint` **必须留空** ——
    绕过了方案就不许再记这份方案。

    **但预算仍然照查。** `budget_cap` 是花钱的闸,不是出图参数;
    绕过方案不等于绕过预算,否则 override 会成为超预算出图的后门。
    这一条是本函数自己的决定,记在 `docs/DECISIONS.md`。
    """
    product = session.get(Product, product_id)
    if product is None:
        raise NotFoundError("商品不存在")

    generation_mode = GenerationMode(mode)

    # 素材:未指定则取该商品全部通过检查的素材
    if input_asset_ids:
        assets = list(
            session.scalars(
                select(ProductAsset).where(
                    ProductAsset.id.in_(input_asset_ids),
                    ProductAsset.product_id == product_id,
                )
            )
        )
        if len(assets) != len(set(input_asset_ids)):
            raise ValidationError("部分素材不存在或不属于该商品", code=ErrorCode.INPUT_INVALID)
    else:
        assets = list(
            session.scalars(
                select(ProductAsset).where(
                    ProductAsset.product_id == product_id,
                    ProductAsset.check_passed.is_(True),
                )
            )
        )
    if not assets:
        raise ValidationError("该商品还没有可用素材,请先上传商品图", code=ErrorCode.INPUT_INVALID)

    # ---- 阶段 4:解析当前生效的方案与颜色作用域(§6.4)----
    #
    # 解析不到方案**不拦**:单色 SPU 与阶段 4 之前建的款都没有方案,
    # 拦下来等于让存量商品集体无法出图。方案缺席时 plan 指纹为空串,
    # 与"没传"是同一个键(`build_idempotency_key` 那条守卫钉着这一点)。
    #
    # **这一段必须排在素材检查与 Provider 解析之前**(a47):方案会换掉
    # `model_template_id` 与 `provider`,而那两样正是下面两道检查的入参。
    # 排在后面的表现是"按调用方的模板过了授权闸,却按方案的模板出图"——
    # 一个 §11 授权检查在产品里静默失效的洞。
    scope_variant_id = color_variant_id or getattr(product, "color_variant_id", None)
    sample_fp = _variant_sample_fingerprint(session, product_id, scope_variant_id)
    plan_row = None
    if getattr(product, "spu_id", None):
        plan_row = generation_plan_service.resolve_for(
            session, spu_id=product.spu_id, color_variant_id=scope_variant_id
        )
    # ---- a47:方案决定最终参数(§5.2)----
    #
    # `plan_applied` 是"这一次真的按方案跑了吗"的唯一答案。它同时决定
    # 两件必须同向的事:用谁的参数、两个 plan 列写不写。分成两个变量的
    # 写法出现过(阶段 4 的 `plan_row is not None`),而 override 一来
    # 它们就会分叉:参数按调用方、列却记着方案。
    plan_applied = plan_row is not None and not override_plan
    plan_overrides: tuple[gp.PlanOverride, ...] = ()
    if plan_applied:
        plan_view = generation_plan_service.to_view(plan_row)
        plan_overrides = gp.plan_overrides(
            plan_view,
            {"provider": provider, "model_template_id": model_template_id},
        )
        provider = plan_view.provider or provider
        model_template_id = (
            UUID(plan_view.model_template_id)
            if plan_view.model_template_id
            else model_template_id
        )
        candidate_count = gp.governed_candidate_count(plan_view, fallback=candidate_count)
        compose_template, _template_version = prompt_service.get_active_content(
            session, gp.GENERATION_PROMPT_KEY
        )
        prompt = gp.compose_prompt(
            prompt,
            scene=plan_view.scene,
            pose=plan_view.pose,
            angles=plan_view.angles,
            template=compose_template,
        )

    _assert_assets_are_usable(
        session, assets, generation_mode, model_template_id, product=product
    )

    # Provider 未配置时在这里就挡下,不让任务跑到一半才失败(需求第七章)
    chosen = resolve(
        mode=routing_mode,
        requested=provider,
        generation_mode=generation_mode,
        # 设置页里的「默认 Provider」,读不到时仍然退回 mock
        default_provider=provider_setting("DEFAULT_PROVIDER", "mock") or "mock",
    )
    caps = chosen.capabilities()
    if candidate_count > caps.max_candidates_per_call and not caps.supports_multiple_candidates:
        # 有方案时这个数是**方案算出来的**(角度张数之和),所以错误里要点名
        # 是谁给的 —— 否则运营会去改弹窗里那个他已经改不动的数字
        raise ValidationError(
            f"{chosen.provider_name} 单次最多生成 {caps.max_candidates_per_call} 张候选图"
            + (f",而方案要求一轮出 {candidate_count} 张" if plan_applied else ""),
            code=ErrorCode.INPUT_INVALID,
        )

    if plan_row is not None:
        # ---- 预算闸(§6.4)。**必须排在这里,不能排在方案刚解析出来那一刻** ----
        #
        # 2026-08-11 评审:它原来在上面 `plan_row is not None` 那一支里,
        # 也就是在 provider 与 candidate_count 定下来**之前**。于是它只能
        # 拿"已花多少"和上限比,算不了"这一次要花多少" —— 而那正是
        # 缺陷所在(见 `gp.budget_verdict` 的说明)。要算本次预估,
        # 就必须先知道最终是哪一家、出几张。
        #
        # 预算照查,`override_plan` 也不例外 —— 见函数文档最后一段。
        _assert_budget(
            session,
            plan_row,
            provider=chosen.provider_name,
            candidate_count=candidate_count,
        )

    key = build_idempotency_key(
        product_id=str(product_id),
        mode=mode,
        # 下面四个一律是**解析后**的值(§5.2 硬约束 2)
        provider=chosen.provider_name,
        asset_ids=[str(a.id) for a in assets],
        model_template_id=str(model_template_id) if model_template_id else None,
        candidate_count=candidate_count,
        prompt_version=_resolved_prompt_version(prompt, prompt_version),
        routing_mode=RoutingMode(routing_mode).value,
        max_rounds=max_rounds,
        output_width=output_width,
        output_height=output_height,
        base_seed=base_seed,
        prompt=prompt,
        negative_prompt=negative_prompt,
        provider_params=provider_params,
        color_variant_id=str(scope_variant_id) if scope_variant_id else None,
        # 绕过方案时这里也必须是 None,与下面两列同向:一个记着指纹的键
        # 配一行两列为空的任务,会让"这个任务按方案跑了没有"有两个答案
        plan_fingerprint=plan_row.plan_fingerprint if plan_applied else None,
        sample_fingerprint=sample_fp,
        explicit_key=idempotency_key,
    )

    existing = session.scalar(
        select(GenerationTask).where(GenerationTask.idempotency_key == key)
    )
    if existing is not None:
        # 幂等命中:返回已有任务,绝不创建第二个(需求第九章)
        return existing, True

    task = GenerationTask(
        product_id=product_id,
        model_template_id=model_template_id,
        input_asset_ids=[str(a.id) for a in assets],
        mode=generation_mode.value,
        provider=chosen.provider_name,
        routing_mode=RoutingMode(routing_mode).value,
        candidate_count=candidate_count,
        max_rounds=max_rounds,
        output_width=output_width,
        output_height=output_height,
        base_seed=base_seed,
        prompt=prompt,
        negative_prompt=negative_prompt,
        prompt_version=_resolved_prompt_version(prompt, prompt_version),
        provider_params=provider_params or {},
        idempotency_key=key,
        status=TaskStatus.CREATED.value,
        # §5.3 的要害:绕过了方案就不许再记这份方案。两列一起由
        # `plan_applied` 决定,不各判一次
        generation_plan_id=plan_row.id if plan_applied else None,
        plan_fingerprint=plan_row.plan_fingerprint if plan_applied else None,
        color_variant_id=scope_variant_id,
        sample_fingerprint=sample_fp,
    )
    session.add(task)
    try:
        # 用保存点包住:并发同参请求会同时通过上面那次 SELECT,
        # 然后一起撞唯一约束。撞上的那个不该变成 500,而该拿到已有任务 ——
        # 幂等的意义正在于此(需求第九章)。
        with session.begin_nested():
            session.flush()
    except IntegrityError:
        session.expunge(task)
        existing = session.scalar(
            select(GenerationTask).where(GenerationTask.idempotency_key == key)
        )
        if existing is None:
            # 撞的不是幂等键,是别的约束 —— 不吞,让上层看到真实错误
            raise
        logger.info(
            "idempotency race resolved",
            extra={
                "extra_fields": {
                    "event": "gen.idempotency_race_resolved",
                    "idempotency_key": key,
                    "task_id": str(existing.id),
                }
            },
        )
        return existing, True

    audit.record(
        session,
        actor=actor,
        action=AuditAction.CREATE,
        entity_type="GenerationTask",
        entity_id=task.id,
        payload={
            "product_id": str(product_id),
            "provider": chosen.provider_name,
            "mode": generation_mode.value,
            "candidate_count": candidate_count,
            "color_variant_id": str(scope_variant_id) if scope_variant_id else None,
            "generation_plan_id": str(plan_row.id) if plan_applied else None,
            # §5.2 硬约束 3:冲突不静默。空列表与缺席是两件事 ——
            # 缺席意味着这一版代码还不记这件事,空列表意味着这次没有冲突
            "plan_overrides": [o.as_payload() for o in plan_overrides],
            # 解析到了方案却没有按它跑,只有一个原因。写下来,免得排障的人
            # 看着一行 plan 列为空的任务去查"为什么方案没解析出来"
            "plan_bypassed": bool(override_plan and plan_row is not None),
        },
    )
    return task, False


def _variant_sample_fingerprint(
    session: Session, product_id: UUID, color_variant_id
) -> str | None:
    """§5.3 双作用域指纹里**该颜色那一份**。

    ## 为什么只算颜色那一半

    共享作用域那一份的比较对象是**一条确认事实的** `input_fingerprint`
    (§4.6),而 `ProductAttributeValue` 上没有这一列。

    **这句话在 A45-batch14-20 并线合并那天变过一次,别照着旧版本理解:**
    并行线(`0040_extraction_run_identity`)确实把 `input_fingerprint` 落了,
    但落在 `ProductAttributeExtraction` —— 那是**识别 run 行**。
    `facts_stale()` 问的是"这条事实还成不成立",一次 run 可以产出/更新
    多条事实,一条事实也可以跨多次 run 存活;"这条事实继承哪一次 run 的
    指纹"是个还没做的决定,不是一次赋值。

    所以现在缺的不再是"列不存在",是**事实行上的对照方 + 那个继承决定**。
    在它落地之前硬接的表现没变:算出来没地方存,每次现算一个和空值比,
    而 `facts_stale(stored=None)` 恒为 True —— **全库事实一次性集体过期**,
    运营看到"所有东西同时过期了"而查不出原因。

    颜色这一半不同:它的比较对象是任务行上的 `sample_fingerprint`,
    落地在同一批(迁移 0041),所以有对照方。

    ## 取数走 §5.1 那个唯一入口

    **不许照着 `usable_assets()` 抄一行。**指纹的集合定义是"该颜色的
    PRODUCT_EVIDENCE + READY 素材子集",那正是 `evidence_assets_for` 回答的
    问题;在这里重写一遍条件,"事实是根据哪些图算出来的"和"图什么时候该
    重出"就不是同一个问题的答案了(A45-batch14-19 收口的正是这件事)。

    没有颜色作用域(单色 SPU、存量商品)时返回 None:空串和"算出来是空集合"
    必须分得开 —— 前者是"这一维不参与",后者是"这个颜色一张证据图都没有",
    而后者应该让幂等键变化。
    """
    if not color_variant_id:
        return None
    try:
        assets = media_service.evidence_assets_for(
            session, product_id, scope=color_variant_id, scope_is_explicit=True
        )
    except Exception:  # pragma: no cover - 取数失败不该拦住创建任务
        # 指纹是**幂等键的一个维度**,不是准入条件。读不到时退回 None
        # (等于这一维不参与),而不是拒绝创建 —— 拒绝的话一次数据库抖动
        # 会让整条出图链路停摆,而它防的只是"换了样品却命中旧任务"
        logger.warning("sample fingerprint unavailable", exc_info=True, extra={
            "extra_fields": {
                "event": "gen.sample_fingerprint_unavailable",
            }
        })
        return None
    return scope_fingerprint.variant_fingerprint(
        scope_fingerprint.views(assets), str(color_variant_id)
    )


def _assert_budget(
    session: Session,
    plan_row,
    *,
    provider: str = "",
    candidate_count: int = 0,
) -> None:
    """§6.4 的预算闸:「预算通过(方案 budget_cap + 现有花费台账)」。

    判定在 `workflows.generation_plan.budget_verdict`(fail-closed:读不到
    花费、或者估不出这一次要花多少,都不放行)。这里只负责取那几个数。

    **拦在创建这一步,不拦在调用 Provider 那一步**:后者意味着任务已经
    建好、Celery 已经派发,而预算超了的表现会是一个失败的任务 ——
    运营看到的是"生成失败",不是"预算不够"。

    报错措辞按 verdict 分开(2026-08-11 评审):`UNKNOWN` 有三种成因
    (台账读不到 / 没配单价 / 在途预占估不出),而它们的下一步动作完全不同。
    合并成一句"上限已用尽或花费台账读不到"会让"去配 PROVIDER_PRICE_BOOK"
    这条真正的出路完全看不见。
    """
    verdict = plan_budget_verdict(
        session, plan_row, provider=provider, candidate_count=candidate_count
    )
    if not gp.budget_blocks(verdict):
        return
    if verdict is gp.BudgetVerdict.EXCEEDED:
        raise ValidationError(
            "生成方案的预算上限不够这一次生成了(已花 + 本次预估 > 上限)。"
            "请提高方案的预算上限,或减少这一轮的张数",
            code=ErrorCode.INPUT_INVALID,
        )
    raise ValidationError(
        "生成方案设了预算上限,但算不出这一次要花多少,因此不放行:"
        "要么花费台账读不到,要么价目表里没有这家 Provider 的 submit 单价"
        "(PROVIDER_PRICE_BOOK)。请配上单价,或清空方案的预算上限",
        code=ErrorCode.INPUT_INVALID,
    )


def plan_budget_verdict(
    session: Session, plan_row, *, provider: str = "", candidate_count: int = 0
):
    """这份方案的预算还够不够。

    没设上限时**不去查台账** —— 没有上限就没有要证明的事,而多一次
    聚合查询会让每次创建任务都扫一遍 usage 表。

    ## 上限存在时做三件事,顺序是安全约束的一部分(2026-08-11 评审)

    1. **先上锁。** 按方案 id 的事务级 advisory lock,持有到调用方提交。
       没有它的话第 3 步读到的"在途任务"里不包含另一个正在建的任务 ——
       两个请求各自算出"还够",然后各花一次钱。
    2. **本次预估。** `spend.estimated_cost(provider, submit, 张数)`。
       估不出来给 None,而 `budget_verdict` 对 None 是 fail-closed。
       这一项**原来根本没传**,那就是「预算可以被真实穿透」的直接原因。
    3. **在途预占。** 已经建好但一分钱还没记账的任务(台账是 worker
       调用之后才写的),见 `spend.reserved_for_generation_plan`。

    `provider` / `candidate_count` 缺省是空与 0:那样第 2 步的预估是 0 元,
    也就是退回改之前的行为。**保留这个缺省是为了不让别的调用方静默变红**,
    不是因为那样是对的 —— `create_task` 一定传真值。
    """
    if plan_row is None or plan_row.budget_cap is None:
        return gp.BudgetVerdict.ALLOWED

    # 见 docstring 第 1 条。锁的粒度是这一份方案,不挡别的方案建任务
    locks.advisory_xact_lock(session, "generation_plan_budget", plan_row.id)

    spent = spend.spent_for_generation_plan(session, plan_row.id)
    estimated: Any = 0
    if candidate_count > 0:
        this_one = spend.estimated_cost(
            provider, operation="submit", units=candidate_count
        )
        in_flight = spend.reserved_for_generation_plan(session, plan_row.id)
        # 任一项算不出来就整体"不知道"。两者相加时把 None 当 0 的写法
        # 会让"估不出来"静默变成"这一次不花钱"
        estimated = (
            None if this_one is None or in_flight is None else this_one + in_flight
        )
    return gp.budget_verdict(
        budget_cap=plan_row.budget_cap, spent=spent, estimated=estimated
    )


#: 任务列表允许排序的字段。`updated_at` 对这张表尤其有用 —— 它同时是心跳
#: (`reap_stalled` 靠它判断 worker 还活着),按它倒序排就是「最近有动静的任务」。
TASK_SORTABLE = {
    "created_at": GenerationTask.created_at,
    "updated_at": GenerationTask.updated_at,
    "status": GenerationTask.status,
    "provider": GenerationTask.provider,
    "mode": GenerationTask.mode,
    "current_round": GenerationTask.current_round,
}


def list_tasks(
    session: Session,
    *,
    product_id: UUID | None = None,
    status: str | None = None,
    provider: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[GenerationTask], int]:
    sort_field, direction = normalize_sort(
        sort, order, allowed=TASK_SORTABLE, default_sort="created_at", default_order="desc"
    )
    stmt = select(GenerationTask)
    if product_id:
        stmt = stmt.where(GenerationTask.product_id == product_id)
    if status:
        stmt = stmt.where(GenerationTask.status == status)
    if provider:
        stmt = stmt.where(GenerationTask.provider == provider)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    ordered = apply_order(
        stmt,
        columns=TASK_SORTABLE,
        sort=sort_field,
        order=direction,
        tiebreak=GenerationTask.id,
    )
    rows = list(session.scalars(ordered.offset(offset).limit(limit)))
    return rows, total


def cancel_task(session: Session, task_id: UUID, *, actor: str) -> GenerationTask:
    task = get_task(session, task_id)
    if sm.is_terminal(task.status):
        raise ValidationError(
            f"任务已处于终态 {task.status},无法取消",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )
    if not sm.can_cancel(task.status):
        raise ValidationError(
            f"{task.status} 阶段不能取消,请等待本轮结束",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )

    # 打标记 + 立即置状态。worker 在每个步骤边界检查这个标记(协作式取消)。
    task.cancel_requested = True
    transition(session, task, TaskStatus.CANCELLED)
    audit.record(
        session, actor=actor, action=AuditAction.UPDATE,
        entity_type="GenerationTask", entity_id=task.id, payload={"action": "cancel"},
    )
    return task


def can_delete_task(task: GenerationTask) -> bool:
    """已取消任务可以从业务界面删除；运行中的任务必须先取消。"""
    return task.status == TaskStatus.CANCELLED.value


def delete_task(session: Session, task_id: UUID, *, actor: str) -> None:
    """删除已取消任务；费用流水与删除审计不随任务级联清除。"""
    task = get_task(session, task_id, for_update=True)
    if not can_delete_task(task):
        raise ValidationError(
            "只有已取消的任务可以删除；请先取消仍在运行的任务",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )

    audit.record(
        session,
        actor=actor,
        action=AuditAction.DELETE,
        entity_type="GenerationTask",
        entity_id=task.id,
        payload={
            "reason": "cancelled_by_user",
            "product_id": task.product_id,
            "provider": task.provider,
            "current_round": task.current_round,
            "idempotency_key": task.idempotency_key,
        },
    )
    session.delete(task)


def can_resume_formatting(session: Session, task: GenerationTask) -> bool:
    """判断这次失败是不是"图已经选好了,只是转换那步崩了"。

    条件是有一张 SELECTED 候选、但没有任何启用中的成品图。
    满足时重试不该重新生成 —— 重新生成要再花一次 Provider 的钱,
    而且换了 seed 之后拿到的根本不是当初通过审核的那张图。
    """
    from app.models.output_asset import OutputAsset

    selected = session.scalar(
        select(GenerationCandidate).where(
            GenerationCandidate.task_id == task.id,
            GenerationCandidate.status == CandidateStatus.SELECTED.value,
        )
    )
    if selected is None:
        return False
    existing = session.scalar(
        select(func.count())
        .select_from(OutputAsset)
        .where(OutputAsset.task_id == task.id, OutputAsset.enabled.is_(True))
    )
    return not existing


def can_resume_scoring(session: Session, task: GenerationTask) -> bool:
    """判断这次失败是不是"图已经拿到了,崩在评分那一步"。

    条件有两条,缺一不可:

    1. 错误码是 ``WORKER_STALLED`` —— worker 中途消失,不是评分给出了结论;
    2. 本轮**确实存在**已经下载成功的候选图。

    满足时重试不该回到 QUEUED:那会清掉 external_task_id 并重新调 Provider,
    再花一次生成的钱,而且换了 seed 之后拿到的是另一批图。已经下载入库的
    这批图什么问题都没有,只是还没被评过。

    刻意**不**接受 SUBMIT_RESULT_UNKNOWN:那个码意味着提交阶段就没弄清楚,
    该走对账流程,不该从评分接着跑。
    """
    if task.error_code != WORKER_STALLED_CODE:
        return False
    if not task.current_round:
        return False
    usable = session.scalar(
        select(func.count())
        .select_from(GenerationCandidate)
        .where(
            GenerationCandidate.task_id == task.id,
            GenerationCandidate.round_number == task.current_round,
            GenerationCandidate.status != CandidateStatus.DOWNLOAD_FAILED.value,
        )
    )
    return bool(usable)


def can_resume_provider_results(session: Session, task: GenerationTask) -> bool:
    """判断这次失败是不是"外部任务还在,只是结果没取回来"(EX-03)。

    两条,缺一不可:

    1. 错误码是 ``PROVIDER_RESULT_PENDING`` —— 由 `_await_and_collect` 在
       确认「这一类错误说的是我们没问出来、不是外部任务失败了」之后写下;
    2. ``external_task_id`` 还在任务上 —— 没有它就无从继续。

    写下这个码的地方有三处:

        get_status / fetch_results 抛可重取的 ProviderError   我们没问出来
        _persist_candidates 崩了(A45-batch12-3 / REG-03)      问出来了,没存下
        候选全部下载失败(A45-batch12-3 / REG-01)              问出来了,一张都没下下来
        reap_stalled 回收 PROVIDER_RUNNING / DOWNLOADING       worker 死在等结果那一段
                     且 external_task_id 还在(REG-02)

    后三处是后补的。它们比第一处更贵:那时 `fetch_results` 已经返回或即将返回,
    钱确实花了、图确实生成了,只是我们这一侧没把它收回来。
    原来它们分别走通用分支清 ID 回 QUEUED、或被 reaper 压成
    `SUBMIT_RESULT_UNKNOWN`(强制重试同样清 ID)—— 都是把一批已经付过钱的图
    扔掉重买。

    满足时重试**必须保留 external_task_id**,回到 `PROVIDER_RUNNING`,
    由 worker 从 `get_status()` 接着走。清掉 ID 回 QUEUED 会重新 `submit()`,
    也就是把已经花过钱的那一笔生成扔掉再买一次 —— EX-03 报的正是这件事。

    刻意**不**接受 `SUBMIT_RESULT_UNKNOWN`:那个码的意思是"不知道对方受理
    了没有",没有可信的 ID 可以接着查,该走对账流程。
    """
    if task.error_code != PROVIDER_RESULT_PENDING_CODE:
        return False
    return bool((task.external_task_id or "").strip())


def retry_task(
    session: Session,
    task_id: UUID,
    *,
    actor: str,
    force: bool = False,
    reconciliation_note: str | None = None,
) -> GenerationTask:
    """重试。``force`` 用来放行「提交结果未知」的任务。

    为什么要多一道:提交阶段超时的任务很可能**已经在 Provider 那边跑起来了**。
    直接重试等于再买一次。让人先去对账,确认没跑起来再带 force 回来。

    ## 强制重试必须留下依据(报告 6.4)

    `force` 原来既不进审计、也不要求说明。于是"谁在什么时候、依据什么
    决定再买一次"这件事在库里完全不存在 —— 而这正是唯一一个由人**主动
    承担重复计费风险**的动作。出了重复计费之后能查到的只有一条普通的
    "retry",和自动重试长得一模一样。

    `reconciliation_note` 是人工对账的结论(在 Provider 后台查到了什么)。
    强制重试时必填:填不出来,说明对账根本没做。
    """
    task = get_task(session, task_id)
    if task.error_code == SUBMIT_UNKNOWN_CODE and force:
        note = (reconciliation_note or "").strip()
        if not note:
            raise ValidationError(
                "强制重试必须填写人工对账结论(在 Provider 后台查到了什么),"
                "它会进审计 —— 这是唯一一个由人主动承担重复计费风险的操作",
                code=ErrorCode.INPUT_INVALID,
                http_status=422,
            )
    if task.error_code == SUBMIT_UNKNOWN_CODE and not force:
        raise ValidationError(
            "这条任务在提交时网络超时,无法确认 Provider 是否已经受理。"
            "请先到 Provider 后台核对;确认没有产生结果后,用「强制重试」继续",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )
    # ---- A45-batch12-2 / EX-05:源文件坏了,重试不会收敛 ----
    #
    # 挡在这里而不是让它往下走,是因为**两条往下的路都是错的**:
    #
    #     续跑 FORMATTING   `can_resume_formatting()` 只看库里有没有 SELECTED 行,
    #                       看不见那一行指着的对象还在不在 —— 于是重试再次读同一个
    #                       坏路径,`FAILED -> FORMATTING -> FAILED` 无限循环
    #     回 QUEUED 重生     清掉 external_task_id 重新调 Provider,**再花一次钱**,
    #                       而且换了 seed 拿到的不是当初通过审核的那张图
    #
    # 正确的处理是让人显式选:回审核页换一张候选(免费),或者确认要重新生成
    # (花钱)之后带 force + 说明过来。和 SUBMIT_RESULT_UNKNOWN 用同一套护栏 ——
    # 那一条守的也是"由人主动承担重复计费风险"。
    if task.error_code in SOURCE_ASSET_BROKEN_CODES and not force:
        raise ValidationError(
            "选中的候选图文件已经丢失或损坏,重试只会一遍遍读同一个坏文件。"
            "请回审核页改选一张可用的候选;确实需要重新生成(会再产生一次费用)时,"
            "用「强制重试」并填写说明",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )
    if task.error_code in SOURCE_ASSET_BROKEN_CODES and force:
        note = (reconciliation_note or "").strip()
        if not note:
            raise ValidationError(
                "强制重试必须填写说明:重新生成会再花一次 Provider 的费用,"
                "而且换了 seed 之后拿到的不是当初通过审核的那张图",
                code=ErrorCode.INPUT_INVALID,
                http_status=422,
            )
    if not sm.can_retry(task.status):
        raise ValidationError(
            f"{task.status} 状态不支持重试",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )
    # 在任何状态流转**之前**记下来:`transition()` 会把 error_code 清掉,
    # 而审计要回答的正是"当初是因为什么才需要强制重试"
    previous_error_code = task.error_code
    task.cancel_requested = False

    # 从崩的地方接着走,而不是一律退回花钱的那一步。
    #
    # 以前只认 FORMATTING 一种续跑,别的失败一律清掉 external_task_id 回 QUEUED。
    # 于是"卡在评分被回收成 FAILED"的任务一点重试就会重新调 Provider ——
    # 已经生成并下载入库的那批候选图整批作废,钱白花一次,而问题
    # (评分器不通、worker 被杀)根本不在图片上。
    #
    # A45-batch12-2 / EX-05:源文件坏了的强制重试**必须重新生成**,
    # 不能走 FORMATTING 续跑 —— 那条路读的正是那个坏文件。判定放在最前面,
    # 因为 `can_resume_formatting()` 会对它返回 True(库里那一行还在)
    if task.error_code in SOURCE_ASSET_BROKEN_CODES:
        resume, target = "", TaskStatus.QUEUED
    elif can_resume_provider_results(session, task):
        # A45-batch12-2 / EX-03。**不清 external_task_id** —— 外部那笔生成
        # 还在跑,它是唯一能把结果取回来的凭证。worker 从 `_run` 顶部的
        # PROVIDER_RUNNING 分支进来,跳过 submit 直接 get_status
        resume, target = "provider_results", TaskStatus.PROVIDER_RUNNING
    elif can_resume_formatting(session, task):
        resume, target = "formatting", TaskStatus.FORMATTING
    elif can_resume_scoring(session, task):
        # **不清 external_task_id**:它是"这一轮已经花过钱"的凭证,
        # 清掉之后再也查不到那次生成
        resume, target = "scoring", TaskStatus.SCORING
    else:
        resume, target = "", TaskStatus.QUEUED

    # ---- A45-batch12-7 / R-05:续跑目标必须是当前状态**真的到得了**的 ----
    #
    # 上面那道 `can_retry()` 问的是「QUEUED 在不在出边里」,而出边含 QUEUED 的
    # 状态有两个:`FAILED` 和 **`CREATED`**。三条续跑边(FORMATTING / SCORING /
    # PROVIDER_RUNNING)只从 FAILED 出发,CREATED 一条都没有。于是一条还没跑过
    # 的任务只要撞上续跑判定,`transition()` 就抛 `不能从 CREATED 转到 SCORING`
    # —— 一句状态机内部语言,而运营手上没有任何下一步。
    #
    # 构造条件苛刻(CREATED 的任务通常没有候选图),但不是构造不出来:重试是
    # 幂等键复用的入口,`dispatch_service.reactivate()` 与 `requeue_stranded`
    # 也走这里;只要库里有一行本轮 SELECTED 候选、而任务被打回 CREATED,
    # `can_resume_formatting()` 就会返回 True —— 它查的是候选,不问任务状态。
    #
    # 修法**不是**把 CREATED 从 `can_retry()` 里摘掉:`CREATED -> QUEUED`
    # 合法且正确,摘掉等于让「刚建出来的任务点重试」从能走变成 409。
    # 这里只把**续跑**挡在转移表后面 —— 到不了就退回重生,和三条判定
    # 都不成立时走同一条路。转移表仍然是唯一真相源(state_machine 顶部第一条),
    # 所以这里问的是 `can_transition()`,不是手写一份「哪些状态可以续跑」。
    #
    # 兜底那一步不会再落空:`can_retry()` 的定义就是 QUEUED 在出边里。
    if not sm.can_transition(task.status, target):
        resume, target = "", TaskStatus.QUEUED
    if not resume:
        # 重生:清掉 external_task_id,下一轮重新 submit
        task.external_task_id = None
    transition(session, task, target)

    payload: dict = {"action": "retry", "resume": resume or "regenerate"}
    if force:
        # 强制重试单独标注,并把对账结论一起留下(报告 6.4)。
        # 审计是追加的,任务状态不是 —— 重试完成后 error_code 会被清掉,
        # 那时"这条任务曾经是提交结果未知"就只剩这里还记得
        payload["force"] = True
        payload["previous_error_code"] = previous_error_code
        if reconciliation_note:
            payload["reconciliation_note"] = reconciliation_note.strip()[:500]
    audit.record(
        session, actor=actor, action=AuditAction.UPDATE,
        entity_type="GenerationTask", entity_id=task.id,
        payload=payload,
    )
    return task


def list_candidates(session: Session, task_id: UUID) -> list[GenerationCandidate]:
    return list(
        session.scalars(
            select(GenerationCandidate)
            .where(GenerationCandidate.task_id == task_id)
            .order_by(
                GenerationCandidate.round_number, GenerationCandidate.candidate_index
            )
        )
    )


def list_attempts(session: Session, task_id: UUID) -> list[GenerationAttempt]:
    return list(
        session.scalars(
            select(GenerationAttempt)
            .where(GenerationAttempt.task_id == task_id)
            .order_by(GenerationAttempt.round_number, GenerationAttempt.attempt_number)
        )
    )


#: 一次生成调用的计费身份前缀。见 `submit_billing_key()`。
SUBMIT_BILLING_OPERATION = "submit"


def submit_billing_key(attempt_id: UUID | str) -> str:
    """一条 attempt 的生成费用身份。**同一条 attempt 只能有一笔生成费用。**

    这不是"约定俗成",是可以论证的:`attempt` 的定义就是"我们向 Provider
    发过一次生成请求"(见 `models/generation.GenerationAttempt` 的文档)。
    换 Provider 重试会新建 attempt,重生下一轮会新建 attempt —— 那些都是
    真的又买了一次。而续跑(`resuming=True`)复用的是**同一条 attempt**,
    因为它复用的正是同一个 `external_task_id`,也就是同一笔已经付过的钱。

    所以 attempt 与"一笔生成消费"是一一对应的,拿它当计费键是准确的,
    不是近似。
    """
    return f"{attempt_id}:{SUBMIT_BILLING_OPERATION}"


def record_usage(
    session: Session,
    *,
    provider: str,
    task_id: UUID | None,
    attempt_id: UUID | None,
    operation: str,
    succeeded: bool,
    candidate_count: int = 0,
    duration_ms: int | None = None,
    error_code: str | None = None,
    billable_units: int | None = None,
    billing_key: str | None = None,
    units_source: str = UnitsSource.INFERRED.value,
    provider_attempts: int | None = None,
) -> ProviderUsageRecord:
    """记一条 Provider 调用流水,**并在这一刻把钱算进去**。

    ## 计价必须发生在这里

    `core/pricing.py` 与 `services/spend.py` 的算术早就写好了,但在 a25 之前
    **一次都没有被调用过** —— 流水行是直接 `ProviderUsageRecord(...)` 建出来的,
    五个费用列全留默认值。后果正是 `pricing.py` 顶部警告的那件事:

        /api/usage/spend        恒为 0
        预算档位                 永远 OK,告警横幅一次都不会亮
        unpriced_calls          恒等于全部调用数,于是「去配价目表」的提示
                                在价目表**已经配好**之后仍然常亮

    最后一条最伤:一条永远亮着的提示会被训练成噪音,等它某天说的是真的,
    没有人会再看它。

    计价放在创建那一刻而不是事后补算,理由见 `spend.record_cost` ——
    事后补算等于「按今天的价目表 × 历史用量」,厂商一调价上个月的报表就变了。

    ## `billable_units` 为什么可以显式传

    默认取 `max(candidate_count, 1)`:绝大多数厂商按「一次调用」或「一张图」
    计费,而**失败的调用往往也计费** —— 一次失败可能 units=1 却一张图都没产出,
    那正是最需要被记上账的一种。所以默认不是 `candidate_count`(失败时是 0),
    而是至少 1。

    调用方知道得更准时显式传:比如确定没到达厂商、根本不会计费的那种失败传 0。

    ## `units_source`:上面那个数**是谁说的**(A45-batch14-18 / 任务 9)

    默认 `inferred` —— 不传就是"我们数出来的"。这个默认方向是刻意的:
    忘了接线的表现是台账诚实地说"这是估的",而不是一张估算表冒充和账单同源。

    只有拿到厂商自述时才传 `provider`,判定在
    `providers/base.settle_billable_units()` —— 那是唯一一处做这个选择的地方,
    别在调用点上各自判一遍。

    今天**只有 FASHN 的生成调用**能报出 `provider`;轮询、取结果、评分、
    属性识别四类仍然全是 `inferred`(欠账逐条记在 `docs/STATUS.md`)。

    ## `provider_attempts`:这一行代表**几次真实网络请求**(A45-batch18 / P1-2)

    `billable_units` 回答的是"记几个计费单位",而对账的人先要回答另一个
    问题:"这一行对应供应商后台的几条调用记录"。两者在识别链路上恰好
    相等(一次往返 = 一个单位),在生成链路上不等(FASHN 一次 POST 可能
    出多张图、按额度计价),所以不能合成一列。

    `None` = 这一类调用没有接上次数上报。**不写 0** —— 0 的意思是
    "确认一个请求都没发出去"(preflight 失败),和"不知道"是两件事,
    合并之后一次真实调用会在对账表上凭空消失。

    ## `billing_key`:同一笔消费只能有一行(A45-batch12-5 / NEW-01)

    传了键就变成「有则更新,无则新增」。这一条是 REG-01 那次修复的直接后果:
    它给 `_await_and_collect()` 开了第二条进入路径(续跑),而这个函数的公共
    出口无条件写一条 `operation=submit` 的流水。于是同一笔外部生成:

        第一次   submit / units=3 / succeeded=False    全部下载失败
        恢复后   submit / units=3 / succeeded=True     同一笔,又记一遍

    Provider 只跑过一次、只收过一次钱,台账记了 6 个单位。**这不是重复扣费**
    (外部没有重复 submit),是内部账目失真 —— 而失真的恰好是恢复过的那批任务,
    也就是最需要被看清楚的一批。

    更新而不是新增,还有一个不能省的理由:恢复成功之后那次调用的**结论变了**
    (从"失败"变成"成功",候选数从 0 变成 3)。新增一行会让同一笔消费在库里
    同时是成功和失败,失败率统计从此无解。

    ### 计费单位只增不减

    `max(已记, 本次)`。恢复这一遍拿回来的候选数可能比第一遍少(`fetch_results`
    返回的批次缩水,见 `_drop_shrunk_tail`),但钱是按**第一次实际产出**收的。
    往下改等于让台账少记一笔已经花掉的钱,而这个方向的错误没有人会去发现。

    ## 返回值

    返回那条流水行(新建的或被更新的)。调用方基本不用,但测试要拿它断言
    "到底是新增了一行还是更新了一行"—— 而那正是这次修复的全部内容。
    """
    record: ProviderUsageRecord | None = None
    if billing_key:
        # 会话是 `autoflush=False` 建的(见 `db/session.py`),所以这条 SELECT
        # **看不见**同一事务里刚 `add()` 还没落库的行。同一笔消费在一个事务里
        # 记两次今天不会发生,但这是个静默失败:漏掉时的表现是又多一行,
        # 和这次要修的 bug 一模一样。先 flush 一次把这条路堵死 ——
        # 调用点上这一句都是 no-op(`_finish_attempt` / `_persist_candidates`
        # 刚刚才 flush 过)。
        session.flush()
        record = session.scalar(
            select(ProviderUsageRecord).where(
                ProviderUsageRecord.billing_key == billing_key
            )
        )

    units = max(candidate_count, 1) if billable_units is None else billable_units

    if record is not None:
        # 同一笔消费的第二次记账:更新结论,不新增行
        units = max(int(record.billable_units or 0), int(units))
        record.succeeded = succeeded
        record.candidate_count = candidate_count
        record.duration_ms = duration_ms
        record.error_code = error_code
        # provider 可能因为修复策略换过(`_apply_decision` 的 switch_provider),
        # 但那会新建 attempt,所以走到这里两次一定是同一个 provider。
        # 仍然跟着写,是为了让这一行永远自洽:价格是按 provider 解析的
        record.provider = provider
        # 来源跟着 `units` 一起更新。两者必须同进同退:恢复那一遍如果拿到了
        # 厂商自述而第一遍没有,`units` 会变成厂商的数,而 `units_source`
        # 留在 `inferred` —— 一行**数对了但标错来源**的流水,比两个都错更难查。
        record.units_source = units_source
        # 次数与 units 同进同退,理由同上一行:一行数对了但次数留在旧值,
        # 比两个都旧更难查。`None` 不覆写已有值 —— 恢复那一遍没接上报,
        # 不代表第一遍数出来的次数不作数
        if provider_attempts is not None:
            record.provider_attempts = int(provider_attempts)
        logger.info(
            "updating the existing usage record instead of adding a second one",
            extra={
                "extra_fields": {"event": "gen.usage_record_updated",
                    "billing_key": billing_key,
                    "operation": operation,
                    "billable_units": units,
                    "units_source": units_source,
                    "succeeded": succeeded,
                }
            },
        )
    else:
        record = ProviderUsageRecord(
            provider=provider,
            task_id=task_id,
            attempt_id=attempt_id,
            operation=operation,
            succeeded=succeeded,
            candidate_count=candidate_count,
            duration_ms=duration_ms,
            error_code=error_code,
            billing_key=billing_key,
            units_source=units_source,
            provider_attempts=(
                None if provider_attempts is None else int(provider_attempts)
            ),
        )
        session.add(record)

    # 重算而不是保留原值:单位数可能变了(第一次未知、恢复后确定),
    # 而 `record_cost` 是原地写那五列的,更新路径同样适用
    spend.record_cost(record, provider=provider, operation=operation, billable_units=units)
    return record


#: 派发失败时写进 error_code 的标记。扫描器靠它找滞留任务。

#: 可能因为派发失败而滞留的状态。任务已经落库,但队列里没有对应的消息。
STRANDED_STATUSES = (TaskStatus.CREATED.value, TaskStatus.REGENERATING.value)


def find_stranded(session: Session, *, older_than_seconds: int = 120) -> list[GenerationTask]:
    """找出可能卡在「已落库但没进队列」的任务。**只服务存量数据。**

    0008 迁移之后新建的任务都有 Outbox 记录,漏投是可以直接查出来的,不需要猜。
    这个函数留下来只为捞回迁移**之前**创建、因而没有 Outbox 记录的任务:
    显式标了 DISPATCH_FAILED 的,以及停在可派发状态超过一段时间的。

    它是启发式的 —— 队列积压时会把正常排队的任务也算进来。重复派发本身安全
    (worker 侧有原子认领),但这份不精确正是当初要做 Outbox 的原因。
    存量清空之后可以整个删掉。

    `mark_dispatch_failed()` 已经删除:派发失败现在由 Outbox 的 attempts /
    last_error 记录,再留一个"打错误码"的入口只会让人绕过 Outbox。
    """
    cutoff = utc_now() - timedelta(
        seconds=max(0, older_than_seconds)
    )
    return list(
        session.scalars(
            select(GenerationTask).where(
                GenerationTask.status.in_(STRANDED_STATUSES),
                GenerationTask.updated_at < cutoff,
            )
        )
    )


# ------------------------------------------------------------------ 卡死回收


def _stall_cutoff(older_than_seconds: int) -> datetime:
    return utc_now() - timedelta(
        seconds=max(0, older_than_seconds)
    )


def _resolve_stall_window(older_than_seconds: int | None) -> int:
    """没指定就按**当前配置**算(A45-batch12-5 / NEW-03)。

    以前这两个函数的默认参数写的是 `= STALL_THRESHOLD_SECONDS`,而 Python 的
    默认参数在 **import 那一刻**求值。于是运营在设置页把视觉超时从 90 调到 180
    (页面上写着"改完就生效"),阈值仍然是旧那一版算出来的数 —— 而这一改**正好**
    是让合法耗时变长的方向,也就是最需要阈值跟着变的方向。

    改成运行时解析之后,"改完就生效"对回收器也成立了。
    """
    if older_than_seconds is None:
        return stall_threshold_seconds()
    return older_than_seconds


def find_stalled(
    session: Session, *, older_than_seconds: int | None = None
) -> list[GenerationTask]:
    """找出 worker 中途消失、卡在进行中状态的任务。**只读,不改任何东西。**

    判据是「停在 worker 持有的状态 + 超过阈值没有心跳 + 没有活着的阶段租约」。
    拆短事务之后每个阶段边界都会推进 ``updated_at``,长阶段里还有逐张心跳
    (A45-batch12-5 / NEW-03),所以\"多久没动过\"是一个可靠的心跳信号。

    刻意不含 QUEUED(队列积压是容量问题)和 MANUAL_REVIEW(在等人,等多久都正常),
    这两条由 `state_machine.STALLABLE_STATUSES` 守着。
    """
    now = utc_now()
    return list(
        session.scalars(
            select(GenerationTask)
            .where(
                GenerationTask.status.in_(sm.STALLABLE_STATUSES),
                GenerationTask.updated_at < _stall_cutoff(
                    _resolve_stall_window(older_than_seconds)
                ),
                _lease_is_not_alive(now),
            )
            .order_by(GenerationTask.updated_at)
        )
    )


def _lease_is_not_alive(now: datetime):
    """「这一行现在没有人正拿着阶段租约」的过滤条件(A45-batch12-5 / NEW-03)。

    ## 为什么心跳之外还要看租约

    心跳判据有一个前提:worker 活着就一定会推进 `updated_at`。这个前提在
    **库抖一下**的时候不成立 —— 心跳那次 UPDATE 失败了,worker 却还在正常
    评分。而回收器接着就会把它落成 FAILED,原 worker 最后提交时撞上状态冲突,
    已经产生的视觉模型费用可能进不了台账,运营重试之后又重新评一遍。

    租约是第二个独立信号:持有者每个提交点都会续期,续期成功意味着**它此刻
    确实还在跑**。两个信号都指向"没动静"才回收。

    代价是一个真死掉的 worker 会多挡一会儿(最多一个租约周期)。这个方向是
    对的:回收晚了只是让一条已经死掉的任务多躺一会儿,回收早了要重新花钱。
    """
    from sqlalchemy import or_

    return or_(
        GenerationTask.phase_lease_until.is_(None),
        GenerationTask.phase_lease_until < now,
    )


def _reap_batch(
    session: Session,
    *,
    cutoff: datetime,
    statuses: tuple[str, ...],
    error_code: str,
    error_message: str,
    spent_only: bool,
    external_id: bool | None = None,
) -> list[str]:
    """一条带状态条件的原子 UPDATE。返回真正被这次调用回收的任务 id。

    为什么必须是一条 UPDATE 而不是\"先查再改\":回收器可能同时跑在 beat 和
    运维手跑的脚本里,而且**任务随时可能自己活过来** —— worker 只是慢,不是死。
    把 ``status`` 和 ``updated_at`` 一起写进 WHERE,数据库会保证只有当这一行
    仍然是我们看到的那个样子时才改它;活过来的任务会推进 updated_at,
    于是自动落在条件之外,不会被误杀。

    ``external_id`` 只影响**措辞分组**,不影响判定:
    True 只收有外部 ID 的,False 只收没有的,None 不限。
    两种\"结果未知\"给运营的下一步动作不同(照 ID 查 vs 按时间窗查),
    而错误消息是一条 UPDATE 写死的常量,所以只能靠分两次写来区分。
    """
    stmt = (
        update(GenerationTask)
        .where(
            GenerationTask.status.in_(statuses),
            GenerationTask.updated_at < cutoff,
            # 正拿着阶段租约的行不回收:租约在每个提交点续期,续期成功就是
            # "它此刻还在跑"的第二个独立证据(见 `_lease_is_not_alive`)
            _lease_is_not_alive(utc_now()),
        )
        .values(
            status=TaskStatus.FAILED.value,
            error_code=error_code,
            error_message=error_message,
            updated_at=utc_now(),
        )
        .returning(GenerationTask.id)
        .execution_options(synchronize_session=False)
    )
    if external_id is True:
        stmt = stmt.where(GenerationTask.external_task_id.is_not(None))
    elif external_id is False:
        stmt = stmt.where(GenerationTask.external_task_id.is_(None))
    if not spent_only:
        # 已经在上一批处理过的\"可能已计费\"任务不再重复认领。
        #
        # ## A45-batch12-2 / EX-01:这里原来还带着 `external_task_id IS NOT NULL`
        #
        # 两批的划分原来是\"状态可能已计费 **且** 已经拿到外部 ID\"。那个 `且`
        # 假设了一件不成立的事:**拿到外部 ID 和把它落库是两个事务。**
        #
        #     _checkpoint(task, \"submitting\")   提交前落库,状态 = SUBMITTING
        #     provider.submit(request)          对方受理,可能已经开始计费
        #     task.external_task_id = 外部 ID
        #     session.commit()                  <-- 崩在这里
        #
        # 崩在最后那一句,库里留下的是「SUBMITTING + external_task_id 为空」,
        # 而 Provider 那边任务已经存在。带着 ID 条件分批的话,这一行落进
        # 下面的 plain 批次、判成 `WORKER_STALLED` —— 那个码的含义是\"钱没花出去,
        # 直接重试即可\",于是运营点一次重试就再买一次,而第一次那批图永远没人认领。
        #
        # 现在两批**只按状态划分**:进了 `SPENT_STATUSES` 就意味着请求已经
        # 送出去过,不管 ID 有没有来得及落库。代价是\"崩在 submit 之前\"的任务
        # 也要走一次人工对账;收益是没有任何一条可能已计费的任务会掉进
        # 可一键重试的分支。这两个方向的代价不对称,取贵的那一侧。
        stmt = stmt.where(~GenerationTask.status.in_(sm.SPENT_STATUSES))
    return [str(row) for row in session.scalars(stmt)]


def reap_stalled(
    session: Session, *, older_than_seconds: int | None = None
) -> list[str]:
    """把卡死的任务落成 FAILED,让它们重新可以被重试或取消。

    调用方负责 commit —— 这样脚本里可以先 `find_stalled()` 打印一遍再决定动不动手。

    分批写,是因为这几批任务**人该怎么处理不一样**。判据有两个维度:
    停在哪个状态,以及外部 ID 在不在。

    ## A45-batch12-3 / REG-02:两个维度缺一不可

    上一版只按状态分,整个 `SPENT_STATUSES` 一律落 `SUBMIT_RESULT_UNKNOWN`。
    那个码的含义是「不知道对方受理了没有」,而 `can_resume_provider_results()`
    只认 `PROVIDER_RESULT_PENDING` —— 于是停在 `PROVIDER_RUNNING` /
    `DOWNLOADING`、**外部 ID 明明就在任务上**的那一批,强制重试会掉进
    `retry_task()` 最后那个 `else`:

        status           -> QUEUED
        external_task_id -> NULL
        下一个 worker    -> provider.submit()   再买一次

    外部那笔生成还在跑或已经跑完,而我们主动把唯一能取回它的凭证删掉了。
    这条路上人是被系统**逼**着走的:对账流程的唯一出口就是强制重试。

    现在的分类:

        SUBMITTING,有/无 ID          `SUBMIT_RESULT_UNKNOWN`,人工对账
            提交那一瞬崩的。有 ID 的照 ID 查,没 ID 的按商品 + 时间窗查。
            两种都不能自动重试:请求可能已经到了对方那里。

        PROVIDER_RUNNING / DOWNLOADING,有 ID    `PROVIDER_RESULT_PENDING`
            **已经确定受理过,而且凭证还在。** 重试保留 ID 回
            `PROVIDER_RUNNING`,从 `get_status` 接着走,submit 次数保持 1。

        PROVIDER_RUNNING / DOWNLOADING,无 ID    `SUBMIT_RESULT_UNKNOWN`
            不该存在的形状,成因只可能是落 ID 那次 commit 崩了 ——
            也就是钱可能花了、我们却查无此 ID。按最贵的那一侧处理。

        其余                          `WORKER_STALLED`
            钱没花出去,直接重试即可。
    """
    older_than_seconds = _resolve_stall_window(older_than_seconds)
    cutoff = _stall_cutoff(older_than_seconds)
    submit_known = _reap_batch(
        session,
        cutoff=cutoff,
        statuses=tuple(sm.SUBMIT_IN_DOUBT_STATUSES),
        error_code=SUBMIT_UNKNOWN_CODE,
        error_message=(
            "worker 在提交期间中断,Provider 是否已受理未知。"
            "任务上已记录外部任务 ID,请照它到 Provider 后台核对,"
            "再决定是否强制重试"
        ),
        spent_only=True,
        external_id=True,
    )
    submit_blind = _reap_batch(
        session,
        cutoff=cutoff,
        statuses=tuple(sm.SUBMIT_IN_DOUBT_STATUSES),
        error_code=SUBMIT_UNKNOWN_CODE,
        error_message=(
            "worker 在提交期间中断,且外部任务 ID 没能落库 —— Provider 可能"
            "已经受理并开始计费,而我们这边查无此 ID。请按这条任务的商品与"
            "提交时间窗到 Provider 后台核对,确认没有产生结果之后再强制重试"
        ),
        spent_only=True,
        external_id=False,
    )
    # 这一批是 REG-02 修的那条:**不落 SUBMIT_RESULT_UNKNOWN。**
    # 它已经确定受理过,而且凭证还在 —— 让它走对账 + 强制重试等于逼着人
    # 把凭证删掉再买一次
    resumable = _reap_batch(
        session,
        cutoff=cutoff,
        statuses=tuple(sm.AWAITING_PROVIDER_RESULT_STATUSES),
        error_code=PROVIDER_RESULT_PENDING_CODE,
        error_message=(
            "worker 在等待或下载外部结果期间中断。外部任务已经受理、"
            "任务 ID 也在库里 —— 重试会保留这个 ID 从查询结果那一步接着走,"
            "不会重新提交生成,也不会再产生一次费用"
        ),
        spent_only=True,
        external_id=True,
    )
    awaiting_blind = _reap_batch(
        session,
        cutoff=cutoff,
        statuses=tuple(sm.AWAITING_PROVIDER_RESULT_STATUSES),
        error_code=SUBMIT_UNKNOWN_CODE,
        error_message=(
            "worker 在等待外部结果期间中断,但任务上没有外部任务 ID —— "
            "这说明落 ID 的那次提交没能完成,Provider 可能已经受理并开始计费,"
            "而我们这边查无此 ID。请按商品与提交时间窗到 Provider 后台核对"
        ),
        spent_only=True,
        external_id=False,
    )
    spent_blind = [*submit_blind, *awaiting_blind]
    spent = [*submit_known, *spent_blind, *resumable]
    plain = _reap_batch(
        session,
        cutoff=cutoff,
        statuses=sm.STALLABLE_STATUSES,
        error_code=WORKER_STALLED_CODE,
        error_message=(
            f"worker 中断,任务超过 {older_than_seconds} 秒没有心跳,已回收为失败,可以重试"
        ),
        spent_only=False,
    )
    reaped = [*spent, *plain]
    if reaped:
        # 上面走的是 Core UPDATE,身份映射里的对象还是旧状态。
        # 不过期的话,同一个会话后面读到的会是回收前的样子。
        session.expire_all()
        logger.warning(
            "reaped stalled tasks",
            extra={
                "extra_fields": {"event": "gen.stalled_tasks_reaped",
                    "count": len(reaped),
                    "possibly_billed": len(spent),
                    # 这一批连外部 ID 都没有,对账成本最高。单独报数是为了让它
                    # 在日志里可被告警 —— 它出现说明落 ID 的那次 commit 在失败
                    "possibly_billed_without_external_id": len(spent_blind),
                    # 这一批不用人工对账,重试就能安全续取。单独报数是为了
                    # 让"该走对账的"和"能自己好的"在告警上分得开
                    "resumable_with_external_id": len(resumable),
                    "older_than_seconds": older_than_seconds,
                }
            },
        )
    return reaped
