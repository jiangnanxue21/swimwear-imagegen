"""§4.5 过期(stale)触发矩阵 —— 把任务书里的表格变成可穷举的规格。**零依赖纯数据。**

任务书 §4.5 用一张 6 行 × 3 列的表回答「哪些变化触发哪些对象过期」,
而 §5.3「过期草稿漏提示 0 次」的判定方法就是**按这张矩阵逐行验证**。
问题在于:表格活在 docx 里,代码各处(`stale.py`、`image_set_rules.needs_downgrade`、
`copy_service.approve_copy`、`workbench/service._copy_is_stale`)各自实现了自己
负责的格子,没有任何一处把 18 个格子当成一个**封闭集合**来核对。
少实现一格不会有任何测试变红 —— 它只会在运营导出了一份旧事实时被发现。

本模块就是那张表本身:

    - 6 个 `ChangeSource` × 3 个 `Target`,`MATRIX` 恰好 18 格,不多不少
    - 每一格的 `effect` 抄任务书原文(过期 / 不过期 / 重置为待批准 /
      需重新校验 / 全部草稿过期)
    - 每个有效应的格子用 `mechanism` 点名代码里负责它的那个函数 ——
      `tests/pure/test_stale_matrix.py` 逐格验证机制真的存在且行为符合

改这张表之前先改任务书;改代码里的过期行为之前先改这张表。
两边不一致时,测试会指出是哪一格。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from itertools import product as _cartesian


class ChangeSource(StrEnum):
    """§4.5 的六个变更源(矩阵的行)。"""

    #: 已确认属性被修改(保存修改时立即)
    CONFIRMED_ATTRIBUTE_MODIFIED = "CONFIRMED_ATTRIBUTE_MODIFIED"
    #: 素材被替换或隔离(素材状态变更时)
    ASSET_REPLACED_OR_QUARANTINED = "ASSET_REPLACED_OR_QUARANTINED"
    #: 已批准图片集被重新编排(保存编排时)
    APPROVED_IMAGE_SET_REARRANGED = "APPROVED_IMAGE_SET_REARRANGED"
    #: 已批准文案被编辑(保存编辑时)
    APPROVED_COPY_EDITED = "APPROVED_COPY_EDITED"
    #: 导出模板版本变更(模板配置更新时)
    EXPORT_SPEC_VERSION_CHANGED = "EXPORT_SPEC_VERSION_CHANGED"
    #: 文案规则/禁词库变更(规则配置更新时)
    COPY_RULES_CHANGED = "COPY_RULES_CHANGED"
    # ---- PRD v3.1 §8.1 新增的变更源。**一次只补有机制可点名的那几行** ----
    #
    # §8.1 一共要求新增六个。这一批(A45-batch14-20 / 阶段 4)只补两个,
    # 因为另外四个点名的机制要等阶段 5 的颜色结构化字段与价格/库存快照 ——
    # 先画格子后补实现的话,`test_every_effective_cell_names_a_mechanism_that_exists_in_code`
    # 会红,而让它变绿最省事的做法是随便指一个函数,于是矩阵从"封闭集合"
    # 退化成"一张看起来很全的表"。欠账清单在
    # `tests/pure/test_a45_batch14_9_scope_fingerprint.py`,补一行划一条。
    #: 更换生成方案(plan_fingerprint 变化)
    GENERATION_PLAN_CHANGED = "GENERATION_PLAN_CHANGED"
    #: 替换批准图片集(新一版被批准,旧的那一版不再是当前版)
    APPROVED_IMAGE_SET_REPLACED = "APPROVED_IMAGE_SET_REPLACED"


class Target(StrEnum):
    """矩阵的四列。

    `FACTS` 是 PRD v3.1 §8.1 加的那一列(「目标新增 FACTS 列」)。加它之前,
    这张自称封闭的矩阵**回答不了「商品事实什么时候过期」** —— 而那是
    D1 那条风险的落点:指纹作用域过宽时,补一张 A 色图会 stale 掉 B 色事实,
    而矩阵里连「事实会过期」这件事都没有格子。
    """

    FACTS = "FACTS"
    IMAGE_SET = "IMAGE_SET"
    COPY = "COPY"
    DRAFT = "DRAFT"


class Effect(StrEnum):
    """格子的取值,措辞与任务书 §4.5 / PRD §8.1 表格一致。"""

    NOT_AFFECTED = "不过期"
    #: PRD §8.1「修改确认的共享结构事实 → 新版本」。它和 STALE 不是一回事:
    #: 过期是「这份东西还在,但不能再采信」,新版本是「换了一份」——
    #: 前者要人去重新确认,后者已经确认过了。合并这两个取值会让
    #: 「改了一个事实之后该做什么」这个问题失去答案。
    NEW_VERSION = "新版本"
    STALE = "过期"
    RESET_TO_PENDING = "重置为待批准"
    REVALIDATE = "需重新校验"
    ALL_DRAFTS_STALE = "全部草稿过期"


#: 触发时机(矩阵最后一列),按行。
TRIGGERS: dict[ChangeSource, str] = {
    ChangeSource.CONFIRMED_ATTRIBUTE_MODIFIED: "保存修改时立即",
    ChangeSource.ASSET_REPLACED_OR_QUARANTINED: "素材状态变更时",
    ChangeSource.APPROVED_IMAGE_SET_REARRANGED: "保存编排时",
    ChangeSource.APPROVED_COPY_EDITED: "保存编辑时",
    ChangeSource.EXPORT_SPEC_VERSION_CHANGED: "模板配置更新时",
    ChangeSource.COPY_RULES_CHANGED: "规则配置更新时",
    ChangeSource.GENERATION_PLAN_CHANGED: "方案保存并启用时",
    ChangeSource.APPROVED_IMAGE_SET_REPLACED: "新版本被批准时",
}


@dataclass(frozen=True)
class MatrixCell:
    source: ChangeSource
    target: Target
    effect: Effect
    #: 代码里负责这一格的机制,`模块点路径[:限定说明]`。
    #: `effect == NOT_AFFECTED` 的格子留空 —— 「不做某事」没有机制可点名,
    #: 但有对应的**反向测试**钉住(见 test_stale_matrix)。
    mechanism: str = ""
    #: 任务书措辞与实现落点不完全同名时,在这里说清对应关系。
    note: str = ""


def _cell(source, target, effect, mechanism="", note="") -> MatrixCell:
    return MatrixCell(source, target, effect, mechanism, note)


MATRIX: tuple[MatrixCell, ...] = (
    # ---- 第 1 行:已确认属性被修改 ------------------------------------
    _cell(
        ChangeSource.CONFIRMED_ATTRIBUTE_MODIFIED,
        Target.FACTS,
        Effect.NEW_VERSION,
        mechanism="app.attributes.service.confirm",
        note="改一个已确认事实 = 落一版新的 is_current 值(§8.2 版本链),"
        "不是把旧值标成过期 —— 旧值留在链上供追溯",
    ),
    _cell(
        ChangeSource.CONFIRMED_ATTRIBUTE_MODIFIED,
        Target.IMAGE_SET,
        Effect.NOT_AFFECTED,
        note="image_set_rules 的判定输入(SetView/ItemView)不含任何属性字段",
    ),
    _cell(
        ChangeSource.CONFIRMED_ATTRIBUTE_MODIFIED,
        Target.COPY,
        Effect.STALE,
        mechanism="app.workbench.stale.copy_attrs_stale",
        note="已批准文案的内容计划快照与当前已确认属性版本集合不一致 → "
        "CopyFacts.stale → 流程判定 COPY_STALE(重新生成或人工复核)",
    ),
    _cell(
        ChangeSource.CONFIRMED_ATTRIBUTE_MODIFIED,
        Target.DRAFT,
        Effect.STALE,
        mechanism="app.workbench.stale.diff_components:attributes",
        note="草稿组件快照的 attrs 与当前不一致,提示点名到字段",
    ),
    # ---- 第 2 行:素材被替换或隔离 ------------------------------------
    _cell(
        ChangeSource.ASSET_REPLACED_OR_QUARANTINED,
        Target.FACTS,
        Effect.STALE,
        mechanism="app.attributes.scope_fingerprint.changed_scopes",
        note="**按作用域细化(§5.3 修复 D1)**:变的是哪些作用域由 "
        "changed_scopes 算出来。给颜色 A 补/换/隔离样品 → 共享与 A 色事实 "
        "stale,B 色事实不受影响(AC-21);改素材颜色归属 A→B → 共享、A、B "
        "三个作用域同时变。事实是否过期由 facts_stale 拿存下的 "
        "input_fingerprint 与当前作用域指纹比,**不落 STALE 列**(§8.2)",
    ),
    _cell(
        ChangeSource.ASSET_REPLACED_OR_QUARANTINED,
        Target.IMAGE_SET,
        Effect.STALE,
        mechanism="app.listings.image_set_rules.needs_downgrade",
        note="任务书的「过期」在图片集上的落点是自动降级:已批准集引用了"
        "不可用素材 → 状态回 PENDING_REVIEW + ImageSetFacts.downgraded"
        " → 流程判定 IMAGESET_DOWNGRADED(§7.3);不降级的话,一张已"
        "判定不合规的图会继续被发布",
    ),
    _cell(
        ChangeSource.ASSET_REPLACED_OR_QUARANTINED,
        Target.COPY,
        Effect.NOT_AFFECTED,
        note="文案过期判定(copy_attrs_stale)不接收任何素材状态输入",
    ),
    _cell(
        ChangeSource.ASSET_REPLACED_OR_QUARANTINED,
        Target.DRAFT,
        Effect.STALE,
        mechanism="app.workbench.stale.diff_components:image_set_revoked",
        note="降级后当前已无可用的已批准图片集,refresh_draft 以 "
        "image_set=None 对比 → 「不再是批准状态」话术",
    ),
    # ---- 第 3 行:已批准图片集被重新编排 ------------------------------
    _cell(
        ChangeSource.APPROVED_IMAGE_SET_REARRANGED,
        Target.FACTS,
        Effect.NOT_AFFECTED,
        note="事实的 input_fingerprint 只由证据素材集合决定,"
        "图片集是事实的下游,不进指纹",
    ),
    _cell(
        ChangeSource.APPROVED_IMAGE_SET_REARRANGED,
        Target.IMAGE_SET,
        Effect.RESET_TO_PENDING,
        mechanism="app.listings.image_set_service.derive",
        note="已批准集不可原地修改:derive() 生成 version+1 的新集,"
        "初始状态 DRAFT,须重新校验并批准 —— 「重置为待批准」由"
        "版本化不可变实现,APPROVED 只能由 approve() 赋予",
    ),
    _cell(
        ChangeSource.APPROVED_IMAGE_SET_REARRANGED,
        Target.COPY,
        Effect.NOT_AFFECTED,
        note="文案过期判定不接收图片集输入",
    ),
    _cell(
        ChangeSource.APPROVED_IMAGE_SET_REARRANGED,
        Target.DRAFT,
        Effect.STALE,
        mechanism="app.workbench.stale.diff_components:image_set_version",
        note="草稿快照记录的 (id, version) 与当前批准版不一致",
    ),
    # ---- 第 4 行:已批准文案被编辑 ------------------------------------
    _cell(
        ChangeSource.APPROVED_COPY_EDITED,
        Target.FACTS,
        Effect.NOT_AFFECTED,
        note="文案是事实的下游,不进指纹",
    ),
    _cell(
        ChangeSource.APPROVED_COPY_EDITED,
        Target.IMAGE_SET,
        Effect.NOT_AFFECTED,
        note="图片集判定不接收文案输入",
    ),
    _cell(
        ChangeSource.APPROVED_COPY_EDITED,
        Target.COPY,
        Effect.RESET_TO_PENDING,
        mechanism="app.listings.copy_service.save_copy",
        note="编辑=落一版新文案,状态只会是 VALIDATED 或 REJECTED,"
        "须重新走 approve_copy —— APPROVED 只能由 approve_copy 赋予",
    ),
    _cell(
        ChangeSource.APPROVED_COPY_EDITED,
        Target.DRAFT,
        Effect.STALE,
        mechanism="app.workbench.stale.diff_components:copies",
        note="草稿快照记录的 {locale: (id, version)} 与当前批准版不一致,"
        "提示按 locale 点名",
    ),
    # ---- 第 5 行:导出模板版本变更 ------------------------------------
    _cell(
        ChangeSource.EXPORT_SPEC_VERSION_CHANGED,
        Target.FACTS,
        Effect.NOT_AFFECTED,
        note="导出模板决定事实怎么映射到渠道字段,不决定事实本身是什么",
    ),
    _cell(
        ChangeSource.EXPORT_SPEC_VERSION_CHANGED,
        Target.IMAGE_SET,
        Effect.NOT_AFFECTED,
        note="图片集判定不接收模板版本输入",
    ),
    _cell(
        ChangeSource.EXPORT_SPEC_VERSION_CHANGED,
        Target.COPY,
        Effect.NOT_AFFECTED,
        note="文案过期判定不接收导出模板版本输入(文案自己的 spec_version"
        " 指文案规则版本,是第 6 行的事,两者不是一个东西)",
    ),
    _cell(
        ChangeSource.EXPORT_SPEC_VERSION_CHANGED,
        Target.DRAFT,
        Effect.ALL_DRAFTS_STALE,
        mechanism="app.workbench.stale.diff_components:spec_version",
        note="spec_version / mapping_version 进每一份草稿的组件快照与指纹,"
        "版本一换,逐份对比时每份都过期 —— 「全部」由此成立",
    ),
    # ---- 第 6 行:文案规则/禁词库变更 ---------------------------------
    _cell(
        ChangeSource.COPY_RULES_CHANGED,
        Target.FACTS,
        Effect.NOT_AFFECTED,
        note="禁词管的是怎么说,不管这件泳衣是什么颜色",
    ),
    _cell(
        ChangeSource.COPY_RULES_CHANGED,
        Target.IMAGE_SET,
        Effect.NOT_AFFECTED,
        note="图片集判定不接收文案规则输入",
    ),
    _cell(
        ChangeSource.COPY_RULES_CHANGED,
        Target.COPY,
        Effect.REVALIDATE,
        mechanism="app.listings.copy_service.approve_copy",
        note="文案落库时记下 spec_version;approve_copy 发现与当前规则版本"
        "不一致就先 revalidate 再决定能否批准 —— 新禁词在这一步拦下",
    ),
    _cell(
        ChangeSource.COPY_RULES_CHANGED,
        Target.DRAFT,
        Effect.NOT_AFFECTED,
        note="草稿组件快照没有文案规则版本这一项;规则变化经由文案的"
        "重新校验/重新批准(新版本)间接传导,那时命中第 4 行",
    ),
    # ---- 第 7 行:更换生成方案(§8.1 新增) ----------------------------
    _cell(
        ChangeSource.GENERATION_PLAN_CHANGED,
        Target.FACTS,
        Effect.NOT_AFFECTED,
        note="方案决定这件衣服怎么被拍出来,不决定这件衣服是什么。"
        "事实的 input_fingerprint 只由证据素材集合决定,方案不进指纹",
    ),
    _cell(
        ChangeSource.GENERATION_PLAN_CHANGED,
        Target.IMAGE_SET,
        Effect.STALE,
        mechanism="app.workflows.generation_plan.image_set_stale_scopes",
        note="**按作用域细化**:改 SPU 默认方案只影响**没有颜色覆盖**的那些"
        "颜色(effective_fingerprints 先解析出每个颜色当前生效的是哪一份),"
        "改某个颜色的覆盖只影响那一个颜色。按\"方案变了就全部过期\"写的话,"
        "九色 SPU 调一次默认角度会重出八个颜色的全部候选,每张都是付费调用",
    ),
    _cell(
        ChangeSource.GENERATION_PLAN_CHANGED,
        Target.COPY,
        Effect.NOT_AFFECTED,
        note="文案过期判定(copy_attrs_stale)的输入只有已确认属性版本集合,"
        "不接收任何生成侧参数",
    ),
    _cell(
        ChangeSource.GENERATION_PLAN_CHANGED,
        Target.DRAFT,
        Effect.STALE,
        mechanism="app.workbench.stale.diff_components:image_set",
        note="§8.1 那一行写的是\"图片映射过期\":方案变化让对应颜色的图片集"
        "降级/重出,草稿快照里记的 (id, version) 因此对不上。**草稿不直接"
        "记方案指纹** —— 记了就有两个都说得通的过期理由,而它们会漂移",
    ),
    # ---- 第 8 行:替换批准图片集(§8.1 新增) --------------------------
    _cell(
        ChangeSource.APPROVED_IMAGE_SET_REPLACED,
        Target.FACTS,
        Effect.NOT_AFFECTED,
        note="图片集是事实的下游",
    ),
    _cell(
        ChangeSource.APPROVED_IMAGE_SET_REPLACED,
        Target.IMAGE_SET,
        Effect.NEW_VERSION,
        mechanism="app.listings.image_set_service.approve",
        note="§8.1 那一行写的是\"新版本生效\",**不是过期** —— 与第 3 行"
        "(重新编排 → 重置为待批准)是两件事:那边是已批准集被改动、"
        "必须重新走审批,这边是新一版**已经**批准了、旧版自动退位。"
        "合并这两格会让\"批准一版新图之后该做什么\"失去答案。"
        "唯一批准点由 uq_listing_image_sets_one_approved 兜底",
    ),
    _cell(
        ChangeSource.APPROVED_IMAGE_SET_REPLACED,
        Target.COPY,
        Effect.NOT_AFFECTED,
        note="文案过期判定不接收图片集输入",
    ),
    _cell(
        ChangeSource.APPROVED_IMAGE_SET_REPLACED,
        Target.DRAFT,
        Effect.STALE,
        mechanism="app.workbench.stale.diff_components:image_set_version",
        note="草稿快照记的 (id, version) 与当前批准版不一致",
    ),
)


def cell(source: ChangeSource, target: Target) -> MatrixCell:
    """取一格。矩阵是封闭的,取不到说明调用方拿着不存在的行列。"""
    for c in MATRIX:
        if c.source is source and c.target is target:
            return c
    raise KeyError((source, target))


def affected_targets(source: ChangeSource) -> tuple[Target, ...]:
    """某个变更源会波及哪些对象(effect 非「不过期」)。"""
    return tuple(
        c.target for c in MATRIX if c.source is source and c.effect is not Effect.NOT_AFFECTED
    )


def is_exhaustive() -> bool:
    """32 格,每个 (行, 列) 恰好一次。测试断言它,这里也自证一次。

    §8.1 要求新增 6 个变更源。A45-batch14-20 补了其中**两个**
    (更换生成方案、替换批准图片集)—— 阶段 4 落地了它们点名的机制。
    剩下四个(改素材颜色归属、改人工材质卖点、改价格库存、
    新增/停用 ACTIVE 颜色)仍然欠着,清单在
    `tests/pure/test_a45_batch14_9_scope_fingerprint.py`,补一行划一条。

    **不要为了让表看起来全而先画格子**:
    `test_every_effective_cell_names_a_mechanism_that_exists_in_code`
    会红,而让它变绿最省事的做法是随便指一个函数。
    """
    pairs = [(c.source, c.target) for c in MATRIX]
    return sorted(pairs) == sorted(_cartesian(ChangeSource, Target)) and len(pairs) == len(
        set(pairs)
    )
