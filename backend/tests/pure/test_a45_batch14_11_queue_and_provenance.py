"""A45-batch14-11:§11 的两条新场景 —— 确认队列口径 + AI 图伪装拦截。

## 这一批守的是两条「今天不报错、等外部事实发生才漏」的规则

**一、确认队列。**`decision.decide_status` 未校准时返回 `CANDIDATE`,
而真实抽取器接上的第一天校准分箱是空的 —— 那时**每一个字段都是 CANDIDATE**。
按本批之前的判定,那批商品会集体显示「有建议值待确认」、属性步 60% 完成度,
而队列里一个可点的都没有。改的是代码,触发的是**数据**:
`AttributeStatus` 的文档字符串自己写着两者不能混,而 `flow.py` 一直在混,
`tests/` 里 `candidate_only` 出现次数为零。

**二、溯源冲突。**`ingest()` 的去重键是 `(product_id, sha256)`,而 §11 写的是
**同 SPU**。差出来的那一块正是最常发生的走法:把 A 色生成好的图当样品传给
B 色的 SKU。去重不命中 → 新建一条 `PRODUCT_EVIDENCE` → 直接进识别输入,
每张一次真实付费调用。

## 三处穷举

判定是纯函数、输入空间可枚举,那就没有理由挑样本:

    每一个 `AttributeStatus` 成员    是不是被显式归过档
    每一个 `MediaSource` 成员        算不算「自称原始样品」
    四个溯源信号的**全部 16 种组合** 任一命中即算带溯源

第三条是一个全称命题(「四个信号任一命中」),挑样本证明不了它 ——
而它今天真正会命中的只有 `legacy_kind` 那一路,另外三个都要等阶段 2 的列。
少了穷举,删掉那一路的变异会全绿。
"""
from __future__ import annotations

import ast
import itertools

from app.attributes import queue_policy as qp
from app.core.enums import AttributeStatus, MediaSource, MediaStatus, RoleSource
from app.media import provenance_conflict as pc
from app.media.mapping import LEGACY_CANDIDATE, LEGACY_PRODUCT_ASSET
from app.workbench import batch as b
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
    ProductFlow,
    StepState,
)
from tests.pure._helpers import BACKEND_ROOT  # noqa: F401

APP = BACKEND_ROOT / "app"

#: 八个必填字段。**八个不是随便挑的**:两条确定性守卫(D 组)靠它 ——
#: 集合迭代序碰巧和必填序的前三个逐个相同的概率是 1/(8·7·6)。
#: 挑三个字段的话,乱序有相当大的概率碰巧等于有序,那条守卫就成了摆设。
REQUIRED: tuple[str, ...] = (
    "garment_type",
    "primary_color",
    "pattern",
    "neckline",
    "closure",
    "lining",
    "padding",
    "waist_rise",
)


class _Asset:
    """假素材行。字段名与判定读的那几个对齐,**不 import ORM 模型**。

    默认值是「一张人工上传、没有任何溯源痕迹的照片」—— 也就是唯一
    不该被拦下来的那一种。每条用例只改它关心的那一个字段。
    """

    def __init__(
        self,
        *,
        source: str = MediaSource.MANUAL_UPLOAD.value,
        legacy_kind: str | None = LEGACY_PRODUCT_ASSET,
        generation_task_id: object = None,
        generation_candidate_id: object = None,
        role: str | None = "PRODUCT_FRONT",
        role_source: str = RoleSource.HUMAN.value,
        status: str = MediaStatus.READY.value,
    ) -> None:
        self.source = source
        self.legacy_kind = legacy_kind
        self.generation_task_id = generation_task_id
        self.generation_candidate_id = generation_candidate_id
        self.role = role
        self.role_source = role_source
        self.status = status


def _flow(**kwargs) -> ProductFlow:
    """一件除属性外处处就绪的商品。属性那一层由每条用例自己给。"""
    base = dict(
        product_id="p1",
        sku="SW-001-BLK-S",
        spu="SW-001",
        audience=AudienceFacts(audience="WOMEN"),
        material=MaterialFacts(
            total=1,
            usable=1,
            usable_roles=frozenset({"PRODUCT_FRONT"}),
            gate_roles=frozenset({"PRODUCT_FRONT"}),
        ),
        attribute=AttributeFacts(
            required_fields=REQUIRED, confirmed=frozenset(REQUIRED), extraction_count=1
        ),
        image_set=ImageSetFacts(exists=True, status="APPROVED", version=1, item_count=3),
        copy=CopyFacts(exists=True, status="APPROVED", version=1, locale="en"),
        draft=DraftFacts(exists=True, status="VALIDATED"),
    )
    base.update(kwargs)
    return ProductFlow(**base)


def _attr_issues(result, code: str) -> list:
    return [i for i in result.issues if i.code == code]


# ==========================================================
# Q 组:确认队列口径(PRD §11)
# ==========================================================


def test_every_attribute_status_is_filed_by_hand():
    """**穷举。**每一个 `AttributeStatus` 成员都要在归档表里被点名。

    写成 `!= CANDIDATE` 的排除法今天等价,分歧在将来:新增一个取值时它
    默认进确认队列,而新增取值的那个人不会想起这道口径。
    漏一个成员在这里当场红,而不是等到那个字段涌进运营的待办列表。
    """
    for status in AttributeStatus:
        assert status.value in qp.DISPOSITION_BY_STATUS, (
            f"{status.value} 没有被归档 —— 它会落进兜底,而兜底是一个没人做过的决定"
        )
    assert set(qp.DISPOSITION_BY_STATUS) == {s.value for s in AttributeStatus}, (
        "归档表里有 `AttributeStatus` 之外的键"
    )


def test_every_disposition_has_a_bucket():
    """五个去向都要有落点。少一个的表现是那一档的字段从每个集合里同时消失。"""
    for disposition in qp.Disposition:
        assert disposition in qp._BUCKET_ATTR, f"{disposition} 没有对应的桶"


def test_an_uncalibrated_field_is_not_something_to_confirm():
    """**§11 的本体。**CANDIDATE = 留了证据但不采信,它不进确认队列。

    这一条今天不改变任何一件商品的判定,因为 Mock 抽取器给的置信度够高。
    它要到真实模型接上、而校准分箱是空的那天才生效 —— 那时
    `decide_status` 对每一个字段都返回 CANDIDATE。
    """
    assert qp.disposition(AttributeStatus.CANDIDATE.value) is qp.Disposition.EVIDENCE_ONLY
    assert not qp.enters_confirm_queue(AttributeStatus.CANDIDATE.value)
    assert qp.enters_confirm_queue(AttributeStatus.SUGGESTED.value)
    assert qp.enters_confirm_queue(AttributeStatus.CONFLICT.value)


def test_the_confirm_queue_is_exactly_the_two_dispositions_section_11_names():
    """§11 原文:确认队列只出现 SUGGESTED / CONFLICT。多一档少一档都不行。"""
    assert qp.CONFIRM_QUEUE == frozenset(
        {qp.Disposition.CONFIRMABLE, qp.Disposition.ADJUDICABLE}
    )


def test_dismissed_facts_do_not_come_back_into_the_queue():
    """人工拒绝过的、被新版本取代的,都不再问第二遍。"""
    for status in (AttributeStatus.REJECTED, AttributeStatus.SUPERSEDED):
        assert qp.disposition(status.value) is qp.Disposition.DISMISSED
        assert not qp.enters_confirm_queue(status.value)


def test_an_unrecognisable_status_still_produces_a_todo():
    """兜底方向是「产出一条待办」,不是「静默消失」。

    反过来兜的话,库里一个拼错的状态字符串会让那个字段从确认队列和阻断
    清单里**同时**消失,而商品照样导不出去 —— 运营看到的是一件卡住
    但没有任何理由的商品。
    """
    for weird in ("PENDING_REVIEW", "", None, "candidate"):
        assert qp.disposition(weird) is qp.Disposition.EVIDENCE_ONLY, weird
        assert not qp.enters_confirm_queue(weird)


def test_the_confirm_queue_property_covers_both_kinds_of_decision():
    """`confirm_queue` 要同时含「等点头」和「等裁决」。

    只算前者的表现是冲突字段从队列计数里消失,而它恰恰是唯一必须由人
    裁决的那一类。
    """
    buckets = qp.bucket_fields(
        {
            AttributeStatus.SUGGESTED.value: ["a"],
            AttributeStatus.CONFLICT.value: ["b"],
            AttributeStatus.CANDIDATE.value: ["c"],
        }
    )
    assert buckets.confirm_queue == frozenset({"a", "b"})


def test_bucket_fields_keeps_unbelieved_evidence_visible():
    """不采信的字段要落在 `evidence_only` 桶里,不能被归到「已处理」。

    归错桶的表现:`flow` 拿到的 `candidate_only` 是空的,于是那批字段
    走到「尚无可用值」那一支,提示语说「先跑一次属性识别」—— 而识别
    刚跑完,再跑一次是一笔白花的钱。
    """
    buckets = qp.bucket_fields(
        {
            AttributeStatus.CANDIDATE.value: ["pattern", "lining"],
            AttributeStatus.REJECTED.value: ["padding"],
        }
    )
    assert buckets.evidence_only == frozenset({"pattern", "lining"})
    assert buckets.dismissed == frozenset({"padding"})


def test_bucket_fields_files_every_status_somewhere():
    """**穷举。**每个状态喂一个字段名进去,它必须出现在某一个桶里。"""
    for status in AttributeStatus:
        buckets = qp.bucket_fields({status.value: ["only_field"]})
        seen = (
            buckets.settled
            | buckets.confirmable
            | buckets.adjudicable
            | buckets.evidence_only
            | buckets.dismissed
        )
        assert "only_field" in seen, f"{status.value} 的字段掉出了所有桶"


# ==========================================================
# F 组:属性步的判定(flow)
# ==========================================================


def _all_candidate_flow() -> ProductFlow:
    """识别跑过了,而八个必填字段的结论全是「不采信」。

    这就是真实抽取器接上、校准为空那天每一件商品的样子。
    """
    return _flow(
        attribute=AttributeFacts(
            required_fields=REQUIRED,
            confirmed=frozenset(),
            candidate_only=frozenset(REQUIRED),
            extraction_count=1,
        )
    )


def test_unbelieved_evidence_blocks_instead_of_asking_for_confirmation():
    """有证据但不采信 = 阻断,不是「待确认」。

    降成 NEEDS_CONFIRM 的后果不是多一句提示,是**这件商品能一路走到导出**:
    `_evaluate_attribute` 的 DONE 判定只看必填是否确认,而阻断计数是
    导出闸与看板共用的那个数字。
    """
    result = flow.evaluate(_all_candidate_flow())
    issues = _attr_issues(result, "ATTR_EVIDENCE_ONLY")
    assert len(issues) == len(REQUIRED), "不采信的字段没有逐个报出来"
    assert all(i.level is IssueLevel.BLOCKING for i in issues)
    assert result.blocking_count == len(REQUIRED)


def test_unbelieved_evidence_has_its_own_code_not_the_no_value_one():
    """自成一码。合并的代价是两条动线在看板上分不开。

    「尚无可用值」的下一步是跑识别,「有证据但不采信」的下一步是人工填写 ——
    合成一个码之后,按码分组的看板会把两批商品送去同一个地方,
    而其中一批去了会白花一次付费调用。
    """
    result = flow.evaluate(_all_candidate_flow())
    assert not _attr_issues(result, "ATTR_NOT_CONFIRMED"), (
        "不采信的字段被报成了「尚无可用值」"
    )


def test_the_unbelieved_hint_does_not_send_anyone_to_run_extraction_again():
    """提示语不许说「先跑一次属性识别」。

    这条动线上识别**已经跑过了**,再跑一次得到的是同一批不采信的证据 ——
    而那是一次真实付费调用。提示语把人送去花那笔钱,比不给提示更糟。
    """
    result = flow.evaluate(_all_candidate_flow())
    for issue in _attr_issues(result, "ATTR_EVIDENCE_ONLY"):
        assert "先跑一次属性识别" not in issue.hint, issue.hint
        assert "人工直接填写" in issue.hint


def test_all_candidate_is_in_progress_not_awaiting_confirmation():
    """步骤状态是 IN_PROGRESS,不是 NEEDS_CONFIRM。

    差别在完成度:`STATE_PROGRESS` 给 NEEDS_CONFIRM 的是 0.6、
    给 IN_PROGRESS 的是 0.3。一件模型一个字段都没答上来的商品记 0.6 分,
    会排在真的做了一半的商品后面,而运营点进去一个可确认的值都找不到。
    """
    result = flow.evaluate(_all_candidate_flow())
    assert result.step_state(FlowStep.ATTRIBUTE) is StepState.IN_PROGRESS
    assert flow.STATE_PROGRESS[StepState.NEEDS_CONFIRM] == 0.6
    assert flow.STATE_PROGRESS[StepState.IN_PROGRESS] == 0.3


def test_a_suggested_field_still_asks_for_confirmation():
    """反向:真有建议值的字段,行为一个字都没变。

    本批只把 CANDIDATE 那一档摘出去。少了这条守卫,「把两档一起打成阻断」
    这种过度收紧不会被任何东西发现 —— 而它的表现是全库商品集体变红。
    """
    result = flow.evaluate(
        _flow(
            attribute=AttributeFacts(
                required_fields=REQUIRED,
                confirmed=frozenset(REQUIRED[1:]),
                suggested=frozenset({REQUIRED[0]}),
                extraction_count=1,
            )
        )
    )
    assert result.step_state(FlowStep.ATTRIBUTE) is StepState.NEEDS_CONFIRM
    assert result.blocking_count == 0
    assert result.next_action.code is NextActionCode.CONFIRM_ATTRIBUTES


def test_nothing_to_confirm_means_the_button_says_fill_not_confirm():
    """**说错话比不说话更难查。**

    队列是空的时候按钮却写着「确认属性」,运营点开找不到可点的东西,
    下一步多半是去点「启动属性识别」—— 一次真实付费调用,
    换回同一批不采信的证据。
    """
    result = flow.evaluate(_all_candidate_flow())
    assert result.next_action.code is NextActionCode.FILL_ATTRIBUTES
    assert result.next_action.label == "人工填写属性"


def test_the_new_action_lands_on_the_attribute_step():
    """它属于属性步。归错步的表现是前端把运营跳去素材页。"""
    assert flow.ACTION_STEP[NextActionCode.FILL_ATTRIBUTES] is FlowStep.ATTRIBUTE


def test_a_mixed_product_is_asked_to_confirm_what_it_can():
    """有的能确认、有的只能填时,先说能确认的那件事。

    反过来的话,一件只差一个确认的商品会被报成「待填写」,
    而运营去属性页看到的是一个已经有建议值的字段 —— 他会以为系统算错了。
    """
    result = flow.evaluate(
        _flow(
            attribute=AttributeFacts(
                required_fields=REQUIRED,
                confirmed=frozenset(REQUIRED[2:]),
                suggested=frozenset({REQUIRED[1]}),
                candidate_only=frozenset({REQUIRED[0]}),
                extraction_count=1,
            )
        )
    )
    assert result.next_action.code is NextActionCode.CONFIRM_ATTRIBUTES
    assert REQUIRED[1] in result.next_action.reason


def test_the_fill_reason_names_fields_in_the_required_order():
    """**确定性。**八个字段,提示语里的前三个必须是必填序的前三个。

    按集合迭代序拼的表现是:同一件商品刷新两次,提示语里的字段先后颠倒 ——
    谁都不会把这件事报成 bug,只会觉得这个界面有点飘。
    八个元素时乱序碰巧等于有序的概率是 1/(8·7·6)。
    """
    result = flow.evaluate(_all_candidate_flow())
    expected = "待填写:" + "、".join(REQUIRED[:3]) + "…"
    assert result.next_action.reason == expected, result.next_action.reason


def test_the_confirm_reason_names_fields_in_the_required_order():
    """同一条确定性,换成「待确认」那条动线。"""
    result = flow.evaluate(
        _flow(
            attribute=AttributeFacts(
                required_fields=REQUIRED,
                confirmed=frozenset(),
                suggested=frozenset(REQUIRED),
                extraction_count=1,
            )
        )
    )
    expected = "待确认:" + "、".join(REQUIRED[:3]) + "…"
    assert result.next_action.reason == expected, result.next_action.reason


def test_the_new_action_code_has_a_batch_exception_category():
    """新动作码必须带一个具体的批次异常类别,而且**不许可重试**。

    可重试的话,批次会自动再跑一次识别 —— 一次真实付费调用,
    换回同一批不采信的证据。
    """
    code = b.PRECHECK_CODE_BY_ACTION[NextActionCode.FILL_ATTRIBUTES]
    assert code is b.BatchErrorCode.ATTRIBUTE_NOT_CREDIBLE
    meta = b.ERROR_REGISTRY[code]
    assert meta.retryable is False, "属性不采信被标成可重试 —— 批次会自动再花一次钱"
    assert meta.needs_human is True
    assert meta.category is FlowStep.ATTRIBUTE


def test_the_dead_primary_image_flag_stays_dead():
    """`has_primary_image` 删掉了,别让它回来。

    它算了没人读,而它读的是 `usable_roles`、§6.2 门禁读的是 `gate_roles` ——
    第一个接它的人会拿到一个「AI 图也算有主图」的答案,而两侧的测试全绿。
    真需要这个问题的答案时,问 `missing_role_groups(gate_roles)`。
    """
    assert not hasattr(MaterialFacts(), "has_primary_image")
    assert "has_primary_image" not in (APP / "workbench" / "service.py").read_text(
        encoding="utf-8"
    )


# ==========================================================
# P 组:溯源判定(PRD §11 / AC-22)
# ==========================================================

#: 四个溯源信号。**穷举它们的全部组合**,理由见模块文档第三处穷举。
_SIGNALS = ("generation_task_id", "generation_candidate_id", "ai_source", "legacy_candidate")


def _asset_with(signals: frozenset[str]) -> _Asset:
    return _Asset(
        generation_task_id="t1" if "generation_task_id" in signals else None,
        generation_candidate_id="c1" if "generation_candidate_id" in signals else None,
        source=(
            MediaSource.AI_GENERATED.value
            if "ai_source" in signals
            else MediaSource.MANUAL_UPLOAD.value
        ),
        legacy_kind=(
            LEGACY_CANDIDATE if "legacy_candidate" in signals else LEGACY_PRODUCT_ASSET
        ),
    )


def test_any_one_provenance_signal_is_enough():
    """**穷举 16 种组合。**四个信号任一命中即算带溯源,一个都没有才算干净。

    这是一个全称命题,挑样本证明不了它。而今天真正会命中的只有
    `legacy_kind` 那一路 —— 两个溯源列是阶段 2 的,还没落库;
    `source=AI_GENERATED` 只在生成链路自己回写时出现。
    少了这条穷举,「删掉 legacy_kind 那一路」的变异会全绿,
    而那之后这个模块一条也拦不住。
    """
    for count in range(len(_SIGNALS) + 1):
        for combo in itertools.combinations(_SIGNALS, count):
            signals = frozenset(combo)
            asset = _asset_with(signals)
            assert pc.carries_generation_provenance(asset) is bool(signals), sorted(signals)


def test_the_legacy_shadow_marker_is_the_only_signal_that_fires_today():
    """把「今天为什么拦得住」写成断言,而不是让它继续当一条运气。

    `shadow_from_candidate()` 写的 `legacy_kind=generation_candidate` 曾经是
    **唯一**存在的溯源痕迹。A45-batch14-16 落了那两列(迁移 0038),
    所以「唯一」这个词过期了 —— 但这条守卫本身仍然成立,它只说这一路够用。

    **过渡记号那一路必须留着**:存量的 AI 影子行只有它,而本批刻意不回填
    (回填要信任 `legacy_id` 指得到候选行,而那上面没有任何约束)。
    删掉这一路的表现是历史上所有 AI 影子行一次性失去溯源。
    """
    only_legacy = _Asset(
        legacy_kind=LEGACY_CANDIDATE,
        source=MediaSource.AI_GENERATED.value,
        generation_task_id=None,
        generation_candidate_id=None,
    )
    assert pc.carries_generation_provenance(only_legacy)
    # 把 source 也换成人工的,只剩影子标记一路
    only_legacy.source = MediaSource.MANUAL_UPLOAD.value
    assert pc.carries_generation_provenance(only_legacy), (
        "去掉 legacy_kind 那一路之后,存量的 AI 影子行一条也拦不住"
    )
    # A45-batch14-16:两条真列现在也各自能单独命中
    for column in ("generation_task_id", "generation_candidate_id"):
        row = _Asset(source=MediaSource.MANUAL_UPLOAD.value)
        setattr(row, column, "some-uuid")
        assert pc.carries_generation_provenance(row), f"{column} 这一路不命中"


def test_every_media_source_is_decided_about():
    """**穷举 `MediaSource`。**每一个来源都要明确算不算「自称原始样品」。

    判据复用 §5.1 的派生,所以这条守卫同时钉住两件事:派生认的来源集合
    放宽一档时,这里会立刻看见。
    """
    verdicts = {
        source.value: pc.claims_product_evidence(source=source.value)
        for source in MediaSource
    }
    assert verdicts == {
        MediaSource.MANUAL_UPLOAD.value: True,
        MediaSource.SUPPLIER_FEED.value: True,
        # 外链默认不可信(§5.1 没有可信域名注册表),所以它不自称证据
        MediaSource.IMPORTED_URL.value: False,
        MediaSource.AI_GENERATED.value: False,
        MediaSource.PLATFORM_SYNC.value: False,
    }


def test_the_generation_pipeline_writing_back_its_own_candidate_is_not_a_conflict():
    """生成链路每跑完一个任务就回写一次候选图。那不是冲突。

    把它算成冲突的话,**每一张候选图落盘都会隔离自己** ——
    修一个洞,砸掉整条出图动线。
    """
    ai_sibling = _asset_with(frozenset({"legacy_candidate", "ai_source"}))
    assert (
        pc.verdict(
            source=MediaSource.AI_GENERATED.value, same_digest_in_spu=[ai_sibling]
        )
        is pc.Verdict.ACCEPT
    )


def test_reuploading_an_ai_candidate_as_a_sample_is_a_conflict():
    """**AC-22 的落点。**人工上传 + 同 SPU 已有同图的 AI 产出 = 来源冲突。"""
    ai_sibling = _asset_with(frozenset({"legacy_candidate", "ai_source"}))
    assert (
        pc.verdict(
            source=MediaSource.MANUAL_UPLOAD.value, same_digest_in_spu=[ai_sibling]
        )
        is pc.Verdict.SOURCE_CONFLICT
    )


def test_a_plain_duplicate_is_not_a_conflict():
    """同一张人工图传两遍是普通重复,照常去重,不许隔离。

    误伤这一条的表现是运营重传一次就被拦下,而他看不出为什么。
    """
    clean = _Asset()
    assert (
        pc.verdict(source=MediaSource.MANUAL_UPLOAD.value, same_digest_in_spu=[clean])
        is pc.Verdict.ACCEPT
    )
    assert (
        pc.verdict(source=MediaSource.MANUAL_UPLOAD.value, same_digest_in_spu=[])
        is pc.Verdict.ACCEPT
    )


def test_the_conflict_is_found_wherever_it_sits_in_the_list():
    """同 SPU 可能有好几条同图,带溯源的那条不一定排第一。

    只看第一条的写法在样本里看不出问题(用例都把它放在第一位),
    而真实数据的顺序由 SQL 决定。
    """
    ai_sibling = _asset_with(frozenset({"legacy_candidate"}))
    rows = [_Asset(), _Asset(), ai_sibling]
    assert (
        pc.verdict(source=MediaSource.MANUAL_UPLOAD.value, same_digest_in_spu=rows)
        is pc.Verdict.SOURCE_CONFLICT
    )


def test_a_provenanced_row_never_gets_its_role_filled_in():
    """去重命中那一路的防线:AI 行不许被补上一个人工角色。

    少了它,把图传回原 SKU 会让一条 `source=AI_GENERATED` 的记录拿到
    `role_source=HUMAN` —— §6.2 门禁问的正是「角色是不是人定的」。
    """
    assert pc.may_fill_role(_Asset()) is True
    for count in range(1, len(_SIGNALS) + 1):
        for combo in itertools.combinations(_SIGNALS, count):
            assert pc.may_fill_role(_asset_with(frozenset(combo))) is False, combo


def test_the_quarantine_reason_tells_the_operator_who_can_unblock_it():
    """隔离理由要说清下一步谁能解开。

    少掉这一句的表现是运营在素材页看见一条红的、然后去重新传一次 ——
    而重传会撞上同一条规则,他会认为系统坏了。
    """
    assert "放行" in pc.QUARANTINE_REASON
    assert "AI" in pc.QUARANTINE_REASON


# ==========================================================
# W 组:接线(AST —— 判定验得再干净,没被调用都等于零)
# ==========================================================


def _module(rel: str) -> ast.Module:
    return ast.parse((APP / rel).read_text(encoding="utf-8"))


def _func(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"找不到函数 {name}")


def _body_without_docstring(fn: ast.FunctionDef) -> list[ast.stmt]:
    """**只看函数体,不看文档字符串。**

    按整段源码找字符串的写法在这个仓库栽过三次(batch13-3 的 M2/M11、
    batch14 的 M30):文档字符串里恰好提到那几个名字,于是断言比较的是
    一段说明文字。
    """
    body = list(fn.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _dump(nodes: list[ast.stmt]) -> str:
    return "\n".join(ast.dump(n) for n in nodes)


def test_the_provenance_lookup_is_scoped_by_spu_not_by_the_dedup_key():
    """**本批最贵的一条。**取数范围必须是 SPU,不是去重键。

    缩回 `product_id` 之后,「同 SPU 不同 SKU」那一路整条失效 ——
    而那正是这条流水线上最常发生的走法:把 A 色生成好的图当样品传给
    B 色的 SKU。后果是一张 AI 图变成 `PRODUCT_EVIDENCE` 直接进识别输入,
    每张一次真实付费调用。
    """
    fn = _func(_module("media/service.py"), "_same_digest_in_spu")
    body = _body_without_docstring(fn)
    assert "attr='spu'" in _dump(body), "取数不再按 SPU 找了"

    # **断言打在那个三元表达式的条件上,不是「有没有 IfExp」。**
    # 第一版写的是 `"IfExp" in dumped`,而变异把条件换成 `if True` 之后
    # 它照样是一个 IfExp —— 守卫全绿。同 batch14 的 M10:
    # 「出现了某个语法结构」和「那个结构问的是对的问题」是两回事。
    branches = [
        n
        for n in ast.walk(ast.Module(body=body, type_ignores=[]))
        if isinstance(n, ast.IfExp)
    ]
    assert branches, "SPU 为空时的退路没了"
    assert any(
        isinstance(n.test, ast.Attribute)
        and n.test.attr == "spu"
        and isinstance(n.test.value, ast.Name)
        and n.test.value.id == "product"
        for n in branches
    ), (
        "分支条件不再是「这件商品有没有 SPU」—— `spu == None` 会渲染成 "
        "`IS NULL`,全库没有 SPU 的素材会被当成同一个 SPU 的兄弟,"
        "一次上传能被一件毫不相干的商品的历史 AI 图拦下来"
    )


def test_ingest_actually_asks_the_provenance_judgement():
    """判定验得再干净,没被调用都等于零。"""
    fn = _func(_module("media/service.py"), "ingest")
    dumped = _dump(_body_without_docstring(fn))
    assert "'_same_digest_in_spu'" in dumped
    assert "'verdict'" in dumped, "ingest 没有问过溯源判定"


def test_a_conflicting_upload_is_quarantined_with_the_shared_reason():
    """§11 要的是「拒绝**并且**落隔离待人工放行」——两件事都要。

    只记日志的表现是那张 AI 图照样成为 `PRODUCT_EVIDENCE`;
    隔离了但不写理由的表现是素材页上一条查不出来路的红行。
    """
    dumped = _dump(_body_without_docstring(_func(_module("media/service.py"), "ingest")))
    assert "attr='QUARANTINED'" in dumped, "冲突时不再落隔离"
    assert "attr='QUARANTINE_REASON'" in dumped, "隔离了但没有写理由"
    assert "attr='quarantine_reason'" in dumped


def test_the_upload_check_message_does_not_clobber_the_conflict_reason():
    """两句理由撞车时,溯源冲突那一句更要紧。

    盖掉之后运营读到的是「上传检查未通过」,而真正的原因是这张图和一张
    AI 产出字节相同 —— 两句话指向的下一步完全不同。
    """
    fn = _func(_module("media/service.py"), "shadow_from_product_asset")

    # **断言打在「这个 if 的条件里有没有『理由还是空的』这一问」上。**
    # 第一版写的是「函数体里出现过 `Not` 且出现过 `quarantine_reason`」,
    # 而这个函数本来就有 `not deduped`、也本来就要给 `quarantine_reason`
    # 赋值 —— 于是删掉那一问之后守卫全绿(与 batch13-3 的 M2 同型:
    # 两个各自都真的东西凑在一起,证明不了它们在同一处)。
    def _reads_reason(node: ast.AST) -> bool:
        return any(
            isinstance(n, ast.UnaryOp)
            and isinstance(n.op, ast.Not)
            and isinstance(n.operand, ast.Attribute)
            and n.operand.attr == "quarantine_reason"
            for n in ast.walk(node)
        )

    guarded = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.If) and _reads_reason(n.test)
    ]
    assert guarded, (
        "写隔离理由之前不再问「已经有理由了吗」—— 上传检查那句话会盖掉"
        "溯源冲突那一句,而两句话指向的下一步完全不同"
    )


def test_filling_a_missing_role_goes_through_the_provenance_check():
    """去重命中那一路的唯一防线。"""
    fn = _func(_module("media/service.py"), "_fill_missing_role")
    assert "'may_fill_role'" in _dump(_body_without_docstring(fn)), (
        "补角色不再问溯源 —— AI 影子行会拿到 role_source=HUMAN"
    )


def test_the_workbench_buckets_come_from_the_queue_policy():
    """服务层按判定分桶,不自己按状态取。

    自己取的表现:枚举新增一个取值时那个字段不落进任何桶,
    于是它在确认队列、阻断清单里同时消失,而商品照样导不出去。
    """
    fn = _func(_module("workbench/service.py"), "_attribute_facts")
    dumped = _dump(_body_without_docstring(fn))
    assert "'bucket_fields'" in dumped, "服务层没有走 queue_policy"
    assert "attr='confirmable'" in dumped
    assert "attr='evidence_only'" in dumped
    assert "attr='CANDIDATE'" not in dumped, (
        "服务层又直接按状态取桶了 —— 归档表白写了"
    )


def test_the_two_sets_on_attribute_facts_are_not_merged_at_the_seam():
    """`suggested` 与 `candidate_only` 在接缝处也不许合并。

    判定分好了桶而服务层又把两桶喂进同一个字段,是 §11 那条规则
    第二个失效入口,而它绕过了 `queue_policy` 的全部守卫。
    """
    fn = _func(_module("workbench/service.py"), "_attribute_facts")
    for keyword in ast.walk(fn):
        if isinstance(keyword, ast.keyword) and keyword.arg in {
            "suggested",
            "candidate_only",
        }:
            assert isinstance(keyword.value, ast.Attribute), (
                f"{keyword.arg} 不再是单独一个桶了:{ast.dump(keyword.value)}"
            )
