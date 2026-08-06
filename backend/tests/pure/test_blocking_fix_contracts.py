"""阻断项整改的静态契约(A 阶段)。

这一批不连数据库,用 AST / 源码扫描钉住"改过的地方不会被悄悄改回去"。
判据都选**改回去时必然失败**的形状,而不是"看起来对"的形状。
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.listings.image_set_rules import (
    ImageSetViolation,
    ItemView,
    SetView,
    validate_set,
)
from tests.pure._helpers import BACKEND_ROOT, window

APP = Path(BACKEND_ROOT) / "app"


def _source(rel: str) -> str:
    return (APP / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------- BLOCK-02


def _item(**kw) -> ItemView:
    base = dict(media_asset_id="m1", role="FRONT", sort_order=0, is_primary=True)
    base.update(kw)
    return ItemView(**base)


def _set(items) -> SetView:
    return SetView(
        image_set_id="s1", spu="SPU1", version=1, status="DRAFT", items=tuple(items)
    )


def test_variant_coverage_actually_fires_when_a_colour_has_no_image():
    """规则本身是好的 —— 缺陷在于从来没人传 variant_ids 进去。"""
    problems = validate_set(
        _set([_item(variant_id="RED")]), variant_ids=("RED", "BLUE")
    )
    assert ImageSetViolation.MISSING_IMAGE_FOR_VARIANT in [p.code for p in problems]


def test_spu_generic_images_no_longer_cover_every_variant():
    """**BLOCK-02 闭环之后,这条翻过来了(A45-batch14-20 / PRD §6.5)。**

    这条断言当初写下来时,它守的是"这次改动别把单色 SPU 打断"。
    而它实际钉住的是一条**权宜口径**:通用图算覆盖,因为那时
    `listing_image_items.variant_id` 根本没有写入方,硬阻断等于停产。

    §6.5 同时给了两样东西:颜色绑定入口(候选入集时继承生成任务的
    `color_variant_id`),以及"缺图就是缺图"的业务决定。前者让后者
    不再是停产 —— 所以这条口径可以、也必须收紧。

    单色 SPU 的行为由 `test_image_set_rules.test_a_single_colour_spu_is_untouched_by_that_change`
    单独守着:不传 `variant_ids` 就不做颜色级检查。
    """
    problems = validate_set(
        _set([_item(variant_id=None)]), variant_ids=("RED", "BLUE")
    )
    assert ImageSetViolation.MISSING_IMAGE_FOR_VARIANT in [p.code for p in problems]


def test_variant_coverage_now_blocks_and_is_still_visible():
    """**这条按它自己写下的条件升级了(A45-batch14-20)。**

    原文写着「等前端补上变体编排入口,这条可以升级成真正的阻断断言」——
    §6.5 就是那个条件。所以现在两件事都要成立:

        阻断    覆盖不全的图片集批不了(上面那条守着)
        可见    缺口仍然出现在详情响应和批准审计里,不是只丢一个 422

    只留阻断的话,运营看到的是"不能批准",而看不到缺的是哪个颜色 ——
    §4.5.1 那条(每条提示都要说清对象、字段、动作)对它同样成立。
    """
    src = _source("listings/image_set_service.py")
    assert "def variant_coverage(" in src
    assert "variant_binding_missing" in src
    assert '"variant_coverage"' in src  # 进审计
    assert "variant_coverage" in _source("api/image_sets.py")  # 进详情响应


def test_approve_passes_real_variant_ids():
    """`approve()` 必须把真实变体传进校验,否则规则等于死代码(BLOCK-02)。"""
    src = _source("listings/image_set_service.py")
    assert "variants.required_variant_ids(session, row.spu)" in src
    # 两个调用点(validate 与 approve)都要带上
    assert src.count("required_variant_ids") >= 2


def test_variant_id_rule_has_exactly_one_definition():
    """变体口径只能有一份实现,否则导出与批准会各算各的。

    钉的是"颜色为空时回落到 sku"这个**判定**,不是 `primary_color` 这个字段名 ——
    后者在别处有正当用途(例如 SPU 聚合页要显示颜色,空值就是空值,
    不该回落成 sku)。
    """
    import re

    shape = re.compile(r"primary_color\s+or\s+\w+\.sku")
    hits = [
        path.relative_to(APP).as_posix()
        for path in APP.rglob("*.py")
        if shape.search(path.read_text(encoding="utf-8"))
    ]
    assert hits == ["listings/variants.py"], hits


# ---------------------------------------------------------------- BLOCK-01


def test_image_set_creation_checks_asset_ownership():
    src = _source("listings/image_set_service.py")
    assert "_assert_items_belong_to_spu" in src
    assert "_assert_spu_exists" in src
    # 校验必须在真正插入之前发生:放在 flush 之后等于没有校验
    assert src.index("_assert_items_belong_to_spu(session, spu=spu, items=items)") < src.index(
        "session.add(row)"
    )


# ---------------------------------------------------------------- BLOCK-03


def test_batch_retry_respects_the_execution_mode():
    """重试和创建必须走同一套模式判定。"""
    src = _source("api/workbench_batch.py")
    retry = src[src.index("def retry_batch(") :]
    assert "BATCH_EXECUTION_MODE" in retry
    assert "reset_items_for_retry" in retry
    assert "dispatch_state" in retry


# ---------------------------------------------------------------- BLOCK-15


def test_no_raw_exception_text_is_written_to_the_candidate_row():
    """`f"{type(exc).__name__}: {exc}"` 这个形状不能再出现在写库路径上。"""
    src = _source("tasks/generation_tasks.py")
    assert 'f"{type(exc).__name__}: {exc}"' not in src
    assert "safe_error_message(exc)" in src


# ---------------------------------------------------------------- BLOCK-16


def test_publish_renews_the_lease_before_each_call():
    src = _source("services/publish_service.py")
    assert "_renew_lease" in src
    # 续租必须发生在外部调用**之前**
    assert src.index("if not _renew_lease(") < src.index("outcome = _invoke(call")


# ---------------------------------------------------------------- BLOCK-17


def test_export_filters_before_paging():
    """`only_complete` 必须在分页之前生效,`total` 必须是真实总数。"""
    src = _source("api/exports.py")
    body = window(src, "def export_products(", what="export_products 及其之后")
    # 旧写法的两个特征都不能再出现
    assert 'p["complete"]' not in body
    assert '"total": len(payloads)' not in body
    assert "complete_product_filter" in body
    assert "offset" in body


# ---------------------------------------------------------------- BLOCK-18


def test_dashboard_does_not_count_completed_as_auto_approved():
    src = _source("services/dashboard_service.py")
    assert "auto_completed" in src
    assert (
        # 拆成两段相邻字面量,编译期拼回原值 —— 只为过 E501,断言内容一字未改
        'task_by_status.get(TaskStatus.AUTO_APPROVED.value, 0)\n'
        '            + task_by_status.get(TaskStatus.COMPLETED.value, 0)'
        not in src
    )


def test_average_rounds_excludes_manual_review():
    """MANUAL_REVIEW 是"在等人",不是终态。"""
    tree = ast.parse(_source("services/dashboard_service.py"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "finished_statuses" for t in node.targets
        ):
            names = [
                n.attr
                for n in ast.walk(node.value)
                if isinstance(n, ast.Attribute) and n.attr.isupper()
            ]
            assert "MANUAL_REVIEW" not in names
            return
    raise AssertionError("找不到 finished_statuses")


# ---------------------------------------------------------------- BLOCK-19


def test_daily_spend_is_grouped_by_currency():
    src = _source("services/spend.py")
    daily = src[src.index("def _daily(") :]
    assert "ProviderUsageRecord.currency" in daily
    assert "by_currency" in daily


# ---------------------------------------------------------------- 6.4 / 6.11


def test_force_retry_requires_a_reconciliation_note():
    src = _source("services/generation_service.py")
    assert "reconciliation_note" in src
    assert 'payload["force"] = True' in src


def test_prompt_save_cannot_be_signed_as_someone_else():
    """`updated_by` 不能再来自请求体。"""
    src = _source("api/prompts.py")
    assert "updated_by: str | None" not in src
    assert "updated_by=actor" in src


# ---------------------------------------------------------------- 6.5


def test_zero_score_is_not_reported_as_missing():
    src = _source("api/reviews.py")
    assert "if candidate.overall_score else None" not in src
    assert "candidate.overall_score is not None" in src


def test_review_list_does_not_swallow_query_errors():
    src = _source("api/reviews.py")
    assert 'out.task_status = ""\n    return out' not in src
    assert "_batch_context" in src


# ---------------------------------------------------------------- BE-GLOBAL


def test_http_exceptions_use_the_unified_envelope():
    src = _source("main.py")
    assert "StarletteHTTPException" in src
    assert "_HTTP_STATUS_TO_CODE" in src


def test_api_docs_are_closed_in_production():
    src = _source("main.py")
    assert "docs_enabled = not settings.is_production" in src
    assert 'openapi_url="/openapi.json" if docs_enabled else None' in src
