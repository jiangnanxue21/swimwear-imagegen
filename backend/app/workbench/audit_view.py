"""审计查询的展示口径(阶段 3,FE-307)。**零数据库依赖纯函数。**

FE-307 的验收结果是「可追溯到用户和时间」。库里已经有这两样了
(`audit_logs.actor` / `created_at`,BE-206 在阶段 2 已经落地),
这一层要解决的是**读得懂**:

    entity_type   `ListingDraft` 对运营没有意义,「上架草稿」有
    action        CREATE/UPDATE 太粗。真正发生的事写在 payload.action 里
                  (`export` / `approve` / `retry` / `build`…),要把它提上来
    一句话         "谁 在什么时候 对哪个对象 做了什么" —— 一行一句,不让人读 JSON

## 为什么这一层不做筛选逻辑

查询条件(时间、操作人、对象、动作)是 SQL 的事,在 api 层。这里只做
枚举到中文的翻译,和 `frontend/src/api/*.ts` 里的文案表是同一个性质 ——
所以它也受契约测试保护:后端记了一个新的 payload.action 而这里没有翻译,
界面上就会出现一个英文单词,而没人会立刻发现。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.core.enums import AuditAction

#: 审计动作(数据库列)。粗粒度,只表示写操作的类型。
AUDIT_ACTION_LABELS: Mapping[str, str] = {
    AuditAction.CREATE.value: "新建",
    AuditAction.UPDATE.value: "修改",
    AuditAction.DELETE.value: "删除",
    AuditAction.UPLOAD.value: "上传",
    AuditAction.IMPORT.value: "导入",
    AuditAction.READ.value: "读取",
}

#: 业务动作(payload.action)。**这才是运营关心的那个词。**
#:
#: 取值来自各 service 写审计时放进 payload 的 `action` 键。加一个新动作
#: 而不在这里登记,界面会显示原始英文 —— `tests/pure/test_frontend_contract.py`
#: 用一次 grep 反查 app/ 下所有 `"action": "..."` 字面量,漏了就失败。
PAYLOAD_ACTION_LABELS: Mapping[str, str] = {
    # A45-#35:比对用导出。与 `export` 分开,免得它被当成交付证据
    "export_preview": "导出比对文件",
    "approve": "批准",
    "reject": "驳回",
    "build": "生成草稿",
    "export": "导出上架文件",
    # 评审第 6 条把"生成"和"下载"拆成了两个动作。旧的 "download" 保留:
    # 历史审计行里还有它,删掉那些行就会显示成英文
    "download": "下载批量文件(旧)",
    "generate_export_file": "生成批量上架文件",
    "download_export_file": "下载批量上架文件",
    # A-21:作废重生成。与上面的"生成"分开一个词 —— 审计里必须能一眼数出
    # "这份文件被换过几版",而那正是平台驳回定位要问的第一个问题
    "regenerate_export_file": "重新生成批量上架文件",
    "retry": "重试",
    "cancel": "取消",
    "regenerate": "重新生成",
    "platform_status": "更新平台状态",
    "record_rejection": "记录平台驳回",
    "resolve_rejection": "解决平台驳回",
    # 评审第 7 条:关闭驳回拆成"声明修复"与"真的解决";重新上传独立成一个动作
    "declare_rejection_fixed": "声明已修复,待重新导出",
    "resubmit_after_rejection": "确认修复后重新上传",
}

#: 对象类型。键是各处 `audit.record(entity_type=...)` 用的字符串。
ENTITY_LABELS: Mapping[str, str] = {
    "Product": "商品",
    "ProductAsset": "商品素材",
    "MediaAsset": "素材",
    "ProductAttributeValue": "属性值",
    "ListingImageSet": "图片集",
    "ListingCopy": "文案",
    "ListingDraft": "上架草稿",
    "ContentPlan": "内容计划",
    "GenerationTask": "生成任务",
    "ReviewItem": "审核项",
    "OutputAsset": "成品图",
    "ModelTemplate": "模特模板",
    "BatchJob": "批量任务",
    "PlatformRejection": "平台驳回记录",
    "app_setting": "系统设置",
}

#: 工作台审计页默认只看这几类对象。
#:
#: 不是权限限制,是**降噪**:审计表里混着生成任务的状态流转,而 FE-307 要查的是
#: 「关键修改、批准、导出记录」。默认过滤掉与上架闭环无关的对象,
#: 界面上仍然可以手动切到全部。
WORKBENCH_ENTITY_TYPES: tuple[str, ...] = (
    "Product",
    "ProductAttributeValue",
    "MediaAsset",
    "ListingImageSet",
    "ListingCopy",
    "ListingDraft",
    "BatchJob",
    "PlatformRejection",
)


def business_action(entry_action: str, payload: Mapping[str, Any] | None) -> str:
    """真正发生的事。payload 里有就用它,否则退回数据库列。"""
    raw = (payload or {}).get("action")
    if isinstance(raw, str) and raw:
        return raw
    return entry_action


def business_action_label(entry_action: str, payload: Mapping[str, Any] | None) -> str:
    action = business_action(entry_action, payload)
    if action in PAYLOAD_ACTION_LABELS:
        return PAYLOAD_ACTION_LABELS[action]
    return AUDIT_ACTION_LABELS.get(action, action)


def entity_label(entity_type: str) -> str:
    return ENTITY_LABELS.get(entity_type, entity_type)


def describe(
    *,
    actor: str | None,
    action: str,
    entity_type: str,
    payload: Mapping[str, Any] | None,
) -> str:
    """一行一句。**不拼 JSON 原文**,只挑几个能说明白的键。

    挑哪几个键是按"出事时会问什么"选的:导出问哪一版(指纹)、
    批准问哪个版本、批量问几件。其余留在展开的详情里。
    """
    payload = payload or {}
    who = actor or "system"
    what = business_action_label(action, payload)
    target = entity_label(entity_type)

    bits: list[str] = []
    for key, label in (
        ("spu", "SPU"),
        ("sku", "SKU"),
        ("version", "版本"),
        ("field_name", "字段"),
        ("fingerprint", "指纹"),
        ("format", "格式"),
        ("items", "条目数"),
        ("spu_count", "SPU 数"),
        ("created", "新增"),
        ("skipped", "跳过"),
    ):
        value = payload.get(key)
        if value not in (None, "", [], {}):
            bits.append(f"{label} {value}")
    suffix = f"({', '.join(bits)})" if bits else ""
    return f"{who} {what} {target} {suffix}".strip()
