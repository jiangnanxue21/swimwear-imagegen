"""提示词的**纯策略**部分:用途常量、默认值来源、体检规则。

和 ``prompt_service`` 分开是因为这些东西一条数据库连接都不需要 ——
体检规则是纯策略,为了测它去起一个 Postgres 是本末倒置,而且那样的话
它多半就不会被测了。``prompt_service`` 原样再导出这些名字,调用方不必关心这层拆分。
"""
from __future__ import annotations

from dataclasses import dataclass

#: 评分系统提示词的用途键。**真身在 evaluators 层**(`vision_schema`,与两份
#: 默认提示词、`prompt_key_for` 同一个模块):选键发生在评分链路上,而评分
#: 链路受依赖方向契约(grading-stays-pure)约束,不得触达 services。
#: 这里原样再导出,设置页与 `prompt_service` 的调用方不必搬家 ——
#: 按受众各一份、不加条件分支的理由也写在那边。
from app.evaluators.vision_schema import (
    ANTI_INJECTION_BLOCK,
    VISION_SYSTEM_PROMPT,
    VISION_SYSTEM_PROMPT_MEN,
)
from app.prompts import registry

#: 可落库编辑的用途键。**值与改造前逐字节相同**((女, 男)),只是来源换成了
#: 注册表的 `editable_keys()` —— 「哪些 key 改了会生效」从此只有一份答案
#: (PRD BE-301;editable 的语义见 registry 模块文档)。守卫比对两边一致。
KNOWN_KEYS = registry.editable_keys()
if KNOWN_KEYS != (VISION_SYSTEM_PROMPT, VISION_SYSTEM_PROMPT_MEN):
    # `python -O` 会剥掉模块级 assert(本仓有守卫钉着这一条),用 raise。
    raise AssertionError("注册表的 editable 集合与评分链路的用途常量分叉了")

MAX_PROMPT_CHARS = 20000


@dataclass(frozen=True)
class PromptWarning:
    code: str
    message: str


def default_content(key: str) -> str:
    if key == VISION_SYSTEM_PROMPT:
        from app.evaluators.vision_schema import DEFAULT_SYSTEM_PROMPT

        return DEFAULT_SYSTEM_PROMPT
    if key == VISION_SYSTEM_PROMPT_MEN:
        from app.evaluators.vision_schema import DEFAULT_SYSTEM_PROMPT_MEN

        return DEFAULT_SYSTEM_PROMPT_MEN
    raise ValueError(f"未知的提示词用途:{key}")


def lint_prompt(key: str, content: str) -> list[PromptWarning]:
    """体检,不是门禁。返回的每一条都只是提示。

    检查项都指向同一类事故:提示词和 Schema 说的不是一回事,于是模型的输出
    通不过 parser。这类失败不会在保存时暴露,只会在下一批任务里零零散散地出现。
    """
    warnings: list[PromptWarning] = []
    surface = registry.surface_of(key)
    if surface is None:
        # 注册表之外的 key:与改造前对未知 key 的行为一致 —— 不体检,不报错。
        return warnings

    stripped = content.strip()
    if not stripped:
        warnings.append(PromptWarning("empty", "提示词是空的,模型将只收到用户段指令"))
        return warnings

    # ---- 按层分派(PRD BE-304)。FREE 落到下面的既有规则;这两层先回答自己的问题。
    if surface.tier is registry.Tier.MAPPING:
        # 本轮只读(§14.2),体检只做「结构可解析」:键封闭性推迟。
        import json as _json

        try:
            parsed = _json.loads(content)
        except (TypeError, ValueError):
            warnings.append(
                PromptWarning("invalid_json", "MAPPING 层的内容必须是合法 JSON")
            )
        else:
            if not isinstance(parsed, dict):
                warnings.append(
                    PromptWarning("invalid_json", "MAPPING 层的内容必须是 JSON 对象")
                )
        return warnings
    if surface.tier is registry.Tier.TEMPLATE:
        # 「你能改措辞和排版,但槽位删掉就报警」—— 少一个槽位,拼装代码填不进去,
        # 提示词会带着字面 {slot} 出门,或者干脆缺一段清单(AC-26)。
        for slot in surface.required_slots:
            if "{" + slot + "}" not in content:
                warnings.append(
                    PromptWarning(
                        "missing_slot",
                        f"缺少必需槽位 {{{slot}}} —— 这一段由代码填充,删掉它模型就收不到",
                    )
                )
        return warnings

    # ---- FREE 层:防注入段存在性(BE-308)。内置默认自带这一段,不会触发。
    if ANTI_INJECTION_BLOCK not in content:
        warnings.append(
            PromptWarning(
                "no_anti_injection",
                "缺少「最高优先级:数据与指令的边界」防注入段 —— 图片与元数据里的文字"
                "将不再被明确标记为待审数据,一行嵌入指令就能操纵评分",
            )
        )

    if len(content) > MAX_PROMPT_CHARS:
        warnings.append(
            PromptWarning(
                "too_long",
                f"超过 {MAX_PROMPT_CHARS} 字,会挤占图片的 token 预算并推高每次评分成本",
            )
        )

    if "JSON" not in content.upper():
        warnings.append(
            PromptWarning(
                "no_json_instruction",
                "没有提到 JSON。模型很可能返回散文,而散文一律会被判为评分失败",
            )
        )

    lowered = content.lower()
    for token, code, hint in (
        ("markdown", "no_markdown_ban", "没有禁止 Markdown,模型可能用 ```json 包裹输出"),
        ("hard_fail", "no_hard_fail", "没有提到 hard_fail,模型可能不输出硬错误判定"),
    ):
        if token not in lowered:
            warnings.append(PromptWarning(code, hint))

    for forbidden, code in (
        ("overall_score", "asks_overall"),
        ("grade", "asks_grade"),
    ):
        if forbidden in lowered and "不要" not in content and "不得" not in content:
            warnings.append(
                PromptWarning(
                    code,
                    f"提到了 {forbidden} 但没有明确禁止输出它 —— 总分与分档由后端计算,"
                    "模型给出的会被忽略,写进提示词只会浪费 token",
                )
            )

    # 刻意**不检查**系统提示词里有没有列出评分维度和硬错误代码:
    # 两者都由用户段和 JSON Schema 注入,系统段里不写也照常工作。
    # 为此报警的话,内置默认提示词自己就会带两条警告 —— 一打开页面就是黄色告警,
    # 而这正是让人从此忽略整个警告面板的开始。

    return warnings
