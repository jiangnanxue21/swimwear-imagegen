"""草稿构建与 STALE 判定(M6,§9.1)。**零依赖纯函数。**

指纹 = 属性版本 id 集合 + image_set_id(含 version) + 文案版本 id 集合
      + manual_payload + spec_version + mapping_version

上游任一变化 → 指纹变化 → 草稿标 `STALE` → **不允许导出与提交**。

这是 v1 那个纯 dataclass 方案完全无法表达的一类问题:草稿建好之后
属性被改了、图片集出了新版本、文案重新生成了,而它看起来仍然是
「校验通过」的。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.core.enums import DraftStatus
from app.listings.contracts import ListingDraftData

#: 允许导出与提交的状态。**STALE 不在其中。**
PUBLISHABLE_STATUSES: frozenset[str] = frozenset(
    {DraftStatus.VALIDATED.value, DraftStatus.APPROVED.value}
)


def compute_fingerprint(draft: ListingDraftData) -> str:
    """草稿的上游指纹。实现在契约上,这里只是转发。

    放在契约上是因为渠道适配器也要算它(判断手里这份草稿还新不新),
    而适配器**不得查数据库** —— 指纹必须能从纯数据算出来。
    """
    return draft.source_fingerprint()


def is_stale(stored_fingerprint: str, current: ListingDraftData) -> bool:
    """库里那份草稿是不是已经过期。"""
    return stored_fingerprint != compute_fingerprint(current)


def copy_locale_problems(draft: ListingDraftData) -> list[str]:
    """映射键与文案自身 locale 不一致的那些条目(评审第 5 条)。

    **必须在建草稿之前查一次。** 指纹已经把映射键纳进哈希,能保证
    「换个语言挂一遍」会被判 STALE,但它拦不住第一次就挂错的那份 ——
    指纹只回答"变没变",不回答"对不对"。
    """
    return [
        f"{key} 这一栏挂的是 {draft.copies[key].locale} 的文案"
        for key in draft.locale_mismatches()
    ]


def can_export(status: str, stored_fingerprint: str, current: ListingDraftData) -> bool:
    """能不能导出 / 提交。

    三个条件都要满足:状态是 VALIDATED/APPROVED、指纹没变、
    **且文案的语言绑定是对的**。

    只看状态的话,一份"校验通过"的草稿会在属性被改之后继续可以提交,
    而提交上去的是旧事实。
    """
    if status not in PUBLISHABLE_STATUSES:
        return False
    if copy_locale_problems(current):
        return False
    return not is_stale(stored_fingerprint, current)


def refresh_status(
    status: str, stored_fingerprint: str, current: ListingDraftData
) -> str:
    """按指纹刷新状态。已经是终态的不动。

    `ARCHIVED` 的草稿不该因为上游变了就变成 STALE —— 它已经归档了,
    上游怎么变都和它无关。
    """
    if status == DraftStatus.ARCHIVED.value:
        return status
    if is_stale(stored_fingerprint, current):
        return DraftStatus.STALE.value
    return status


def missing_prerequisites(
    *,
    image_set_status: str,
    copy_statuses: Mapping[str, str],
    required_locales: Sequence[str] = (),
) -> list[str]:
    """构建草稿之前的前置条件(硬规矩 5)。

    发布读的是 `APPROVED` 的图片集 + `CONFIRMED` 的属性 + `APPROVED` 的
    渠道文案,**不读生成任务状态**。
    """
    problems: list[str] = []
    if image_set_status != "APPROVED":
        problems.append(f"图片集尚未批准(当前 {image_set_status})")
    for locale in required_locales:
        status = copy_statuses.get(locale)
        if status is None:
            problems.append(f"缺少 {locale} 的文案")
        elif status != "APPROVED":
            problems.append(f"{locale} 的文案尚未批准(当前 {status})")
    return problems


def summarize(draft: ListingDraftData) -> dict[str, Any]:
    """给页面用的摘要。不含图片字节、不含完整快照。"""
    return {
        "spu": draft.spu,
        "channel": draft.channel,
        "site": draft.site,
        "category_id": draft.category_id,
        "image_set_id": draft.image_set.image_set_id,
        "image_set_version": draft.image_set.version,
        "locales": sorted(draft.copies),
        "fingerprint": compute_fingerprint(draft),
        "spec_version": draft.spec_version,
    }
