"""纯逻辑测试:workbench/platform.py 的状态机与版本定位。

所有用例零数据库依赖。
"""
from __future__ import annotations

import re

from app.core.enums import PlatformStatus
from app.workbench import platform as pf
from tests.pure._helpers import BACKEND_ROOT

# ==========================================================
# 闭合性
# ==========================================================


def test_every_rejection_reason_has_a_target_step():
    """REJECTION_REASON_STEP 里漏了任何一个 RejectionReason,「去处理」按钮就不知道跳哪。"""
    for reason in pf.RejectionReason:
        assert reason in pf.REJECTION_REASON_STEP, f"{reason} 没有 target_step"
        assert reason in pf.REJECTION_REASON_LABELS, f"{reason} 没有中文标签"
        assert reason in pf.REJECTION_REASON_ADVICE, f"{reason} 没有操作建议"


def test_platform_status_labels_cover_all_values():
    from app.workbench.flow import PLATFORM_STATUS_LABELS
    for status in PlatformStatus:
        assert status.value in PLATFORM_STATUS_LABELS, f"{status.value} 没有中文标签"


def test_every_located_by_value_used_in_code_has_a_label():
    """`located_by` 落库的每一种取值都要有中文标签,否则界面上显示原文。

    原来这条断言的是"恰好这三种"。任务 15 加进第四种(API 提交留痕)时它红了,
    而它红得没有道理 —— 加一种定位方式并没有破坏任何东西,真正要守的是
    **写进库的值都有标签**。所以改成从代码里找出所有取值再逐个查表:
    这样下次再加一种,忘了配标签才会红,而那次红是对的。
    """
    used = {pf.LOCATED_BY_PUBLISH_ATTEMPT}
    source = (BACKEND_ROOT / "app" / "workbench" / "platform.py").read_text(
        encoding="utf-8"
    )
    for match in re.finditer(r'located_by=["\']([a-z_]+)["\']', source):
        used.add(match.group(1))

    # 三种"从导出记录定位"的把握程度是这个模块的原始设计,少一种说明
    # `locate_export` 的返回值被改窄了,那是要被看见的改动
    used |= {"audit", "current_draft", "unlocated"}

    for value in used:
        assert value in pf.LOCATED_BY_LABELS, f"{value} 没有中文标签"


# ==========================================================
# 状态转移
# ==========================================================


def test_none_can_go_to_submitted_only():
    allowed = pf.manual_targets("NONE", open_rejections=0)
    assert allowed == frozenset({PlatformStatus.SUBMITTED})


def test_submitted_can_go_to_live_or_none():
    allowed = pf.manual_targets("SUBMITTED", open_rejections=0)
    assert PlatformStatus.LIVE in allowed
    assert PlatformStatus.NONE in allowed
    # 已上传时不能手工跳到 REJECTED
    assert PlatformStatus.REJECTED not in allowed


def test_live_can_go_back_to_submitted():
    allowed = pf.manual_targets("LIVE", open_rejections=0)
    assert PlatformStatus.SUBMITTED in allowed
    assert PlatformStatus.REJECTED not in allowed


def test_rejected_with_zero_open_gets_submitted():
    # 驳回记录全被解决后,允许重新走上传流程
    allowed = pf.manual_targets("REJECTED", open_rejections=0)
    assert PlatformStatus.SUBMITTED in allowed


def test_any_status_blocked_when_open_rejections_exist():
    """有未解决驳回时,哪儿也去不了。先处理驳回再改状态。"""
    for raw in ("NONE", "SUBMITTED", "LIVE", "REJECTED"):
        allowed = pf.manual_targets(raw, open_rejections=3)
        assert allowed == frozenset(), f"状态 {raw} 有 open_rejections 时仍有可用转移"


def test_rejected_never_in_manual_targets():
    """REJECTED 只能由 record_rejection 写入,手工目标里永远没有它。"""
    for raw in ("NONE", "SUBMITTED", "LIVE", "REJECTED"):
        for open_count in (0, 1):
            targets = pf.manual_targets(raw, open_rejections=open_count)
            assert PlatformStatus.REJECTED not in targets


def test_unknown_status_treated_as_none():
    allowed = pf.manual_targets("INVALID_STATUS", open_rejections=0)
    assert allowed == frozenset({PlatformStatus.SUBMITTED})


# ==========================================================
# 指纹比对
# ==========================================================


def test_identical_fingerprints_match():
    assert pf.fingerprints_match("abc12345def67890", "abc12345def67890")


def test_full_starts_with_short_matches():
    full = "abc12345def67890abcdef01234567"
    short = full[:12]
    assert pf.fingerprints_match(full, short)
    assert pf.fingerprints_match(short, full)


def test_different_fingerprints_do_not_match():
    assert not pf.fingerprints_match("abc12345def6", "xyz12345def6")


def test_short_under_8_chars_never_matches():
    assert not pf.fingerprints_match("abc", "abcdef")
    assert not pf.fingerprints_match("", "abc12345def6")


def test_none_fingerprint_does_not_match():
    assert not pf.fingerprints_match(None, "abc12345def6")
    assert not pf.fingerprints_match("abc12345def6", None)


# ==========================================================
# locate_export
# ==========================================================

_FINGERPRINT = "aabbccddeeff0011aabbccddeeff0011"
_SHORT_FP = _FINGERPRINT[:12]

_COMPONENTS = {
    "image_set": {"id": "img-001", "version": 3},
    "copies": {"en": {"id": "copy-001", "version": 2}},
    "spec_version": "1",
    "attrs": ["attr-1", "attr-2"],  # should be trimmed
    "manual": {"price": "99"},       # should be trimmed
}


def test_locate_uses_latest_entry_when_no_fingerprint_specified():
    entries = [
        {"fingerprint": _FINGERPRINT, "at": "2026-07-01T08:00:00", "components": _COMPONENTS},
        {"fingerprint": "older000000011", "at": "2026-06-01T08:00:00", "components": None},
    ]
    result = pf.locate_export(
        wanted_fingerprint=None,
        entries=entries,
        current_fingerprint=_FINGERPRINT,
        current_components=_COMPONENTS,
        current_exported_at="2026-07-01T08:00:00",
    )
    assert result is not None
    assert result.fingerprint == _FINGERPRINT
    assert result.located_by == "audit"
    # trim_components should drop attrs and manual
    assert "attrs" not in (result.components or {})
    assert "manual" not in (result.components or {})
    assert "image_set" in (result.components or {})


def test_locate_by_fingerprint_prefix_match():
    entries = [
        {"fingerprint": _SHORT_FP, "at": "2026-07-01T08:00:00", "components": _COMPONENTS},
    ]
    result = pf.locate_export(
        wanted_fingerprint=_FINGERPRINT,
        entries=entries,
        current_fingerprint=_FINGERPRINT,
        current_components=_COMPONENTS,
        current_exported_at="2026-07-01T08:00:00",
    )
    assert result is not None
    assert result.located_by == "audit"


def test_locate_returns_none_when_specified_fingerprint_not_found():
    entries = [{"fingerprint": "other000000011", "at": "2026-07-01T08:00:00", "components": None}]
    result = pf.locate_export(
        wanted_fingerprint="ffffffffffffffff",
        entries=entries,
        current_fingerprint=_FINGERPRINT,
        current_components=_COMPONENTS,
        current_exported_at="2026-07-01T08:00:00",
    )
    assert result is None


def test_locate_falls_back_to_current_draft_when_audit_entry_has_no_components():
    entries = [{"fingerprint": _FINGERPRINT, "at": "2026-07-01T08:00:00", "components": None}]
    result = pf.locate_export(
        wanted_fingerprint=None,
        entries=entries,
        current_fingerprint=_FINGERPRINT,  # same fingerprint → draft not rebuilt
        current_components=_COMPONENTS,
        current_exported_at="2026-07-01T08:00:00",
    )
    assert result is not None
    assert result.located_by == "current_draft"
    assert result.components is not None


def test_locate_is_unlocated_when_fingerprints_differ_and_no_audit_components():
    entries = [{"fingerprint": _SHORT_FP, "at": "2026-06-01T08:00:00", "components": None}]
    result = pf.locate_export(
        wanted_fingerprint=None,
        entries=entries,
        current_fingerprint="completely_different_fingerprint_00",
        current_components=_COMPONENTS,
        current_exported_at="2026-07-01T08:00:00",
    )
    assert result is not None
    assert result.located_by == "unlocated"
    assert result.components is None


def test_locate_uses_current_draft_when_no_audit_entries_but_exported():
    result = pf.locate_export(
        wanted_fingerprint=None,
        entries=[],
        current_fingerprint=_FINGERPRINT,
        current_components=_COMPONENTS,
        current_exported_at="2026-07-01T08:00:00",
    )
    assert result is not None
    assert result.located_by == "current_draft"


def test_locate_returns_none_when_never_exported():
    result = pf.locate_export(
        wanted_fingerprint=None,
        entries=[],
        current_fingerprint=_FINGERPRINT,
        current_components=_COMPONENTS,
        current_exported_at=None,
    )
    assert result is None


# ==========================================================
# trim_components
# ==========================================================


def test_trim_keeps_image_set_copies_spec_drops_attrs_manual():
    raw = {
        "image_set": {"id": "img-1", "version": 2},
        "copies": {"en": {"id": "c1", "version": 1}},
        "spec_version": "2",
        "attrs": ["a", "b", "c"],
        "manual": {"price": "99"},
        "mapping_version": "1",
    }
    trimmed = pf.trim_components(raw)
    assert "image_set" in trimmed
    assert "copies" in trimmed
    assert "spec_version" in trimmed
    assert "attrs" not in trimmed
    assert "manual" not in trimmed


def test_trim_handles_none_gracefully():
    assert pf.trim_components(None) == {"image_set": None, "copies": {}, "spec_version": None}


# ==========================================================
# 导出留痕 payload 的往返
# ==========================================================
#
# 这一组是本轮走查补的:写方(service.record_export)与读方(platform_service)
# 曾经对不上 —— 注释宣称 payload 带着组件版本,实际只写了 12 位指纹,
# 于是 `located_by="audit"` 这条最可信的定位路径从未被走到过,而且不报错。
# 往返测试是这类"静默降级"唯一的防线:payload 写出来、读回来、判定必须是 audit。


def test_export_payload_round_trips_into_an_audit_located_rejection():
    payload = pf.export_audit_payload(
        fmt="xlsx",
        spu="SW-001",
        fingerprint=_FINGERPRINT,
        components=_COMPONENTS,
        export_count=1,
    )
    entry = pf.export_entry(payload, "2026-07-01T08:00:00")
    assert entry is not None
    result = pf.locate_export(
        wanted_fingerprint=None,
        entries=[entry],
        current_fingerprint=None,
        current_components=None,
        current_exported_at=None,
    )
    assert result is not None
    # 关键:草稿其后即使重建过(current_* 全空),也仍然定位到了当时那一版
    assert result.located_by == "audit"
    assert result.fingerprint == _FINGERPRINT
    assert (result.components or {})["image_set"] == {"id": "img-001", "version": 3}


def test_export_payload_keeps_a_short_fingerprint_for_humans_and_a_full_one_for_machines():
    payload = pf.export_audit_payload(
        fmt="xlsx", spu="SW-001", fingerprint=_FINGERPRINT, components=None, export_count=2
    )
    assert payload["fingerprint"] == _SHORT_FP  # 审计列表一行摘要读的是这个
    assert payload["fingerprint_full"] == _FINGERPRINT  # 比对读的是这个
    assert pf.export_entry(payload, None)["fingerprint"] == _FINGERPRINT


def test_export_payload_trims_components_before_they_reach_the_audit_table():
    payload = pf.export_audit_payload(
        fmt="xlsx", spu="SW-001", fingerprint=_FINGERPRINT,
        components=_COMPONENTS, export_count=1,
    )
    assert "manual" not in payload["components"], "手填字段(可能含价格)不该复制进审计"
    assert "attrs" not in payload["components"]


def test_export_payload_carries_batch_id_only_when_it_is_a_batch_export():
    single = pf.export_audit_payload(
        fmt="xlsx", spu="SW-001", fingerprint=_FINGERPRINT, components=None, export_count=1
    )
    assert "batch_id" not in single
    batched = pf.export_audit_payload(
        fmt="xlsx", spu="SW-001", fingerprint=_FINGERPRINT, components=None,
        export_count=1, batch_id="batch-42",
    )
    assert batched["batch_id"] == "batch-42"


def test_export_entry_ignores_audit_rows_that_are_not_exports():
    assert pf.export_entry({"action": "build", "spu": "SW-001"}, None) is None
    assert pf.export_entry({"action": "platform_status"}, None) is None
    assert pf.export_entry(None, None) is None


def test_old_export_rows_without_components_still_parse():
    """本轮之前的导出审计只有 12 位指纹。读回来不能炸,只是定位把握降级。"""
    entry = pf.export_entry(
        {"action": "export", "fingerprint": _SHORT_FP, "spu": "SW-001"},
        "2026-06-01T08:00:00",
    )
    assert entry == {
        "fingerprint": _SHORT_FP,
        "at": "2026-06-01T08:00:00",
        "components": None,
    }
    result = pf.locate_export(
        wanted_fingerprint=None,
        entries=[entry],
        current_fingerprint=_FINGERPRINT,
        current_components=_COMPONENTS,
        current_exported_at="2026-06-01T08:00:00",
    )
    assert result is not None and result.located_by == "current_draft"


# ==========================================================
# A4:关闭驳回的最小闸门(resolve_gate)
# ==========================================================

_REJECTED_AT = "2026-07-20T10:00:00"
_OLD_FP = "1111111122222222333333334444444455555555666666667777777788888888"
_NEW_FP = "99999999aaaaaaaabbbbbbbbccccccccddddddddeeeeeeeeffffffff00000000"


def _export(fingerprint: str, at: str) -> dict:
    return {"fingerprint": fingerprint, "at": at, "components": None}


def test_gate_fails_when_nothing_happened_after_the_rejection():
    """什么都没改就点解决 —— 这正是 A4 要拦的那个动作。"""
    gate = pf.resolve_gate(
        rejected_fingerprint=_OLD_FP,
        rejected_at=_REJECTED_AT,
        entries=[],
        current_fingerprint=_OLD_FP,
    )
    assert not gate.satisfied
    assert pf.MISSING_NO_NEW_EXPORT in gate.missing
    assert pf.MISSING_FINGERPRINT_UNCHANGED in gate.missing


def test_gate_passes_after_a_real_fix_and_reexport():
    gate = pf.resolve_gate(
        rejected_fingerprint=_OLD_FP,
        rejected_at=_REJECTED_AT,
        entries=[_export(_NEW_FP, "2026-07-21T09:00:00")],
        current_fingerprint=_NEW_FP,
    )
    assert gate.satisfied
    assert gate.has_new_export and gate.fingerprint_changed
    assert gate.missing == ()


def test_reexporting_without_changing_anything_is_not_a_fix():
    """重导了但指纹一样:文件是新的,内容是旧的,平台会再驳一次。"""
    gate = pf.resolve_gate(
        rejected_fingerprint=_OLD_FP,
        rejected_at=_REJECTED_AT,
        entries=[_export(_OLD_FP, "2026-07-21T09:00:00")],
        current_fingerprint=_OLD_FP,
    )
    assert gate.has_new_export
    assert not gate.fingerprint_changed
    assert gate.missing == (pf.MISSING_FINGERPRINT_UNCHANGED,)


def test_exports_before_the_rejection_do_not_count():
    """驳回之前的导出当然不能算成"已经修过了"。

    entries 是整条审计历史,不做时间过滤的话,任何导出过的商品都能直接过闸。
    """
    gate = pf.resolve_gate(
        rejected_fingerprint=_OLD_FP,
        rejected_at=_REJECTED_AT,
        entries=[_export(_NEW_FP, "2026-07-19T09:00:00")],
        current_fingerprint=_OLD_FP,
    )
    assert not gate.has_new_export
    assert pf.MISSING_NO_NEW_EXPORT in gate.missing


def test_gate_uses_the_newest_export_after_the_rejection():
    """驳回后导了两次:以最新那次为准(entries 新在前)。"""
    gate = pf.resolve_gate(
        rejected_fingerprint=_OLD_FP,
        rejected_at=_REJECTED_AT,
        entries=[
            _export(_NEW_FP, "2026-07-22T09:00:00"),
            _export(_OLD_FP, "2026-07-21T09:00:00"),
        ],
        current_fingerprint=_NEW_FP,
    )
    assert gate.satisfied


def test_gate_falls_back_to_current_draft_when_audit_has_no_new_export():
    """审计里没有驳回后的导出时,指纹一项拿当前草稿比。

    审计可能被清理过。此时"有没有重导"答不上来(仍算缺),
    但"内容变没变"还是能答的,不该一并退化成不可知。
    """
    gate = pf.resolve_gate(
        rejected_fingerprint=_OLD_FP,
        rejected_at=_REJECTED_AT,
        entries=[],
        current_fingerprint=_NEW_FP,
    )
    assert not gate.has_new_export
    assert gate.fingerprint_changed
    assert gate.missing == (pf.MISSING_NO_NEW_EXPORT,)


def test_missing_fingerprint_counts_as_unchanged_not_as_changed():
    """老驳回记录没存指纹 —— 比不了不等于变了。

    这里若判成"已变化",所有没存指纹的历史记录会自动过闸,
    而它们恰恰是最该被人看一眼的那批。
    """
    gate = pf.resolve_gate(
        rejected_fingerprint=None,
        rejected_at=_REJECTED_AT,
        entries=[_export(_NEW_FP, "2026-07-21T09:00:00")],
        current_fingerprint=_NEW_FP,
    )
    assert not gate.fingerprint_changed
    assert pf.MISSING_FINGERPRINT_UNCHANGED in gate.missing


def test_gate_tolerates_truncated_audit_fingerprints():
    """老导出审计只存 12 位。前缀相同 = 同一版,不能判成"变了"。"""
    gate = pf.resolve_gate(
        rejected_fingerprint=_OLD_FP,
        rejected_at=_REJECTED_AT,
        entries=[_export(_OLD_FP[:12], "2026-07-21T09:00:00")],
        current_fingerprint=_OLD_FP,
    )
    assert not gate.fingerprint_changed


def test_gate_survives_mixed_timezone_timestamps():
    """naive 与带时区的时间戳混在一起不能抛异常。

    库里是 naive UTC,审计 payload 与接口可能带 Z 或 +08:00。
    直接比会抛 "can't compare offset-naive and offset-aware datetimes",
    而这个异常会出现在关闭驳回这条路径上。
    """
    gate = pf.resolve_gate(
        rejected_fingerprint=_OLD_FP,
        rejected_at="2026-07-20T18:00:00+08:00",  # = 10:00 UTC
        entries=[_export(_NEW_FP, "2026-07-20T11:00:00Z")],
        current_fingerprint=_NEW_FP,
    )
    assert gate.satisfied


def test_unparseable_rejection_time_flags_instead_of_passing():
    """时间读不出来时报"缺",不是放行。

    闸门误报的代价是运营多勾一个框,漏报的代价是台账退化成备忘录。
    """
    gate = pf.resolve_gate(
        rejected_fingerprint=_OLD_FP,
        rejected_at="not-a-timestamp",
        entries=[_export(_NEW_FP, "2026-07-21T09:00:00")],
        current_fingerprint=_NEW_FP,
    )
    assert not gate.has_new_export
    assert pf.MISSING_NO_NEW_EXPORT in gate.missing


def test_gate_labels_cover_every_missing_code():
    """缺口码漏了标签,界面上会显示成 no_new_export 这种字样。"""
    for code in (pf.MISSING_NO_NEW_EXPORT, pf.MISSING_FINGERPRINT_UNCHANGED):
        assert code in pf.RESOLVE_GATE_LABELS
        assert code in pf.RESOLVE_GATE_ADVICE


# ==========================================================
# A-13:API 自动发布的驳回也能被系统自证关闭
#
# 原来闸门只认「驳回之后有一次新的导出」,而 API 发布的商品根本不走导出,
# 于是这条路径来的驳回永远过不了闸,只能人工关闭(评审 A-13)。
# 现在「驳回之后有一次成功的 PublishAttempt」是等价证据 —— 但指纹那半边
# 不放松,所以**没改草稿的重复提交仍然过不了闸**。
# ==========================================================


def _attempt(at: str) -> dict:
    return {"at": at}


def test_a13_new_publish_attempt_counts_as_evidence_when_content_changed():
    """API 路径:没有导出,但改了草稿并重新提交成功 —— 闸门放行。"""
    gate = pf.resolve_gate(
        rejected_fingerprint=_OLD_FP,
        rejected_at=_REJECTED_AT,
        entries=[],
        current_fingerprint=_NEW_FP,
        attempt_entries=[_attempt("2026-07-21T09:00:00")],
    )
    assert gate.satisfied
    assert gate.has_new_attempt
    assert not gate.has_new_export  # 证据来自提交,不是导出


def test_a13_resubmitting_without_changing_the_draft_still_fails_the_gate():
    """验收的另一半:重复提交同一版内容不能解除阻断。"""
    gate = pf.resolve_gate(
        rejected_fingerprint=_OLD_FP,
        rejected_at=_REJECTED_AT,
        entries=[],
        current_fingerprint=_OLD_FP,
        attempt_entries=[_attempt("2026-07-21T09:00:00")],
    )
    assert not gate.satisfied
    assert gate.has_new_attempt
    assert pf.MISSING_FINGERPRINT_UNCHANGED in gate.missing
    # 提交这半边已经满足,不该再报"没有新导出"
    assert pf.MISSING_NO_NEW_EXPORT not in gate.missing


def test_a13_attempts_before_the_rejection_do_not_count():
    """被驳回的正是那一次提交 —— 它不能反过来当作修复证据。"""
    gate = pf.resolve_gate(
        rejected_fingerprint=_OLD_FP,
        rejected_at=_REJECTED_AT,
        entries=[],
        current_fingerprint=_NEW_FP,
        attempt_entries=[_attempt("2026-07-19T09:00:00")],
    )
    assert not gate.has_new_attempt
    assert pf.MISSING_NO_NEW_EXPORT in gate.missing


def test_a13_attempt_timestamps_survive_mixed_timezones():
    """提交留痕来自 DB(naive UTC),驳回时间可能带偏移 —— 两边必须可比。"""
    gate = pf.resolve_gate(
        rejected_fingerprint=_OLD_FP,
        rejected_at="2026-07-20T18:00:00+08:00",  # = 10:00 UTC
        entries=[],
        current_fingerprint=_NEW_FP,
        attempt_entries=[_attempt("2026-07-20T11:00:00")],
    )
    assert gate.satisfied


def test_a13_gate_without_attempts_behaves_exactly_as_before():
    """默认参数不改变既有行为:导出路径的判定原样保留。"""
    gate = pf.resolve_gate(
        rejected_fingerprint=_OLD_FP,
        rejected_at=_REJECTED_AT,
        entries=[_export(_NEW_FP, "2026-07-21T09:00:00")],
        current_fingerprint=_NEW_FP,
    )
    assert gate.satisfied
    assert gate.has_new_export
    assert not gate.has_new_attempt
