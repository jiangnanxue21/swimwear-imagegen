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
能写，但只有完成消费接线的提示词才能标为 editable。当前登记的 8 处都已接入
当前生效版本：自由正文、带固定槽位的模板、带固定代码键的 JSON 映射分别走各自
的校验与消费路径。只翻布尔值而不接消费方，会得到「保存成功、毫无效果」——
比不给入口更糟。

## 依赖约束

只依赖标准库与 `app.core`。守卫 `test_the_registry_only_depends_on_core`
用 AST 钉着这一条 —— 破了它,`grading-stays-pure` 迟早跟着破。
"""
from __future__ import annotations

import dataclasses
import importlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum, StrEnum


class Tier(StrEnum):
    """三层模型(PRD §10)。统一管理的前提是承认它们形态不同。"""

    FREE = "FREE"          # 全文 textarea;校验 = 现有 lint + 防注入段存在性
    TEMPLATE = "TEMPLATE"  # 措辞可改,必需槽位一个都不能少
    MAPPING = "MAPPING"    # 键值映射;正文是 JSON,键闭包由 key_source 校验


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
    #: 提示词中心展示正文时使用的引用。多数项与 default_ref 相同；运行时构造器
    #: 需要参数的项指向一个无参的代表性预览函数，避免页面只能展示 Python 源码。
    preview_ref: str | None = None
    #: 代表性预览不等于每次调用的最终正文，这句话必须随正文一起展示。
    preview_note: str | None = None
    #: 只有评分链路把调用逐次写进 evaluation_attempts；可编辑不等于有这份统计。
    usage_stats: bool = False
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
        usage_stats=True,
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
        usage_stats=True,
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
        default_ref="app.evaluators.vision_schema:SCORING_USER_PROMPT_TEMPLATE",
        preview_ref="app.evaluators.vision_schema:preview_user_prompt",
        preview_note=(
            "代表性预览：FULL 深度、1 张参考图和示例商品元数据；"
            "实际调用会按评分深度、图片数和商品数据动态展开。"
        ),
        required_slots=(
            "depth_instruction",
            "dimension_lines",
            "check_lines",
            "codes",
            "image_lines",
            "metadata_text",
        ),
        editable=True,
        consumers=(
            "evaluation_service.py 每轮读取 prompt_templates 当前生效模板，"
            "经 rule_set 注入视觉评分器",
            "build_user_prompt 只替换登记在案的槽位；商品 JSON 中的花括号不会被误解析",
            "维度/检查项清单仍由代码事实源动态展开，默认模板展开结果保持原评分口径",
        ),
    ),
    PromptSurface(
        key="scoring_depth_instructions",
        label="评分深度指令",
        tier=Tier.MAPPING,
        default_ref="app.evaluators.vision_schema:SCORING_DEPTH_INSTRUCTIONS_DEFAULT",
        key_source="app.evaluators.vision_schema:_DEPTH_INSTRUCTIONS",
        preview_note="完整展示 FULL / QUICK 两档指令映射；运行时按评分深度选择其中一段。",
        editable=True,
        consumers=(
            "evaluation_service.py 每轮读取 JSON 版本并在任何付费评分调用前完成校验",
            "vision_schema.py:build_user_prompt 按 EvaluationDepth 选择 FULL / QUICK 正文",
            "MAPPING 体检校验合法 JSON、缺失档位和意外新增键",
        ),
    ),
    PromptSurface(
        key="extraction_prompt",
        label="属性识别提示词",
        tier=Tier.TEMPLATE,
        default_ref="app.extractors.schema:EXTRACTION_PROMPT_TEMPLATE",
        preview_ref="app.extractors.schema:build_extraction_prompt",
        required_slots=("field_lines",),
        preview_note="代表性预览：按默认受众展开全部可识别字段；实际调用会按目标字段和商品受众缩小清单。",
        editable=True,
        consumers=(
            "attributes/service.py 每次识别前读取 prompt_templates 当前生效模板,"
            "注入 VisionAttributeExtractor",
            "枚举清单由 core/enums.py 单点供给(schema.py 自述:手抄一份的话加一项时"
            "抄的那份不会跟着变)",
            "{field_lines} 由目标字段与受众动态展开;版本按模板内容派生(BE-306)",
        ),
    ),
    PromptSurface(
        key="copy_llm_system_prompt",
        label="文案生成系统提示词",
        tier=Tier.TEMPLATE,
        default_ref="app.listings.copy_generator:LLM_SYSTEM_PROMPT",
        required_slots=(
            "title_max",
            "description_max",
            "bullet_min",
            "bullet_max",
            "bullet_item_max",
        ),
        preview_note="系统 Prompt 原文；{title_max} 等槽位会在调用时由当前文案规则填入。",
        editable=True,
        consumers=(
            "workbench/service.py 每次生成前读取 prompt_templates 当前生效版本,"
            "注入 LLMCopyGenerator",
            "正文含 {title_max} 等运行参数槽位,数值由 CopyRules 注入 —— 提示词说 120"
            "而校验按 100 判的事故是这个设计防的",
            "TEMPLATE 层保存前会逐个检查必需槽位",
        ),
    ),
    PromptSurface(
        key="repair_prompt_additions",
        label="修复提示词补丁表",
        tier=Tier.MAPPING,
        default_ref="app.evaluators.repair:REPAIR_TABLE_DEFAULT",
        key_source="app.evaluators.repair:REPAIR_TABLE",
        preview_note=(
            "完整修复策略 JSON；键是问题代码，值中的 prompt_additions / "
            "negative_prompt_additions 会进入下一轮出图提示词。"
        ),
        editable=True,
        consumers=(
            "evaluation_service.py 每轮读取并解析当前生效 JSON，评分完成后的决策共用该快照",
            "decision.py 将解析后的 RepairAction 映射传给 build_repair_plan",
            "缺默认问题代码、字段类型错误或未知字段会在付费评分前失败关闭",
        ),
    ),
    PromptSurface(
        key="generation_prompt_compose",
        label="出图提示词拼装",
        tier=Tier.TEMPLATE,
        default_ref="app.workflows.generation_plan:GENERATION_PROMPT_TEMPLATE",
        required_slots=("base_prompt", "scene_segment", "pose_segment", "angles_segment"),
        preview_ref="app.workflows.generation_plan:preview_generation_prompt",
        preview_note=(
            "代表性预览：商品一致性基础 Prompt + 默认影棚 + 默认站姿 + 正反面；"
            "实际正文会按任务、场景、姿势和角度动态拼装。"
        ),
        editable=True,
        consumers=(
            "generation_service.py 应用方案时读取 prompt_templates 当前生效模板,"
            "由 compose_prompt 填入基础正文/场景/姿势/角度四个槽位",
            "拼装结果全文落库(generations.prompt),版本继续按最终正文内容派生",
            "TEMPLATE 层保存前会逐个检查必需槽位",
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
    return _resolve_ref(surface.default_ref)


def resolve_preview(surface: PromptSurface) -> object | None:
    """解析一份能直接给人看的正文；无参构造器会在这里执行。"""
    value = _resolve_ref(surface.preview_ref or surface.default_ref)
    return value() if callable(value) else value


def preview_text(surface: PromptSurface) -> str:
    """把字符串、枚举映射和 dataclass 策略表统一变成人能读的正文。"""

    def plain(item: object) -> object:
        if dataclasses.is_dataclass(item) and not isinstance(item, type):
            return {
                one.name: plain(getattr(item, one.name))
                for one in dataclasses.fields(item)
            }
        if isinstance(item, Enum):
            return item.value
        if isinstance(item, Mapping):
            return {str(plain(key)): plain(val) for key, val in item.items()}
        if isinstance(item, (tuple, list)):
            return [plain(one) for one in item]
        return item

    value = resolve_preview(surface)
    if isinstance(value, str):
        return value
    return json.dumps(plain(value), ensure_ascii=False, indent=2, default=str)


def mapping_keys(surface: PromptSurface) -> tuple[str, ...]:
    """MAPPING 层必须保留的代码键；从领域事实源惰性派生，不在注册表手抄。"""
    value = _resolve_ref(surface.key_source)
    if not isinstance(value, Mapping):
        return ()
    return tuple(str(plain.value if isinstance(plain, Enum) else plain) for plain in value)


def _resolve_ref(ref: str | None) -> object | None:
    if ref is None:
        return None
    module_path, _, attr = ref.partition(":")
    if not module_path or not attr:
        raise ValueError(f"提示词引用不是 module:attr 形状:{ref!r}")
    module = importlib.import_module(module_path)
    return getattr(module, attr)
