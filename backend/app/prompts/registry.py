"""提示词注册表(`docs/PRD-A55-PROMPT-REGISTRY-AND-LOG-CONSOLE.md` §11.2)。

## 这张表登记的是什么

仓库里的提示词共 **8 处**,散在 6 个模块(PRD §8 的盘点表)。此前只有第 1 处
能在界面上看到,其余 7 处的存在只写在一份 PRD 的表格里 —— 表格不会在
新增第 9 处时变红,也答不出"这个 key 的默认值还解析得出来吗"。

这张表是那份盘点的**机器可读版**:每一处答得出 key、层(tier)、默认值
引用、谁在消费、能不能编辑、界面到不到得了。`tests/pure/
test_a56_prompt_registry.py` 逐条比对(照 `providers/comfyui.py` 的
`UNWIRED_CONFIG_FIELDS` + `test_a51_comfyui_config_wiring.py` 的既有模式,
PRD 点名沿用)。

## 只存元数据,不存正文

正文留在各自领域模块(PRD NG1:`grading-stays-pure` 契约禁 evaluators
触达 services,把正文搬进统一位置会把依赖方向搅乱)。`default_ref` 是
点分路径,`resolve_default` 惰性解析 —— 本模块**不 import 任何领域模块**,
否则 `app.prompts` 会把 evaluators / listings 的依赖面全部带给
每一个 import 它的人(lint 在 services 层,评分键选择在 evaluators 层,
两边都要 import 这里)。

## `editable` 的语义:消费链路会不会读库

**不是"想不想让人改",是"改了会不会生效"。** `prompt_templates` 表谁都
能写,但 8 处里只有评分两份(#1 #2)的消费链路走 `get_active_content`
读库;其余 6 处的正文由代码拼装或直接引用常量,库里存一版新的,链路
**一个字都不会读**。把这 6 处标成可编辑,得到的是「保存成功、毫无效果」——
比男装那条"后端通、前端不可达"的死路(PRD 问题 A)更糟,因为它连报错
都没有。所以 editable=False 的准确含义写在每一项的 consumers 里。

## 依赖约束

只依赖标准库与 `app.core`。守卫 `test_the_registry_only_depends_on_core`
用 AST 钉着这一条 —— 破了它,`grading-stays-pure` 迟早跟着破。
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from enum import StrEnum


class Tier(StrEnum):
    """三层模型(PRD §10)。统一管理的前提是承认它们形态不同。"""

    FREE = "FREE"          # 全文 textarea;校验 = 现有 lint + 防注入段存在性
    TEMPLATE = "TEMPLATE"  # 措辞可改,必需槽位一个都不能少
    MAPPING = "MAPPING"    # 键值映射;本轮只读(PRD §14.2 决议)


@dataclass(frozen=True)
class PromptSurface:
    """一处提示词的元数据。字段语义见 PRD §11.2。

    ``default_ref`` 形如 ``"app.evaluators.vision_schema:DEFAULT_SYSTEM_PROMPT"``。
    为 None 表示这一处没有静态默认正文(运行时拼装,如出图提示词)——
    那不是登记不全,是如实描述:它的"默认值"是一个函数的行为,不是一段文本。

    ``ui_reachable`` 为 False 时,**原因必须写进 consumers**(PRD §14.1:
    「后端支持但前端悄悄看不见」这个中间态不允许无记录地存在)。
    """

    key: str
    label: str
    tier: Tier
    default_ref: str | None
    required_slots: tuple[str, ...] = ()
    key_source: str | None = None
    editable: bool = True
    ui_reachable: bool = True
    consumers: tuple[str, ...] = field(default_factory=tuple)


#: 8 处提示词,顺序与 PRD §8 盘点表一致。key 的取值与 `prompt_templates.key`
#: 及 `vision_schema` 里的用途常量一致 —— 守卫逐一比对,这里不 import 那边。
PROMPT_SURFACES: tuple[PromptSurface, ...] = (
    PromptSurface(
        key="vision_system_prompt",
        label="评分系统提示词(女装)",
        tier=Tier.FREE,
        default_ref="app.evaluators.vision_schema:DEFAULT_SYSTEM_PROMPT",
        editable=True,
        ui_reachable=True,
        consumers=(
            "app/evaluators/vision_schema.py:prompt_key_for 选键",
            "评分链路经 prompt_service.get_active_content 读库,改了会生效",
            "frontend/src/pages/PromptsPage.tsx 唯一在页面上可达的一处",
        ),
    ),
    PromptSurface(
        key="vision_system_prompt_men",
        label="评分系统提示词(男装)",
        tier=Tier.FREE,
        default_ref="app.evaluators.vision_schema:DEFAULT_SYSTEM_PROMPT_MEN",
        editable=True,
        # PRD §14.1 / AC-24:a58 起前端可达 —— 提示词页从"写死女装"改成按路由 key
        # (`/prompts/:key`),两份提示词走的是同一段代码。a56 那行 `ui_reachable=False`
        # 写着「解锁条件 = FE-301 落地」,现在落了,所以这里翻转。
        #
        # **翻转的判据是"同一段代码",不是"页面上能看见它"。** 如果详情页里还留着
        # 任何一处写死的女装 key,男装那条路径就是没人走过的代码,而 True 会把
        # 「后端通、前端不可达」换成一个更糟的状态:看起来可达、点进去出错。
        # `test_a58_prompt_center.py` 钉着这一条。
        ui_reachable=True,
        consumers=(
            "app/evaluators/vision_schema.py:prompt_key_for 按受众选它,链路读库,API 可编辑",
            "frontend/src/pages/PromptListPage.tsx 列出,PromptsPage.tsx 按 :key 编辑",
            "代码注释自述「这份提示词尚未校准(阶段 4)」—— 可编辑不等于已校准,"
            "校准是阶段 4 的事,与前端可达性无关",
        ),
    ),
    PromptSurface(
        key="scoring_user_prompt",
        label="评分用户段",
        tier=Tier.TEMPLATE,
        default_ref="app.evaluators.vision_schema:build_user_prompt",
        required_slots=(
            "dimension_lines",
            "check_lines",
            "codes",
            "image_lines",
            "metadata_text",
        ),
        editable=False,
        consumers=(
            "app/evaluators/vision_schema.py:build_user_prompt 运行时拼装,不读库",
            "editable=False 的原因:正文由 f-string 拼装而非模板串,维度/检查项清单"
            "由 fact_consistency.FACT_CHECKS 单点供给 —— 模板化重构触及评分口径,"
            "必须在能跑 AC-34 全量回归的环境里做",
        ),
    ),
    PromptSurface(
        key="scoring_depth_instructions",
        label="评分深度指令",
        tier=Tier.TEMPLATE,
        default_ref="app.evaluators.vision_schema:_DEPTH_INSTRUCTIONS",
        editable=False,
        consumers=(
            "app/evaluators/vision_schema.py:_DEPTH_INSTRUCTIONS,按 EvaluationDepth 取,不读库",
            "editable=False 的原因:与 scoring_user_prompt 同一条 —— 接库待 AC-34 环境",
        ),
    ),
    PromptSurface(
        key="extraction_prompt",
        label="属性识别提示词",
        tier=Tier.TEMPLATE,
        default_ref="app.extractors.schema:build_extraction_prompt",
        editable=False,
        consumers=(
            "app/extractors/schema.py:build_extraction_prompt 运行时拼装,不读库",
            "枚举清单由 core/enums.py 单点供给(schema.py 自述:手抄一份的话加一项时"
            "抄的那份不会跟着变)",
            "editable=False 的原因:同上,模板化待 AC-34 环境;版本已改内容派生(BE-306)",
        ),
    ),
    PromptSurface(
        key="copy_llm_system_prompt",
        label="文案生成系统提示词",
        tier=Tier.FREE,
        default_ref="app.listings.copy_generator:LLM_SYSTEM_PROMPT",
        editable=False,
        consumers=(
            "app/listings/copy_generator.py:LLM 生成器直接引用常量,不读库",
            "正文含 {title_max} 等运行参数槽位,数值由 CopyRules 注入 —— 提示词说 120"
            "而校验按 100 判的事故是这个设计防的",
            "editable=False 的原因:消费链路不读库;接库时槽位校验一并做",
        ),
    ),
    PromptSurface(
        key="repair_prompt_additions",
        label="修复提示词补丁表",
        tier=Tier.MAPPING,
        default_ref="app.evaluators.repair:REPAIR_TABLE",
        key_source="app.evaluators.repair:REPAIR_TABLE",
        # PRD §14.2 决议:MAPPING 本轮只读。改它等于改修复策略,风险高于改措辞。
        editable=False,
        consumers=(
            "app/evaluators/repair.py:REPAIR_TABLE,问题代码 -> 修复动作",
            "editable=False 的原因:PRD §14.2 决议本轮只读 —— "
            "改它等于改修复策略,风险高于改措辞;键封闭性校验推迟",
        ),
    ),
    PromptSurface(
        key="generation_prompt_compose",
        label="出图提示词拼装",
        tier=Tier.TEMPLATE,
        default_ref="app.workflows.generation_plan:compose_prompt",
        editable=False,
        consumers=(
            "app/workflows/generation_plan.py:compose_prompt 把场景/姿势/角度并进提示词,"
            "拼装结果全文落库(generations.prompt)",
            "没有静态默认正文可言 —— default_ref 指向拼装函数本身;版本由"
            "generation_service 对拼装结果取内容哈希(BE-306)",
            "editable=False 的原因:正文是运行时拼装",
        ),
    ),
)

_BY_KEY = {surface.key: surface for surface in PROMPT_SURFACES}
if len(_BY_KEY) != len(PROMPT_SURFACES):  # `python -O` 不剥 raise,剥 assert
    raise AssertionError("注册表里有重复的 key")


def all_keys() -> tuple[str, ...]:
    """全部登记在案的 key,按盘点顺序。lint 的"认识范围"从这里取。"""
    return tuple(surface.key for surface in PROMPT_SURFACES)


def editable_keys() -> tuple[str, ...]:
    """消费链路会读库的 key —— 也就是"改了会生效"的那几个。

    保存端点按它拦:对 editable=False 的 key 落库是制造「保存成功、
    毫无效果」的死路,比拒绝更伤人。
    """
    return tuple(surface.key for surface in PROMPT_SURFACES if surface.editable)


def surface_of(key: str) -> PromptSurface | None:
    return _BY_KEY.get(key)


def resolve_default(surface: PromptSurface) -> object | None:
    """惰性解析 ``default_ref`` 指向的对象。

    返回对象本身(str / dict / callable),不在这里转成文本 —— 「怎么展示」
    是 API 层的事,这里只回答「引用还悬不悬空」。``default_ref`` 为 None
    返回 None。解析失败**抛出**而不是吞掉:一个悬空的引用是登记错误,
    AC-23 的守卫就该在这里红。
    """
    if surface.default_ref is None:
        return None
    module_path, _, attr = surface.default_ref.partition(":")
    if not module_path or not attr:
        raise ValueError(f"default_ref 不是 module:attr 形状:{surface.default_ref!r}")
    module = importlib.import_module(module_path)
    return getattr(module, attr)
