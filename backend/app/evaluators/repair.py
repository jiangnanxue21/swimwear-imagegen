"""问题代码 -> 确定性修复参数(需求第十二章 B 档)。

需求原文是"根据问题代码生成确定性的修复参数"。关键词是**确定性**:
同一组问题必须永远得到同一份修复计划,否则"这一轮为什么这么跑"就没法复盘。
因此这里是纯函数,不读时间、不读随机数、不查库,输出前对所有集合排序。

修复手段限定在四类(需求第十二章):
    更换 seed / 更换模特模板 / 更换提示词模板 / 更换输出参数
外加 C 档才允许的"必要时切换 Provider"。
大模型在这里没有任何自由裁量空间 —— Provider 与参数的切换只能由规则触发(需求第七章)。
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.core.enums import Grade, HardFailCode, RegenerationReason

REPAIR_PROMPT_ADDITIONS_KEY = "repair_prompt_additions"


@dataclass(frozen=True)
class RepairAction:
    """单条问题对应的修复动作。"""

    change_seed: bool = True
    change_model_template: bool = False
    switch_provider: bool = False
    prompt_additions: tuple[str, ...] = ()
    negative_prompt_additions: tuple[str, ...] = ()
    provider_param_overrides: tuple[tuple[str, Any], ...] = ()
    note: str = ""


#: 问题代码 -> 修复动作。键覆盖需求第十一章全部硬错误,外加几个常见轻度问题。
#: 新增问题代码时必须在这里登记,否则只会得到"换 seed"这个兜底动作。
REPAIR_TABLE: dict[str, RepairAction] = {
    # ---- 商品身份类:换 seed 意义不大,主要靠提示词锁死商品特征 ----
    HardFailCode.GARMENT_WRONG: RepairAction(
        change_seed=True,
        switch_provider=True,
        prompt_additions=("严格复现参考图中的同一件泳衣,不得替换款式",),
        note="生成了另一件衣服,换 seed 无效时需要换 Provider",
    ),
    HardFailCode.SKU_IMAGE_MISMATCH: RepairAction(
        change_seed=True,
        switch_provider=True,
        prompt_additions=("以商品原图为唯一依据",),
        note="与 SKU 不符",
    ),
    HardFailCode.GARMENT_PART_MISSING: RepairAction(
        prompt_additions=("完整呈现泳衣的全部部件,不得省略",),
        negative_prompt_additions=("缺失部件", "残缺服装"),
        note="部件缺失",
    ),
    HardFailCode.GARMENT_PART_ADDED: RepairAction(
        prompt_additions=("不得增加参考图中不存在的装饰、系带或配件",),
        negative_prompt_additions=("多余装饰", "额外系带"),
        note="凭空多出部件",
    ),
    # ---- 结构类:换 seed + 提示词强调,通常一两轮能修好 ----
    HardFailCode.STRAP_CHANGED: RepairAction(
        prompt_additions=("保持肩带类型与走向和商品原图完全一致",),
        negative_prompt_additions=("改变肩带",),
        note="肩带被改",
    ),
    HardFailCode.NECKLINE_CHANGED: RepairAction(
        prompt_additions=("保持领口形状与深度和商品原图完全一致",),
        negative_prompt_additions=("改变领口",),
        note="领口被改",
    ),
    HardFailCode.BACK_STYLE_CHANGED: RepairAction(
        prompt_additions=("保持背部样式和商品原图完全一致",),
        negative_prompt_additions=("改变背部样式",),
        note="背部样式被改",
    ),
    HardFailCode.COVERAGE_CHANGED: RepairAction(
        prompt_additions=("保持覆盖面积与剪裁比例不变",),
        negative_prompt_additions=("改变覆盖面积",),
        note="覆盖度被改",
    ),
    # ---- 男装结构类(PRD v2 §14.4):腰头和裤长是男装最常被改的两处 ----
    HardFailCode.WAISTBAND_CHANGED: RepairAction(
        prompt_additions=("保持腰头形制(松紧/抽绳/纽扣)与商品原图完全一致,抽绳位置不变",),
        negative_prompt_additions=("改变腰头", "抽绳消失"),
        note="腰头形制被改",
    ),
    HardFailCode.INSEAM_CHANGED: RepairAction(
        prompt_additions=("保持裤长与商品原图完全一致,不得改成其它长度",),
        negative_prompt_additions=("改变裤长",),
        note="裤长被改到另一个长度区间",
    ),
    HardFailCode.LINER_CHANGED: RepairAction(
        prompt_additions=("不得增删或外露内衬结构",),
        negative_prompt_additions=("外露内衬", "增加内衬"),
        note="内衬被增删或改变",
    ),
    # ---- 受众错配(§12.4):**方向与 GARMENT_WRONG 相反** ----
    # GARMENT_WRONG 是商品换了,该换 seed / 换 Provider;
    # 这里商品往往是对的,错的是穿的人 —— 修复动作是换模特,
    # 并让人核对商品受众是否录错。两者混用会让重生策略走错路。
    HardFailCode.AUDIENCE_MISMATCH: RepairAction(
        change_seed=False,
        change_model_template=True,
        note="穿着者呈现与商品受众不符:换受众匹配的模特,并核对商品受众是否录错",
    ),
    # ---- 渲染缺陷(A45-batch14-20 / §6.5 事实一致性清单末两项)----
    #
    # 方向与 GARMENT_WRONG 也相反,而且比受众错配那条更容易走错:
    # 这两条里**商品是对的、模特也是对的**,坏的是融合与几何。
    # 换 seed 有机会(采样噪声不同),但真正管用的是降低融合强度 /
    # 换一个姿势更简单的模板 —— 一件左右不对称的正确泳衣如果只是反复换
    # seed 重生,每一轮都是真实付费调用而命中率不变。
    HardFailCode.ASYMMETRY_INTRODUCED: RepairAction(
        change_seed=True,
        change_model_template=True,
        prompt_additions=("严格保持左右对称的部件对称,不得单边化",),
        negative_prompt_additions=("不对称", "单边肩带", "两侧不等宽"),
        note="对称部件被生成成不对称:换 seed 并改用姿势更正的模特模板",
    ),
    HardFailCode.FUSION_DEFECT: RepairAction(
        change_seed=True,
        change_model_template=True,
        prompt_additions=("泳衣边缘与皮肤、背景之间保持清晰分界",),
        negative_prompt_additions=("边缘熔化", "衣物与皮肤粘连", "图案穿过身体"),
        note="融合缺陷(边界熔化/粘连):换 seed 并改用遮挡更少的姿势",
    ),
    # ---- 颜色与图案:提高输出分辨率对图案还原帮助最大 ----
    HardFailCode.COLOR_VARIANT_WRONG: RepairAction(
        prompt_additions=("严格使用商品原图的颜色,不得做色彩风格化",),
        negative_prompt_additions=("改变颜色", "色偏"),
        note="颜色变体错误",
    ),
    HardFailCode.PATTERN_DISTORTED: RepairAction(
        prompt_additions=("保持印花图案的比例、方向与连续性",),
        negative_prompt_additions=("图案扭曲", "花纹错乱"),
        provider_param_overrides=(("upscale_pattern", True),),
        note="图案扭曲,提高图案区域采样",
    ),
    HardFailCode.LOGO_OR_TEXT_CHANGED: RepairAction(
        prompt_additions=("完整保留商品上的品牌标识与文字",),
        negative_prompt_additions=("篡改文字", "错误 logo"),
        note="logo 或文字被改",
    ),
    # ---- 人体与遮挡:换 seed 帮助有限,应当换模特模板 ----
    HardFailCode.ANATOMY_HARD_ERROR: RepairAction(
        change_model_template=True,
        negative_prompt_additions=("多余肢体", "畸形手部", "扭曲人体"),
        note="人体结构错误,换模特模板",
    ),
    HardFailCode.FACE_HARD_ERROR: RepairAction(
        change_model_template=True,
        negative_prompt_additions=("面部畸形", "五官错位"),
        note="面部错误,换模特模板",
    ),
    HardFailCode.PRODUCT_SEVERELY_OCCLUDED: RepairAction(
        change_model_template=True,
        prompt_additions=("正面站姿,双臂自然下垂,不遮挡商品主体",),
        note="商品被严重遮挡,换姿势",
    ),
    HardFailCode.IMAGE_CORRUPTED: RepairAction(
        note="图像损坏,单纯重出一张",
    ),
    # ---- 轻度问题:只调提示词或输出参数 ----
    "LIGHTING_FLAT": RepairAction(
        prompt_additions=("柔和影棚布光,保留材质高光",),
        note="光照平淡",
    ),
    "BACKGROUND_NOISY": RepairAction(
        prompt_additions=("纯净浅灰影棚背景",),
        negative_prompt_additions=("杂乱背景",),
        note="背景杂乱",
    ),
    "COMPOSITION_OFF": RepairAction(
        prompt_additions=("居中构图,完整包含全身",),
        provider_param_overrides=(("framing", "full_body_centered"),),
        note="构图偏移",
    ),
    "SKIN_PLASTIC": RepairAction(
        prompt_additions=("自然皮肤质感,保留毛孔细节",),
        negative_prompt_additions=("塑料感皮肤", "过度磨皮"),
        note="皮肤塑料感",
    ),
}


def _action_payload(action: RepairAction) -> dict[str, Any]:
    return {
        "change_seed": action.change_seed,
        "change_model_template": action.change_model_template,
        "switch_provider": action.switch_provider,
        "prompt_additions": list(action.prompt_additions),
        "negative_prompt_additions": list(action.negative_prompt_additions),
        "provider_param_overrides": dict(action.provider_param_overrides),
        "note": action.note,
    }


# 提示词中心编辑的是 JSON，而纯决策层消费的是 RepairAction。默认值从运行时表
# 派生，避免为了 UI 手抄第二份很快就漂掉的补丁表。
REPAIR_TABLE_DEFAULT = json.dumps(
    {str(code): _action_payload(action) for code, action in REPAIR_TABLE.items()},
    ensure_ascii=False,
    indent=2,
)


def parse_repair_table(content: str) -> dict[str, RepairAction]:
    """解析并校验版本库中的补丁表；默认键不允许被悄悄删掉。"""
    try:
        raw = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise ValueError("修复提示词补丁表必须是合法 JSON 对象") from exc
    if not isinstance(raw, dict):
        raise ValueError("修复提示词补丁表必须是 JSON 对象")

    required = {str(code) for code in REPAIR_TABLE}
    missing = sorted(required - set(raw))
    if missing:
        raise ValueError(f"修复提示词补丁表缺少问题代码:{'、'.join(missing)}")

    def string_list(code: str, field_name: str, value: object) -> tuple[str, ...]:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"{code}.{field_name} 必须是字符串数组")
        return tuple(value)

    parsed: dict[str, RepairAction] = {}
    allowed_fields = set(RepairAction.__dataclass_fields__)
    for code, payload in raw.items():
        if not isinstance(code, str) or not code.strip() or not isinstance(payload, dict):
            raise ValueError("补丁表的键必须是非空问题代码，值必须是 JSON 对象")
        unknown = sorted(set(payload) - allowed_fields)
        if unknown:
            raise ValueError(f"{code} 含未知字段:{'、'.join(unknown)}")

        booleans: dict[str, bool] = {}
        for field_name, default in (
            ("change_seed", True),
            ("change_model_template", False),
            ("switch_provider", False),
        ):
            value = payload.get(field_name, default)
            if not isinstance(value, bool):
                raise ValueError(f"{code}.{field_name} 必须是布尔值")
            booleans[field_name] = value

        overrides = payload.get("provider_param_overrides", {})
        if not isinstance(overrides, dict):
            raise ValueError(f"{code}.provider_param_overrides 必须是 JSON 对象")
        note = payload.get("note", "")
        if not isinstance(note, str):
            raise ValueError(f"{code}.note 必须是字符串")
        parsed[code] = RepairAction(
            **booleans,
            prompt_additions=string_list(
                code, "prompt_additions", payload.get("prompt_additions", [])
            ),
            negative_prompt_additions=string_list(
                code,
                "negative_prompt_additions",
                payload.get("negative_prompt_additions", []),
            ),
            provider_param_overrides=tuple(overrides.items()),
            note=note,
        )
    return parsed

#: 表里没登记的问题代码走这个兜底:换 seed 重出一张
FALLBACK_ACTION = RepairAction(note="未登记的问题代码,仅更换 seed 重试")


@dataclass
class RepairPlan:
    """一轮重生的完整修复计划,会原样写进 GenerationAttempt.repair_strategy。"""

    reason: RegenerationReason
    grade: Grade | None = None
    change_seed: bool = True
    change_model_template: bool = False
    switch_provider: bool = False
    prompt_additions: list[str] = field(default_factory=list)
    negative_prompt_additions: list[str] = field(default_factory=list)
    provider_param_overrides: dict[str, Any] = field(default_factory=dict)
    triggered_by: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason.value,
            "grade": self.grade.value if self.grade else None,
            "change_seed": self.change_seed,
            "change_model_template": self.change_model_template,
            "switch_provider": self.switch_provider,
            "prompt_additions": list(self.prompt_additions),
            "negative_prompt_additions": list(self.negative_prompt_additions),
            "provider_param_overrides": dict(self.provider_param_overrides),
            "triggered_by": list(self.triggered_by),
            "notes": list(self.notes),
        }


def build_repair_plan(
    *,
    grade: Grade,
    problem_codes: list[str],
    hard_fail_codes: list[str] | None = None,
    allow_provider_switch: bool = True,
    repair_table: Mapping[str, RepairAction] | None = None,
) -> RepairPlan:
    """按问题代码拼出确定性的修复计划。

    输入的代码顺序不影响输出:内部先去重再排序。
    """
    codes = sorted({*(problem_codes or []), *(hard_fail_codes or [])})

    reason = (
        RegenerationReason.HARD_FAIL
        if hard_fail_codes
        else RegenerationReason.LOW_SCORE
    )
    plan = RepairPlan(reason=reason, grade=grade, triggered_by=codes)

    prompts: list[str] = []
    negatives: list[str] = []
    overrides: dict[str, Any] = {}
    notes: list[str] = []

    active_table = repair_table if repair_table is not None else REPAIR_TABLE
    for code in codes:
        action = active_table.get(code, FALLBACK_ACTION)
        plan.change_seed = plan.change_seed or action.change_seed
        plan.change_model_template = plan.change_model_template or action.change_model_template
        if action.switch_provider:
            plan.switch_provider = True
        prompts.extend(p for p in action.prompt_additions if p not in prompts)
        negatives.extend(n for n in action.negative_prompt_additions if n not in negatives)
        overrides.update(dict(action.provider_param_overrides))
        if action.note and action.note not in notes:
            notes.append(f"{code}: {action.note}")

    # C 档默认允许换模特模板 —— 换 seed 已经证明不够用了(需求第十二章)
    if grade is Grade.C:
        plan.change_model_template = True
        notes.append("C 档:淘汰当前候选,更换 seed 与模特模板")
    # D 档在轮次未耗尽时同样继续生成,策略与 C 档一致但更激进
    if grade is Grade.D:
        plan.change_model_template = True
        if allow_provider_switch:
            plan.switch_provider = True
        notes.append("D 档:当前候选淘汰,若仍有轮次则换模特模板并尝试换 Provider")

    if not allow_provider_switch and plan.switch_provider:
        plan.switch_provider = False
        notes.append("规则集禁用了 Provider 切换,本轮仅在当前 Provider 内重试")

    if not codes and grade is Grade.B:
        notes.append("B 档但无具体问题代码:仅更换 seed 重出")

    plan.prompt_additions = prompts
    plan.negative_prompt_additions = negatives
    plan.provider_param_overrides = overrides
    plan.notes = notes
    return plan


def apply_prompt_additions(base: str | None, additions: list[str]) -> str | None:
    """把修复提示词并入原始提示词。去重且保持顺序,避免多轮累积成一堵墙。"""
    if not additions:
        return base
    parts = [p.strip() for p in (base or "").split(";") if p.strip()]
    for addition in additions:
        if addition not in parts:
            parts.append(addition)
    return "; ".join(parts) if parts else None
