"""a70 守卫:提示词调用统计(BE-302 / FE-301 / FE-306)。

## 这一批守的是三条「出数了但数是错的」

统计类缺陷有一个共同形状:**它不会报错,只会给一个看起来正常的数**,
而运营正是拿它判断"要不要回滚这一版"。三条各自的来路:

**整轮汇总行算进了调用数。** `evaluation_attempts` 里有两行不对应任何一次
API 调用("这一轮一张都没评成")。算进去既虚增分母,又把已经逐张记过的失败
再加一遍。计费侧为完全相同的一条踩过坑 —— `_record_attempt` 的
`billable_provider` 只在逐张三处传,注释写着「给它们记账会让调用数凭空翻倍」。
**同一个坑在同一张表上第二次出现**,所以这次钉住。

**零调用被显示成 0% 失败率。** 那会让一个没人用过的版本和一个跑得很好的版本
在界面上长得一模一样,而这一列存在的全部理由是把这两者分开。

**判定层的字面量与枚举分叉。** `prompt_stats` 为了零依赖不 import
`core.enums`,自己列了五个 outcome 字面量。枚举改一个取值而这边不跟着改,
统计会安静地把那一类失败漏掉 —— 双向比对钉这一条。

## 射程

只判**判定层**与两处字面量接缝。**判不了**:SQL 分组对不对、JSONB 那两个
`->>` 取得出取不出、索引有没有被用上 —— 那些要真库。这里不假装守住了它们
(a66 那条同样的判据:一个被说大的守卫比没有守卫更贵)。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from app.core.enums import EvaluationOutcome
from app.workflows import prompt_stats

BACKEND = Path(__file__).resolve().parents[2]
EVAL_SERVICE = BACKEND / "app" / "services" / "evaluation_service.py"
USAGE = BACKEND / "app" / "services" / "prompt_usage.py"


# --------------------------------------------------------------- 整轮行不算调用


def test_round_level_rows_do_not_count_as_calls():
    """整轮汇总行既不进分母,也不进任何一类失败。

    这是本批的 motivating case:计费侧已经为同一件事踩过一次坑。
    """
    rows = [
        {"outcome": "PROVIDER_ERROR", "round_level": True},
        {"outcome": "ROUND_RETRY_SCHEDULED", "round_level": True},
    ]
    stats = prompt_stats.tally(rows)
    assert stats.calls == 0, "整轮汇总行不对应任何一次 API 调用,不该进分母"
    assert stats.failures == 0, "整轮的失败是逐张失败的汇总,再加一遍等于重复计数"
    assert stats.parse_failure_rate is None


def test_a_round_level_provider_error_is_not_confused_with_a_per_call_one():
    """整轮与逐张都会写 PROVIDER_ERROR —— 区分只能靠 `round_level`。

    这一条钉的是"别改回按 outcome 分":改回去之后逐张的 PROVIDER_ERROR
    会跟着整轮的一起被排掉,失败率凭空变好看。
    """
    stats = prompt_stats.tally(
        [
            {"outcome": "PROVIDER_ERROR", "round_level": False},
            {"outcome": "PROVIDER_ERROR", "round_level": True},
        ]
    )
    assert stats.calls == 1
    assert stats.provider_error == 1, "逐张的那条必须留下,整轮的那条必须排掉"


# --------------------------------------------------------------- 空分母不是 0%


def test_no_calls_yields_none_not_zero_rate():
    """一次都没跑过时两个比率都是 None。

    0.0 会让「没人用过」和「跑得很好」在界面上长得一样。
    """
    stats = prompt_stats.PromptCallStats()
    assert stats.parse_failure_rate is None
    assert stats.failure_rate is None
    payload = stats.as_dict()
    assert payload["parse_failure_rate"] is None, "出参里也不许兜底成 0"
    assert payload["failure_rate"] is None
    assert payload["calls"] == 0


def test_rates_are_computed_over_calls_not_over_failures():
    stats = prompt_stats.tally(
        [
            {"outcome": "SUCCEEDED", "round_level": False, "count": 7},
            {"outcome": "PARSE_FAILED", "round_level": False, "count": 2},
            {"outcome": "PROVIDER_ERROR", "round_level": False, "count": 1},
        ]
    )
    assert stats.calls == 10
    assert stats.parse_failed == 2
    assert stats.failures == 3
    assert stats.parse_failure_rate == 0.2
    assert stats.failure_rate == 0.3


def test_grouped_counts_match_row_by_row_counting():
    """`count` 分组与逐行展开必须数出同一份统计。

    服务层用 GROUP BY 压缩之后送进来,判定不变 —— 这条钉住那个等价。
    """
    expanded = [
        {"outcome": "PARSE_FAILED", "round_level": False} for _ in range(3)
    ] + [{"outcome": "SUCCEEDED", "round_level": False} for _ in range(5)]
    grouped = [
        {"outcome": "PARSE_FAILED", "round_level": False, "count": 3},
        {"outcome": "SUCCEEDED", "round_level": False, "count": 5},
    ]
    assert prompt_stats.tally(expanded) == prompt_stats.tally(grouped)


def test_diagnostic_calls_stay_inside_the_denominator():
    """诊断调用**不从 calls 里扣掉**,只是额外报一个数。

    它们是真实调用、真的花了钱、真的用了这一版提示词。扣掉的话
    「调用 0 次」会出现在一个刚刚被测过的版本上。
    """
    stats = prompt_stats.tally(
        [{"outcome": "SUCCEEDED", "round_level": False, "diagnostic": True, "count": 4}]
    )
    assert stats.calls == 4
    assert stats.diagnostic == 4


def test_unknown_outcome_widens_the_denominator_but_is_not_a_failure():
    """认不出的 outcome 计进 calls,不计进失败。

    反过来会让新增一个枚举取值静默抬高所有历史提示词的失败率。
    """
    stats = prompt_stats.tally([{"outcome": "SOMETHING_NEW", "round_level": False}])
    assert stats.calls == 1
    assert stats.failures == 0


# --------------------------------------------------------------- 两处字面量接缝


def test_the_outcome_literals_match_the_enum_both_ways():
    """判定层那五个字面量与 `EvaluationOutcome` 双向对得上。

    `prompt_stats` 为零依赖不 import 枚举(它在 `app.core`,依赖本身无害,
    但开了口子下一个人会顺手 import models)。代价是两份清单,所以这里比对。
    """
    declared = {
        value
        for name, value in vars(prompt_stats).items()
        if name.startswith("OUTCOME_") and isinstance(value, str)
    }
    actual = {member.value for member in EvaluationOutcome}
    assert declared == actual, (
        "判定层的 outcome 字面量与枚举分叉了。"
        f"多出来的:{sorted(declared - actual)};漏掉的:{sorted(actual - declared)}。"
        "漏掉一个 = 那一类失败在统计里静默消失"
    )


def test_failure_outcomes_exclude_success_and_the_round_only_one():
    assert prompt_stats.OUTCOME_SUCCEEDED not in prompt_stats.FAILURE_OUTCOMES
    assert (
        prompt_stats.OUTCOME_ROUND_RETRY_SCHEDULED not in prompt_stats.FAILURE_OUTCOMES
    ), "它只出现在整轮汇总行上,而整轮行根本不进统计"


def test_the_meta_keys_the_query_reads_are_the_ones_the_writer_writes():
    """取数侧读的 `meta` 键名 == 写入侧写的键名。

    拼错一个字母的后果是**全部行都被当成非整轮、非诊断**,而统计照样出数,
    没有任何东西会红 —— 正是这一批要防的那个形状。
    """
    written = _meta_keys_written_by_record_attempt()
    read = set(re.findall(r'_META_[A-Z_]+ = "([a-z_]+)"', USAGE.read_text("utf-8")))
    assert read, "没解析出取数侧的键名,守卫形状变了"
    missing = read - written
    assert not missing, (
        f"取数侧读的这些 `meta` 键,写入侧从来不写:{sorted(missing)}。"
        "取不到的话全部行都会被当成非整轮、非诊断,而统计照样出数"
    )


def _meta_keys_written_by_record_attempt() -> set[str]:
    """`_record_attempt` 里 `meta={...}` 那个字面量的键集合。

    读 AST 而不是正则:那个 dict 里有嵌套调用(`meta.get(...)`),
    正则数不清括号。
    """
    tree = ast.parse(EVAL_SERVICE.read_text("utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.keyword) or node.arg != "meta":
            continue
        if isinstance(node.value, ast.Dict):
            return {
                k.value
                for k in node.value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
    raise AssertionError("没找到 `_record_attempt` 里的 meta={...} 字面量")


# --------------------------------------------------------------- 窗口与文案同源


def test_the_window_is_a_single_constant():
    """窗口是常量,而且列头文案要从它来。

    PRD §12.1 写死「近 7 天」。窗口与文案分开维护的话,改了一个不改另一个
    没有任何东西会红 —— 界面会理直气壮地用另一个窗口的数字说「近 7 天」。
    """
    assert prompt_stats.RECENT_WINDOW_DAYS == 7
