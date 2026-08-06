"""平台侧闭环判定(OPS-REVIEW P1:驳回回流)。**零数据库依赖纯函数。**

流程原本在"文件已下载"处结束,但运营的一天还有第三幕:

    上传平台 -> 平台审核 -> 部分被驳回 -> 定位驳回的是哪一版 -> 修 -> 重导

本文件回答其中三个需要被穷举测试的问题:

    平台状态能不能这样改        `manual_targets()` —— REJECTED 只能由记录驳回产生
    驳回指向的是哪一次导出      `locate_export()` —— 定位到当时的指纹与版本
    驳回原因指向哪个流程步骤    `RejectionReason` 的 target_step 映射

## 为什么"定位到那一版"是可做的

图片集与文案都是版本化不可变的(`image_sets.py` 模块注释通篇写着
"驳回回流第一件事是定位到具体那一版")。草稿行虽然会被重建复用,但
**每次导出都在审计日志留了指纹**,而指纹与组件版本的对应关系从本轮起
也进了导出审计的 payload(`service.record_export`)。于是驳回记录能把
"平台驳回的那个文件"钉到 图片集 vN + 文案 vM 上,哪怕草稿其后已重建过。

## 定位有三种把握,必须如实标注

    audit          导出审计里带了组件版本(本轮之后的导出),最可信
    current_draft  审计没带版本,但当前草稿的指纹与那次导出一致 ——
                   草稿没重建过,当前快照就是当时的快照
    unlocated      老导出 + 草稿已重建。指纹还在,版本对应关系已不可考

把 unlocated 硬凑成一个版本号,比承认"定位不到"更糟:运营会按错的
版本去改图。三种把握都写在记录上,界面照实显示。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from app.core.enums import PlatformStatus, RejectCategory, RejectionStatus
from app.workbench.flow import PLATFORM_STATUS_LABELS, FlowStep

__all__ = [
    "EXPORT_AUDIT_ACTION",
    "PREVIEW_EXPORT_AUDIT_ACTION",
    "DELIVERY_EXPORT_FORMATS",
    "PLATFORM_STATUS_LABELS",
    "REJECTION_REASON_ADVICE",
    "REJECTION_REASON_LABELS",
    "REJECTION_REASON_STEP",
    "REJECTION_STATUS_LABELS",
    "REJECT_CATEGORY_REASON",
    "reason_for_category",
    "LOCATED_BY_LABELS",
    "LOCATED_BY_PUBLISH_ATTEMPT",
    "GATE_CONFIRM_NOTE",
    "MISSING_FINGERPRINT_UNCHANGED",
    "MISSING_NO_NEW_EXPORT",
    "RESOLVE_GATE_ADVICE",
    "RESOLVE_GATE_LABELS",
    "LocatedExport",
    "RejectionReason",
    "ResolveGate",
    "export_audit_payload",
    "export_entry",
    "fingerprints_match",
    "locate_export",
    "manual_targets",
    "RESOLVE_REFUSED",
    "RESOLVE_TO_FIXED_PENDING",
    "RESOLVE_TO_RESOLVED",
    "resolve_gate",
    "resolve_target",
    "trim_components",
]


class RejectionReason(StrEnum):
    """平台驳回原因码。

    这不是各平台驳回码的复刻(那是接平台 API 之后的事),而是**运营视角的
    分诊**:驳回原话贴在 `platform_note` 里原样留档,原因码只回答一个问题 ——
    该去哪个标签页改。所以每个码都必须映射到一个流程步骤,漏一个,
    「去处理」按钮就不知道往哪跳(`REJECTION_REASON_STEP` 的闭合性受测)。
    """

    IMAGE_ISSUE = "IMAGE_ISSUE"                  # 图片不合规:主图、水印、尺寸、背景
    COPY_VIOLATION = "COPY_VIOLATION"            # 文案违规:禁词、夸大声明、字数
    ATTRIBUTE_MISMATCH = "ATTRIBUTE_MISMATCH"    # 属性与实物/图文不符
    QUALIFICATION_MISSING = "QUALIFICATION_MISSING"  # 资质/检测报告缺失
    CATEGORY_ERROR = "CATEGORY_ERROR"            # 类目挂错
    PRICE_ISSUE = "PRICE_ISSUE"                  # 价格异常(手填字段)
    OTHER = "OTHER"                              # 其它,以平台原话为准


REJECTION_REASON_LABELS: Mapping[RejectionReason, str] = {
    RejectionReason.IMAGE_ISSUE: "图片不合规",
    RejectionReason.COPY_VIOLATION: "文案违规",
    RejectionReason.ATTRIBUTE_MISMATCH: "属性与图文不符",
    RejectionReason.QUALIFICATION_MISSING: "资质/检测报告缺失",
    RejectionReason.CATEGORY_ERROR: "类目挂错",
    RejectionReason.PRICE_ISSUE: "价格异常",
    RejectionReason.OTHER: "其它(见平台原话)",
}

#: 原因码 -> 该去改的流程步骤。「去处理」按钮按它跳标签页。
#:
#: 资质与类目在首期没有独立步骤,归到草稿(手填字段与类目都在草稿层);
#: OTHER 也归草稿 —— 那是导出物所在的一步,从那里能看到全貌。
REJECTION_REASON_STEP: Mapping[RejectionReason, FlowStep] = {
    RejectionReason.IMAGE_ISSUE: FlowStep.IMAGE_SET,
    RejectionReason.COPY_VIOLATION: FlowStep.COPY,
    RejectionReason.ATTRIBUTE_MISMATCH: FlowStep.ATTRIBUTE,
    RejectionReason.QUALIFICATION_MISSING: FlowStep.DRAFT,
    RejectionReason.CATEGORY_ERROR: FlowStep.DRAFT,
    RejectionReason.PRICE_ISSUE: FlowStep.DRAFT,
    RejectionReason.OTHER: FlowStep.DRAFT,
}

#: 平台驳回类目 -> 运营原因码(任务 15 的驳回回流)。
#:
#: 两个枚举分别属于两侧:`RejectCategory` 是**渠道侧**的归类,由 Adapter 从
#: 平台响应里读出来;`RejectionReason` 是**运营侧**的分诊码,决定「去处理」
#: 按钮跳哪个标签页。它们看起来很像,合并成一个却是错的 —— 平台增删一个
#: 驳回类目,不该连带改变我们工作台的标签页结构。
#:
#: 表放在这里而不是 `poll_policy` 里:这是**唯一**拥有 `RejectionReason` 的
#: 模块,它已经维护着 `REJECTION_REASON_STEP`(原因码 -> 流程步骤)。让所有
#: 以 RejectionReason 为终点的映射待在一起,加一个原因码时漏改哪张表是
#: 一眼能看出来的;散在两个包里就只能靠记性。闭合性由纯测试守着。
REJECT_CATEGORY_REASON: Mapping[RejectCategory, RejectionReason] = {
    RejectCategory.IMAGE: RejectionReason.IMAGE_ISSUE,
    RejectCategory.COPY: RejectionReason.COPY_VIOLATION,
    RejectCategory.ATTRIBUTE: RejectionReason.ATTRIBUTE_MISMATCH,
    RejectCategory.CATEGORY: RejectionReason.CATEGORY_ERROR,
    RejectCategory.COMPLIANCE: RejectionReason.QUALIFICATION_MISSING,
    RejectCategory.OTHER: RejectionReason.OTHER,
}


def reason_for_category(category: RejectCategory | str | None) -> RejectionReason:
    """渠道侧类目翻成运营侧原因码。不认识一律落 OTHER。

    不抛错是刻意的,理由同 `poll_policy._rejection_from`:平台**已经驳回**了,
    那是既成事实。一个没见过的类目字符串不该让这条驳回记不进台账 ——
    那样商品会顶着"审核中"沉底,而它其实已经被拒了。落 OTHER 的代价是
    运营多看一眼平台原话(原话一字不改地存在 `platform_note` 里)。
    """
    if category is None:
        return RejectionReason.OTHER
    try:
        key = RejectCategory(str(category).upper())
    except ValueError:
        return RejectionReason.OTHER
    return REJECT_CATEGORY_REASON.get(key, RejectionReason.OTHER)

REJECTION_REASON_ADVICE: Mapping[RejectionReason, str] = {
    RejectionReason.IMAGE_ISSUE: "到图片集页基于被驳回那一版派生新版,换掉问题图后重新批准",
    RejectionReason.COPY_VIOLATION: "到文案页按平台原话修改,重新校验并批准",
    RejectionReason.ATTRIBUTE_MISMATCH: "到属性页核对并改正,确认后文案与草稿会提示重新生成",
    RejectionReason.QUALIFICATION_MISSING: "补齐资质材料;涉及声明用语时同步修改文案",
    RejectionReason.CATEGORY_ERROR: "核对类目配置后重新生成草稿",
    RejectionReason.PRICE_ISSUE: "到草稿页修改手填的价格/币种后重新生成",
    RejectionReason.OTHER: "按平台原话判断该改哪一步;拿不准时先从图片集与文案查起",
}

REJECTION_STATUS_LABELS: Mapping[str, str] = {
    RejectionStatus.OPEN.value: "待处理",
    RejectionStatus.FIXED_PENDING_EXPORT.value: "已声明修复,等待重新导出",
    RejectionStatus.RESOLVED.value: "已解决",
}


# ==========================================================
# 手工状态转移
# ==========================================================


def manual_targets(
    current: str | None, *, open_rejections: int
) -> frozenset[PlatformStatus]:
    """当前状态下,运营可以**手工**把平台状态改成什么。

    两条铁律,其余尽量宽松(平台侧本来就是手工录入,过严只会逼人绕过):

        REJECTED 不在任何返回值里 —— 它只能由「记录驳回」产生。
        手工设置一个没有驳回记录的 REJECTED,状态和驳回台账就各说各话了。

        有未解决驳回时哪儿也去不了 —— 先把驳回记录处理掉(标记解决),
        状态会自动回到 SUBMITTED。允许绕过的话,"驳回 2 条未解决"的商品
        顶着"已上架"标签,比没有平台状态更误导。
    """
    if open_rejections > 0:
        return frozenset()
    status = _parse_status(current)
    if status is PlatformStatus.NONE:
        return frozenset({PlatformStatus.SUBMITTED})
    if status is PlatformStatus.SUBMITTED:
        # NONE 是给误点的退路:刚点了"已上传"发现文件其实没传成功
        return frozenset({PlatformStatus.LIVE, PlatformStatus.NONE})
    if status is PlatformStatus.LIVE:
        # 上架后整改重传(平台下架整改、或运营主动换版)
        return frozenset({PlatformStatus.SUBMITTED})
    # REJECTED 且驳回已清零:解决路径已经把状态改回 SUBMITTED,
    # 这里只会在数据被手改过时出现,给一条回 SUBMITTED 的路自救
    return frozenset({PlatformStatus.SUBMITTED})


def _parse_status(raw: str | None) -> PlatformStatus:
    try:
        return PlatformStatus(str(raw))
    except ValueError:
        return PlatformStatus.NONE


# ==========================================================
# 定位:驳回指向的是哪一次导出
# ==========================================================


@dataclass(frozen=True)
class LocatedExport:
    """一次被定位到的导出。`components` 为空时 `located_by` 必是 unlocated。"""

    fingerprint: str
    exported_at: str | None
    components: Mapping[str, Any] | None
    located_by: str  # audit / current_draft / unlocated


#: 「被驳回的是哪一版」这个问题的把握程度。**必须如实标注**,
#: 硬凑一个版本号比承认定位不到更糟:运营会按错的版本去改图。
LOCATED_BY_LABELS: Mapping[str, str] = {
    "audit": "导出留痕带版本",
    "current_draft": "当前草稿快照(其后未重建)",
    "unlocated": "版本不可考(老导出且草稿已重建)",
    # 任务 15:API 自动上架的商品可能一次 Excel 都没导过,于是前三种把握
    # 一种都用不上。它被驳回的那一版由**提交尝试**确定 —— 草稿指纹进过
    # 幂等键,报文快照存在 `PublishAttempt` 上,两者一起把版本钉死,
    # 而且比导出审计更准:导出是人手工上传的,提交是系统自己发的
    "publish_attempt": "API 提交留痕(草稿指纹 + 报文快照)",
}

#: 由 API 轮询回流写入的定位方式。单独拎出来给 service 层引用,
#: 免得那边写一个裸字符串 —— 拼错了不会有任何报错,只会让界面显示原文
LOCATED_BY_PUBLISH_ATTEMPT = "publish_attempt"


def fingerprints_match(a: str | None, b: str | None) -> bool:
    """指纹相等判定,容忍前缀。

    老的导出审计只存了指纹前 12 位(一行摘要里 64 位十六进制没法读),
    新的 payload 带全量 `fingerprint_full`。两边比对必须容忍"一个是另一个
    的前缀",但前缀短于 8 位不算数 —— 太短的前缀撞车概率不可忽略,
    把两次不同的导出判成同一次,定位出来的版本就是错的。
    """
    left = (a or "").strip()
    right = (b or "").strip()
    if len(left) < 8 or len(right) < 8:
        return False
    return left == right or left.startswith(right) or right.startswith(left)


def trim_components(components: Mapping[str, Any] | None) -> dict[str, Any]:
    """草稿组件快照 -> 驳回记录要存的最小集合。

    只留"定位那一版"需要的四样:图片集 id+版本、各语言文案 id+版本、
    模板版本。手填字段与属性版本 id 列表不进驳回记录 —— 它们在草稿快照里
    仍可回查,复制一份只会让两处口径漂移。
    """
    source = dict(components or {})
    image_set = source.get("image_set")
    copies = source.get("copies")
    return {
        "image_set": dict(image_set) if isinstance(image_set, Mapping) else None,
        "copies": {
            str(locale): dict(v)
            for locale, v in (copies or {}).items()
            if isinstance(v, Mapping)
        }
        if isinstance(copies, Mapping)
        else {},
        "spec_version": source.get("spec_version"),
    }


# ==========================================================
# 导出留痕的 payload:写方与读方定义在一起
# ==========================================================

#: 导出留痕在审计 payload 里的动作名。写方与读方都从这里取常量,
#: 不各写一个字符串字面量 —— 改一处忘一处的那次,定位功能会静默失效:
#: 审计表里明明有导出记录,`locate_export` 却一条也筛不出来。
EXPORT_AUDIT_ACTION = "export"

#: **比对用**的导出。它不是交付给平台的文件,所以不算"改过并重导过"(A45-#35)。
#:
#: 驳回闸门(`resolve_gate`)的证据链就是 `EXPORT_AUDIT_ACTION` 那些审计行,
#: 而它只看时间不看格式。CSV 的下载按钮上前端明写着"不作为交付给平台的文件",
#: 于是导一次它就能把闸门关掉 —— 一个界面上说"这个不算"的动作,在系统里算。
#:
#: 分成两个动作名而不是给闸门加过滤:审计里"这次导的是什么"本来就该看得出来,
#: 而按 payload 里的 `fmt` 过滤等于让闸门去理解导出的内部结构。
PREVIEW_EXPORT_AUDIT_ACTION = "export_preview"

#: 会被平台真正消费的格式。只有它们算交付
DELIVERY_EXPORT_FORMATS: frozenset[str] = frozenset({"xlsx"})


def export_audit_payload(
    *,
    fmt: str,
    spu: str,
    fingerprint: str | None,
    components: Mapping[str, Any] | None,
    export_count: int,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """一次导出要写进审计 payload 的内容。

    **这个函数存在的唯一理由是和 `export_entry` 挨着。**

    本轮走查抓到的一处实例:模块注释写着"指纹与组件版本的对应关系已进导出
    审计的 payload",而写方(`service.record_export`)当时只写了 12 位指纹,
    既没有全量指纹也没有组件版本。后果不是报错,是**静默降级**:
    `locate_export` 永远拿不到 `located_by="audit"`,每一条驳回记录都退到
    "当前草稿快照"或"版本不可考",而这恰恰是 P1 想解决的那件事。
    写方与读方隔着两个文件三层调用,谁都不觉得自己错了。

    所以两者现在同处一个文件、共用 `EXPORT_AUDIT_ACTION` 与
    `trim_components`,并由 `test_workbench_platform` 用一条往返用例钉住:
    payload 写出去,`export_entry` 读回来,`locate_export` 必须判成 audit。

    `fingerprint` 保留 12 位截断版:审计列表一行摘要里放不下 64 位十六进制,
    而那一列是给人看的。全量指纹另存 `fingerprint_full` 给机器比对。
    """
    payload: dict[str, Any] = {
        "action": (
            EXPORT_AUDIT_ACTION
            if fmt in DELIVERY_EXPORT_FORMATS
            else PREVIEW_EXPORT_AUDIT_ACTION
        ),
        "format": fmt,
        "spu": spu,
        "fingerprint": (fingerprint or "")[:12],
        "fingerprint_full": fingerprint or None,
        "components": trim_components(components) if components else None,
        "export_count": export_count,
    }
    if batch_id:
        payload["batch_id"] = batch_id
    return payload


def export_entry(
    payload: Mapping[str, Any] | None, at: str | None
) -> dict[str, Any] | None:
    """审计行 -> `locate_export` 吃的一条。不是导出动作时返回 None。

    优先用全量指纹:老记录只有 12 位,新记录两个都有。取全量的那份能让
    驳回记录存下完整指纹,后续再比对时不必依赖前缀容忍逻辑。
    """
    data = dict(payload or {})
    if data.get("action") != EXPORT_AUDIT_ACTION:
        return None
    return {
        "fingerprint": data.get("fingerprint_full") or data.get("fingerprint"),
        "at": at,
        "components": data.get("components"),
    }


def locate_export(
    *,
    wanted_fingerprint: str | None,
    entries: Sequence[Mapping[str, Any]],
    current_fingerprint: str | None,
    current_components: Mapping[str, Any] | None,
    current_exported_at: str | None,
) -> LocatedExport | None:
    """定位驳回指向的那一次导出。

    入参:
        wanted_fingerprint   运营在界面上选的那次导出(None = 最近一次)
        entries              导出审计,新在前。每条:
                             {fingerprint, at, components|None}
        current_*            当前草稿的指纹 / 组件快照 / 最近导出时间,
                             作为"审计没带版本"时的回退

    返回 None 的两种情况都该在 service 层变成一句人话的 409:
        指定了指纹但审计里没有匹配的导出;
        没指定指纹、审计里也没有任何导出、当前草稿又没导出过 ——
        没导出过的商品谈不上"被平台驳回"。
    """
    target: Mapping[str, Any] | None = None
    if wanted_fingerprint:
        for entry in entries:
            if fingerprints_match(str(entry.get("fingerprint") or ""), wanted_fingerprint):
                target = entry
                break
        if target is None:
            # 指定的指纹在历史里找不到:多半是复制漏了字符。
            # 不静默退回"最近一次" —— 那会把驳回钉到错的导出上
            return None
    elif entries:
        target = entries[0]
    elif current_exported_at and current_fingerprint:
        # 审计被清理过但草稿行还记着最近一次导出:退一步用当前草稿
        return LocatedExport(
            fingerprint=current_fingerprint,
            exported_at=current_exported_at,
            components=trim_components(current_components),
            located_by="current_draft",
        )
    else:
        return None

    fingerprint = str(target.get("fingerprint") or "")
    exported_at = target.get("at")
    exported_at = None if exported_at is None else str(exported_at)

    components = target.get("components")
    if isinstance(components, Mapping) and components:
        return LocatedExport(
            fingerprint=fingerprint,
            exported_at=exported_at,
            components=trim_components(components),
            located_by="audit",
        )

    if fingerprints_match(fingerprint, current_fingerprint) and current_components:
        # 审计没带版本,但草稿其后没重建过:当前快照就是当时的快照。
        # 顺手把可能只有 12 位的审计指纹升级成全量指纹
        full = current_fingerprint or fingerprint
        return LocatedExport(
            fingerprint=full,
            exported_at=exported_at,
            components=trim_components(current_components),
            located_by="current_draft",
        )

    return LocatedExport(
        fingerprint=fingerprint,
        exported_at=exported_at,
        components=None,
        located_by="unlocated",
    )


# ==========================================================
# A4:关闭驳回的最小闸门
# ==========================================================

#: 缺口码。两条分别对应方案 A4 里的两句检查,前端照这两个码显示"还缺哪一步"。
MISSING_NO_NEW_EXPORT = "no_new_export"
MISSING_FINGERPRINT_UNCHANGED = "fingerprint_unchanged"

RESOLVE_GATE_LABELS: Mapping[str, str] = {
    MISSING_NO_NEW_EXPORT: "驳回之后没有新的导出或成功提交留痕",
    MISSING_FINGERPRINT_UNCHANGED: "指纹与被驳回的那一版相同",
}

RESOLVE_GATE_ADVICE: Mapping[str, str] = {
    MISSING_NO_NEW_EXPORT: "先改内容,重新导出或重新提交,再回来关闭这条驳回",
    MISSING_FINGERPRINT_UNCHANGED: "重导/重提了但指纹没变,说明内容其实没改动",
}

#: 勾选「已修改并重新上传」时落进 `resolved_note` 的话。
#:
#: 方案 A4 的验收要求"关闭时 `resolved_note` 非空",而交互上允许运营
#: **勾选**而不是打字。两者不矛盾:勾选本身就是一句声明,把它如实写进台账
#: 即可。留空则台账上这一条只剩"某人在某时点了解决",三个月后没人说得清
#: 当时到底改没改。
GATE_CONFIRM_NOTE = "运营确认:已修改并重新上传(系统未观察到新导出或指纹变化)"


#: 关闭驳回的三种走法(评审第 7 条)。判定在 `resolve_target`,
#: service 层只执行 —— 状态机不许有第二份实现。
RESOLVE_TO_RESOLVED = "RESOLVED"
RESOLVE_TO_FIXED_PENDING = "FIXED_PENDING_EXPORT"
RESOLVE_REFUSED = "REFUSED"


def resolve_target(
    *,
    current_status: str,
    gate_satisfied: bool,
    declared_fixed: bool,
) -> str:
    """这次"标记解决"能把驳回推到哪一步。**纯函数。**

    第 7 条的核心是一句话:**"已解决"必须由系统看到的证据决定,
    不能由运营的一次点击决定。**

        闸门过了            -> RESOLVED。有新导出、指纹变了,证据齐全
        闸门没过 + 声明修过 -> FIXED_PENDING_EXPORT。运营的话如实记下,
                               但它仍然阻断,直到新的导出出现
        闸门没过 + 什么都没说 -> REFUSED

    注意**没有**"闸门没过但强行 RESOLVED"这条路。原来有 ——
    勾一个框或写一句备注即可 —— 那条路存在的时候,`RESOLVED` 这个词
    在台账上什么也不代表。

    已经在 FIXED_PENDING_EXPORT 的记录再点一次,规则完全相同:
    有证据就转 RESOLVED,没有就还停在原地(返回 FIXED_PENDING_EXPORT,
    调用方据此报"还是那句话,系统还是没看到新导出")。
    """
    if gate_satisfied:
        return RESOLVE_TO_RESOLVED
    if declared_fixed or current_status == RejectionStatus.FIXED_PENDING_EXPORT.value:
        return RESOLVE_TO_FIXED_PENDING
    return RESOLVE_REFUSED


@dataclass(frozen=True)
class ResolveGate:
    """关闭一条驳回时,系统能不能自证"这一条真的修过"。

    `satisfied=False` **不代表禁止关闭**。平台侧全程手工录入,系统看不见
    运营在 SHEIN 后台做了什么,不可能判定"已重新上传";硬阻断只会逼出
    "先随便导一次骗过闸门"的动作,那比没有闸门更糟。
    所以这里只回答"有没有证据",要不要放行由 service 层按确认与说明决定。
    """

    has_new_export: bool
    fingerprint_changed: bool
    missing: tuple[str, ...]
    #: A-13:驳回之后是否出现过**成功的** PublishAttempt。API 自动发布的
    #: 商品不经过导出,这一条与 `has_new_export` 互为等价的"重新提交"证据
    has_new_attempt: bool = False

    @property
    def satisfied(self) -> bool:
        return not self.missing


def _as_utc_naive(raw: str | None) -> datetime | None:
    """ISO 字符串 -> naive UTC datetime。解析不了返回 None。

    我们写进库的是 naive UTC(统一走 `core/clock.utc_now()`;列本身是
    `timestamptz`,这个错配的来龙去脉见 `core/clock.py`),
    但审计 payload 与 API 入参可能带 `Z` 或 `+08:00`。两边直接比会抛
    "can't compare offset-naive and offset-aware datetimes",而这个异常会
    出现在**关闭驳回**这条路径上 —— 所以统一在这里归一,不在调用点各写一遍。
    """
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def resolve_gate(
    *,
    rejected_fingerprint: str | None,
    rejected_at: str | None,
    entries: Sequence[Mapping[str, Any]],
    current_fingerprint: str | None,
    attempt_entries: Sequence[Mapping[str, Any]] = (),
) -> ResolveGate:
    """A4:关闭这条驳回之前,系统观察到"改过并重导/重提过"了吗。

    入参:
        rejected_fingerprint  被驳回的那一版的指纹(驳回记录上存着)
        rejected_at           驳回记录的产生时间
        entries               导出审计,新在前,同 `locate_export` 的格式
        current_fingerprint   当前草稿指纹,审计里没有更新记录时的回退
        attempt_entries       **成功的** PublishAttempt 留痕,新在前,
                              每条至少带 {"at": ISO 时间}。A-13:API 自动
                              发布的商品不走导出,重新提交与重新导出等价

    两条检查:

        有没有新的导出/提交  驳回时间**之后**是否还有导出留痕,或成功的提交尝试
        指纹变没变          那次新导出(没有则取当前草稿)的指纹是否已不同于被驳回的那版

    "驳回后出现新的 PublishAttempt,且草稿指纹发生变化"由这两条组合覆盖:
    新提交满足第一条;API 路径没有新导出,第二条自动落到当前草稿指纹,
    于是**未修改草稿的重复提交仍然过不了闸**(fingerprint_unchanged)。

    ## 判不出来的时候一律算"缺"

    `rejected_at` 解析不了、指纹一方为空、审计被清理干净 —— 这些情况下
    函数没有证据,于是报"缺"。方向是刻意的:闸门的产物是一次提醒,
    误报的代价是运营多勾一个框,漏报的代价是驳回台账退化成备忘录。
    """
    since = _as_utc_naive(rejected_at)
    newer: list[Mapping[str, Any]] = []
    has_new_attempt = False
    if since is not None:
        for entry in entries:
            at = _as_utc_naive(entry.get("at"))
            if at is not None and at > since:
                newer.append(entry)
        for entry in attempt_entries:
            at = _as_utc_naive(entry.get("at"))
            if at is not None and at > since:
                has_new_attempt = True
                break
    has_new_export = bool(newer)

    # entries 新在前,所以 newer[0] 就是驳回之后最近的那次导出。
    # 一次都没有时退回当前草稿指纹:审计可能被清理过,但草稿行还记着最新一版。
    if newer:
        compare = str(newer[0].get("fingerprint") or "") or None
    else:
        compare = current_fingerprint

    if not rejected_fingerprint or not compare:
        # 一方为空 = 比不了 ≠ 变了。这里若写成 True,没存指纹的老驳回记录
        # 会全部自动过闸,而它们恰恰是最该被人看一眼的那批
        fingerprint_changed = False
    else:
        fingerprint_changed = not fingerprints_match(compare, rejected_fingerprint)

    missing: list[str] = []
    if not has_new_export and not has_new_attempt:
        missing.append(MISSING_NO_NEW_EXPORT)
    if not fingerprint_changed:
        missing.append(MISSING_FINGERPRINT_UNCHANGED)

    return ResolveGate(
        has_new_export=has_new_export,
        fingerprint_changed=fingerprint_changed,
        missing=tuple(missing),
        has_new_attempt=has_new_attempt,
    )
