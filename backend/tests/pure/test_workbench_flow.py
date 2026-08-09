"""运营流程判定(阶段 1)。

任务书 §2.3 的验收脚本被逐条翻译成断言 —— 那五个步骤本来就是可执行的:

    1 五件不同阶段的商品 -> 列表能看出五种卡点
    2 缺素材的商品       -> 唯一下一步是「补充素材」
    3 属性冲突的商品     -> 顶部显示待确认;下一步是「处理属性冲突」
    4 筛「草稿校验失败」 -> 只显示对应商品
    5 刷新详情页         -> 状态不丢(这条在前端,后端这里守住「同一输入同一结论」)

外加 §2.4 那条最容易被违反的退出条件:**每个商品只能有一个推荐下一步**,
而且不能是非法的。
"""
from __future__ import annotations

from dataclasses import replace

from app.workbench import flow
from app.workbench.flow import (
    AttributeFacts,
    AudienceFacts,
    CopyFacts,
    DraftFacts,
    FlowStep,
    ImageSetFacts,
    IssueLevel,
    MaterialFacts,
    NextActionCode,
    PlanFacts,
    ProductFlow,
    SetupFacts,
    StepState,
)
from tests.pure._helpers import BACKEND_ROOT  # noqa: F401  (装 sys.path)

REQUIRED = ("primary_color", "garment_type")


def _flow(**kwargs) -> ProductFlow:
    base = dict(
        product_id="p1",
        sku="SW-001-BLK-S",
        spu="SW-001",
        # 受众默认**已确认**(PRD v2)。这一层的用例问的是"素材缺一张时
        # 下一步是什么"、"冲突字段算不算两次"—— 让它们各自只验证一件事。
        # 受众未确认会抢先成为唯一下一步(§8.2:它排在待选模特之前),
        # 那条路径由 test_a45_batch10_fixes.py 专门穷举,不在这里顺带测。
        audience=AudienceFacts(audience="WOMEN"),
        # 建档与方案默认**已完成**(阶段 6 七步增维,batch27)。理由与受众
        # 那一条相同:这一层的用例问的是"素材缺一张时下一步是什么",
        # 而建档未核对会抢先成为唯一下一步(它排在最前),
        # 于是每一条用例都会答"去补全建档" —— 那不是它们要验的东西。
        #
        # **默认值只在测试这一侧给。** 生产侧的 `SetupFacts()` 空视图判
        # NEEDS_CONFIRM,与受众的空视图同一个处理:存量数据的未知要人来
        # 解决,而不是系统替他猜。两条各自专门的路径由
        # `test_a45_batch27_seven_steps.py` 穷举。
        setup=SetupFacts(has_spu_ref=True, has_colour_ref=True),
        plan=PlanFacts(has_active_plan=True, plan_is_colour_level=True, has_model=True),
        # `usable_roles` 与 `gate_roles` **不是一回事**,两份都要填(§6.2):
        #   usable_roles  素材库里有哪些角色的图 —— 提醒类问题读它
        #   gate_roles    门禁认哪些角色 —— 要求那张图是本商品的证据、
        #                 且角色是人工确认的
        # 新字段刻意**没有**宽松默认值:漏填的表现是当场变红,而不是悄悄放行。
        material=MaterialFacts(
            total=3,
            usable=3,
            usable_roles=frozenset({"PRODUCT_FRONT", "PRODUCT_BACK", "DETAIL"}),
            gate_roles=frozenset({"PRODUCT_FRONT", "PRODUCT_BACK", "DETAIL"}),
        ),
        attribute=AttributeFacts(
            required_fields=REQUIRED,
            confirmed=frozenset(REQUIRED),
            extraction_count=1,
        ),
        image_set=ImageSetFacts(exists=True, status="APPROVED", version=1, item_count=3),
        copy=CopyFacts(exists=True, status="APPROVED", version=1, locale="en"),
        draft=DraftFacts(exists=True, status="VALIDATED"),
    )
    base.update(kwargs)
    return ProductFlow(**base)


# ---------------------------------------------------------------- 素材


def test_missing_front_image_blocks_and_asks_for_material():
    """验收脚本第 2 步。"""
    result = flow.evaluate(
        _flow(
            material=MaterialFacts(
                total=1,
                usable=1,
                usable_roles=frozenset({"DETAIL"}),
                gate_roles=frozenset({"DETAIL"}),
            )
        )
    )
    assert result.step_state(FlowStep.MATERIAL) is StepState.BLOCKED
    assert result.next_action.code is NextActionCode.UPLOAD_MATERIAL
    assert "正面图" in result.next_action.reason
    assert result.blocking_count >= 1


def test_missing_back_image_is_only_a_reminder():
    """背面图缺失不阻断 —— 做成阻断会逼运营传一张凑数的图。"""
    result = flow.evaluate(
        _flow(
            material=MaterialFacts(
                total=1,
                usable=1,
                usable_roles=frozenset({"PRODUCT_FRONT"}),
                gate_roles=frozenset({"PRODUCT_FRONT"}),
            )
        )
    )
    assert result.step_state(FlowStep.MATERIAL) is StepState.DONE
    assert result.blocking_count == 0
    codes = {i.code for i in result.issues if i.level is IssueLevel.REMINDER}
    assert "MATERIAL_MISSING_RECOMMENDED_ROLE" in codes


def test_quarantined_material_needs_a_human_not_a_block():
    result = flow.evaluate(
        _flow(
            material=MaterialFacts(
                total=3,
                usable=2,
                quarantined=1,
                usable_roles=frozenset({"PRODUCT_FRONT"}),
                gate_roles=frozenset({"PRODUCT_FRONT"}),
            )
        )
    )
    assert result.step_state(FlowStep.MATERIAL) is StepState.NEEDS_CONFIRM
    assert result.next_action.code is NextActionCode.RELEASE_QUARANTINE
    assert result.pending_count >= 1


# ---------------------------------------------------------------- 属性


def test_attribute_conflict_shows_pending_and_routes_to_conflict():
    """验收脚本第 3 步:顶部显示待确认;下一步为「处理属性冲突」。"""
    result = flow.evaluate(
        _flow(
            attribute=AttributeFacts(
                required_fields=REQUIRED,
                confirmed=frozenset({"garment_type"}),
                conflicted=frozenset({"primary_color"}),
                extraction_count=1,
            )
        )
    )
    assert result.step_state(FlowStep.ATTRIBUTE) is StepState.NEEDS_CONFIRM
    assert result.next_action.code is NextActionCode.RESOLVE_CONFLICT
    assert result.pending_count >= 1
    assert "primary_color" in result.next_action.reason


def test_conflict_is_never_counted_twice():
    """冲突字段同时也是未确认字段。计两次的话阻断数会虚高。"""
    result = flow.evaluate(
        _flow(
            attribute=AttributeFacts(
                required_fields=REQUIRED,
                confirmed=frozenset(),
                conflicted=frozenset({"primary_color", "garment_type"}),
                extraction_count=1,
            )
        )
    )
    attr_issues = [i for i in result.issues if i.target_step is FlowStep.ATTRIBUTE]
    assert len(attr_issues) == 2
    assert {i.code for i in attr_issues} == {"ATTR_CONFLICT"}


def test_never_extracted_asks_to_run_extraction_first():
    result = flow.evaluate(
        _flow(
            attribute=AttributeFacts(
                required_fields=REQUIRED, confirmed=frozenset(), extraction_count=0
            )
        )
    )
    assert result.step_state(FlowStep.ATTRIBUTE) is StepState.TODO
    assert result.next_action.code is NextActionCode.RUN_EXTRACTION


def test_suggested_but_unconfirmed_is_pending_not_blocking():
    result = flow.evaluate(
        _flow(
            attribute=AttributeFacts(
                required_fields=REQUIRED,
                confirmed=frozenset({"garment_type"}),
                suggested=frozenset({"primary_color"}),
                extraction_count=1,
            )
        )
    )
    assert result.step_state(FlowStep.ATTRIBUTE) is StepState.NEEDS_CONFIRM
    assert result.blocking_count == 0
    assert result.next_action.code is NextActionCode.CONFIRM_ATTRIBUTES


# ---------------------------------------------------------------- 图片集 / 文案 / 草稿


def test_image_set_violation_blocks_approval():
    result = flow.evaluate(
        _flow(
            image_set=ImageSetFacts(
                exists=True,
                status="DRAFT",
                version=1,
                item_count=3,
                violation_codes=("MISSING_PRIMARY_FOR_VARIANT",),
            )
        )
    )
    assert result.next_action.code is NextActionCode.FIX_IMAGE_SET
    assert result.blocking_count >= 1
    assert "主图" in result.next_action.reason


def test_clean_image_set_awaits_approval():
    result = flow.evaluate(
        _flow(image_set=ImageSetFacts(exists=True, status="DRAFT", version=1, item_count=3))
    )
    assert result.next_action.code is NextActionCode.APPROVE_IMAGE_SET


def test_copy_hard_failure_blocks_approval():
    result = flow.evaluate(
        _flow(copy=CopyFacts(exists=True, status="REJECTED", version=1, blocking_violations=2))
    )
    assert result.next_action.code is NextActionCode.FIX_COPY
    assert result.blocking_count >= 1


def test_copy_warning_alone_does_not_block():
    result = flow.evaluate(
        _flow(copy=CopyFacts(exists=True, status="VALIDATED", version=1, warning_violations=3))
    )
    assert result.blocking_count == 0
    assert result.next_action.code is NextActionCode.APPROVE_COPY


def test_stale_copy_is_its_own_state():
    """过期不是「失败」。运营要做的事是重新生成,不是去找哪里写错了。"""
    result = flow.evaluate(
        _flow(copy=CopyFacts(exists=True, status="APPROVED", version=1, stale=True))
    )
    assert result.step_state(FlowStep.COPY) is StepState.STALE
    assert result.next_action.code is NextActionCode.GENERATE_COPY


def test_draft_field_error_blocks_export():
    """验收脚本第 10 步:必填字段缺失 -> 不能正式导出。"""
    result = flow.evaluate(
        _flow(draft=DraftFacts(exists=True, status="INVALID", error_count=1))
    )
    assert result.next_action.code is NextActionCode.FIX_DRAFT
    assert result.blocking_count >= 1


def test_stale_draft_blocks_export_even_when_validated():
    """§9.1:STALE 草稿不允许导出,哪怕它上一次校验是通过的。"""
    result = flow.evaluate(
        _flow(draft=DraftFacts(exists=True, status="STALE", stale=True))
    )
    assert result.step_state(FlowStep.DRAFT) is StepState.STALE
    assert result.next_action.code is NextActionCode.REFRESH_DRAFT
    assert result.blocking_count >= 1


def test_validated_draft_offers_export():
    result = flow.evaluate(_flow())
    assert result.next_action.code is NextActionCode.EXPORT


def test_exported_product_has_no_next_step():
    result = flow.evaluate(
        _flow(draft=DraftFacts(exists=True, status="APPROVED", exported=True))
    )
    assert result.next_action.code is NextActionCode.DONE
    assert result.completion == 100


def test_open_platform_rejection_blocks_done():
    """P1:有未解决驳回时商品不能算完成,而且阻断数要计上去。"""
    result = flow.evaluate(
        _flow(draft=DraftFacts(exists=True, status="APPROVED", exported=True, open_rejections=1))
    )
    assert result.next_action.code is NextActionCode.RESOLVE_REJECTION
    assert result.blocking_count >= 1
    # 被驳回的商品完成度不能是 100%
    assert result.completion < 100


def test_resolve_rejection_action_names_the_count():
    result = flow.evaluate(
        _flow(draft=DraftFacts(exists=True, status="APPROVED", exported=True, open_rejections=2))
    )
    assert result.next_action.code is NextActionCode.RESOLVE_REJECTION
    assert "2" in result.next_action.reason


def test_platform_rejection_issue_appears_in_blocking_issues():
    result = flow.evaluate(
        _flow(draft=DraftFacts(exists=True, status="APPROVED", exported=True, open_rejections=1))
    )
    blocking_codes = {i.code for i in result.issues if i.level is IssueLevel.BLOCKING}
    assert "PLATFORM_REJECTED" in blocking_codes


def test_resolve_rejection_comes_after_stale_and_field_errors():
    """驳回修复路径本身会让草稿过期,所以优先级低于过期和字段错误。"""
    stale_with_rejection = _flow(
        draft=DraftFacts(exists=True, status="STALE", exported=True, stale=True, open_rejections=1)
    )
    assert flow.evaluate(stale_with_rejection).next_action.code is NextActionCode.REFRESH_DRAFT

    invalid_with_rejection = _flow(
        draft=DraftFacts(
            exists=True, status="INVALID", exported=True, error_count=2, open_rejections=1
        )
    )
    assert flow.evaluate(invalid_with_rejection).next_action.code is NextActionCode.FIX_DRAFT


def test_zero_open_rejections_does_not_block():
    """驳回记录全解决后商品恢复正常流程。"""
    result = flow.evaluate(
        _flow(draft=DraftFacts(exists=True, status="APPROVED", exported=True, open_rejections=0))
    )
    assert result.next_action.code is NextActionCode.DONE
    assert result.blocking_count == 0


def test_platform_status_appears_in_draft_summary():
    """平台状态进草稿列摘要,运营不用点开详情就能看到。"""
    result = flow.evaluate(
        _flow(draft=DraftFacts(exists=True, status="VALIDATED", exported=True,
                               platform_status="SUBMITTED"))
    )
    draft_step = next(s for s in result.steps if s.step is FlowStep.DRAFT)
    assert "SUBMITTED" in draft_step.summary or "平台" in draft_step.summary


# ---------------------------------------------------------------- 唯一下一步


def test_upstream_blocks_downstream_so_the_next_step_is_never_illegal():
    """§2.4:不出现非法下一步。

    素材缺、属性没确认、图片集没编排、文案没生成 —— 全都缺的时候,
    推荐动作必须是**最上游**那个,不能是「生成文案」。
    """
    result = flow.evaluate(
        _flow(
            material=MaterialFacts(total=0, usable=0),
            attribute=AttributeFacts(required_fields=REQUIRED, extraction_count=0),
            image_set=ImageSetFacts(),
            copy=CopyFacts(),
            draft=DraftFacts(),
        )
    )
    assert result.next_action.code is NextActionCode.UPLOAD_MATERIAL
    assert result.step_state(FlowStep.ATTRIBUTE) is StepState.BLOCKED
    assert result.step_state(FlowStep.IMAGE_SET) is StepState.BLOCKED
    assert result.step_state(FlowStep.COPY) is StepState.BLOCKED
    assert result.step_state(FlowStep.DRAFT) is StepState.BLOCKED


def test_every_action_code_maps_to_a_step_and_a_label():
    """加了动作码却忘了配文案/标签页,界面上会出现一个点不动的按钮。"""
    for code in NextActionCode:
        assert code in flow.ACTION_STEP, f"{code} 没有对应步骤"
        assert code in flow.ACTION_LABELS, f"{code} 没有中文标签"
        assert flow.ACTION_LABELS[code].strip()


def test_evaluation_is_deterministic():
    """验收脚本第 5 步的后端一半:同一输入必须给出同一结论。

    刷新后状态漂移的经典成因是判定里掺了时间或随机数。
    """
    sample = _flow(
        attribute=AttributeFacts(
            required_fields=REQUIRED,
            confirmed=frozenset({"garment_type"}),
            conflicted=frozenset({"primary_color"}),
            extraction_count=2,
        )
    )
    first = flow.evaluate(sample)
    second = flow.evaluate(sample)
    assert first == second


def test_completion_is_monotonic_along_the_pipeline():
    """越往后完成度越高。倒挂的话进度条会在运营干完活之后往回走。"""
    stages = [
        _flow(
            material=MaterialFacts(total=0, usable=0),
            attribute=AttributeFacts(required_fields=REQUIRED),
            image_set=ImageSetFacts(),
            copy=CopyFacts(),
            draft=DraftFacts(),
        ),
        _flow(
            attribute=AttributeFacts(required_fields=REQUIRED, extraction_count=1),
            image_set=ImageSetFacts(),
            copy=CopyFacts(),
            draft=DraftFacts(),
        ),
        _flow(image_set=ImageSetFacts(exists=True, status="DRAFT", version=1, item_count=3),
              copy=CopyFacts(), draft=DraftFacts()),
        _flow(copy=CopyFacts(exists=True, status="VALIDATED", version=1), draft=DraftFacts()),
        _flow(draft=DraftFacts()),
        _flow(),
        _flow(draft=DraftFacts(exists=True, status="APPROVED", exported=True)),
    ]
    values = [flow.evaluate(s).completion for s in stages]
    assert values == sorted(values), values
    # 起点从 0 变成 5(batch27 的口径迁移):建档是第一步,而这批夹具的
    # 归属是挂好的 —— 归属挂好本身就是一件**已经做完的事**,给 0 分等于
    # 说它没做。这不是把分数调松:归属没挂好的商品(`SetupFacts()` 空视图)
    # 仍然拿不到这 5 分,那条路径由 batch27 的守卫穷举。
    assert values[0] == 5
    assert values[-1] == 100



# ---------------------------------------------------------------- 筛选


def _five_products() -> list[tuple[str, object]]:
    """验收脚本第 1 步:五件卡在不同地方的商品。"""
    return [
        ("缺素材", _flow(material=MaterialFacts(total=0, usable=0))),
        (
            "属性冲突",
            _flow(
                attribute=AttributeFacts(
                    required_fields=REQUIRED,
                    confirmed=frozenset({"garment_type"}),
                    conflicted=frozenset({"primary_color"}),
                    extraction_count=1,
                )
            ),
        ),
        (
            "图片集未批准",
            _flow(image_set=ImageSetFacts(exists=True, status="DRAFT", version=1, item_count=3)),
        ),
        (
            "文案未通过",
            _flow(
                copy=CopyFacts(exists=True, status="REJECTED", version=1, blocking_violations=1)
            ),
        ),
        ("草稿校验失败", _flow(draft=DraftFacts(exists=True, status="INVALID", error_count=2))),
    ]


def test_each_step_can_be_the_unique_next_step():

    """验收脚本第 1 步:五件商品要能一眼看出五种不同卡点。"""
    results = [flow.evaluate(f) for _, f in _five_products()]
    codes = [r.next_action.code for r in results]
    assert len(set(codes)) == 5, codes
    steps = [r.current_step for r in results]
    assert len(set(steps)) == 5, steps


def test_filter_by_draft_failure_matches_only_that_product():
    """验收脚本第 4 步。"""
    results = [(name, flow.evaluate(f)) for name, f in _five_products()]
    matched = [
        name
        for name, r in results
        if flow.matches_filters(r, step=FlowStep.DRAFT.value, state=StepState.IN_PROGRESS.value)
    ]
    assert matched == ["草稿校验失败"]


def test_clearing_the_filter_restores_everything():
    results = [flow.evaluate(f) for _, f in _five_products()]
    assert sum(1 for r in results if flow.matches_filters(r)) == 5


def test_only_blocked_filter_excludes_pending_only_products():
    """「只看阻断」不能把待确认也算进来 —— 否则这个筛选没有意义。"""
    conflict = flow.evaluate(
        _flow(
            attribute=AttributeFacts(
                required_fields=REQUIRED,
                confirmed=frozenset({"garment_type"}),
                conflicted=frozenset({"primary_color"}),
                extraction_count=1,
            )
        )
    )
    assert conflict.blocking_count == 0
    assert not flow.matches_filters(conflict, only_blocked=True)


def test_upstream_blocked_step_does_not_inflate_blocking_count():
    """刚建好的商品:没素材、3 个必填属性。

    阻断数必须只数运营**现在能处理**的问题。以前这里是 5(素材 2 条 +
    每个必填属性 1 条),必填属性越多的品类越虚高 —— 数字一虚,真正的
    阻断也会跟着被无视。等上游不是待办,BLOCKED 状态本身就是说明。
    """
    result = flow.evaluate(
        ProductFlow(
            product_id="p-new",
            sku="SW-NEW",
            spu="SW-NEW",
            # 归属显式填齐。"刚建好"指的是没素材、没跑过识别,不是"没挂 SPU"
            # —— 生产侧建档走 `/spus/new`,那条路径出来的行两个外键都在
            # (`service._setup_facts` 直接读列,不会是 `None`)。留空的话
            # 建档步判 NEEDS_CONFIRM 并抢先成为唯一下一步,这条用例就
            # 不再是在验"素材那 2 条阻断就是全部"了。
            setup=SetupFacts(has_spu_ref=True, has_colour_ref=True),
            attribute=AttributeFacts(required_fields=("color", "fabric", "style")),
        )
    )
    assert result.step_state(FlowStep.ATTRIBUTE) is StepState.BLOCKED
    # 属性步不产出任何问题 —— 素材步的 2 条阻断就是全部
    assert all(i.target_step is not FlowStep.ATTRIBUTE for i in result.issues)
    assert result.blocking_count == 2
    assert result.next_action.code is NextActionCode.UPLOAD_MATERIAL


def test_awaiting_export_draft_counts_as_pending():
    """草稿列亮着「待确认」,顶部的待确认计数就必须包含它。

    以前 VALIDATED 草稿的状态是 NEEDS_CONFIRM 却不带任何问题,
    列表页列显示与顶部计数各说各话 —— 正是本模块声明要杜绝的口径分裂。
    """
    result = flow.evaluate(_flow())  # 默认草稿 VALIDATED,等人点导出
    assert result.step_state(FlowStep.DRAFT) is StepState.NEEDS_CONFIRM
    assert result.pending_count >= 1
    codes = {i.code for i in result.issues if i.level is IssueLevel.NEEDS_CONFIRM}
    assert "DRAFT_AWAITING_EXPORT" in codes
    counts = flow.summarize([result])
    assert counts["needs_confirm"] == 1
    assert counts["exportable"] == 1  # 两个口径都对:它既待确认,也可导出


def test_summary_counts_line_up_with_individual_results():
    results = [flow.evaluate(f) for _, f in _five_products()]
    counts = flow.summarize(results)
    assert counts["total"] == 5
    assert counts["blocked"] == sum(1 for r in results if r.blocking_count)
    assert counts["done"] == 0


def test_step_states_covers_every_step():
    """列表页一行一列直接用它,少一个键就会渲染出一个空单元格。

    列数在 batch27 从五变成七(建档 / 方案两步)。这条断言写成
    「与 `STEP_ORDER` 等长」而不是写死数字,是因为写死的那一版在
    batch26 实测里正是**加了两步而不红**的那一类。
    """

    states = flow.step_states(flow.evaluate(_flow()))
    assert set(states) == {s.value for s in flow.STEP_ORDER}
    assert len(flow.STEP_ORDER) == 7


def test_state_progress_matches_the_taskbook_scoring_table():
    """§3.2 计分口径:终态 100、待确认 60、进行中 30、未开始/失败/冲突/过期 0。

    过期给 0 分不是苛刻,是运营事实:过期产出不能用,和没做在结果上
    是一回事;给部分分,草稿过期的商品会显示 90%+ 并沉到列表底部,
    而它此刻恰恰导不出去。数值曾经漂移过(STALE=0.6),所以钉死。
    """
    from app.workbench.flow import STATE_PROGRESS, STEP_WEIGHTS, StepState

    assert STATE_PROGRESS == {
        StepState.BLOCKED: 0.0,
        StepState.TODO: 0.0,
        StepState.IN_PROGRESS: 0.3,
        StepState.NEEDS_CONFIRM: 0.6,
        StepState.STALE: 0.0,
        StepState.DONE: 1.0,
    }
    # 七步不等权(batch27 的口径迁移)。分配理由写在 `STEP_WEIGHTS` 上,
    # 这里只钉两件事:每一步都有分,合计正好 100
    assert set(STEP_WEIGHTS) == set(flow.STEP_ORDER)
    # 五步全 DONE 恰好 100,不多不少 —— 计分表怎么改都不许溢出
    assert sum(STEP_WEIGHTS.values()) * STATE_PROGRESS[StepState.DONE] == 100.0



# ---------------------------------------------------------------- 快审退回(A10)

def test_reject_reason_tables_cover_the_whole_enum():
    """两张表(中文、下一步)必须与 `RejectReason` 枚举严格一致。

    表在 flow.py、枚举在 reject.py,因为反向 import 会成环(理由写在两处注释里)。
    分开放的代价就是这条测试:加一个退回原因、漏了改表,后果是运营选了那个原因、
    界面显示一串英文常量,而下一步落到兜底动作 —— 两件事都不会报错。
    """
    from app.workbench import flow
    from app.workbench.reject import REJECT_REASON_LABELS, RejectReason

    codes = {r.value for r in RejectReason}
    assert set(flow.REJECT_REASON_MESSAGES) == codes
    assert {r.value for r in REJECT_REASON_LABELS} == codes
    # 「其他」不在按原因索引的表里:它的下一步由**被退回的对象**决定,
    # 一张按原因索引的表回答不了这个问题(见 flow.REJECT_STEP_FALLBACK)。
    # 其余八个必须都在,漏一个的表现是那个原因静默落到兜底动作
    assert set(flow.REJECT_REASON_ACTIONS) == codes - {"OTHER"}
    # 下一步必须是真实的动作码,不能是拼错的字符串
    for action in flow.REJECT_REASON_ACTIONS.values():
        assert isinstance(action, flow.NextActionCode)
    for action in flow.REJECT_STEP_FALLBACK.values():
        assert isinstance(action, flow.NextActionCode)
    # 每个原因、每个对象都必须能算出下一步 —— 这才是"不用自己猜"的判据
    for reason in RejectReason:
        for target in ("image_set", "copy"):
            assert isinstance(
                flow.reject_next_action(target, reason.value), flow.NextActionCode
            )


def test_the_chinese_for_a_reason_has_exactly_one_source():
    """两个模块各有一张退回原因的中文表,值必须逐条相同。

    上一版只校验了两张表**各自**覆盖整个枚举,没有校验两边的字是同一句 ——
    于是改一处漏一处不会有任何报错,而界面(取 reject.py)和判定结论
    (取 flow.py)会各说各的。现在 `REJECT_REASON_LABELS` 直接从
    `REJECT_REASON_MESSAGES` 推导,这条测试盯住那个推导没有被人改回手抄。
    """
    from app.workbench.reject import REJECT_REASON_LABELS, RejectReason

    for reason in RejectReason:
        assert REJECT_REASON_LABELS[reason] == flow.REJECT_REASON_MESSAGES[reason.value]


def test_other_sends_you_back_to_the_step_you_rejected():
    """「其他」的下一步由被退回的对象决定,不由原因决定。

    原先它在表里硬写成 `FIX_IMAGE_SET`,于是用「其他」退回一版**文案**
    会把运营送去重排图片 —— 而问题在文字上。系统选择不猜他想干什么,
    那就该把他送回出问题的那一步,而不是猜错一步。
    """
    assert flow.reject_next_action("copy", "OTHER") is flow.NextActionCode.FIX_COPY
    assert (
        flow.reject_next_action("image_set", "OTHER")
        is flow.NextActionCode.FIX_IMAGE_SET
    )
    # 认不出的原因码走同一条兜底路径,同样按对象分
    assert flow.reject_next_action("copy", "WAT") is flow.NextActionCode.FIX_COPY
    assert flow.reject_next_action("copy", None) is flow.NextActionCode.FIX_COPY


def test_rejected_image_set_does_not_walk_back_into_the_review_queue():
    """A10 的核心回归:退回过的图片集不能又变成"等待批准"。

    退回往往正是因为"校验全过但人眼看着不对" —— 那时 `violation_codes` 是空的。
    如果判定层看不见退回状态,下一步会落回 `APPROVE_IMAGE_SET`,
    于是运营刚退回的那一件立刻回到快审队列、排在原来的位置。
    退回这个动作就等于不存在。
    """
    # _flow() 默认图片集已批准,先退回到"待批准"作为干净基线
    facts = replace(
        _flow(), image_set=replace(_flow().image_set, status="PENDING_REVIEW")
    )
    clean = flow.evaluate(facts)
    assert clean.next_action.code is flow.NextActionCode.APPROVE_IMAGE_SET

    rejected = replace(
        facts,
        image_set=replace(
            facts.image_set, status="REJECTED", reject_reason="COLOR_MISMATCH"
        ),
    )
    result = flow.evaluate(rejected)
    assert result.next_action.code is flow.NextActionCode.FIX_IMAGE_SET
    assert "颜色不一致" in result.next_action.reason
    # 退回要在问题清单里留下阻断项,否则列表页的阻断数不会变
    codes = {i.code for i in result.issues if i.level is IssueLevel.BLOCKING}
    assert "IMAGESET_REJECTED" in codes


def test_a_copy_rejected_by_the_rules_is_not_called_a_quick_review_reject():
    """文案的 `REJECTED` 有两个来源,判据是**有没有退回原因**,不是状态本身。

    `save_copy` / `revalidate` 在有硬失败时也置 REJECTED。只看状态的话,
    一版因禁词被驳回的文案会被说成「文案被快审退回:未注明原因」——
    一句完全错误的话,而且同一屏上方还挂着"N 处硬失败"那条,两句互相矛盾。
    运营会先信哪一句不好说,但两句里必有一句会把他带错地方。
    """
    rules_rejected = _flow(
        copy=CopyFacts(exists=True, status="REJECTED", version=1, blocking_violations=2)
    )
    result = flow.evaluate(rules_rejected)
    assert result.next_action.code is NextActionCode.FIX_COPY
    assert "快审退回" not in result.next_action.reason
    assert "硬失败" in result.next_action.reason
    codes = {i.code for i in result.issues}
    assert "COPY_REJECTED" not in codes
    assert "COPY_BLOCKING_VIOLATION" in codes

    # 同一个状态 + 退回原因 = 真的被人退回过,这时才说"快审退回"
    reviewed = _flow(
        copy=CopyFacts(
            exists=True,
            status="REJECTED",
            version=1,
            reject_reason="UNNATURAL_PT",
        )
    )
    result = flow.evaluate(reviewed)
    assert result.next_action.code is NextActionCode.FIX_COPY
    assert "快审退回:葡语不自然" in result.next_action.reason
    assert "COPY_REJECTED" in {i.code for i in result.issues}


def test_a_rejected_copy_does_not_walk_back_into_the_review_queue():
    """A10 的核心回归,文案侧:退回过的文案不能又变成"等待批准"。

    图片集那条已经有测试(见上),文案这条更容易漏 —— 因为文案退回时
    `blocking_violations` 往往是 0(校验全过、人眼看着不对),
    判定层看不见退回原因的话下一步就是 `APPROVE_COPY`,
    那件商品立刻回到快审队列排在原处。
    """
    clean = flow.evaluate(_flow(copy=CopyFacts(exists=True, status="VALIDATED", version=1)))
    assert clean.next_action.code is NextActionCode.APPROVE_COPY

    rejected = flow.evaluate(
        _flow(
            copy=CopyFacts(
                exists=True,
                status="REJECTED",
                version=1,
                reject_reason="COPY_FACT_ERROR",
            )
        )
    )
    assert rejected.next_action.code is NextActionCode.FIX_COPY
    assert rejected.blocking_count >= 1


def test_reject_reason_decides_the_next_step_not_the_object_type():
    """「商品结构改变」退回图片集,下一步是改**属性**,不是重排图片。

    A10 要的是"退回后系统给出明确下一步"。结构变了意味着属性记错了 ——
    直接重新生成会拿着同一份错属性再生成一次,运营会以为系统在空转。
    """
    facts = _flow()
    result = flow.evaluate(
        replace(
            facts,
            image_set=replace(
                facts.image_set, status="REJECTED", reject_reason="STRUCTURE_CHANGED"
            ),
        )
    )
    assert result.next_action.code is flow.NextActionCode.CONFIRM_ATTRIBUTES

    # 缺素材要落到素材页:缺的是输入,重新生成变不出新素材
    result = flow.evaluate(
        replace(
            facts,
            image_set=replace(
                facts.image_set, status="REJECTED", reject_reason="NEEDS_MATERIAL"
            ),
        )
    )
    assert result.next_action.code is flow.NextActionCode.UPLOAD_MATERIAL


def test_unknown_reject_reason_falls_back_but_stays_visible():
    """认不出的原因码要如实回显,不写"未知"。

    看到一个陌生的码,下一步是去查它是谁写进来的;而"未知"把这条线索抹掉了。
    下一步则落到兜底动作 —— 让人去修,而不是让这件商品卡在无动作可做的状态。
    """
    facts = _flow()
    result = flow.evaluate(
        replace(
            facts,
            image_set=replace(facts.image_set, status="REJECTED", reject_reason="WAT"),
        )
    )
    assert result.next_action.code is flow.NextActionCode.FIX_IMAGE_SET
    assert "WAT" in result.next_action.reason


def test_note_is_required_only_for_the_other_reason():
    """选了「其他」必须写清楚是什么。

    不强制的话它会变成默认选项:九个里挑一个要想,「其他」不用想。
    而一条只有「其他」没有说明的退回记录,对下一个接手的人等于没有信息。
    """
    from app.workbench.reject import RejectReason, note_required

    assert note_required(RejectReason.OTHER)
    for reason in RejectReason:
        if reason is not RejectReason.OTHER:
            assert not note_required(reason)


def test_reject_reasons_are_scoped_to_the_object_being_rejected():
    """用「葡语不自然」退回一个图片集是点错了行,不是一次有意义的退回。

    那条记录事后没人看得懂,而 UAT 结束时要靠这些记录回答"退回集中在哪一类" ——
    污染了统计,就失去了决定"先修提示词还是先补素材"的依据。
    """
    from app.workbench.reject import RejectReason, reasons_for

    assert RejectReason.UNNATURAL_PT not in reasons_for("image_set")
    assert RejectReason.UNNATURAL_PT in reasons_for("copy")
    assert RejectReason.LOW_IMAGE_QUALITY not in reasons_for("copy")
    # 两边都要能选「其他」和「商品结构改变」:结构错了图和文案都会跟着错
    for target in ("image_set", "copy"):
        assert RejectReason.OTHER in reasons_for(target)
        assert RejectReason.STRUCTURE_CHANGED in reasons_for(target)
