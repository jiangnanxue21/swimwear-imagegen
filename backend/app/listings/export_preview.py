"""导出预览的颜色→SKU→图片那一段(PRD §13 阶段 5 第五项 / AC-19)。

**零依赖纯函数。** 只用标准库 + `workbench.upstream_snapshot` 的读取助手。

## 它补的是哪一格

`draft_preview` 今天已经满足了 AC-19 的一半:字段预览读的是落库的
`mapped_payload`(`_mapped_from_row`),与导出读同一份数据 —— §5.3 的
「导出一致率 100%」就是这么来的。

**缺的是另一半:图片。** 那份预览里没有任何一格回答得了

    红色的 M 码上架时用的是哪一张主图
    这张图是这个颜色自己的,还是一张被标了「通用」的图
    有没有哪一行 SKU 根本没拿到主图

而这三个问题的答案已经在库里了 —— 5-2 把它写进了
`listing_drafts.color_sku_image_map`。本模块只是把它读出来铺成行。

## 只读落库的那一份,**一格都不重新推断**

这是 AC-19 的字面要求(「导出预览与最终导出读取同一份已存映射,
不在预览时重新推断」),而它有一个很具体的理由:

重新推断的预览**永远是自洽的**。它会显示"当前该用哪些图",而导出用的是
草稿生成那一刻存下来的那些。两者在上游变过之后就不是一回事 ——
于是运营在预览里看到红色有主图、导出出来那一行是空的,
**而两边都不报错**。

所以这个模块的签名里只有 `mapping` 一个必需入参:它**拿不到**图片集、
拿不到颜色表、拿不到商品行,想重新推断也无从下手。这不是保守,
是把"不许重新推断"变成一件做不到的事。

反向守卫钉着这一点:本模块不许 import `image_set_rules` /
`upstream_collect`,也不许出现 `coverage` / `build_color_sku_image_map`。

## 缺图不是异常,是预览要显示的一种状态

一行 SKU 没有主图时给 `status="MISSING"` 而不是抛错,也不是跳过。

跳过的话,预览里那一行**消失**,而运营会读成"这个尺码不在这次导出里"——
实际上它在,只是没有图。READY 门禁(`ready_problems`)会拦下这份草稿,
但预览要在被拦之前就让人看见是哪一行缺。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.workbench.upstream_snapshot import IMAGE_MAP_SCHEMA_VERSION

#: 一行 SKU 的图片状态。**封闭取值集**,前端按它分组着色。
#:
#: 写成常量而不是散在各处的字面量,与 `upstream_snapshot.COMPONENTS` 同一条
#: 理由:改一个拼写不会有任何东西红,表现是那一类行从分组里消失。
ROW_OK = "OK"
ROW_MISSING = "MISSING"
ROW_STATUSES: tuple[str, ...] = (ROW_OK, ROW_MISSING)

#: 整份映射的状态。`UNPROVEN` 是 5-1 D2 那条 NULL 语义在预览层的落点。
MAP_READY = "READY"
MAP_UNPROVEN = "UNPROVEN"
MAP_INCOMPATIBLE = "INCOMPATIBLE"


def image_preview(mapping: Mapping[str, Any] | None) -> dict[str, Any]:
    """把落库的颜色→SKU→图片映射铺成预览行。

    返回形状:

        {"status": ..., "note": ..., "colors": [
            {"variant_id": ..., "variant_code": ..., "rows": [
                {"sku": ..., "primary": ..., "extras": [...], "status": ...}
            ]}
        ]}

    `status` 三档,而且**三档都要能被前端区分**:

        READY         映射存在且形状可读。逐行看 `rows[].status`
        UNPROVEN      草稿建于 0049 之前,颜色维一次都没算过
        INCOMPATIBLE  形状版本对不上,旧快照与当前口径不可比

    后两档合并成一档的话,运营看到的是同一句"没有图片信息",
    而两者的动作不同:一个重新生成草稿就好,另一个说明有人换过快照形状。
    """
    if mapping is None:
        return {
            "status": MAP_UNPROVEN,
            "note": "这份草稿建于颜色维快照落列之前,没有可预览的图片映射;重新生成一次草稿",
            "colors": [],
        }
    if str(mapping.get("schema") or "") != IMAGE_MAP_SCHEMA_VERSION:
        return {
            "status": MAP_INCOMPATIBLE,
            "note": (
                f"映射快照的形状版本是 {mapping.get('schema') or '空'},"
                f"当前口径是 {IMAGE_MAP_SCHEMA_VERSION},不可比;重新生成草稿"
            ),
            "colors": [],
        }

    colors: list[dict[str, Any]] = []
    for variant_id, payload in sorted(
        ((str(k), dict(v or {})) for k, v in (mapping.get("colors") or {}).items()),
        key=lambda pair: pair[0],
    ):
        rows = [
            _row(sku, dict(cell or {}))
            for sku, cell in sorted(
                (str(k), v) for k, v in (payload.get("skus") or {}).items()
            )
        ]
        colors.append(
            {
                "variant_id": variant_id,
                # 与 `upstream_snapshot._color_label` 同口径:有码用码,
                # 没有就退回短 id。**不用 `display_name`** —— 那一列的写入点
                # 要等 5-4,拿它当标识会让每个颜色都显示成空
                "variant_code": payload.get("variant_code") or f"{variant_id[:8]}…",
                "rows": rows,
                "missing_count": sum(1 for r in rows if r["status"] == ROW_MISSING),
            }
        )
    return {"status": MAP_READY, "note": "", "colors": colors}


def _row(sku: str, cell: Mapping[str, Any]) -> dict[str, Any]:
    """一行 SKU 的预览格。

    `primary` 取空值时状态是 `MISSING` 而不是把这一行丢掉:丢掉的话
    运营会把它读成"这个尺码不在这次导出里",而它在,只是没有图。
    """
    primary = cell.get("primary")
    extras = [str(v) for v in (cell.get("extras") or []) if v]
    return {
        "sku": sku,
        "primary": str(primary) if primary else None,
        "extras": extras,
        "status": ROW_OK if primary else ROW_MISSING,
    }


def missing_rows(preview: Mapping[str, Any]) -> list[str]:
    """预览里缺主图的 SKU 清单。给「导出前再看一眼」用。

    从 `image_preview()` 的产物上再算一遍,而不是让 `image_preview` 顺手
    返回:两者的消费者不同(一个铺表格、一个做提示),而合成一个返回值
    之后,加一个提示项就要改表格那一侧的形状。
    """
    return [
        row["sku"]
        for color in preview.get("colors") or []
        for row in color.get("rows") or []
        if row.get("status") == ROW_MISSING
    ]


__all__ = [
    "MAP_INCOMPATIBLE",
    "MAP_READY",
    "MAP_UNPROVEN",
    "ROW_MISSING",
    "ROW_OK",
    "ROW_STATUSES",
    "image_preview",
    "missing_rows",
]
