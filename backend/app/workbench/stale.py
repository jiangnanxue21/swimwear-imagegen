"""过期原因对比(BE-205,任务书 §4.5)。**零依赖纯函数。**

草稿的 `source_fingerprint` 只回答「变没变」。运营要的是三句话:
**哪个上游对象变了、变了哪些字段、需要执行什么动作**(§4.5.1:
仅显示"已过期"三个字视为未完成)。

对比的两边都是普通 dict,由 service 从库里组装:

    stored   草稿构建时落库的组件快照(canonical_snapshot)
    current  此刻按同样方法重新组装出来的组件

放在独立文件里而不是 service 里,是因为「属性 v3 换成 v5 该说什么话」
需要被穷举测试,而只要它能查库,就没人会写那个穷举测试。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StaleChange:
    """一条上游变化。"""

    #: attributes / image_set / copy / manual / spec / mapping / missing
    component: str
    #: 给运营看的一句话
    message: str
    #: 变化字段名(能点名就点名)
    fields: tuple[str, ...] = ()
    old_value: str | None = None
    new_value: str | None = None
    #: 运营该做什么
    action: str = "重新生成草稿"


def _as_str_list(value: Any) -> list[str]:
    if not value:
        return []
    return [str(v) for v in value]


def _attr_field(version_id: str) -> str:
    """`v-primary_color` / `<uuid>` -> 尽量还原字段名,还原不了就原样返回。"""
    if version_id.startswith("v-"):
        return version_id[2:]
    return version_id


def diff_components(
    stored: Mapping[str, Any], current: Mapping[str, Any]
) -> list[StaleChange]:
    """逐组件对比。返回空列表 = 没有可解释的变化(草稿不过期)。

    组件结构(两边一致):

        attrs          排序后的属性版本 id 列表
        attr_fields    {version_id: field_name},可选,用来把 id 翻译成字段名
        image_set      {"id": str, "version": int} 或 None
        copies         {locale: {"id": str, "version": int}}
        manual         运营手填 dict
        spec_version   str
        mapping_version str
    """
    changes: list[StaleChange] = []

    # ---- 属性(§4.5 第 1 行:已确认属性被修改 -> 文案与草稿过期)
    stored_attrs = set(_as_str_list(stored.get("attrs")))
    current_attrs = set(_as_str_list(current.get("attrs")))
    if stored_attrs != current_attrs:
        names = {
            **{str(k): str(v) for k, v in (stored.get("attr_fields") or {}).items()},
            **{str(k): str(v) for k, v in (current.get("attr_fields") or {}).items()},
        }
        touched = sorted(
            {
                names.get(vid, _attr_field(vid))
                for vid in stored_attrs.symmetric_difference(current_attrs)
            }
        )
        changes.append(
            StaleChange(
                component="attributes",
                message="已确认属性在草稿生成之后被修改",
                fields=tuple(touched),
                old_value=f"{len(stored_attrs)} 个属性版本",
                new_value=f"{len(current_attrs)} 个属性版本",
                action="重新生成草稿;文案若也过期,先重新生成并批准文案",
            )
        )

    # ---- 图片集(§4.5 第 2/3 行)
    stored_set = stored.get("image_set") or None
    current_set = current.get("image_set") or None
    if current_set is None:
        if stored_set is not None:
            changes.append(
                StaleChange(
                    component="image_set",
                    message="草稿引用的图片集已不再是批准状态(被降级或归档)",
                    old_value=_set_label(stored_set),
                    new_value="无已批准图片集",
                    action="回到图片集标签页重新批准一版,再重新生成草稿",
                )
            )
    elif stored_set is None or _set_key(stored_set) != _set_key(current_set):
        changes.append(
            StaleChange(
                component="image_set",
                message="已批准图片集在草稿生成之后被重新编排",
                old_value=_set_label(stored_set),
                new_value=_set_label(current_set),
                action="重新生成草稿,新草稿会引用当前批准的那一版",
            )
        )

    # ---- 文案(§4.5 第 4 行)
    stored_copies = {str(k): v for k, v in (stored.get("copies") or {}).items()}
    current_copies = {str(k): v for k, v in (current.get("copies") or {}).items()}
    for locale in sorted(set(stored_copies) | set(current_copies)):
        old = stored_copies.get(locale)
        new = current_copies.get(locale)
        if new is None:
            changes.append(
                StaleChange(
                    component="copy",
                    message=f"{locale} 的文案已不再是批准状态",
                    fields=(locale,),
                    old_value=_copy_label(old),
                    new_value="无已批准文案",
                    action="回到文案标签页重新批准一版,再重新生成草稿",
                )
            )
        elif old is None or _copy_key(old) != _copy_key(new):
            changes.append(
                StaleChange(
                    component="copy",
                    message=f"{locale} 的已批准文案在草稿生成之后换了版本",
                    fields=(locale,),
                    old_value=_copy_label(old),
                    new_value=_copy_label(new),
                    action="重新生成草稿,新草稿会引用当前批准的那一版",
                )
            )

    # ---- 手填字段。指纹包含 manual,但 manual 的变化来自草稿自身的编辑,
    #      正常路径是「保存手填 -> 立即重建草稿」,不应残留;残留说明有人
    #      绕过接口改库,照样要点名
    stored_manual = dict(stored.get("manual") or {})
    current_manual = dict(current.get("manual") or {})
    if stored_manual != current_manual:
        touched = sorted(
            k
            for k in set(stored_manual) | set(current_manual)
            if stored_manual.get(k) != current_manual.get(k)
        )
        changes.append(
            StaleChange(
                component="manual",
                message="运营手填字段与草稿生成时不一致",
                fields=tuple(touched),
                action="重新生成草稿以套用当前手填值",
            )
        )

    # ---- 模板 / 映射版本(§4.5 第 5 行:模板换版 -> 全部草稿过期)
    for key, component, label in (
        ("spec_version", "spec", "导出模板版本"),
        ("mapping_version", "mapping", "字段映射版本"),
    ):
        old = str(stored.get(key) or "")
        new = str(current.get(key) or "")
        if old != new:
            changes.append(
                StaleChange(
                    component=component,
                    message=f"{label}发生变更",
                    old_value=old,
                    new_value=new,
                    action="重新生成草稿,按新版本重新映射与校验",
                )
            )

    return changes


def copy_attrs_stale(
    status: str,
    plan_attr_ids: Sequence[Any] | None,
    confirmed_ids: Sequence[Any],
    *,
    plan_missing: bool = False,
) -> bool:
    """§4.5 第 1 行的文案列:已确认属性被修改 → 已批准文案过期。

    判据:文案的内容计划记录了组装时用到的属性版本 id;当前已确认属性
    版本集合与它不一致,文案里的事实就可能不再成立。

    两个刻意的边界:

        - 只有 `APPROVED` 谈得上过期 —— 草稿态的文案本来就还没被采信,
          给它标过期只会让「过期」这个词失去分量。
        - 内容计划找不到(`plan_missing`)按过期算:没有快照就无法证明
          文案仍然成立,而这里的默认必须偏保守。

    这是从 `workbench/service._copy_is_stale` 里抽出来的纯判定 ——
    §4.5 矩阵要逐格穷举验证,而只要判定还贴在查库代码上,
    就没人会写那个穷举测试(与本模块顶部注释同一个理由)。

    输入刻意只有属性:素材状态、图片集、导出模板版本都不在参数表里。
    矩阵第 2/3/5 行的文案列写着「不过期」,做法就是让那些变化根本
    到不了这个函数。
    """
    if status != "APPROVED":
        return False
    if plan_missing:
        return True
    return tuple(sorted(str(v) for v in (plan_attr_ids or []))) != tuple(
        sorted(str(v) for v in confirmed_ids)
    )


def _set_key(value: Mapping[str, Any]) -> tuple[str, str]:
    return (str(value.get("id") or ""), str(value.get("version") or ""))


def _set_label(value: Mapping[str, Any] | None) -> str:
    if not value:
        return "无"
    return f"v{value.get('version')}({str(value.get('id'))[:8]}…)"


def _copy_key(value: Mapping[str, Any]) -> tuple[str, str]:
    return (str(value.get("id") or ""), str(value.get("version") or ""))


def _copy_label(value: Mapping[str, Any] | None) -> str:
    if not value:
        return "无"
    return f"v{value.get('version')}"


def serialize(changes: Sequence[StaleChange]) -> list[dict[str, Any]]:
    return [
        {
            "component": c.component,
            "message": c.message,
            "fields": list(c.fields),
            "old_value": c.old_value,
            "new_value": c.new_value,
            "action": c.action,
        }
        for c in changes
    ]
