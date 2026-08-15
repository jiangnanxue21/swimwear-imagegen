"""AC-05 的服务端闸(阶段 6 批次 6-4 修复 / F-2)。

## 这个模块存在的理由

AC-05 的判据原文:

> 绕过前置直接调用**后续动作**时,服务端按同一份判定拒绝,而不是仅前端置灰。

上一版把这道闸写在 `api/workbench.py` 里的一个私有函数上,并在三个端点上
手工调用。三个是 `generate_copy` / `build_draft` / `export_draft` ——
而 `NextActionCode` 有 22 个成员,其中 `RUN_EXTRACTION`、`CHOOSE_PLAN`、
`BUILD_IMAGE_SET`、`APPROVE_IMAGE_SET`、`APPROVE_COPY` 各自都有 HTTP 写端点。
也就是说 `POST /image-sets/{id}/approve` 在属性一条都没确认时,**今天不会被
任何东西拒绝** —— 而向导上那个按钮是灰的。AC-05 要拦的正是这个差。

它没有被守卫看见,是因为那条守卫**按名字点名**:

    for name, action in (("generate_copy", …), ("build_draft", …), ("export_draft", …)):
        assert "_ensure_action_allowed(" in functions[name]

这条断言证明的是"这三个过了闸",而不是"过闸的是全部"。新增一个不过闸的
前进端点,它永远不会红 —— §3.43 那一族的又一次:一个算好了没人读的规则,
和没有它是一样的。

## 所以这一版把"哪些端点属于前进动作"做成表

三张表,三条守卫(`tests/test_a45_batch31_action_gate.py`):

    GATED_ENDPOINTS          端点 -> 它执行的动作码。**这些必须调闸**
    UNGATED_ENDPOINTS        端点 -> 为什么它不需要过闸(一句话,不是空白)
    ACTIONS_WITHOUT_ENDPOINT 动作码 -> 为什么它没有对应的写端点

    守卫一   两张端点表的并集 == 应用里全部写路由,且交集为空
             新增一个写端点而没决定它该不该过闸 -> 当场红
    守卫二   `GATED_ENDPOINTS` 里的每个函数体里真的有一次 `ensure_*` 调用
             写进表却忘了接线 -> 当场红
    守卫三   每个 `NextActionCode` 要么在 `GATED_ENDPOINTS` 的值里出现,
             要么在 `ACTIONS_WITHOUT_ENDPOINT` 里给出理由
             新增一个动作码而没人执行它 -> 当场红

三条合起来的性质是:**这张表漏一行,测试就红**。而上一版的守卫漏一行是静默的。

## 为什么闸在接口层而不是 service 层

沿用上一版的判断,原文照搬(它是对的,只是覆盖面不够):

    HTTP        运营(或一个手搓的请求)发起的一次动作。AC-05 说的"调用"
                就是这一条 —— 它要拦的是"界面上按钮是灰的,我直接 POST 一下"
    批次执行    `batch_service` 直接调 service 函数,它有自己的跳过判定与
                回执表。在 service 层加同一道闸会让批次在"这一件还没就绪"时
                抛异常而不是记一条跳过 —— 那是另一件事,而且是退步

## 为什么理由从判定层取,不在这里写

AC-05 的最后一句是「判定层与前端读同一个结果」。这里如果自己写一句
"属性还没确认",向导上显示的会是 `gate_reason` 那一句,而 409 里是另一句 ——
同一件事两种说法,运营会以为是两个问题。

## SPU 级动作按「任一行可做」判,不是「全部行都可做」

图片集与生成方案是 SPU 级对象,而 `flow.evaluate()` 的作用域是一行 SKU。
两种合并规则:

    任一行可做即放行    拦住的是"一行都还没就绪时直接 POST" —— 那正是
                        AC-05 要拦的形状
    全部行就绪才放行    九色 SPU 里有一个颜色还没确认属性,整个 SPU 的图
                        就批不了。这会在**正常流程**里误伤,而运营没有
                        任何手段绕过它 —— 一道会挡住正常操作的闸,
                        最后一定会被关掉

所以取前者。它比不判严格,比"全部就绪"宽松,而这个位置正是 AC-05 的原意:
拒绝的是**没有任何依据**的调用。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ErrorCode, ValidationError
from app.models.product import Product
from app.workbench import flow as flow_rules
from app.workbench import service as wb
from app.workbench import wizard as wizard_rules
from app.workbench.flow import NextActionCode

# ================================================================ 判定

#: 端点 -> 它执行的动作码。**键是 `模块名:函数名`**,与守卫读的是同一个形状。
#:
#: 只列**前进动作**。往回走的动作(退回、回滚、取消)不在这里,理由逐条写在
#: `UNGATED_ENDPOINTS` 上 —— 不是"忘了",是"决定了不加"。
GATED_ENDPOINTS: Mapping[str, NextActionCode] = {
    # 属性步
    "attributes:extract_attributes": NextActionCode.RUN_EXTRACTION,
    "attributes:create_spu_extraction_run": NextActionCode.RUN_EXTRACTION,
    "attributes:confirm_attributes": NextActionCode.CONFIRM_ATTRIBUTES,
    # 方案步(SPU 级)
    "generation_plans:create_plan": NextActionCode.CHOOSE_PLAN,
    "generation_plans:activate_plan": NextActionCode.CHOOSE_PLAN,
    # 图片集步(SPU 级)
    "image_sets:create_set": NextActionCode.BUILD_IMAGE_SET,
    "image_sets:derive": NextActionCode.BUILD_IMAGE_SET,
    "image_sets:approve": NextActionCode.APPROVE_IMAGE_SET,
    # 文案步
    "workbench:generate_copy": NextActionCode.GENERATE_COPY,
    "workbench:approve_copy": NextActionCode.APPROVE_COPY,
    # 草稿步
    "workbench:build_draft": NextActionCode.BUILD_DRAFT,
    "workbench:export_draft": NextActionCode.EXPORT,
}

#: 不过闸的写端点 -> 为什么。**每一条都要有理由**,而且理由要说得出
#: 它不是"前进动作"。
#:
#: 空着比写错更危险:一个没有理由的豁免下次会被当成"以前就这样"。
UNGATED_ENDPOINTS: Mapping[str, str] = {
    # ---- 往回走 / 纠错。闸只拦"往前",拦住纠错等于把人锁在错误状态里 ----
    "image_sets:reject": "快审退回是往回走;拦住它,一版有问题的图就退不回去",
    "image_sets:rollback": "回滚到旧版本是往回走",
    "workbench:reject_copy": "快审退回是往回走",
    "workbench:resolve_rejection": "处理平台驳回是纠错回流,前置是驳回本身",
    "generation_tasks:cancel_task": "取消一次生成是往回走",
    "attributes:cancel_extraction": "取消一次识别是往回走",
    "generation_tasks:retry_task": "重试的前置是上一次失败,不是流程前置",
    "generation_tasks:delete_task": "删掉一条任务记录是清理,往前一步都不走",
    "media_library:quarantine": "隔离一张素材是纠错,不是往前走一步",
    "media_library:release": "放行被隔离的素材是纠错,不是往前走一步",
    "media_library:delete_media": "删掉一张废图是清理,往前一步都不走",
    # ---- 归档:它把商品移出流程,是往回走的最远那一步 ----
    #
    # 尤其**不能**过闸:这道闸拦的是「前置没满足就往前走」,而一件卡在
    # 第一步、什么前置都没满足的商品,恰恰是最需要能被归档的那一件。
    # 写进 GATED 的表现是「越是想放弃的商品越放弃不掉」。
    "products:archive_product": "归档把商品移出流程,不是往前走一步",
    "products:restore_product": "恢复是撤销归档,往回走",
    # ---- 素材步是自阻断步(`wizard.SELF_BLOCKING_STEPS`),闸恒放行 ----
    #
    # 这几条**不是**"不该过闸",是"过了也永远放行"。写进 GATED 的话,
    # 表面上覆盖率好看,实际是一道恒真的闸 —— 而恒真的闸会让读表的人
    # 以为素材步有前置。理由写在这里,比一道假闸诚实。
    "products:upload_asset": "素材步是自阻断步,闸恒放行(见 wizard.SELF_BLOCKING_STEPS)",
    "media_library:set_role": "同上:确认素材角色属于素材步,闸恒放行",
    # ---- 建档步:它的前置是"这一行还不存在" ----
    "products:create_product": "建档动作本身。此刻还没有 product 可供判定",
    "products:import_products": "建档动作本身,而且是批量入口",
    "spus:create_spu": "建档动作本身(SPU / 颜色 / SKU)",
    "spus:update_spu": "维护建档元数据，不推进流程",
    "spus:add_color_variant": "补建颜色与 SKU，属于建档动作",
    "spus:update_color_variant": "维护颜色元数据，不推进流程",
    "spus:create_skus": "补建 SKU，属于建档动作",
    "workbench_batch:preview_import": "只读预览,不写库,不改任何状态",
    "workbench_batch:commit_import": "建档动作本身(批量导入落库)",
    "products:update_product": "改的是商品自身字段(受众、品类),不是流程前进",
    # ---- 只读预览。它们是 POST 只因为入参放不进 query string ----
    "generation_plans:preview_activation": "只读预览(§7.5),算代价但不改任何状态",
    "prompts:preview_prompt": "只读预览,渲染一次提示词但不落库",
    "providers:test_provider": "连通性自检,不碰商品",
    "reviews:test_candidate_evaluation": "评分诊断调用,不覆盖正式分数或候选图状态",
    "workbench:test_copy_generation": "文案诊断调用,不保存正式文案或推进流程",
    # ---- 生成任务:它的前置在方案与素材上,由 generation 域自己判 ----
    "generation_tasks:create_task": "出图任务的前置由 generation 域判(方案 + 素材)",
    # ---- 平台侧回流:事实录入,不是流程前进 ----
    "workbench:set_platform_status": "录入平台侧事实,不推进本地流程",
    "workbench:record_rejection": "录入一条平台驳回,不推进流程",
    "workbench:mark_resubmitted": "标记已重新提交,不推进本地流程",
    "publish:submit": "发布提交有自己的 READY 门禁(§17),比这道闸严",
    "publish:poll": "查平台侧状态,录入事实不推进本地流程",
    "publish:delist": "下架是往回走,不是前进动作",
    "publish:reconcile": "对账:比对平台侧事实后写回,不推进本地流程",
    "publish:redeliver": "重投是纠错(上一次投递失败)",
    # ---- 文案人工编辑:它是"改",不是"往前走一步" ----
    "workbench:save_copy": "人工编辑落新版本;它不推进步骤,批准才推进",
    # ---- 审核队列:审核对象的前置由它自己的对象判定 ----
    "reviews:approve_review": "审核队列的判定在 reviews 域",
    "reviews:reject_review": "审核退回是往回走",
    "reviews:regenerate_review": "重生成是纠错,前置在 reviews 域",
    # ---- 批次:批次有自己的跳过判定与回执表(见模块文档) ----
    "workbench_batch:create_batch": "批次执行有自己的跳过判定与回执表",
    "workbench_batch:plan_batch": "只读试算,不建批次",
    "workbench_batch:retry_batch": "重试是纠错,前置是上一次失败",
    "workbench_batch:generate_batch_file": "批次产物,不推进单件流程",
    "workbench_batch:regenerate_batch_file": "批次产物重出,不推进单件流程",
    # ---- 配置面:不属于任何商品的流程 ----
    "model_templates:create_template": "配置面,不属于任何商品",
    "model_templates:update_template": "配置面,不属于任何商品",
    "model_templates:enable_template": "配置面,不属于任何商品",
    "model_templates:disable_template": "配置面,不属于任何商品",
    "prompts:save_prompt": "配置面,不属于任何商品",
    "prompts:activate_prompt": "配置面,不属于任何商品",
    "prompts:reset_prompt": "配置面,不属于任何商品",
    "settings:update_settings": "配置面,不属于任何商品",
    "settings:reset_settings": "配置面,恢复默认值,不属于任何商品",
    # ---- 认证面:它在商品流程之外,而且闸的前提本身依赖它 ----
    #
    # 这两条和上面所有豁免不是一类。别的豁免是"这个动作不推进流程";
    # 这两条更强:**闸根本不可能拦它们**。`ensure_*` 要先有 `product_id` 才
    # 判得出前置,而登录发生在任何商品被选中之前 —— 拿不到判定对象。
    #
    # 反过来说更清楚:一道拦得住登录的闸,会把人锁在"没登录所以过不了闸,
    # 过不了闸所以登不上"的死循环里。
    "auth:login": "登录在商品流程之外,而且闸的前置判定本身要求已登录",
    "auth:logout": "退出登录在商品流程之外,更不是前进动作",
}

#: 没有对应写端点的动作码 -> 为什么。**穷举 `NextActionCode` 的另一半。**
#:
#: 一个动作码在这里,意思是"向导会推荐它,但执行它的不是一次 HTTP 写调用"
#: (多半是跳到某一页去做别的事),或者"它不是一个可执行动作"。
ACTIONS_WITHOUT_ENDPOINT: Mapping[NextActionCode, str] = {
    NextActionCode.COMPLETE_SETUP: "补全归属在 SPU 页做(products:update_product),它是建档不是前进",
    NextActionCode.UPLOAD_MATERIAL: "素材步自阻断,见 UNGATED_ENDPOINTS 的同一组",
    NextActionCode.RELEASE_QUARANTINE: "同上",
    NextActionCode.CONFIRM_ASSET_ROLE: "同上",
    NextActionCode.CONFIRM_AUDIENCE: "受众确认走 products:update_product(改的是商品字段)",
    NextActionCode.RESOLVE_CONFLICT: "解决冲突走 attributes:confirm_attributes(已过闸)",
    NextActionCode.FILL_ATTRIBUTES: "人工填写走 attributes:confirm_attributes(已过闸)",
    NextActionCode.FIX_IMAGE_SET: "修图片集走 image_sets:derive(已过闸)",
    NextActionCode.FIX_COPY: "修文案走 workbench:save_copy,它是改不是前进",
    NextActionCode.FIX_DRAFT: "修草稿字段走 workbench:build_draft(已过闸,手填字段一并提交)",
    NextActionCode.REFRESH_DRAFT: "重建草稿走 workbench:build_draft(已过闸)",
    NextActionCode.RESOLVE_REJECTION: "驳回回流是纠错,见 UNGATED_ENDPOINTS",
    NextActionCode.DONE: "「已完成」不是一个可执行动作(`check_action` 一律拒绝)",
}


def _refuse(action: NextActionCode, reason: str) -> None:
    raise ValidationError(
        f"「{flow_rules.ACTION_LABELS[action]}」现在还不能做:{reason}",
        code=ErrorCode.INPUT_INVALID,
        http_status=409,
    )


def ensure_allowed(
    session: Session, product: Product, action: NextActionCode
) -> None:
    """一行 SKU 上的前进动作能不能做。**判定来自 `wizard.check_action`。**

    比各处原有的前置检查**严**,这是一次口径收紧:`generate_copy` 原来只要求
    "有任意一个已确认属性",现在要求属性步整体 DONE —— 与向导上那个按钮
    亮不亮的判据完全一致。收紧的理由是 AC-05 的原话:服务端要按同一份判定
    拒绝,而不是仅前端置灰。
    """
    verdict = wizard_rules.check_action(
        wb.collect(session, product, dry_run=True).result, action
    )
    if verdict.allowed:
        return
    _refuse(action, verdict.reason)


def _products_of_spu(
    session: Session, *, spu: str | None = None, spu_id: UUID | None = None
) -> Sequence[Product]:
    """这个 SPU 下的全部 SKU 行,按 `sku` 排序。

    排序是刻意的:拒绝时要引用其中一行的理由,而"引用哪一行"必须稳定 ——
    随查询顺序变的话,同一个请求两次会得到两句不同的 409。
    """
    stmt = select(Product)
    if spu_id is not None:
        stmt = stmt.where(Product.spu_id == spu_id)
    elif spu is not None:
        stmt = stmt.where(Product.spu == spu)
    else:  # pragma: no cover - 调用方两个都不给是编程错误
        raise ValueError("spu 与 spu_id 至少给一个")
    return sorted(session.scalars(stmt), key=lambda p: p.sku or "")


def ensure_allowed_for_spu(
    session: Session,
    action: NextActionCode,
    *,
    spu: str | None = None,
    spu_id: UUID | None = None,
) -> None:
    """SPU 级前进动作能不能做。**任一行可做即放行**(理由见模块文档)。

    一行都没有时**放行**,不是拒绝:那说明这个 SPU 还没有 SKU,而
    "图片集建在一个没有 SKU 的 SPU 上"是建档顺序问题,由建档那一步管。
    在这里拒绝会给出一句与原因无关的话(「等属性确认」),而真正的问题
    是没有 SKU —— **说错话比不说话更难查**(与 `CONFIRM_ASSET_ROLE`
    那条注释同一条)。
    """
    rows = _products_of_spu(session, spu=spu, spu_id=spu_id)
    if not rows:
        return
    first_reason = ""
    for product in rows:
        verdict = wizard_rules.check_action(
            wb.collect(session, product, dry_run=True).result, action
        )
        if verdict.allowed:
            return
        first_reason = first_reason or verdict.reason
    _refuse(action, first_reason or "上游还没就绪")
