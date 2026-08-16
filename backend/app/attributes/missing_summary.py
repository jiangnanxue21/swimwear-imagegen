"""一次识别中仍然没有可用值的字段。

模型按图回答。同一字段在正面图上报告「这个角度判断不了」、在背面图上给出
有效值是正常形状；逐图的 missing 证据仍要留档，但商品级提示不能因此要求
运营再补一张已经存在的背面图。

这里是后端纯判定，前端只展示结果。否则每个消费端都会各写一遍
「某张图说没有值，是否已被另一张图解决」，迟早再次分叉。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class UnresolvedMissing:
    field_name: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"field_name": self.field_name, "reason": self.reason}


def _read(item: Any, key: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(key)
    return getattr(item, key, None)


def unresolved_missing(evidence: Iterable[Any]) -> tuple[UnresolvedMissing, ...]:
    """返回本次 run 中没有被任一有效值解决的 ``(字段, 原因)``。

    判定与合并层对可用值的口径同向：``normalized_value`` 必须是非空字符串。
    同一个字段只要有一张图给出可用值，其他角度的 missing 仍留档，但不再成为
    商品级下一步。未知 reason 原样保留，让前端至少能显示可检索的编码。
    """
    rows = tuple(evidence)
    resolved = {
        str(_read(item, "field_name")).strip()
        for item in rows
        if isinstance(_read(item, "normalized_value"), str)
        and _read(item, "normalized_value").strip()
    }
    pending: set[tuple[str, str]] = set()
    for item in rows:
        field = str(_read(item, "field_name") or "").strip()
        reason = str(_read(item, "missing_reason") or "").strip()
        if field and reason and field not in resolved:
            pending.add((field, reason))
    return tuple(
        UnresolvedMissing(field_name=field, reason=reason)
        for field, reason in sorted(pending)
    )
