"""运营流程判定(阶段 1,任务书 §2)。**零依赖纯函数。**

这一层回答运营在列表页问的四个问题:

    这件商品做到哪一步了      -> 五个子状态
    卡在什么地方              -> 阻断 / 待确认 / 提醒,三级问题
    完成度多少                -> 一个百分数
    我下一步该干什么          -> **有且只有一个**推荐动作

## 为什么必须是纯函数

任务书 §2.4 的退出条件里有一条「商品列表与详情页状态一致,无明显延迟或
相互矛盾」。做到这件事只有一个可靠办法:两个页面读同一个判定结果,
而这个判定只有一份实现。

它在这里而不是在 service 里,是因为「缺背面图时下一步是不是补素材」
这类判断需要被穷举测试,而只要它能查库,就没人会写那个穷举测试。
service 负责把库里的行翻译成下面这些 dataclass,判定全在本文件。

## 三级问题不是三种颜色

    BLOCKING       不解决就走不下去,也不允许导出
    NEEDS_CONFIRM  要人做一个决定(冲突、低置信度),系统不替他决定
    REMINDER       可以带着它继续走,但导出前最好看一眼

分级的意义在于「阻断数」这个数字要能被信任。把提醒也算成阻断,
运营会发现每件商品都是红的,然后不再看这个数 —— 那时候真正的
阻断也一起被忽略了。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

# 本文件的「零依赖」说的是**不碰基础设施**(不 import sqlalchemy / fastapi /
# 模型层),不是不 import 任何东西。`media/sample_completeness` 和这里一样是
# 纯判定层,拿进来不会把库拖进纯测试。
#
# 为什么是 import 而不是在这里再写一份满足组:「什么算一张够格的正面图」
# 只能有一个答案。写两份的表现是判定说「或平铺图」、门禁说「必须正面图」,
# 两处都不报错,而运营会去改素材的角色标注来迁就其中较严的那一份。
from app.media import sample_completeness as gate


class FlowStep(StrEnum):
    """五个子状态(任务书 FE-102)。

    顺序有意义:`STEP_ORDER` 依赖它推出「唯一下一步」。
    """

    SETUP = "SETUP"            # 建档(SPU / 颜色 / SKU 的归属)
    MATERIAL = "MATERIAL"      # 素材
    ATTRIBUTE = "ATTRIBUTE"    # 属性
    PLAN = "PLAN"              # 模特与生成方案
    IMAGE_SET = "IMAGE_SET"    # 图片集
    COPY = "COPY"              # 文案
    DRAFT = "DRAFT"            # 上架草稿


#: 流程顺序。**唯一下一步就是这个序列里第一个没做完的步骤。**
#:
#: 写成常量而不是靠字典顺序:下一步的正确性完全依赖这个顺序,
#: 它必须是一个能被测试直接断言的对象。
STEP_ORDER: tuple[FlowStep, ...] = (
    FlowStep.SETUP,
    FlowStep.MATERIAL,
    FlowStep.ATTRIBUTE,
    FlowStep.PLAN,
    FlowStep.IMAGE_SET,
    FlowStep.COPY,
    FlowStep.DRAFT,
)

STEP_LABELS: Mapping[FlowStep, str] = {
    FlowStep.SETUP: "建档",
    FlowStep.MATERIAL: "素材",
    FlowStep.ATTRIBUTE: "属性",
    FlowStep.PLAN: "方案",
    FlowStep.IMAGE_SET: "图片集",
    FlowStep.COPY: "文案",
    FlowStep.DRAFT: "草稿",
}


class StepState(StrEnum):
    """一个步骤的状态。

    `STALE` 与 `BLOCKED` 分开:前者是「做完了但上游变了」,后者是
    「前置条件不满足,现在做不了」。运营要做的事完全不同 ——
    过期的重新生成一次即可,阻断的要回头补东西。

    一处刻意的例外:素材步没有上游,它的 `BLOCKED` 表示「缺少必备素材,
    整条流程从这里起步不了」。除素材步以外,`BLOCKED` 一律指上游未就绪,
    且**不携带任何 issue**(等上游不是待办);素材步的 BLOCKED 则带着
    具体缺什么的阻断问题。前端按状态上色时注意这两种红指向的动作不同。
    """

    BLOCKED = "BLOCKED"
    TODO = "TODO"
    IN_PROGRESS = "IN_PROGRESS"
    NEEDS_CONFIRM = "NEEDS_CONFIRM"
    STALE = "STALE"
    DONE = "DONE"


#: 步骤完成度权重。五步等权,合计 100。
#:
#: 每步的部分分**按任务书 §3.2 的计分口径**给:终态 100%、待确认/待批准 60%、
#: 进行中 30%、未开始/失败/冲突/过期 0%。权重写成表而不是算术,
#: 是为了让「为什么是 45%」永远有一个可以指着看的答案。
#:
#: 过期给 0 分是 §3.2 的明文,也有运营上的道理:过期的产出**不能用**,
#: 和没做在结果上是一回事;给它部分分,一件草稿过期的商品会显示 90%+,
#: 而它此刻恰恰导不出去 —— 完成度越高的行越晚被看,过期件就一直沉底。
#: (此处曾给过 STALE=0.6 / IN_PROGRESS=0.5,与 §3.2 不符,已纠正;
#: 数值由 test_workbench_flow 按 §3.2 表格钉住。)
STATE_PROGRESS: Mapping[StepState, float] = {
    StepState.BLOCKED: 0.0,
    StepState.TODO: 0.0,
    StepState.IN_PROGRESS: 0.3,
    StepState.NEEDS_CONFIRM: 0.6,
    StepState.STALE: 0.0,
    StepState.DONE: 1.0,
}

#: 每一步在完成度里占几分。**合计必须是 100**,由 batch26 的守卫钉着。
#:
#: 原来这里是一个常量 `STEP_WEIGHT = 20.0`,注释写着「5 步 × 20 = 100」——
#: 而那句话是**算术,不是断言**:往 `STEP_ORDER` 里加一步,合计就变成 120,
#: 而没有任何地方会红(batch26 实测:给 `FlowStep` 加两个成员,后端一条不红)。
#:
#: 换成表还有第二个作用:七步增维(阶段 6 批次 6-2)时,完成度口径的变化会
#: **集中在这一张表上**,而不是散在一个常量和一句注释里。改口径的人能指着
#: 它说「我把哪一步的分挪到了哪里」。
#:
#: ## 七步的分配(A45-batch27,阶段 6 批次 6-2)——**这是一次口径变更**
#:
#: 五步等权(每步 20)变成下面这张表。同一件商品的完成度会变:
#: 素材与属性都做完的那一件,从 40% 变成 45%。
#:
#: **这不是显示细节。** 完成度驱动列表排序、批量筛选与审阅队列的顺序,
#: 运营按它找"快好了的那些" —— 口径一变,他昨天记住的那批商品今天不在
#: 原来的位置。口径变更公告见 `docs/STATUS.md` 的 batch27 一节。
#:
#: 分配理由(权重是"这一步有多少人要做的事",不是算术):
#:
#:     SETUP      5   建一次,几乎不出错;它在流程里的作用是**排除**
#:                    (归属没建好,后面每一步的作用域都是错的)
#:     MATERIAL  15   要人去拍/传,但一个颜色几张图就够
#:     ATTRIBUTE 25   本条链路上人工工作量最大的一步:确认、改冲突、补缺失
#:     PLAN      10   选一次模特与方案,之后整个 SPU 复用
#:     IMAGE_SET 20   生成要等、要挑、要批准,而且按颜色重复
#:     COPY      15   多数情况一次生成通过,改的是措辞
#:     DRAFT     10   基本自动(build_draft),人只在字段不合格时介入
STEP_WEIGHTS: Mapping[FlowStep, float] = {
    FlowStep.SETUP: 5.0,
    FlowStep.MATERIAL: 15.0,
    FlowStep.ATTRIBUTE: 25.0,
    FlowStep.PLAN: 10.0,
    FlowStep.IMAGE_SET: 20.0,
    FlowStep.COPY: 15.0,
    FlowStep.DRAFT: 10.0,
}


class IssueLevel(StrEnum):
    BLOCKING = "BLOCKING"
    NEEDS_CONFIRM = "NEEDS_CONFIRM"
    REMINDER = "REMINDER"


#: 受众筛选的合法取值。`UNCONFIRMED` 是哨兵值(见 `matches_filters`),
#: 其余三个与 `core.enums.Audience` 一致 —— 本文件是零依赖层,不 import 枚举,
#: 由 `tests/pure/test_a45_batch10_fixes` 的一条断言守住两边同步。
_AUDIENCE_FILTER_VALUES: frozenset[str] = frozenset(
    {"WOMEN", "MEN", "UNISEX", "UNCONFIRMED"}
)


class NextActionCode(StrEnum):
    """推荐动作。**一个商品同一时刻只会得到其中一个。**

    动作码而不是一句话:前端要按它决定跳到哪个标签页、按钮显示什么、
    要不要禁用,而这三件事不能靠解析中文文案来决定。
    """

    #: §4.4 的归属外键没回填:这一行 SKU 说不出自己属于哪个 SPU / 哪个颜色。
    #:
    #: **不复用任何素材类动作**:运营要做的是把这一行挂回它的颜色,
    #: 而不是去传图。挂错之前传的图会绑到错的作用域上,后面每一步跟着错。
    COMPLETE_SETUP = "COMPLETE_SETUP"            # 补全建档归属
    UPLOAD_MATERIAL = "UPLOAD_MATERIAL"          # 补充素材
    RELEASE_QUARANTINE = "RELEASE_QUARANTINE"    # 处理被隔离的素材
    #: §6.2:素材有了,但角色不是人工确认的,门禁不认。
    #:
    #: **不复用 `UPLOAD_MATERIAL`**:那个按钮会说「补充素材」,而运营
    #: 不需要补任何素材 —— 他要做的是打开素材页看一眼、点确认。说错话
    #: 的代价是他真的去传了一张新图,而新图默认同样没确认,再撞一次。
    #: **也不复用 `RELEASE_QUARANTINE`**:那句文案会说「N 条素材被隔离」,
    #: 而这条动线上 N 可能是 0。**说错话比不说话更难查。**
    CONFIRM_ASSET_ROLE = "CONFIRM_ASSET_ROLE"    # 确认素材角色
    #: PRD v2 §8.2 / §9.4:受众与品类同屏确认。
    #:
    #: **它必须排在选模特之前**(§8.2 原话:受众确认之后模特候选集才是对的;
    #: 顺序反过来会让运营先选一次模特、确认受众后又被清空)。在本文件的
    #: 步骤序里这一点是结构保证的 —— 它属于 ATTRIBUTE 步,而选模特发生在
    #: 素材与属性都就绪之后的生成阶段。
    #:
    #: 上一版没有这个码,后果是 `FlowHeader` 那个橙色「待确认」标记
    #: **是个死胡同**:它告诉你有问题,而界面上没有任何一处能解决它。
    CONFIRM_AUDIENCE = "CONFIRM_AUDIENCE"        # 确认受众与品类
    RUN_EXTRACTION = "RUN_EXTRACTION"            # 启动属性识别
    RESOLVE_CONFLICT = "RESOLVE_CONFLICT"        # 处理属性冲突
    CONFIRM_ATTRIBUTES = "CONFIRM_ATTRIBUTES"    # 确认属性
    #: PRD §11「未校准字段大量涌入」:识别跑过了,但结论全是 CANDIDATE
    #: (未校准 / 置信度不足 / 模型说看不清),确认队列里一个可点的都没有。
    #:
    #: **不复用 `CONFIRM_ATTRIBUTES`**:那个按钮说「确认属性」,而运营点进去
    #: 会发现没有任何东西可确认 —— 他会以为界面坏了,然后去点「启动属性识别」,
    #: 那是一次真实付费调用,结果是同一批不采信的证据。
    #: **也不复用 `RUN_EXTRACTION`**:那句话直接把他送去花那笔钱。
    #: 与 `CONFIRM_ASSET_ROLE` 同一条理由 —— **说错话比不说话更难查。**
    FILL_ATTRIBUTES = "FILL_ATTRIBUTES"          # 人工填写属性
    #: 这个颜色还没有生效的生成方案(模特 / 参数)。
    #:
    #: **不复用 `BUILD_IMAGE_SET`**:那个按钮会把运营送去编排图片集,
    #: 而这时候一张生成图都还没有 —— 他会看到一个空列表,然后以为坏了。
    CHOOSE_PLAN = "CHOOSE_PLAN"                  # 选择模特与生成方案
    BUILD_IMAGE_SET = "BUILD_IMAGE_SET"          # 编排图片集
    FIX_IMAGE_SET = "FIX_IMAGE_SET"              # 修复图片集问题
    APPROVE_IMAGE_SET = "APPROVE_IMAGE_SET"      # 批准图片集
    GENERATE_COPY = "GENERATE_COPY"              # 生成文案
    FIX_COPY = "FIX_COPY"                        # 修复文案问题
    APPROVE_COPY = "APPROVE_COPY"                # 批准文案
    BUILD_DRAFT = "BUILD_DRAFT"                  # 生成上架草稿
    FIX_DRAFT = "FIX_DRAFT"                      # 修复草稿字段
    REFRESH_DRAFT = "REFRESH_DRAFT"              # 重新生成过期草稿
    EXPORT = "EXPORT"                            # 导出上架文件
    RESOLVE_REJECTION = "RESOLVE_REJECTION"      # 处理平台驳回(P1 驳回回流)
    DONE = "DONE"                                # 已导出,无待办


#: 动作 -> 它属于哪个步骤。前端据此跳标签页。
ACTION_STEP: Mapping[NextActionCode, FlowStep] = {
    NextActionCode.COMPLETE_SETUP: FlowStep.SETUP,
    NextActionCode.UPLOAD_MATERIAL: FlowStep.MATERIAL,
    NextActionCode.RELEASE_QUARANTINE: FlowStep.MATERIAL,
    NextActionCode.CONFIRM_ASSET_ROLE: FlowStep.MATERIAL,
    NextActionCode.CONFIRM_AUDIENCE: FlowStep.ATTRIBUTE,
    NextActionCode.RUN_EXTRACTION: FlowStep.ATTRIBUTE,
    NextActionCode.RESOLVE_CONFLICT: FlowStep.ATTRIBUTE,
    NextActionCode.CONFIRM_ATTRIBUTES: FlowStep.ATTRIBUTE,
    NextActionCode.FILL_ATTRIBUTES: FlowStep.ATTRIBUTE,
    NextActionCode.CHOOSE_PLAN: FlowStep.PLAN,
    NextActionCode.BUILD_IMAGE_SET: FlowStep.IMAGE_SET,
    NextActionCode.FIX_IMAGE_SET: FlowStep.IMAGE_SET,
    NextActionCode.APPROVE_IMAGE_SET: FlowStep.IMAGE_SET,
    NextActionCode.GENERATE_COPY: FlowStep.COPY,
    NextActionCode.FIX_COPY: FlowStep.COPY,
    NextActionCode.APPROVE_COPY: FlowStep.COPY,
    NextActionCode.BUILD_DRAFT: FlowStep.DRAFT,
    NextActionCode.FIX_DRAFT: FlowStep.DRAFT,
    NextActionCode.REFRESH_DRAFT: FlowStep.DRAFT,
    NextActionCode.EXPORT: FlowStep.DRAFT,
    NextActionCode.RESOLVE_REJECTION: FlowStep.DRAFT,
    NextActionCode.DONE: FlowStep.DRAFT,
}

ACTION_LABELS: Mapping[NextActionCode, str] = {
    NextActionCode.COMPLETE_SETUP: "补全建档归属",
    NextActionCode.UPLOAD_MATERIAL: "补充素材",
    NextActionCode.RELEASE_QUARANTINE: "处理隔离素材",
    NextActionCode.CONFIRM_ASSET_ROLE: "确认素材角色",
    NextActionCode.CONFIRM_AUDIENCE: "确认受众与品类",
    NextActionCode.RUN_EXTRACTION: "启动属性识别",
    NextActionCode.RESOLVE_CONFLICT: "处理属性冲突",
    NextActionCode.CONFIRM_ATTRIBUTES: "确认属性",
    NextActionCode.FILL_ATTRIBUTES: "人工填写属性",
    NextActionCode.CHOOSE_PLAN: "选择模特与生成方案",
    NextActionCode.BUILD_IMAGE_SET: "编排图片集",
    NextActionCode.FIX_IMAGE_SET: "修复图片集",
    NextActionCode.APPROVE_IMAGE_SET: "批准图片集",
    NextActionCode.GENERATE_COPY: "生成文案",
    NextActionCode.FIX_COPY: "修复文案",
    NextActionCode.APPROVE_COPY: "批准文案",
    NextActionCode.BUILD_DRAFT: "生成上架草稿",
    NextActionCode.FIX_DRAFT: "修复草稿字段",
    NextActionCode.REFRESH_DRAFT: "重新生成草稿",
    NextActionCode.EXPORT: "导出上架文件",
    NextActionCode.RESOLVE_REJECTION: "处理平台驳回",
    NextActionCode.DONE: "已完成",
}


#: 快审退回原因 -> 中文(A10)。
#:
#: 键是字符串而不是 `RejectReason` 枚举:枚举定义在 `app.workbench.reject`,
#: 而那个模块要 import 本模块取 `NextActionCode` —— 反向 import 会成环。
#: 和下面 `PLATFORM_STATUS_LABELS` 是同一种处理,文案表本来就归这一层管。
#: 两张表的键必须与那个枚举完全一致,契约测试盯着,加原因时漏了这里会立刻失败。
REJECT_REASON_MESSAGES: Mapping[str, str] = {
    "STRUCTURE_CHANGED": "商品结构改变",
    "COLOR_MISMATCH": "颜色不一致",
    "PRINT_CHANGED": "印花改变",
    "LOW_IMAGE_QUALITY": "图片质量低",
    "COPY_FACT_ERROR": "文案事实错误",
    "UNNATURAL_PT": "葡语不自然",
    "IRRELEVANT_KEYWORDS": "关键词不相关",
    "NEEDS_MATERIAL": "需要补素材",
    "OTHER": "其他",
}


#: 退回原因 -> 退回之后的唯一下一步。**A10 的核心就是这张表**:
#: 验收标准是"运营遇到不合格商品时不需要离开队列自行猜测处理路径",
#: 也就是退回必须产出一个明确的下一步,而不是把状态推回去让人自己想。
#:
#: 几条不那么显然的:
#:
#:   STRUCTURE_CHANGED   结构变了意味着**属性**记错了,不是图没拍好。先去改属性 ——
#:                       直接重新生成会拿着同一份错属性再生成一次
#:   NEEDS_MATERIAL      去素材页而不是生成页:缺的是输入,重新生成变不出新素材
#:   OTHER               系统不猜。落到出问题的那一步让人自己处理,
#:                       并且退回时强制要求写补充说明(见 reject.py)
REJECT_REASON_ACTIONS: Mapping[str, NextActionCode] = {
    "STRUCTURE_CHANGED": NextActionCode.CONFIRM_ATTRIBUTES,
    "COLOR_MISMATCH": NextActionCode.FIX_IMAGE_SET,
    "PRINT_CHANGED": NextActionCode.FIX_IMAGE_SET,
    "LOW_IMAGE_QUALITY": NextActionCode.FIX_IMAGE_SET,
    "COPY_FACT_ERROR": NextActionCode.FIX_COPY,
    "UNNATURAL_PT": NextActionCode.FIX_COPY,
    "IRRELEVANT_KEYWORDS": NextActionCode.FIX_COPY,
    "NEEDS_MATERIAL": NextActionCode.UPLOAD_MATERIAL,
    # 「其他」**不在这张表里** —— 见下面的 REJECT_STEP_FALLBACK
}


#: 退回之后回到哪一步:按**被退回的对象**定,不按原因定。
#:
#: 两种情况用它:
#:
#:   「其他」          系统不猜运营想干什么,把他送回出问题的那一步。
#:                    这一条原先在上表里硬写成 FIX_IMAGE_SET,于是文案选
#:                    「其他」退回会被送去重排图片,而问题在文字上 ——
#:                    一张按原因索引的表回答不了这个问题,它不知道退的是谁
#:   认不出的原因码    库里存着一个上表没有的值(旧数据、手工 SQL)。
#:                    落到本步的修复动作,让人去修,而不是卡在无动作可做
REJECT_STEP_FALLBACK: Mapping[str, NextActionCode] = {
    "image_set": NextActionCode.FIX_IMAGE_SET,
    "copy": NextActionCode.FIX_COPY,
}


def reject_next_action(target: str, reason: str | None) -> NextActionCode:
    """退回之后的唯一下一步。`target` 是被退回的那一步('image_set' / 'copy')。

    **A10 的核心就是这个函数**:验收标准是"运营遇到不合格商品时不需要离开队列
    自行猜测处理路径",也就是退回必须产出一个明确的下一步。
    """
    fallback = REJECT_STEP_FALLBACK[target]
    if not reason:
        return fallback
    return REJECT_REASON_ACTIONS.get(reason, fallback)


def _reject_label(reason: str | None) -> str:
    """退回原因的中文。认不出来时如实回显原值,不写"未知" ——
    看到一个陌生的码,下一步是去查它是谁写进来的,而"未知"把这条线索抹掉了。"""
    if not reason:
        return "未注明原因"
    return REJECT_REASON_MESSAGES.get(reason, reason)


#: 平台侧状态 -> 给运营看的一句话(OPS-REVIEW P1)。
#:
#: 表放在本文件而不是 platform.py:flow 的草稿列摘要要用它,而 platform.py
#: 已经 import flow(FlowStep)。反向 import 会成环;文案表本来就归这里管
#: (STEP_LABELS / ACTION_LABELS 都在这一层),platform.py 从这里取。
PLATFORM_STATUS_LABELS: Mapping[str, str] = {
    "NONE": "未上传平台",
    "SUBMITTED": "已上传平台",
    "LIVE": "平台已上架",
    "REJECTED": "平台驳回",
}


# ==========================================================
# 输入:service 从库里查出来喂进来的事实
# ==========================================================


@dataclass(frozen=True)
class MaterialFacts:
    """素材层事实。

    `usable_roles` 用角色集合而不是数量:缺的是背面图还是细节图,
    决定了提示语该说什么,而「有 3 张图」这个数字什么也回答不了。

    ## `usable_roles` 与 `gate_roles` 不是一回事,别合并

        usable_roles  素材库里**有**哪些角色的图。谁标的、是不是 AI 生成的,
                      一律不问。提醒类问题读它 —— 一张模型猜出角色的背面图,
                      说「没有背面图」是假的,而假提醒会训练人忽略这一列
        gate_roles    §6.2 门禁**认**哪些角色。要求那张图是本商品的证据,
                      且角色是人定的(判定在 `media/sample_completeness`)

    合并的结果只有两种,都是错的:按宽的算,AI 图能满足完整度;
    按严的算,每件商品都会显示缺背面图。

    ## 两个新字段都**不给宽松默认值**

    默认空集 = 谁没填谁不合格。给一个「没传就用 `usable_roles`」之类的
    兜底,漏填的表现是**悄悄放行** —— 硬规则 4 第二次事故的形状正是如此:
    缺一列不报错,判定一路走到最宽的一档,而两侧的测试全绿。

    代价是接线那天所有旧夹具当场变红。那是设计在起作用,不是回归。
    """

    total: int = 0
    usable: int = 0
    quarantined: int = 0
    pending: int = 0
    usable_roles: frozenset[str] = frozenset()
    #: §6.2 门禁认可的角色集合。**空集 = 一张够格的样品都没有。**
    gate_roles: frozenset[str] = frozenset()
    #: 每个颜色各自的门禁角色(§6.2 的颜色作用域,A45-batch14-15 接线)。
    #:
    #: 归属外键落库之前这里恒为空 —— 那时判定验完了却接不上线,
    #: 14-10 用一条欠账守卫把原因钉在明处。现在那一列有了,这里跟着接上。
    #:
    #: **默认空 dict 而不是「没传就退回 gate_roles」**:退回的表现是
    #: 每个颜色都拿到全 SPU 的角色集合,于是「B 色一张正面图都没有」
    #: 会被判成合格 —— 与 `gate_roles` 那条默认值同一个理由。
    variant_gate_roles: dict[str, frozenset[str]] = field(default_factory=dict)
    #: 颜色 id -> 给运营看的名字。**同时它的键就是「这个 SPU 声明了哪些颜色」。**
    #:
    #: ## 为什么颜色维不能从素材反推(D1,A45-batch14-27)
    #:
    #: `variant_gate_roles` 的键原来是从素材行上的 `color_variant_id` 数出来的
    #: (「颜色维由素材自己带出来」)。那个理由对**指纹**成立 —— 一个没有素材的
    #: 颜色算不出指纹,也没有事实需要过期。
    #:
    #: 对**完整度门禁**它是反的:门禁要回答的正是「哪个颜色还缺图」,而一个
    #: 一张图都没有的颜色在素材里留不下任何痕迹,于是它永远不进这张表、
    #: 永远不被判缺图。**门禁最该拦的那一种,恰好是它唯一看不见的那一种。**
    #:
    #:     备选:继续从素材反推   零改动,但「B 色一张图都没传」静默放行,
    #:                           而界面上 B 色连一个分组都不会出现
    #:     选错的后果             一件只传了 A 色图的商品一路走到刊登,
    #:                           B 色的图片集拿不到样品 —— 而没有任何一步报错
    #:
    #: 所以键取**声明过的颜色**(`color_variants` 表),素材只决定值。
    #: 两者的并集仍然进表:一张挂在已下架颜色上的存量图不该凭空消失。
    #:
    #: 守卫:`test_the_colour_dimension_comes_from_the_declared_list_not_the_assets`
    #: —— 它构造一个「声明了三色、只传了一色图」的 facts,断言另外两色进了
    #: 缺图清单。退回素材反推的话它当场红。
    variant_labels: dict[str, str] = field(default_factory=dict)
    #: 「确认一下角色就能解开门禁」的角色,已排序。非空即意味着门禁没过
    #: (`sample_completeness.confirmable_roles` 的性质),所以它同时是
    #: 「补图」与「确认角色」两条动线的分岔依据
    confirmable_roles: tuple[str, ...] = ()

    # `has_primary_image` 在 A45-batch14-11 删掉了。它**算了没人读**:
    # `service.py` 是唯一写入点,全仓零个读取点。留着的坏处不是多一个
    # 布尔字段,而是它读的是 `usable_roles` 而 §6.2 门禁读的是 `gate_roles`
    # —— 第一个接它的人会拿到一个「AI 图也算有主图」的答案,而两侧的
    # 测试全绿。真需要「有没有主图」时,问 `missing_role_groups(gate_roles)`,
    # 那是本仓唯一一份必备角色口径,而且它答的是「正面图**或**平铺图」。


@dataclass(frozen=True)
class AudienceFacts:
    """受众层事实(PRD v2 §3.4 / §8.2 / §9.4)。

    `audience` 用 `str | None` 而不是枚举:本文件是**零依赖纯函数**层,
    不 import `core.enums`。service 负责把库里的列翻译进来,判定在这里。

    `None` = 待确认,**不是 UNISEX**(§3.4 规则 3)。两者必须分开:
    UNISEX 是一个明确的业务判断,"没填"不是。
    """

    #: 商品受众。None = 待确认
    audience: str | None = None
    #: 取值本身不合法(库里存着一个认不出的字符串)。
    #: 它不是"没填" —— 走兼容缝的话这个错值会静默存在到永远
    invalid: bool = False


@dataclass(frozen=True)
class AttributeFacts:
    """属性层事实。字段名集合,不是数量 —— 提示要能点名到字段。"""

    required_fields: tuple[str, ...] = ()
    confirmed: frozenset[str] = frozenset()
    suggested: frozenset[str] = frozenset()
    conflicted: frozenset[str] = frozenset()
    candidate_only: frozenset[str] = frozenset()
    #: 输入指纹对不上的字段(§5.3 / AC-21)。判定在
    #: `attributes.service.stale_fields`,这一层只把结果递进来。
    #:
    #: **不是"未确认"的另一种说法**:这些字段有值、被人点过头,只是它们
    #: 当初依据的那批样品变了 —— 下一步是"再看一眼",不是"去填一个值"。
    #: 两者混成一句话的后果写在 `_evaluate_attribute` 那个分支里。
    stale: frozenset[str] = frozenset()
    extraction_count: int = 0

    @property
    def stale_confirmed(self) -> frozenset[str]:
        """确认过、且输入指纹对不上的字段。**取交是刻意的,不是保险。**

        `stale` 与 `confirmed` 的关系是一条不变式(判定侧只对 `SETTLED`
        那一档算过期),而**不变式不该靠调用点守规矩** —— 这个仓库刚刚
        为「判据落在别人身上的守卫守不住自己」付过一次学费(§3.37)。

        取交之后,一个没确认过的字段无论怎么被塞进 `stale`,都不会走到
        「已确认的事实过期」那条分支上去说一句假话。下面两个消费者
        (`missing_required` 与问题码分支)都读这一个属性,不读 `stale`。
        """
        return self.stale & self.confirmed

    @property
    def missing_required(self) -> tuple[str, ...]:
        """必填但**尚不满足完成条件**的字段。

        §6.3 的完成条件在双指纹修订之后是两句话:「必填事实 `CONFIRMED`」
        **且**「其 `input_fingerprint` 匹配当前作用域指纹」。所以过期的
        已确认字段照样算欠着 —— 少了后半句,给颜色 A 换掉全部样品之后,
        A 色事实仍然带着旧结论进导出,而界面显示"已完成"。
        """
        return tuple(
            f
            for f in self.required_fields
            if f not in self.confirmed or f in self.stale_confirmed
        )


@dataclass(frozen=True)
class ImageSetFacts:
    exists: bool = False
    status: str | None = None
    version: int = 0
    item_count: int = 0
    #: 图片集规则层的问题码。这一层的问题**全部是阻断级**(见 image_set_rules)
    violation_codes: tuple[str, ...] = ()
    #: 被隔离素材连累而降级
    downgraded: bool = False
    #: A10:被快审退回过,以及退回原因(`RejectReason` 的值)。
    #: 判定层必须看得见它 —— 只落审计的话,图片集回到 DRAFT、校验依旧通过,
    #: 下一步又变成"等待批准",那件商品立刻走回快审队列排在原处
    reject_reason: str | None = None


@dataclass(frozen=True)
class CopyFacts:
    exists: bool = False
    status: str | None = None
    version: int = 0
    blocking_violations: int = 0
    warning_violations: int = 0
    #: 已批准之后属性又变了 —— 文案里的事实可能已经不成立
    stale: bool = False
    locale: str | None = None
    #: A10:被快审退回过,以及退回原因。理由同 `ImageSetFacts.reject_reason`
    reject_reason: str | None = None


@dataclass(frozen=True)
class DraftFacts:
    exists: bool = False
    status: str | None = None
    error_count: int = 0
    warning_count: int = 0
    #: 上游指纹变了。**STALE 草稿不允许导出**(§9.1)
    stale: bool = False
    exported: bool = False
    #: 平台侧状态(OPS-REVIEW P1)。None = 还没有草稿行。
    #: 取值见 `core.enums.PlatformStatus`,由运营在导出页手工录入
    platform_status: str | None = None
    #: 未解决的平台驳回条数。**>0 时这一步永远不算 DONE** ——
    #: 平台说不行,就是还没行;绿色"已完成"挂在被驳回的商品上,
    #: 运营就只能回到系统外拿表格记驳回件(OPS-REVIEW 第 11 环的原话)
    open_rejections: int = 0


@dataclass(frozen=True)
class SetupFacts:
    """建档层事实(§4.4 的归属外键,阶段 6 批次 6-2)。

    「这一行 SKU 说不说得出自己属于哪个 SPU、哪个颜色」。

    ## 为什么它是一步,而不是别处的一个前置检查

    归属决定**后面每一步的作用域**:素材挂到哪个颜色、事实写在哪一层、
    图片映射按哪个颜色铺、文案写进哪个槽(0051 之后)。归属没建好而继续
    往下走,每一步都会算出一个"看起来正常但作用域错了"的结果 ——
    这正是本仓反复付账的那一类。写成第一步,后面的步骤就有资格说
    「上一步没完成,我做不了」。

    ## 存量行判 NEEDS_CONFIRM,不判 BLOCKED

    §4.4 的归属外键是阶段 1 的交付,库里仍有没回填的行。判阻断会让一批
    老商品在向导里一起变红,而运营对着它们能做的事(挂回颜色)恰恰要人来
    做决定 —— 系统按 `spu` 字符串码反查是 §3.39 明令禁掉的形状(那个码
    可能已因改名而断开,反查出来的是另一个款)。
    """

    #: 商品行有没有 `spu_id`。None = 没查
    has_spu_ref: bool | None = None
    #: 商品行有没有 `color_variant_id`
    has_colour_ref: bool | None = None
    #: 这个 SPU 下已建的颜色数。0 = 只建了 SPU,颜色还没建
    colour_count: int = 0


@dataclass(frozen=True)
class PlanFacts:
    """生成方案层事实(§13 阶段 6 的第四步)。

    「这个颜色用什么参数出图」。方案按颜色存,颜色没有自己的那一份时
    **回落到 SPU 级** —— 这不是这里发明的回落,是 `generation_plans` 上
    那条 `COALESCE(color_variant_id::text, '')` 唯一索引本来的形状。
    """

    #: 有没有一份**当前生效**(非 ARCHIVED)的方案。None = 没查
    has_active_plan: bool | None = None
    #: 生效的那一份是颜色级还是 SPU 级回落来的。回落不是问题,只是要说清楚
    plan_is_colour_level: bool = False
    #: 方案选了模特没有。选了参数没选模特的方案跑不出图
    has_model: bool = False


@dataclass(frozen=True)
class ProductFlow:
    """一件商品的全部流程事实。service 负责组装,本文件负责判定。"""

    product_id: str
    sku: str
    spu: str
    #: 受众。默认空 = 待确认 —— 存量商品(迁移 0030 不回填)全部落在这一档,
    #: 所以它产出的是 NEEDS_CONFIRM 而不是 BLOCKING,理由见 `_evaluate_attribute`
    audience: AudienceFacts = field(default_factory=AudienceFacts)
    setup: SetupFacts = field(default_factory=SetupFacts)
    material: MaterialFacts = field(default_factory=MaterialFacts)
    attribute: AttributeFacts = field(default_factory=AttributeFacts)
    plan: PlanFacts = field(default_factory=PlanFacts)
    image_set: ImageSetFacts = field(default_factory=ImageSetFacts)
    copy: CopyFacts = field(default_factory=CopyFacts)
    draft: DraftFacts = field(default_factory=DraftFacts)


# ==========================================================
# 输出
# ==========================================================


@dataclass(frozen=True)
class Issue:
    """一条问题。

    `target_step` 让前端可以把问题做成可点击的 —— 任务书 FE-103 的
    验收结果是「阻断问题醒目且可点击」,点了要能落到具体那一页。
    """

    level: IssueLevel
    code: str
    message: str
    target_step: FlowStep
    hint: str | None = None
    #: 具体到字段 / 素材 / 图片项。没有就留空
    ref: str | None = None


@dataclass(frozen=True)
class StepResult:
    step: FlowStep
    state: StepState
    summary: str
    issues: tuple[Issue, ...] = ()

    def __post_init__(self) -> None:
        """第三个位置参数是 `summary`,不是 `issues`。**这一行是被咬出来的。**

        batch27 的 `_evaluate_setup` / `_evaluate_plan` 都写成
        `StepResult(step, state, tuple(issues))` —— 于是那两步的问题被塞进
        `summary`,而 `issues` 恒为空。三个后果同时发生,一个都不报错:

            界面     出参里 `summary` 是一个**对象数组**,而前端类型写的是
                     `string`;React 渲染对象数组直接抛错 —— 归属未挂或
                     没配方案的商品,总览标签页整块打不开
            阻断数   `blocking_count` 数的是 `result.issues`,那两步的
                     BLOCKING 问题一条都没进去。没配方案的商品显示"阻断 0"
            下游     `gate_reason` 之类按 issue 取因由的判定全部落空

        它活了两批(batch27 → batch28)没有被任何测试看见,因为**没有任何
        测试读那两步的 summary**,而 `SETUP_*` / `PLAN_*` 五个问题码全仓
        零引用 —— 算出来从来没有人读过,正是 §3.43 那一族的形状。

        类型检查放在构造期而不是只写一条测试:测试挡的是今天这两处,
        构造期挡的是**下一个人写第八步**时手滑的同一行。
        """
        if not isinstance(self.summary, str):
            raise TypeError(
                f"{self.step}: summary 必须是字符串,拿到 {type(self.summary).__name__}"
                " —— 第三个位置参数是 summary,issues 是第四个"
            )


@dataclass(frozen=True)
class NextAction:
    code: NextActionCode
    label: str
    step: FlowStep
    reason: str


@dataclass(frozen=True)
class FlowResult:
    steps: tuple[StepResult, ...]
    issues: tuple[Issue, ...]
    completion: int
    blocking_count: int
    pending_count: int
    reminder_count: int
    current_step: FlowStep
    next_action: NextAction

    def step_state(self, step: FlowStep) -> StepState:
        for s in self.steps:
            if s.step is step:
                return s.state
        raise KeyError(step)


# ---------------------------------------------------------------- 各步判定


#: 上架至少要有的素材角色。**判定在 `media/sample_completeness`,不在这里。**
#:
#: 这里原先是 `REQUIRED_ROLES = ("PRODUCT_FRONT",)`,而 §6.2 的原文是
#: 「至少一张 PRODUCT_FRONT/FLAT_LAY」—— 于是只有透明底商品图的商品
#: (旧枚举 `GARMENT_CUTOUT` 有损映射成 `FLAT_LAY`)被永久判「缺少正面图」,
#: 而运营解除它的唯一办法是把平铺图改标成正面图。见那个模块的文档。
#:
#: 推荐角色留在这一层:它们**不进门禁**,缺了只是提醒,所以读的是
#: `usable_roles` 而不是 `gate_roles`(理由写在 `MaterialFacts` 上)。
#: 背面与细节缺失做成阻断会让运营为了过检查去传一张凑数的图,那比没有更糟。
RECOMMENDED_ROLES: tuple[str, ...] = ("PRODUCT_BACK", "DETAIL")

#: 角色文案表。**唯一一份在 `media/sample_completeness`**,这里只是别名 ——
#: 名字保留是因为它已经是本模块的对外常量,而复制一份回来的表现是
#: 改了一处之后判定说「平铺图」、按钮说 `FLAT_LAY`,两处都不报错。
ROLE_LABELS: Mapping[str, str] = gate.ROLE_LABELS
_role_label = gate.role_label


def _evaluate_setup(facts: SetupFacts) -> StepResult:
    """建档:这一行 SKU 说不说得出自己属于哪个 SPU、哪个颜色(§4.4)。

    三种结果,没有第四种:

        没查        UNKNOWN 在本层不存在(那是颜色子态的取值)—— 这里
                    `None` 判 NEEDS_CONFIRM,与受众那一档同一个处理:
                    存量数据的未知要人来解决,而不是系统替他猜
        缺归属      NEEDS_CONFIRM。**不是 BLOCKED** —— 理由在 `SetupFacts`
        齐了        DONE
    """
    issues: list[Issue] = []
    if facts.has_spu_ref is False or facts.has_colour_ref is False:
        missing = []
        if facts.has_spu_ref is False:
            missing.append("SPU")
        if facts.has_colour_ref is False:
            missing.append("颜色")
        issues.append(
            Issue(
                level=IssueLevel.NEEDS_CONFIRM,
                code="SETUP_OWNERSHIP_MISSING",
                message=f"这一行还没挂到{'/'.join(missing)}上",
                target_step=FlowStep.SETUP,
                hint="在 SPU 页把这个 SKU 挂回它所属的颜色",
            )
        )
        return StepResult(
            FlowStep.SETUP,
            StepState.NEEDS_CONFIRM,
            f"缺{'/'.join(missing)}归属",
            tuple(issues),
        )

    if facts.has_spu_ref is None or facts.has_colour_ref is None:
        # 没查过归属。判 DONE 会让「建档完成」这件事在没有证据时成立,
        # 而它是后面每一步作用域的前提
        issues.append(
            Issue(
                level=IssueLevel.NEEDS_CONFIRM,
                code="SETUP_UNVERIFIED",
                message="建档归属未核对",
                target_step=FlowStep.SETUP,
                hint="打开 SPU 页确认这个 SKU 的颜色归属",
            )
        )
        return StepResult(
            FlowStep.SETUP, StepState.NEEDS_CONFIRM, "归属未核对", tuple(issues)
        )

    return StepResult(FlowStep.SETUP, StepState.DONE, "已挂到 SPU 与颜色")


def _evaluate_plan(facts: PlanFacts, *, upstream_ready: bool) -> StepResult:
    """生成方案:这个颜色用什么参数、哪个模特出图(§13 阶段 6 第四步)。

    排在属性之后是 §8.2 的顺序要求(受众确认之后模特候选集才是对的),
    而受众属于属性步 —— 所以这个顺序在步骤序里是结构保证的,
    不靠调用方记得先做哪个。
    """
    if not upstream_ready:
        # **等上游不产出 issue**,与 `_evaluate_attribute` / `_evaluate_image_set`
        # / `_evaluate_copy` 三处同向(`StepState` 的文档明写:除素材步以外
        # BLOCKED 一律指上游未就绪,且不携带任何 issue)。
        #
        # 这里原来带着一条 `PLAN_UPSTREAM_NOT_READY`,而它从来没有被任何人
        # 看见 —— 它被塞进了 `summary`(见 `StepResult.__post_init__`)。
        # 修位置参数时顺带把它按本层的规矩收掉:等上游不是待办,
        # 算进阻断数会让一件刚建档的商品显示"阻断 2",而运营能做的只有一件事。
        return StepResult(FlowStep.PLAN, StepState.BLOCKED, "等属性确认")

    if facts.has_active_plan is None:
        return StepResult(FlowStep.PLAN, StepState.TODO, "方案未查")

    if not facts.has_active_plan:
        return StepResult(
            FlowStep.PLAN,
            StepState.TODO,
            "未选方案",
            (
                Issue(
                    level=IssueLevel.BLOCKING,
                    code="PLAN_MISSING",
                    message="这个颜色还没有生成方案",
                    target_step=FlowStep.PLAN,
                    hint="选择模特与生成参数",
                ),
            ),
        )

    if not facts.has_model:
        # 有方案没模特:参数齐了也跑不出图,而任务会一直排在那里
        return StepResult(
            FlowStep.PLAN,
            StepState.NEEDS_CONFIRM,
            "方案缺模特",
            (
                Issue(
                    level=IssueLevel.NEEDS_CONFIRM,
                    code="PLAN_WITHOUT_MODEL",
                    message="方案里还没有选模特",
                    target_step=FlowStep.PLAN,
                    hint="给这个方案指定一个已授权的模特",
                ),
            ),
        )

    return StepResult(
        FlowStep.PLAN,
        StepState.DONE,
        "颜色级方案" if facts.plan_is_colour_level else "SPU 级方案",
    )


def _evaluate_material(facts: MaterialFacts) -> StepResult:
    issues: list[Issue] = []

    # 按**满足组**判定:组内任一角色即满足。集合用 `gate_roles` 而不是
    # `usable_roles` —— 门禁问的是「有没有一张够格的样品」,而
    # 「够格」要求那张图是本商品的证据、且角色是人定的(§6.2)。
    for group in gate.missing_role_groups(facts.gate_roles):
        issues.append(
            Issue(
                level=IssueLevel.BLOCKING,
                code="MATERIAL_MISSING_REQUIRED_ROLE",
                # 说「正面图**或**平铺图」。只报组里第一个角色的话,运营为了
                # 消掉这句话会把平铺图改标成正面图 —— 而下游每一处按角色
                # 取图的地方跟着错。`ref` 只带第一个,那是跳转依据不是文案
                message=f"缺少{gate.group_label(group)}",
                target_step=FlowStep.MATERIAL,
                hint="上传该组任一角色的素材,或确认已有素材的角色",
                ref=group[0],
            )
        )

    # ---- §6.2 颜色作用域:每个颜色各自要有一张够格的样品 ----
    #
    # 这一段以前**不存在**。`variant_gate_roles` 从 14-15 起就算好并递到了
    # `MaterialFacts` 上,而全仓没有任何一处读它 —— 判定穷举验过、接线也在,
    # 中间断的是最后一跳,所以两侧的测试都绿着。§3.32 说的「算出来没有地方
    # 显示的清单」在这里是「算出来没有人判的清单」,后者更贵:它看起来像
    # 一道已经生效的门禁。
    #
    # ## 顺序:排在 SPU 那一段之后
    #
    # SPU 作用域一张够格的图都没有时,逐颜色再报一遍等于把同一句话说 N+1 遍。
    # 但**不 early-return** —— 「共享有正面图、B 色没有」是真实且常见的一档,
    # 而它恰恰是 D1 那个缺陷要暴露的形状。
    #
    # ## 级别与 SPU 那一档一致(BLOCKING)
    #
    # 降成 REMINDER 的话,`summarize()` 不计数、列表页不显示徽标,于是
    # 「B 色缺正面图」这件事只在详情页第三屏可见,而运营是在列表页决定
    # 今天做哪几件商品的。§6.2 把颜色完整度写成门禁,不是提醒。
    for variant_id in sorted(facts.variant_gate_roles):
        label = facts.variant_labels.get(variant_id, variant_id)
        for group in gate.missing_role_groups(facts.variant_gate_roles[variant_id]):
            issues.append(
                Issue(
                    level=IssueLevel.BLOCKING,
                    code="MATERIAL_VARIANT_MISSING_REQUIRED_ROLE",
                    # 带颜色名。只说「缺少正面图」的话,一件三色商品会得到
                    # 三条一模一样的阻断,运营无从知道该给哪个颜色补图 ——
                    # 而"补错颜色"在界面上没有任何反馈:那张图会进另一个
                    # 颜色的作用域,原来那条阻断纹丝不动
                    message=f"{label} 缺少{gate.group_label(group)}",
                    target_step=FlowStep.MATERIAL,
                    hint=(
                        "按颜色上传:在素材页选中这个颜色再传图。"
                        "通用图(不选颜色)只进 SPU 作用域,证明不了某个颜色有正面照"
                    ),
                    # `ref` 是跳转依据,给**颜色 id** 而不是角色 —— 前端要跳的是
                    # 「那个颜色的分组」,而角色在 SPU 那一档已经带过了
                    ref=variant_id,
                )
            )

    if facts.usable == 0 and facts.total == 0:
        issues.append(
            Issue(
                level=IssueLevel.BLOCKING,
                code="MATERIAL_EMPTY",
                message="还没有任何素材",
                target_step=FlowStep.MATERIAL,
                hint="先上传商品图,识别与图片集都依赖它",
            )
        )

    if facts.confirmable_roles:
        # **级别必须是 NEEDS_CONFIRM,不能降成 REMINDER。**
        #
        # 降级之后行为几乎不变:步骤照样 BLOCKED、下一步照样是「确认素材角色」。
        # 变的是**可见度**:`summarize()` 的「待确认」只数 NEEDS_CONFIRM,
        # 而前端给 REMINDER 的是 `badge: false` —— 列表页根本不计数。
        # 于是唯一能解开这条阻断的事情在看板上一处都不显示。
        #
        # 而 REMINDER 的语义是「可以带着它继续走」,对一件走不下去的商品
        # 说这句话本身就是错的。
        names = "、".join(_role_label(r) for r in facts.confirmable_roles)
        issues.append(
            Issue(
                level=IssueLevel.NEEDS_CONFIRM,
                code="MATERIAL_ROLE_UNCONFIRMED",
                message=f"有素材标着{names},但这个角色不是人工确认的",
                target_step=FlowStep.MATERIAL,
                hint=(
                    "门禁只认人工确认过的角色(§6.2):模型猜的、按文件名推的都不算。"
                    "到素材页看一眼再确认,比重新拍一张便宜"
                ),
                ref=facts.confirmable_roles[0],
            )
        )

    if facts.quarantined:
        issues.append(
            Issue(
                level=IssueLevel.NEEDS_CONFIRM,
                code="MATERIAL_QUARANTINED",
                message=f"{facts.quarantined} 条素材被隔离",
                target_step=FlowStep.MATERIAL,
                hint="隔离是合规预检的结论,有假阳性;人工复核后可放行",
            )
        )

    for role in RECOMMENDED_ROLES:
        if role not in facts.usable_roles:
            issues.append(
                Issue(
                    level=IssueLevel.REMINDER,
                    code="MATERIAL_MISSING_RECOMMENDED_ROLE",
                    message=f"没有{_role_label(role)}",
                    target_step=FlowStep.MATERIAL,
                    hint="不阻断上架,但缺背面/细节图会影响识别置信度",
                    ref=role,
                )
            )

    if any(i.level is IssueLevel.BLOCKING for i in issues):
        state = StepState.BLOCKED
    elif facts.quarantined:
        state = StepState.NEEDS_CONFIRM
    else:
        state = StepState.DONE

    summary = f"可用 {facts.usable}/{facts.total}"
    if facts.quarantined:
        summary += f",隔离 {facts.quarantined}"
    return StepResult(FlowStep.MATERIAL, state, summary, tuple(issues))


def _evaluate_attribute(
    facts: AttributeFacts, *, material_ready: bool,
    audience: AudienceFacts | None = None,
) -> StepResult:
    """属性步。**受众确认也在这一步**(§9.4:受众与品类同屏确认,不拆两步)。

    ## 为什么受众未确认是 NEEDS_CONFIRM 而不是 BLOCKING

    迁移 0030 **不回填**商品受众,存量商品全部是 NULL。做成阻断等于
    一夜之间把所有在途商品拦住,而运营此刻能做的只有逐件确认受众 ——
    `workbench/audience_rules.py` 的兼容矩阵为同一个理由留了同一条缝:
    「NULL 受众 + 无前缀规则包」放行并警告。两处的宽严必须一致,
    否则列表页说"可以走",导出闸说"不行",而两句话都是对的。

    取值不合法(`invalid`)则是**阻断**:那不是"没填",是库里存着一个
    认不出的字符串。给它兼容缝的话,这个错值会静默存在到永远。
    """
    audience = audience or AudienceFacts()
    missing = facts.missing_required
    total_required = len(facts.required_fields)
    done = total_required - len(missing)
    summary = f"已确认 {done}/{total_required}"
    if audience.invalid:
        summary = "受众取值非法 · " + summary
    elif audience.audience is None:
        summary = "受众待确认 · " + summary
    if facts.conflicted:
        summary += f",冲突 {len(facts.conflicted)}"
    # 单独报出来,不并进上面那个分数。**分子已经把它扣掉了** ——
    # 只扣不说的话,运营看到的是"已确认 3/5",而他明明确认过 5 个,
    # 于是他会再点一次那两个已经确认过的字段,以为自己刚才漏了
    if facts.stale_confirmed:
        summary += f",样品已变 {len(facts.stale_confirmed)}"

    if not material_ready:
        # 上游没就绪时**不产出字段级问题**。以前这里照常产出,于是一件
        # 刚建好的商品会显示「阻断 5」(2 条素材 + 每个必填属性 1 条),
        # 必填属性越多的品类数字越虚高 —— 而运营此刻能做的只有一件事:
        # 补素材。阻断数要能被信任,就不能把「等上游」也算成待办。
        # BLOCKED 状态本身已经说明了一切,`_decide_next` 也只看最上游那步。
        return StepResult(FlowStep.ATTRIBUTE, StepState.BLOCKED, summary, ())

    issues: list[Issue] = []

    # 受众排在字段问题之前:它决定了必填字段集本身(§18.3 属性按受众
    # 条件化)。受众没定就去确认属性,确认的是一份**可能是错的**清单 ——
    # 男装商品会被问领型、女装商品会被问腰头
    if audience.invalid:
        issues.append(
            Issue(
                level=IssueLevel.BLOCKING,
                code="AUDIENCE_INVALID",
                message="商品受众取值不合法",
                target_step=FlowStep.ATTRIBUTE,
                hint="库里存着一个认不出的受众值,请在商品资料里重新选择受众",
                ref="audience",
            )
        )
    elif audience.audience is None:
        issues.append(
            Issue(
                level=IssueLevel.NEEDS_CONFIRM,
                code="AUDIENCE_UNCONFIRMED",
                message="受众待确认",
                target_step=FlowStep.ATTRIBUTE,
                hint=(
                    "受众决定模特候选集、检查项与导出模板;"
                    "确认之前必填属性清单可能不是这件商品该填的那一份"
                ),
                ref="audience",
            )
        )

    for name in sorted(facts.conflicted):
        issues.append(
            Issue(
                level=IssueLevel.NEEDS_CONFIRM,
                code="ATTR_CONFLICT",
                message=f"{name} 存在多图结论冲突",
                target_step=FlowStep.ATTRIBUTE,
                hint="系统不会替你选。并列展示各图结论,人工定一个",
                ref=name,
            )
        )

    stale_confirmed = facts.stale_confirmed
    for name in missing:
        if name in facts.conflicted:
            continue  # 已经作为冲突报过一次,不重复计数

        # **过期排在最前面,因为它是唯一一个"有值"的欠账。**
        #
        # 这些字段在 `confirmed` 里,所以它们既不在 `suggested` 也不在
        # `candidate_only` —— 不单开一支的话,它们会掉进最后那个 `else`,
        # 而那句话是「尚无可用值」。那是**假的**:值就在那儿,人也点过头,
        # 变的是它依据的那批样品。
        #
        # 说错话的代价很具体:「尚无可用值」的建议是"先跑一次属性识别",
        # 于是运营去跑一次付费识别 —— 而识别产出的是 CANDIDATE,
        # 盖不掉已有的 MANUAL 值(§6.2 规则 4),那个字段照样过期。
        # 花了钱,什么都没变。
        #
        # 级别取 NEEDS_CONFIRM 不取 BLOCKING:阻断数要能被信任,而这一条
        # 欠的是"人再看一眼",与冲突同一档。它照样拦得住流程 ——
        # `missing_required` 已经把它算成欠账,属性步进不了 DONE。
        if name in stale_confirmed:
            issues.append(
                Issue(
                    level=IssueLevel.NEEDS_CONFIRM,
                    code="ATTR_FACT_STALE",
                    message=f"{name} 依据的样品已变化,需重新确认",
                    target_step=FlowStep.ATTRIBUTE,
                    hint=(
                        "这个字段确认过,但它当初依据的那批样品图变了"
                        "(补图、换图、隔离或改颜色归属)。核对一下结论还成不成立,"
                        "再确认一次即可;不必重跑识别 —— 识别产出的是建议值,"
                        "盖不掉人工确认过的值"
                    ),
                    ref=name,
                )
            )
            continue

        # **CANDIDATE 不在这个 `or` 里,那是 §11 修的那条。**
        #
        # 原来写的是 `name in facts.suggested or name in facts.candidate_only`,
        # 于是一个「留了证据但不采信」的字段会产出「有建议值待确认」这句话。
        # `AttributeStatus` 的文档字符串自己写着两者不能混:
        # 「未校准字段会和低分字段一起涌进待确认列表,人只会全部忽略」。
        #
        # 判定的单点在 `attributes/queue_policy`,service 层按它分桶;
        # 这里读的 `suggested` / `candidate_only` 已经是分好的结果。
        if name in facts.suggested:
            issues.append(
                Issue(
                    level=IssueLevel.NEEDS_CONFIRM,
                    code="ATTR_NOT_CONFIRMED",
                    message=f"{name} 有建议值待确认",
                    target_step=FlowStep.ATTRIBUTE,
                    hint="确认后才会进入文案的事实源",
                    ref=name,
                )
            )
        elif name in facts.candidate_only:
            # 自成一码而不是并进 `ATTR_NOT_CONFIRMED`:两者的下一步不同。
            # 「尚无可用值」的建议是「先跑一次属性识别」,而这条动线上
            # **识别已经跑过了**,再跑一次会得到同一批不采信的证据 ——
            # 把运营送去做一件确定无效、而且要花钱的事。
            issues.append(
                Issue(
                    level=IssueLevel.BLOCKING,
                    code="ATTR_EVIDENCE_ONLY",
                    message=f"{name} 有证据但不采信",
                    target_step=FlowStep.ATTRIBUTE,
                    hint=(
                        "模型给过值,但这个(字段 × 模型 × Prompt)还没校准、"
                        "或置信度低于下限,系统按 fail-closed 不采信。"
                        "再跑一次识别得到的是同一批证据,请人工直接填写"
                    ),
                    ref=name,
                )
            )
        else:
            issues.append(
                Issue(
                    level=IssueLevel.BLOCKING,
                    code="ATTR_NOT_CONFIRMED",
                    message=f"{name} 尚无可用值",
                    target_step=FlowStep.ATTRIBUTE,
                    hint="先跑一次属性识别,或人工直接填写",
                    ref=name,
                )
            )

    # material_ready 为假的分支已在函数开头提前返回
    if facts.extraction_count == 0 and not facts.confirmed:
        state = StepState.TODO
    elif facts.conflicted:
        state = StepState.NEEDS_CONFIRM
    elif missing:
        # 同上:CANDIDATE 不算「等人点头」。一件模型一个字段都没答上来的商品
        # 原来会落进 NEEDS_CONFIRM,而那一档在 `STATE_PROGRESS` 里记 0.6 分 ——
        # 于是它在列表里排得比真的做了一半的商品还靠后,而运营点进去
        # 一个可确认的值都找不到。
        #
        # 过期字段**算**「等人点头」:它有值、有人点过头,运营点进去就有
        # 一个可以确认的东西。这与 CANDIDATE 的处境相反(那一档点进去
        # 什么都没有),所以它跟 `suggested` 归一档而不是跟 `candidate_only`。
        state = (
            StepState.NEEDS_CONFIRM
            if all(m in facts.suggested or m in stale_confirmed for m in missing)
            else StepState.IN_PROGRESS
        )
    elif audience.invalid:
        # **这一行以前不在阶梯里。** 上面那条 `AUDIENCE_INVALID` 是 BLOCKING,
        # 而字段都确认完时这里会落到 `else` 判 DONE —— 一步同时"完成"和
        # "带着阻断",正是 `_no_done_while_blocking` 要拦的形状。靠兜底网
        # 拦住的代价是 `attribute_ready` 与 `_decide_next` 都拿不到这次降级
        # (那是本次修复的 R-1)。判据写回产出它的这一层,兜底网退回兜底。
        #
        # 判 `IN_PROGRESS` 而不是 `NEEDS_CONFIRM`:`NEEDS_CONFIRM` 在
        # `STATE_PROGRESS` 里记 0.6,而受众是个**错值**不是"待填",
        # 必填字段集本身可能是照着错受众算的 —— 那批确认过的字段不该按
        # 六成完成度计分。与 `_evaluate_image_set` 的 `violation_codes`
        # 同一档:东西在,但还不能用。
        state = StepState.IN_PROGRESS
    else:
        state = StepState.DONE

    return StepResult(FlowStep.ATTRIBUTE, state, summary, tuple(issues))


#: 图片集问题码 -> 给运营看的一句话。规则层的码是给程序看的。
IMAGE_SET_VIOLATION_MESSAGES: Mapping[str, str] = {
    "MISSING_PRIMARY_FOR_VARIANT": "没有指定主图",
    "MULTIPLE_PRIMARY_FOR_VARIANT": "指定了多张主图",
    "MISSING_IMAGE_FOR_VARIANT": "有变体没有配图",
    "UNTRUSTED_PRIMARY_ROLE": "主图的角色是模型猜的,把握不足",
    "UNUSABLE_ASSET": "引用了被隔离或未就绪的素材",
    "EMPTY_SET": "图片集里没有任何图片",
}


def _evaluate_image_set(
    facts: ImageSetFacts, *, upstream_ready: bool
) -> StepResult:
    issues: list[Issue] = []

    for code in facts.violation_codes:
        issues.append(
            Issue(
                level=IssueLevel.BLOCKING,
                code=f"IMAGESET_{code}",
                message=IMAGE_SET_VIOLATION_MESSAGES.get(code, code),
                target_step=FlowStep.IMAGE_SET,
                hint="图片集的问题一律阻断批准 —— 主图放错是消费者第一眼看到的错误",
                ref=code,
            )
        )

    if facts.downgraded:
        issues.append(
            Issue(
                level=IssueLevel.NEEDS_CONFIRM,
                code="IMAGESET_DOWNGRADED",
                message="引用的素材被隔离,已批准的图片集被降级为待复核",
                target_step=FlowStep.IMAGE_SET,
                hint="换掉那张素材再重新批准,或先放行该素材",
            )
        )

    if facts.status == "REJECTED":
        issues.append(
            Issue(
                level=IssueLevel.BLOCKING,
                code="IMAGESET_REJECTED",
                message="图片集被快审退回:" + _reject_label(facts.reject_reason),
                target_step=FlowStep.IMAGE_SET,
                hint="按退回原因处理完再重新批准;改内容会派生出新版本",
                ref=facts.reject_reason,
            )
        )

    if not upstream_ready:
        state = StepState.BLOCKED
    elif not facts.exists:
        state = StepState.TODO
    elif facts.violation_codes:
        # **F-1 的根因就在这一行的缺席。**
        #
        # 上一版是 `elif facts.status == "APPROVED": state = DONE`,不看
        # `violation_codes`。于是:一版已批准的图片集,之后新增一个 ACTIVE
        # 颜色 -> `MISSING_IMAGE_FOR_VARIANT` 进 issues,而这一步照样显示
        # DONE。同一个响应里 `colors.worst_by_step.IMAGE_SET` 是 BLOCKED ——
        # 两句话都出自本系统,运营会先信那个绿勾,然后在导出时被门禁拦住。
        #
        # 判 `IN_PROGRESS` 而不是 `BLOCKED`,与 `_evaluate_copy` 的
        # `blocking_violations` 和 `_evaluate_draft` 的 `error_count` 同一档:
        # 本层的 `BLOCKED`(素材步除外)专指"上游未就绪、不携带 issue",
        # 用它表示"这一版有硬失败"会破坏 `StepState` 文档写死的那条口径,
        # 而 `wizard.step_is_open` 正是按那条口径决定这一步能不能进 ——
        # 判 BLOCKED 会让运营连进去修都进不去。
        #
        # 上面那条 issue 的 hint 一直写着"图片集的问题一律阻断批准"。
        # 意图从来是明确的,只有状态没跟上。
        state = StepState.IN_PROGRESS
    elif facts.status == "APPROVED":
        state = StepState.DONE
    else:
        state = StepState.IN_PROGRESS

    if not facts.exists:
        summary = "未编排"
    else:
        summary = f"v{facts.version} · {facts.item_count} 张 · {facts.status}"
    return StepResult(FlowStep.IMAGE_SET, state, summary, tuple(issues))


def _evaluate_copy(facts: CopyFacts, *, upstream_ready: bool) -> StepResult:
    issues: list[Issue] = []

    if facts.blocking_violations:
        issues.append(
            Issue(
                level=IssueLevel.BLOCKING,
                code="COPY_BLOCKING_VIOLATION",
                message=f"文案有 {facts.blocking_violations} 处硬失败",
                target_step=FlowStep.COPY,
                hint="字数、禁词、无依据声明都在这一类。修完才能批准",
            )
        )
    if facts.warning_violations:
        issues.append(
            Issue(
                level=IssueLevel.REMINDER,
                code="COPY_WARNING",
                message=f"文案有 {facts.warning_violations} 处提醒",
                target_step=FlowStep.COPY,
                hint="同义词词典扫出的疑似不一致。词典不全,人工判断即可",
            )
        )
    if facts.stale:
        issues.append(
            Issue(
                level=IssueLevel.NEEDS_CONFIRM,
                code="COPY_STALE",
                message="属性在文案批准之后发生了变化,文案已过期",
                target_step=FlowStep.COPY,
                hint="重新生成或人工复核,确认文案里的事实仍然成立",
            )
        )

    # A10:文案的 `REJECTED` 有两个来源 —— 规则硬失败(save_copy / revalidate 置的)
    # 和快审退回。判据是**有没有退回原因**,不是状态本身:只看状态的话,
    # 一版因禁词被驳回的文案会在这里被说成"快审退回:未注明原因",
    # 而硬失败那一条 issue 已经在上面出过了,运营会看到两句互相矛盾的话
    if facts.status == "REJECTED" and facts.reject_reason:
        issues.append(
            Issue(
                level=IssueLevel.BLOCKING,
                code="COPY_REJECTED",
                message="文案被快审退回:" + _reject_label(facts.reject_reason),
                target_step=FlowStep.COPY,
                hint="按退回原因改完再重新校验与批准",
                ref=facts.reject_reason,
            )
        )

    if not upstream_ready:
        state = StepState.BLOCKED
    elif not facts.exists:
        state = StepState.TODO
    elif facts.stale:
        state = StepState.STALE
    elif facts.blocking_violations:
        # **这一支以前排在 `APPROVED` 之后**,于是"已批准的文案后来被扫出
        # 硬失败"判 DONE —— 与 F-1 在图片集上的那个缺陷**同一个形状**
        # (`elif status == "APPROVED"` 不看 violation),而 F-1 只修了图片集
        # 那一边。规则包更新、`revalidate` 重扫都会造出这一档:文案没动,
        # 判据变严了。
        #
        # 排在 `stale` 之后:两者都成立时该说的是"属性变了要重新生成",
        # 修一版马上要作废的文案是白干。
        state = StepState.IN_PROGRESS
    elif facts.status == "APPROVED":
        state = StepState.DONE
    else:
        state = StepState.IN_PROGRESS

    if not facts.exists:
        summary = "未生成"
    else:
        summary = f"v{facts.version} · {facts.status}"
        if facts.locale:
            summary = f"{facts.locale} · " + summary
    return StepResult(FlowStep.COPY, state, summary, tuple(issues))


def _evaluate_draft(facts: DraftFacts, *, upstream_ready: bool) -> StepResult:
    issues: list[Issue] = []

    if facts.error_count:
        issues.append(
            Issue(
                level=IssueLevel.BLOCKING,
                code="DRAFT_FIELD_ERROR",
                message=f"草稿有 {facts.error_count} 个字段不合格",
                target_step=FlowStep.DRAFT,
                hint="必填缺失、超长、枚举不匹配都在这一类,阻断正式导出",
            )
        )
    if facts.warning_count:
        issues.append(
            Issue(
                level=IssueLevel.REMINDER,
                code="DRAFT_FIELD_WARNING",
                message=f"草稿有 {facts.warning_count} 个字段有提醒",
                target_step=FlowStep.DRAFT,
            )
        )
    if facts.stale:
        issues.append(
            Issue(
                level=IssueLevel.BLOCKING,
                code="DRAFT_STALE",
                message="上游已变化,这份草稿是旧事实",
                target_step=FlowStep.DRAFT,
                hint="过期草稿不允许导出。重新生成一次即可,不会丢手填字段",
            )
        )
    if facts.open_rejections:
        # OPS-REVIEW P1:平台驳回是阻断,不是提醒。它挂在草稿步上
        # (五个流程步里没有"平台"一步,而驳回指向的产物正是这份草稿),
        # 于是异常页的草稿分区会自动收下它,阻断计数也把它算进去 ——
        # 驳回件从此不可能再顶着绿色"已完成"沉在列表底部。
        issues.append(
            Issue(
                level=IssueLevel.BLOCKING,
                code="PLATFORM_REJECTED",
                message=f"平台驳回 {facts.open_rejections} 条未解决",
                target_step=FlowStep.DRAFT,
                hint="到导出页看驳回原因与当时导出的版本;修复上游、重导后标记解决",
            )
        )

    if not upstream_ready:
        state = StepState.BLOCKED
    elif not facts.exists:
        state = StepState.TODO
    elif facts.stale:
        state = StepState.STALE
    elif facts.error_count:
        state = StepState.IN_PROGRESS
    elif facts.open_rejections:
        # 已导出但被平台驳回:这份产出**不能用**,和过期在结果上是一回事,
        # 所以不给 DONE。用 IN_PROGRESS 而不是新造一个状态:
        # 运营要做的确实是"回来接着干活",而每加一个 StepState,
        # 列表五列、筛选、前端文案表、异常判定都要跟着长一格。
        state = StepState.IN_PROGRESS
    elif facts.exported:
        state = StepState.DONE
    elif facts.status in ("VALIDATED", "APPROVED"):
        state = StepState.NEEDS_CONFIRM  # 校验过了,等人点导出
        # 状态是 NEEDS_CONFIRM 就必须有一条对应的问题。以前这里没有,
        # 于是列表页草稿列亮着「待确认」,顶部的待确认计数却不含它 ——
        # 两处口径不一致正是本模块声明要杜绝的事。点导出确实是
        # 「要人做一个决定」,把它算进待确认没有语义上的勉强。
        issues.append(
            Issue(
                level=IssueLevel.NEEDS_CONFIRM,
                code="DRAFT_AWAITING_EXPORT",
                message="草稿已通过校验,等待人工导出",
                target_step=FlowStep.DRAFT,
                hint="核对预览无误后点「导出上架文件」",
            )
        )
    else:
        state = StepState.IN_PROGRESS

    if not facts.exists:
        summary = "未生成"
    else:
        summary = str(facts.status)
        if facts.exported:
            summary += " · 已导出"
        if facts.open_rejections:
            summary += f" · 平台驳回 {facts.open_rejections}"
        elif facts.platform_status and facts.platform_status != "NONE":
            label = PLATFORM_STATUS_LABELS.get(facts.platform_status, facts.platform_status)
            summary += f" · {label}"
    return StepResult(FlowStep.DRAFT, state, summary, tuple(issues))


# ---------------------------------------------------------------- 下一步


def _decide_next(flow: ProductFlow, steps: Mapping[FlowStep, StepResult]) -> NextAction:
    """算出**唯一**的下一步。

    规则很简单:按 `STEP_ORDER` 走,第一个没做完的步骤决定动作。
    于是「不出现非法下一步」是结构保证的 —— 不可能在属性没确认时
    推荐去生成文案,因为属性那一步会先被撞上。
    """

    def action(code: NextActionCode, reason: str) -> NextAction:
        return NextAction(
            code=code, label=ACTION_LABELS[code], step=ACTION_STEP[code], reason=reason
        )

    def why(step: StepResult) -> str:
        """这一步为什么没完。**取它自己产出的第一条 issue 文案,不重拼。**

        与素材步那条 `missing` 同一个理由:重拼出来的话,判定说一句、
        按钮理由说另一句,而两句都出自本系统。
        """
        return next((i.message for i in step.issues), step.summary or step.state.value)

    # ---- 第一步:建档(§14.1 / AC-14 的七步之首) ----
    #
    # 这一段以前**不存在** —— `_decide_next` 的 docstring 写着「按 `STEP_ORDER`
    # 走,第一个没做完的步骤决定动作」,而实现是从 `MATERIAL` 开始的。于是
    # 一行没挂 SPU/颜色的 SKU 会得到 `current_step=SETUP`(那一份按 STEP_ORDER
    # 真的从头走)和 `next_action=EXPORT` —— 同一个响应里两个下一步,
    # 而 AC-15 的原话是「唯一下一步」。
    #
    # 判 `COMPLETE_SETUP` 不会误伤存量:归属齐了就是 DONE,而生产路径上
    # `has_spu_ref` / `has_colour_ref` 由 `service._setup_facts` 直接读列,
    # 不会是 `None`(那一档是给"没查过"留的防御分支)。
    setup = steps[FlowStep.SETUP]
    if setup.state is not StepState.DONE:
        return action(NextActionCode.COMPLETE_SETUP, why(setup))

    material = steps[FlowStep.MATERIAL]
    if material.state is StepState.BLOCKED:
        # ---- 先看有没有「确认一下就能解开」的图 ----
        #
        # 排在补图之前是有代价对比的:确认一个角色是点一下,重新拍一张图
        # 是一天。而 `confirmable_roles` 非空**必然**意味着门禁没过
        # (见 `sample_completeness.confirmable_roles` 的性质),
        # 所以这一支不会抢走一件本来就能往下走的商品。
        confirm = next(
            (i for i in material.issues if i.code == "MATERIAL_ROLE_UNCONFIRMED"), None
        )
        if confirm is not None:
            return action(NextActionCode.CONFIRM_ASSET_ROLE, confirm.message)

        # ---- 理由**直接取 issue 文案,不从 `ref` 重拼** ----
        #
        # `ref` 按设计只带满足组里的第一个角色(它是跳转依据)。从它重拼的
        # 后果是判定说「正面图**或**平铺图」、按钮理由说「缺少正面图」——
        # 本批要修的那个坏循环原样回来:运营为了消掉那句话,
        # 会把平铺图改标成正面图。
        missing = [
            i.message for i in material.issues if i.code == "MATERIAL_MISSING_REQUIRED_ROLE"
        ]
        if missing:
            return action(NextActionCode.UPLOAD_MATERIAL, "、".join(missing))
        return action(NextActionCode.UPLOAD_MATERIAL, "还没有可用素材")
    if material.state is StepState.NEEDS_CONFIRM:
        return action(
            NextActionCode.RELEASE_QUARANTINE,
            f"{flow.material.quarantined} 条素材被隔离,需要人工复核",
        )

    attribute = steps[FlowStep.ATTRIBUTE]

    # ---- §8.2 / §9.4:确认受众与品类 ----
    #
    # 位置有两条约束,这一行同时满足:
    #
    #   排在补素材之后   §8.2 的处理顺序里「需要补充资料」是第 3 条,
    #                   「等待确认受众」是第 4 条
    #   排在选模特之前   §8.2 明文:受众确认之后模特候选集才是对的。
    #                   选模特发生在素材与属性就绪后的生成阶段,
    #                   所以放在属性步之首就够了 —— 结构保证,不靠约定
    #
    # **但不把已经走完属性的存量商品拉回来。** 迁移 0030 不回填受众,
    # 存量商品全是 NULL;`audience_rules` 的兼容矩阵放行「NULL 受众 +
    # 无前缀规则包」并只给警告。如果这里无条件抢先,那批商品会被永久
    # 挡在导出之前 —— 而导出闸恰恰说它们可以过。两处的宽严必须一致。
    if attribute.state is not StepState.DONE:
        if flow.audience.invalid:
            return action(
                NextActionCode.CONFIRM_AUDIENCE,
                "商品受众取值不合法,请重新选择受众",
            )
        if flow.audience.audience is None:
            return action(
                NextActionCode.CONFIRM_AUDIENCE,
                "受众未确认;它决定模特候选集、检查项与导出模板",
            )

    if attribute.state is not StepState.DONE:
        if flow.attribute.conflicted:
            names = "、".join(sorted(flow.attribute.conflicted))
            return action(NextActionCode.RESOLVE_CONFLICT, f"{names} 有冲突结论待人工裁决")
        if flow.attribute.extraction_count == 0:
            return action(NextActionCode.RUN_EXTRACTION, "还没跑过属性识别")
        missing = flow.attribute.missing_required
        # **先问「有没有东西可确认」,再决定说哪句话**(PRD §11)。
        #
        # 原来这里无条件说「确认属性」。识别跑过、而结论全是 CANDIDATE 时
        # 那句话是假的:确认队列是空的。运营点开属性页找不到可点的东西,
        # 下一步多半是去点「启动属性识别」—— 一次真实付费调用,
        # 换回同一批不采信的证据。
        pending = [m for m in missing if m in flow.attribute.suggested]
        if pending:
            return action(
                NextActionCode.CONFIRM_ATTRIBUTES,
                "待确认:" + "、".join(pending[:3]) + ("…" if len(pending) > 3 else ""),
            )
        if missing:
            return action(
                NextActionCode.FILL_ATTRIBUTES,
                "待填写:" + "、".join(missing[:3]) + ("…" if len(missing) > 3 else ""),
            )
        return action(NextActionCode.CONFIRM_ATTRIBUTES, "属性尚未全部确认")

    # ---- 第四步:生成方案 ----
    #
    # 同上,这一段以前也不存在,而它比建档那一条更贵:`PLAN_MISSING` 是
    # **BLOCKING**,于是一件没配方案的商品会显示「阻断 1」、`current_step=PLAN`,
    # 而按钮说「编排图片集」。运营照着按钮走,排出来的图没有模特与参数依据。
    #
    # 位置在属性之后、图片集之前,与 `STEP_ORDER` 一致 —— 不是新口径:
    # `_evaluate_plan` 的 upstream 本来就是属性(§8.2:受众确认之后
    # 模特候选集才是对的)。
    plan = steps[FlowStep.PLAN]
    if plan.state is not StepState.DONE:
        return action(NextActionCode.CHOOSE_PLAN, why(plan))

    image_set = steps[FlowStep.IMAGE_SET]
    if image_set.state is not StepState.DONE:
        if not flow.image_set.exists:
            return action(NextActionCode.BUILD_IMAGE_SET, "还没有图片集")
        # A10:被退回过的,下一步由退回原因决定 —— 这一条必须排在
        # violation_codes 之前。退回往往正是因为"校验全过但人眼看着不对",
        # 那时 violation_codes 是空的,落到最后一行就又变成"等待批准",
        # 于是运营刚退回的那件立刻走回队列排在原处
        if flow.image_set.status == "REJECTED":
            return action(
                reject_next_action("image_set", flow.image_set.reject_reason),
                "快审退回:" + _reject_label(flow.image_set.reject_reason),
            )
        if flow.image_set.violation_codes:
            first = flow.image_set.violation_codes[0]
            return action(
                NextActionCode.FIX_IMAGE_SET,
                IMAGE_SET_VIOLATION_MESSAGES.get(first, first),
            )
        return action(NextActionCode.APPROVE_IMAGE_SET, "图片集校验通过,等待批准")

    copy_step = steps[FlowStep.COPY]
    if copy_step.state is not StepState.DONE:
        if not flow.copy.exists:
            return action(NextActionCode.GENERATE_COPY, "还没有文案")
        if flow.copy.stale:
            return action(NextActionCode.GENERATE_COPY, "属性变了,文案需要重新生成")
        # A10:同上,退回优先于校验问题。判据带上 reject_reason ——
        # 规则硬失败也会把状态置成 REJECTED,那一类的下一步是"修复文案",
        # 由下面那条按 blocking_violations 给出,不该冒充成快审退回
        if flow.copy.status == "REJECTED" and flow.copy.reject_reason:
            return action(
                reject_next_action("copy", flow.copy.reject_reason),
                "快审退回:" + _reject_label(flow.copy.reject_reason),
            )
        if flow.copy.blocking_violations:
            return action(
                NextActionCode.FIX_COPY,
                f"{flow.copy.blocking_violations} 处硬失败未解决",
            )
        return action(NextActionCode.APPROVE_COPY, "文案校验通过,等待批准")

    draft = steps[FlowStep.DRAFT]
    if draft.state is StepState.DONE:
        return action(NextActionCode.DONE, "已导出,没有待办")
    if not flow.draft.exists:
        return action(NextActionCode.BUILD_DRAFT, "图片集与文案都已批准,可以生成草稿")
    if flow.draft.stale:
        return action(NextActionCode.REFRESH_DRAFT, "上游已变化,草稿需要重新生成")
    if flow.draft.error_count:
        return action(NextActionCode.FIX_DRAFT, f"{flow.draft.error_count} 个字段不合格")
    if flow.draft.open_rejections:
        # 排在过期与字段错误之后:驳回的修复路径本身就会让草稿过期
        # (换图片集版本、改文案),那时该显示的机械下一步是"重新生成草稿";
        # 机械步骤都走完之后,这个动作稳定地指着"重导并标记解决",
        # 直到驳回记录被人关掉为止 —— 不会在导出前后来回跳。
        return action(
            NextActionCode.RESOLVE_REJECTION,
            f"平台驳回 {flow.draft.open_rejections} 条未解决;修复后重导并标记解决",
        )
    return action(NextActionCode.EXPORT, "草稿校验通过,可以导出")


# ---------------------------------------------------------------- 入口


def _no_done_while_blocking(step: StepResult) -> StepResult:
    """结构不变量:**带着阻断问题的一步不许显示 DONE。**

    ## 为什么要有这一层,而不是只修出问题的那一个分支

    F-1 是这条不变量的第一次破例(图片集 APPROVED 时不看 `violation_codes`),
    而它活了两批没被任何测试看见。逐个分支去修的问题是:下一个人写第八步时,
    同一行会再手滑一次,而**判定层自己的穷举照样全绿** —— 那次穷举验的是
    "给定这组 facts,状态对不对",不是"状态和它自己产出的 issue 一致不一致"。

    放在这里,新增步骤天生就受它管。

    ## 为什么降到 `IN_PROGRESS` 而不是 `BLOCKED`

    `StepState` 的文档写死:除素材步外,`BLOCKED` 一律指**上游未就绪**且
    不携带任何 issue。而 `wizard.step_is_open` 正是按这条决定"这一步能不能进" ——
    判 BLOCKED 会让运营连进去修都进不去,而这一步恰恰有活要干。

    `IN_PROGRESS` 与 `_evaluate_copy` 的 `blocking_violations`、
    `_evaluate_draft` 的 `error_count` 同一档:东西在,但还不能用。

    ## 它与颜色维的关系(F-1 的另一半)

    AC-15 要求"商品级与颜色级冲突时有明确、唯一的合并规则"。这条不变量
    就是那个规则的一半,而且它落在**三处共用的那一份判定**上 ——
    列表页、详情页、向导都读 `flow.evaluate()` 的结果,所以修在这里
    三处同时对。合并规则的另一半(哪些步骤不下压)写在
    `color_flow` 的模块文档里。
    """
    if step.state is not StepState.DONE:
        return step
    if not any(i.level is IssueLevel.BLOCKING for i in step.issues):
        return step
    return replace(step, state=StepState.IN_PROGRESS)


def evaluate(flow: ProductFlow) -> FlowResult:
    """判定一件商品。**列表页与详情页都调这一个函数。**

    ## 为什么每一步算完**当场**过一次 `_no_done_while_blocking`

    上一版是在最后统一过一遍(`ordered = tuple(_no_done_while_blocking(...))`),
    而 `attribute_ready` / `image_set_ready` / `copy_ready` 取的是**降级前**
    那一份,`_decide_next` 收到的也是降级前的 `by_step`。于是降级只改显示,
    不改下游判定 —— 出参里会出现这种组合:

        ATTRIBUTE=IN_PROGRESS(带 AUDIENCE_INVALID)
        IMAGE_SET=DONE  COPY=DONE            上游没就绪却已完成
        current_step=ATTRIBUTE  next_action=EXPORT   同一份结果里两个下一步

    三处后果都不是显示问题:`wizard.step_is_open` 只把 `BLOCKED` 当关,
    于是那些本该关着的步骤全开,AC-05 那道闸(`api/action_gate`)读的正是
    这份判定,`EXPORT` 会被放行;前端 `detectFlowAnomaly` 则把这种组合
    判成 P0,列表页与详情页的 CTA 一起消失,商品点不动。

    放在这里之后,降级会顺着 `*_ready` 往下传:上游被降级 = 下游 `BLOCKED`,
    与"上游本来就没做完"走同一条路。新增第八步天生受它管。
    """
    setup = _no_done_while_blocking(_evaluate_setup(flow.setup))

    material = _no_done_while_blocking(_evaluate_material(flow.material))
    material_ready = material.state in (StepState.DONE, StepState.NEEDS_CONFIRM)

    attribute = _no_done_while_blocking(
        _evaluate_attribute(
            flow.attribute, material_ready=material_ready, audience=flow.audience
        )
    )
    attribute_ready = attribute.state is StepState.DONE

    plan = _no_done_while_blocking(
        _evaluate_plan(flow.plan, upstream_ready=attribute_ready)
    )

    image_set = _no_done_while_blocking(
        _evaluate_image_set(flow.image_set, upstream_ready=attribute_ready)
    )
    image_set_ready = image_set.state is StepState.DONE

    copy_step = _no_done_while_blocking(
        _evaluate_copy(flow.copy, upstream_ready=attribute_ready)
    )
    copy_ready = copy_step.state is StepState.DONE

    draft = _no_done_while_blocking(
        _evaluate_draft(flow.draft, upstream_ready=image_set_ready and copy_ready)
    )

    by_step = {
        FlowStep.SETUP: setup,
        FlowStep.MATERIAL: material,
        FlowStep.ATTRIBUTE: attribute,
        FlowStep.PLAN: plan,
        FlowStep.IMAGE_SET: image_set,
        FlowStep.COPY: copy_step,
        FlowStep.DRAFT: draft,
    }
    ordered = tuple(by_step[s] for s in STEP_ORDER)

    issues = tuple(i for s in ordered for i in s.issues)
    completion = round(
        sum(STATE_PROGRESS[s.state] * STEP_WEIGHTS[s.step] for s in ordered)
    )

    current = next(
        (s.step for s in ordered if s.state is not StepState.DONE), FlowStep.DRAFT
    )

    return FlowResult(
        steps=ordered,
        issues=issues,
        completion=int(completion),
        blocking_count=sum(1 for i in issues if i.level is IssueLevel.BLOCKING),
        pending_count=sum(1 for i in issues if i.level is IssueLevel.NEEDS_CONFIRM),
        reminder_count=sum(1 for i in issues if i.level is IssueLevel.REMINDER),
        current_step=current,
        next_action=_decide_next(flow, by_step),
    )


def matches_filters(
    result: FlowResult,
    *,
    step: str | None = None,
    state: str | None = None,
    only_blocked: bool = False,
    next_action: str | None = None,
    audience: str | None = None,
    flow: ProductFlow | None = None,
) -> bool:
    """列表筛选(FE-106)。**判定在这里,不在 SQL 里。**

    这些条件全都由判定结果派生,SQL 里没有对应的列。放在这里的代价是
    分页要在内存里做,收益是筛选结果和界面上显示的状态**永远一致** ——
    两处各算一遍的话,「筛了草稿校验失败却看到一件通过的」这类问题
    会非常难查。首期只做单商品闭环,量级完全撑得住。
    """
    if only_blocked and result.blocking_count == 0:
        return False
    if next_action and result.next_action.code.value != next_action:
        return False
    # 受众筛选(PRD v2)。**它是批量导出闸口的配套动作,不是装饰。**
    #
    # 批量导出在批次内出现两个规则包时会拒绝,并让运营"按受众分成两个批次
    # 分别导出" —— 而在这个参数之前,界面上没有任何手段能按受众把批次拆开。
    # §9.5 要求"错误必须包含行动";那条错误给了指令,这里补上行动。
    #
    # `UNCONFIRMED` 是筛"待确认"那一档。它不是 Audience 的第四个取值
    # (§3.4 规则 3),只是这个筛选参数的一个哨兵值 —— 所以判定写在这里,
    # 不写进枚举。
    #
    # **`flow` 缺席时抛错,不是静默跳过筛选。** 与 `/model-templates` 那条
    # 一样的道理:一个筛选条件被静默忽略之后,调用方拿到的是**全量**,
    # 而他以为那就是筛选结果。那种错误不报错、也不长得像错。
    if audience:
        if flow is None:
            raise ValueError(
                "按受众筛选需要同时传入 flow(受众事实在它身上);"
                "静默跳过筛选会让调用方把全量当成筛选结果"
            )
        want = audience.strip().upper()
        if want not in _AUDIENCE_FILTER_VALUES:
            # 拼错的取值必须报错而不是"匹配不上返回空"。后者在界面上是
            # 「这个受众一件商品都没有」—— 一句错得很有说服力的话
            raise ValueError(
                f"受众筛选取值不合法:{audience!r};"
                f"可用:{', '.join(sorted(_AUDIENCE_FILTER_VALUES))}"
            )
        got = (flow.audience.audience or "").upper()
        if want == "UNCONFIRMED":
            if got:
                return False
        elif got != want:
            return False
    if step:
        try:
            want = FlowStep(step)
        except ValueError:
            return False
        if state:
            return result.step_state(want).value == state
        return result.current_step is want
    if state:
        return any(s.state.value == state for s in result.steps)
    return True


def summarize(results: Iterable[FlowResult]) -> dict[str, Any]:
    """列表页顶部的计数。运营先看这个决定今天从哪儿下手。

    ## 为什么这里还要按动作码分一份(A9)

    「今日待办」首页的七张卡片问的是「待确认属性有几件、待审核图片有几件」——
    也就是**按唯一下一步分组的件数**。这份数字有三个可能的产地:

        前端自己数            列表接口只返回当页 20 条,数出来的是"这一页有几件",
                              而首页要说的是"一共有几件"。且 §3.2.1 已经定过纪律:
                              前端不推算状态,状态与计数都原样来自接口
        七次带筛选的请求      每次 `?next_action=X&page_size=1` 读 total。数字是对的,
                              但每次请求都要对全部商品跑一遍 flow 判定 ——
                              首页一次打开等于七遍全量扫描
        这里多分一次组        判定已经算完了,分组只是同一个循环里多记一笔

    所以选第三个。它不新增接口、不新增能力,只是把已经算出来的结论多说一句。

    ## 零值也要在

    十七个动作码全部零填。少一个键,首页的卡片会在计数归零时整张消失,
    于是七张卡片的位置每次打开都不一样 —— 而运营是靠肌肉记忆点第三张的。
    """
    by_action: dict[str, int] = {code.value: 0 for code in NextActionCode}
    counts: dict[str, Any] = {
        "total": 0,
        "blocked": 0,
        "needs_confirm": 0,
        "exportable": 0,
        "done": 0,
        "by_next_action": by_action,
    }
    for r in results:
        counts["total"] += 1
        if r.blocking_count:
            counts["blocked"] += 1
        if r.pending_count:
            counts["needs_confirm"] += 1
        if r.next_action.code is NextActionCode.EXPORT:
            counts["exportable"] += 1
        if r.next_action.code is NextActionCode.DONE:
            counts["done"] += 1
        # `exportable` / `done` 与这里重复计了同一件事,刻意不合并:
        # 上面两个键是既有契约,列表页顶部还在用
        by_action[r.next_action.code.value] += 1
    return counts


def step_states(result: FlowResult) -> dict[str, str]:
    """五个子状态的扁平映射。列表页一行五列直接用它。"""
    return {s.step.value: s.state.value for s in result.steps}


def issues_of(result: FlowResult, level: IssueLevel) -> Sequence[Issue]:
    return [i for i in result.issues if i.level is level]
