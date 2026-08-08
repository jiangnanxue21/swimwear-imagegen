"""提示词可编辑 + 版本管理,以及设置页的覆盖面。

这里不碰数据库 —— prompt_service 的读写路径要 Session,那部分在
``tests/test_prompts.py``(那边的开头正指回本文件,原来这一侧写的是
``tests/test_api_prompts.py``,树里不存在 —— 一条只通一半的双向链接)。
这一批盯的是**不需要库就能测错的东西**:
体检规则、默认值来源、评分器怎么拿到提示词、设置页有没有漏掉配置项。

最后一类是这轮改动的重点。上一轮我往 config.py 加了 16 个 VISION_MODEL_*,
设置页声明表里只有 3 个 —— 功能全对、测试全绿,只是那些配置在界面上根本不存在。
这种漏接不会有任何报错,只会让人在页面上找不到、然后回去改 .env 重启。
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.core.enums import EvaluationDepth
from app.core.settings_schema import FIELDS, SETTING_GROUPS
from app.evaluators.vision import (
    DEPTH_KEY,
    PROMPT_KEY,
    VisionEvaluatorConfig,
    VisionModelImageQualityEvaluator,
)
from app.evaluators.vision_schema import DEFAULT_SYSTEM_PROMPT
from app.services import prompt_rules as prompt_service
from tests.pure._helpers import expect_raises


def _lint(content: str) -> set[str]:
    key = prompt_service.VISION_SYSTEM_PROMPT
    return {w.code for w in prompt_service.lint_prompt(key, content)}


# ---------------------------------------------------------------- 提示词来源

def test_default_prompt_is_the_built_in_constant():
    key = prompt_service.VISION_SYSTEM_PROMPT
    assert prompt_service.default_content(key) is DEFAULT_SYSTEM_PROMPT


def test_unknown_prompt_key_is_rejected():
    expect_raises(ValueError, prompt_service.default_content, "nope")


def test_evaluator_uses_the_injected_prompt():
    """运营改过就用运营那版。"""
    assert (
        VisionModelImageQualityEvaluator.system_prompt({PROMPT_KEY: "自定义口径"})
        == "自定义口径"
    )


def test_evaluator_falls_back_to_the_default_prompt():
    """提示词读失败可以降级 —— 评分口径回到出厂设置,比整批评分停摆好。

    注意这和"模型输出解析失败"是两类事:那个必须失败关闭,不能给默认分。
    """
    for rule_set in ({}, {PROMPT_KEY: ""}, {PROMPT_KEY: "   "}, {PROMPT_KEY: None}):
        assert VisionModelImageQualityEvaluator.system_prompt(rule_set) is DEFAULT_SYSTEM_PROMPT
    assert VisionModelImageQualityEvaluator.system_prompt(None) is DEFAULT_SYSTEM_PROMPT


def test_custom_prompt_reaches_the_request_body():
    """一路传到真正发出去的那个 body 里,中间没有被默认值盖掉。"""
    from app.evaluators.vision import ResponsesAdapter
    from app.llm.images import PreparedImage

    config = VisionEvaluatorConfig(
        base_url="https://api.example.com/v1", model="m", api_key="k"
    )
    evaluator = VisionModelImageQualityEvaluator(config)
    request = ResponsesAdapter(config).build(
        depth=EvaluationDepth.FULL,
        system_prompt=evaluator.system_prompt({PROMPT_KEY: "只看颜色"}),
        user_prompt="U",
        references=[PreparedImage(role="R1", mime_type="image/png", byte_size=1, data_url="d")],
        candidate=PreparedImage(role="C", mime_type="image/png", byte_size=1, data_url="d"),
    )
    assert request.body["input"][0]["content"][0]["text"] == "只看颜色"


def test_build_prompt_honours_the_reference_count():
    """预览要能反映"发三张参考图时长什么样",写死 1 的预览和实际发出去的不是一回事。"""
    evaluator = VisionModelImageQualityEvaluator(VisionEvaluatorConfig())
    preview = evaluator.build_prompt({}, {DEPTH_KEY: "FULL"}, reference_count=3)
    assert "REFERENCE_IMAGE_3" in preview


# ---------------------------------------------------------------- 体检

def test_default_prompt_passes_its_own_lint():
    """内置默认自己必须是干净的,否则第一次打开页面就是一屏警告。"""
    assert _lint(DEFAULT_SYSTEM_PROMPT) == set()


def test_lint_flags_each_kind_of_broken_prompt():
    """同一个 lint 函数的输入矩阵。"""
    cases = [
        ("你是一个审核员,认真看图。", "no_json_instruction",
         "散文输出一律判评分失败,这条提示能省掉一整轮排查"),
        ("   ", "empty", "空提示词"),
        ("只输出 JSON,不要 Markdown。", "no_hard_fail", "丢了硬性失败的概念"),
    ]
    for prompt, expected_code, why in cases:
        assert expected_code in _lint(prompt), f"{why}: 应报 {expected_code}"


def test_lint_is_advisory_not_a_gate():
    """已经确认过要完全开放全文,所以体检只返回清单,不抛异常。"""
    warnings = prompt_service.lint_prompt(prompt_service.VISION_SYSTEM_PROMPT, "随便写点什么")
    assert isinstance(warnings, list)
    assert all(hasattr(w, "code") and hasattr(w, "message") for w in warnings)


def test_lint_ignores_unknown_keys_instead_of_failing():
    assert prompt_service.lint_prompt("something_else", "x") == []


# ---------------------------------------------------------------- 设置页覆盖面

CONFIG_PY = Path(__file__).resolve().parents[2] / "app" / "core" / "config.py"


def _settings_fields(prefix: str) -> set[str]:
    """AST 读 config.py —— 纯测试环境没有 pydantic,不能实例化 Settings。"""
    tree = ast.parse(CONFIG_PY.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    if stmt.target.id.startswith(prefix):
                        names.add(stmt.target.id)
    return names


def test_every_vision_setting_is_reachable_from_the_settings_page():
    """上一轮就是在这里漏了:配置加进了 config.py,却没加进声明表。

    后果不报错,只是那些配置在界面上不存在,换厂商还得改 .env 重启 ——
    而"换厂商不用改代码"正是这层的卖点。
    """
    declared = _settings_fields("VISION_MODEL_")
    on_page = {key for key in FIELDS if key.startswith("VISION_MODEL_")}
    missing = declared - on_page
    assert not missing, f"这些配置项在设置页上没有入口: {sorted(missing)}"


def test_the_two_settings_you_must_change_to_switch_vendors_are_on_the_page():
    """换厂商主要就改这两项。它们不在页面上,这层就白做了。"""
    assert "VISION_MODEL_API_STYLE" in FIELDS
    assert "VISION_MODEL_RESPONSE_FORMAT" in FIELDS


def test_multimodal_and_text_model_are_separate_groups():
    """多模态和非多模态从一开始就分开,将来接上用途时不必再做一次配置迁移。"""
    group_keys = {g.key for g in SETTING_GROUPS}
    assert "evaluator" in group_keys
    assert "text_model" in group_keys

    vision_group = next(g for g in SETTING_GROUPS if g.key == "evaluator")
    text_group = next(g for g in SETTING_GROUPS if g.key == "text_model")
    assert all(f.key.startswith(("EVALUATOR_", "VISION_MODEL_")) for f in vision_group.fields)
    # COPY_GENERATOR 是这一组的开关(选 template 还是 llm),不带 TEXT_MODEL_ 前缀:
    # 它决定的是"用不用下面这些配置",本身不是文本模型的参数。
    assert all(
        f.key.startswith("TEXT_MODEL_") or f.key == "COPY_GENERATOR"
        for f in text_group.fields
    )


def test_text_model_group_no_longer_claims_it_has_no_call_site():
    """接完了还写着"没有任何调用点",和接完了还挂着"待接入"是同一种谎。

    这条测试原来断言的是相反的事(`assert "没有任何调用点" in description`)——
    那句话在 `LLMCopyGenerator` 接上之后就过期了,而它的误导性很强:
    运营照着它理解,会以为填了 TEXT_MODEL_* 不影响任何行为,于是配好 Qwen
    之后发现文案还是模板出的,也不会想到来这一组找原因。

    与下面 `test_evaluator_backend_no_longer_says_vision_is_pending` 同型。
    """
    text_group = next(g for g in SETTING_GROUPS if g.key == "text_model")
    blurb = f"{text_group.title} {text_group.description}"
    assert "预留" not in blurb
    assert "没有任何调用点" not in blurb
    # 光是"不说假话"还不够,要说清楚什么时候生效
    assert "COPY_GENERATOR" in {f.key for f in text_group.fields}


def test_every_text_model_setting_is_reachable_from_the_settings_page():
    declared = _settings_fields("TEXT_MODEL_")
    on_page = {key for key in FIELDS if key.startswith("TEXT_MODEL_")}
    assert declared == on_page


def test_fail_closed_switch_is_on_the_page_and_says_why():
    spec = FIELDS["VISION_MODEL_FAIL_CLOSED"]
    assert "生产" in spec.help
    assert "Mock" in spec.help


def test_evaluator_backend_no_longer_says_vision_is_pending():
    """接完了还挂着"待接入",会让人以为选了也没用。"""
    labels = " ".join(o.label for o in FIELDS["EVALUATOR_BACKEND"].options)
    assert "待接入" not in labels
