"""裸 VARIANT owner_id 的人工归属工具(B-07)。

    python -m tools.repair_variant_owners list
    python -m tools.repair_variant_owners list --owner 黑色
    python -m tools.repair_variant_owners assign --owner 黑色 --spu SW-001 --apply

## 这个工具解决的是什么

`0027` 的 upgrade 把 VARIANT 层的 `owner_id` 从裸变体 id 改写成带 SPU
命名空间的形式,但它的 CTE 用的是**当前** `primary_color` 算 bare,而
`owner_id` 里那个字符串是**写入当时**的颜色名。中间改过一次颜色文案的行,
两者对不上、匹配不到、于是没被改写 —— 那一批行的跨 SPU 串档在迁移里
根本没修,而迁移是绿的。

评审把这条记成 B-07「歧义旧 VARIANT owner **无可执行修复**」。
无可执行修复才是真正的问题:知道有一批脏数据、也知道它危险,却没有
任何一条路可以走。

## 为什么必须是人来指,而不是脚本猜

库里只剩一个字符串。属性表没有 `product_id`,没有任何一列记着这一行
当初属于哪件商品。一个裸 `黑色` 可能属于任何一个有黑色的 SPU ——
猜错的后果正是这套命名空间要消灭的那件事:**把别人的颜色写进这个 SPU**。

所以工具只做两件事:

    list     把决定所需的全部证据摆出来(字段、取值、来源、时间、候选 SPU)
    assign   按人给出的归属改写,一次一个 owner,默认干跑

## 三条安全约束

1. **默认干跑。** 不带 `--apply` 只打印将要执行的改写。
2. **拒绝会撞唯一索引的改写。** 目标 owner 已经有同名字段的当前值时停下 ——
   那说明该 SPU 自己已经确认过这个属性,合并要人决定留哪个。
3. **每一次改写都进审计**,`actor` 必填,payload 记下旧 owner 与新 owner。

不提供 `--all`:一次改一个 owner,是为了让每一次归属决定都对应一次人的
判断。批量参数会把这个工具变成 `0027` 那条 CTE 的手工版本。
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict

from sqlalchemy import select

from app.attributes import validation
from app.core.enums import AuditAction, OwnerType
from app.db.session import SessionLocal
from app.models.attribute import ProductAttributeValue
from app.models.product import Product
from app.services import audit


def _bare_rows(session, owner: str | None) -> list[ProductAttributeValue]:
    """全部 VARIANT 层的裸 owner 行。裸 = 拆不出 SPU 命名空间。"""
    stmt = select(ProductAttributeValue).where(
        ProductAttributeValue.owner_type == OwnerType.VARIANT.value,
        ProductAttributeValue.is_current.is_(True),
    )
    rows = [
        row
        for row in session.scalars(stmt)
        if validation.split_variant_owner_id(row.owner_id) is None
    ]
    if owner is not None:
        rows = [row for row in rows if row.owner_id == owner]
    return rows


def _candidate_spus(session, owner: str) -> list[str]:
    """哪些 SPU 现在有一个叫这个名字的变体。**候选,不是答案。**

    改过名的行恰恰查不到候选 —— 那不是工具坏了,那正是这批数据的成因。
    此时要靠人从取值和时间去推,或者直接删掉重新确认一次。
    """
    spus: set[str] = set()
    for product in session.scalars(select(Product)):
        if owner in {
            product.variant_key,
            (product.primary_color or "").strip(),
            product.sku,
        }:
            spus.add(product.spu)
    return sorted(spus)


def cmd_list(args) -> int:
    with SessionLocal() as session:
        rows = _bare_rows(session, args.owner)
        if not rows:
            print("没有裸 VARIANT owner —— 这批数据是干净的")
            return 0

        grouped: dict[str, list[ProductAttributeValue]] = defaultdict(list)
        for row in rows:
            grouped[row.owner_id].append(row)

        print(f"{len(grouped)} 个裸 owner,共 {len(rows)} 行当前值\n")
        for owner in sorted(grouped):
            candidates = _candidate_spus(session, owner)
            print(f"owner_id = {owner!r}")
            print(
                "  候选 SPU:"
                + (
                    "、".join(candidates)
                    if candidates
                    else "(无)—— 多半是改过颜色名,归属只能靠取值和时间推"
                )
            )
            for row in sorted(grouped[owner], key=lambda r: r.field_name):
                value = row.normalized_value or row.value_json
                print(
                    f"    {row.field_name:20} = {value!r:24} "
                    f"status={row.status} source={row.source} "
                    f"updated_at={row.updated_at}"
                )
            print()
        print(
            "决定归属之后:\n"
            "  python -m tools.repair_variant_owners assign "
            "--owner <owner_id> --spu <SPU> --actor <你的名字> --apply"
        )
    return 0


def cmd_assign(args) -> int:
    with SessionLocal() as session:
        rows = _bare_rows(session, args.owner)
        if not rows:
            print(f"找不到裸 owner {args.owner!r} —— 可能已经处理过了")
            return 1

        target = validation.variant_owner_id(spu=args.spu, variant_id=args.owner)

        # 撞唯一索引的先停下。`(owner_type, owner_id, field_name) WHERE is_current`
        # 上有部分唯一索引,直接改写会抛 IntegrityError,而那时已经改了一半
        taken = {
            row.field_name
            for row in session.scalars(
                select(ProductAttributeValue).where(
                    ProductAttributeValue.owner_type == OwnerType.VARIANT.value,
                    ProductAttributeValue.owner_id == target,
                    ProductAttributeValue.is_current.is_(True),
                )
            )
        }
        clashing = sorted({row.field_name for row in rows} & taken)
        if clashing:
            print(
                f"停下:{args.spu} 的这个变体已经有当前值的字段 {clashing}。\n"
                "合并要人决定留哪一个 —— 先在界面上处理掉,或者把这些行删掉再来。"
            )
            return 2

        print(f"{args.owner!r} -> {target!r}({len(rows)} 行)")
        for row in rows:
            print(f"    {row.field_name} = {row.normalized_value or row.value_json!r}")

        if not args.apply:
            print("\n干跑,没有改任何东西。确认无误后加 --apply")
            return 0

        for row in rows:
            previous = row.owner_id
            row.owner_id = target
            audit.record(
                session,
                actor=args.actor,
                action=AuditAction.UPDATE,
                entity_type="ProductAttributeValue",
                entity_id=row.id,
                payload={
                    "action": "repair_variant_owner",
                    "field_name": row.field_name,
                    "from_owner_id": previous,
                    "to_owner_id": target,
                    "spu": args.spu,
                },
            )
        session.commit()
        print(f"\n已改写 {len(rows)} 行,并写入审计")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="列出裸 VARIANT owner 与决定所需的证据")
    listing.add_argument("--owner", default=None, help="只看这一个 owner_id")
    listing.set_defaults(func=cmd_list)

    assign = sub.add_parser("assign", help="把一个裸 owner 归给某个 SPU")
    assign.add_argument("--owner", required=True)
    assign.add_argument("--spu", required=True)
    assign.add_argument("--actor", required=True, help="审计里记谁做的这次归属")
    assign.add_argument(
        "--apply", action="store_true", help="真的改。不带这个参数只干跑"
    )
    assign.set_defaults(func=cmd_assign)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
