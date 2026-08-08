"""阶段 1 身份收口:`owner_id` 切 UUID、命名空间 hack 与 `variant_key` 退役。

**这不是一次数据迁移,是一次身份变更**(`docs/DECISIONS.md` §3.23 /
`listings/variant_key.identity_of()` 里那颗雷)。四件事必须在**同一个事务**
里做完,分开做的每一种排法都会留下一个静默错误的窗口:

    1. 回填 `products.color_variant_id`
    2. 属性 `owner_id`:`<len>:<spu>/<key>` -> `color_variants.id`
    3. 图片项 `variant_id`:`variant_key` -> `color_variants.id`
    4. 校验:还有没有回填不了的行

## 为什么顺序不能拆

`identity_of()` 三级回落里外键排在 `variant_key` 前面。所以**第 1 步单独跑完
的那一刻**,库里每一行都同时有外键和 key,身份当场从 key 翻成 UUID ——
而已确认的颜色属性还挂在 `<len>:<spu>/<key>` 上,图片标签写的也是 key。
两者同时指向一个不存在的变体,§3.17 那个 bug 在全库一起引爆。

反过来先做第 2、3 步也不行:那时还没有 UUID 可以写进去。

## 这条迁移**不许有** `ON CONFLICT` / `WHERE ... IS NOT NULL` 式的跳过

与 0044 同一条姿态,理由更硬。回填不了的行有两种,两种都必须让迁移停下:

    这件商品的 SPU 下找不到同名颜色    人要去决定它属于哪个颜色。猜一个的
                                       代价是一批属性挂到错的颜色上,而那
                                       在平台侧才会暴露
    同一个 key 映到了两个颜色变体      §4.3 说颜色名在 SPU 内唯一,撞了说明
                                       数据本身有问题,不是迁移能决定的

跳过它们的表现是:迁移绿了、系统起来了,而那批商品的颜色属性从此读不到。
**`raise` 比一批查不到的数据便宜得多。**

## 降级

`downgrade()` 把 owner_id 与图片标签改写回命名空间形式,但**不删回填的
`products.color_variant_id`** —— 那一列从 0035 起就存在,回填进去的是
正确的值,删掉它等于主动制造第二次数据损失。降级之后 `identity_of()` 会
再次优先读外键,所以降级只有在**代码一起回滚**时才是完整的,
文件头这句话是说给那时候的人听的。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: 命名空间分隔符。**这里刻意写字面量而不是 import**
#: `attributes.validation`:迁移是历史的快照,而那个模块正在本批里退役 ——
#: 引它的话,下一次有人改常量会静静地改掉一条已经跑过的迁移的含义。
_NS = "/"


def _namespaced(spu: str, key: str) -> str:
    return f"{len(spu)}:{spu}{_NS}{key}"


def upgrade() -> None:
    bind = op.get_bind()

    # ---- 1. 回填 products.color_variant_id ----
    #
    # 匹配依据是 `(spu_id, variant_key or primary_color)` 对上
    # `color_variants.variant_code` 或 `working_name`。**两列都试**:
    # 0027 分配 key 的种子是 `primary_color or sku`,而建档时填的是
    # `variant_code` —— 存量库里两种写法都有。
    # 不能用两条 `UPDATE ... FROM color_variants`:variant_code 在 SPU 内唯一,
    # working_name 却不是。两个颜色都叫「供应商蓝」时,PostgreSQL 会从两个
    # 匹配行里任挑一个,迁移仍然成功 —— 这与文件头承诺的「歧义就停」相反。
    products = bind.execute(
        sa.text(
            """
            SELECT id, sku, spu_id,
                   COALESCE(NULLIF(btrim(variant_key), ''), btrim(primary_color), '') AS seed
              FROM products
             WHERE color_variant_id IS NULL
               AND spu_id IS NOT NULL
            """
        )
    ).all()
    for product_id, sku, spu_id, seed in products:
        if not seed:
            continue
        matches = bind.execute(
            sa.text(
                """
                SELECT id
                  FROM color_variants
                 WHERE spu_id = :spu_id
                   AND (
                        lower(btrim(variant_code)) = lower(btrim(:seed))
                        OR (
                            btrim(working_name) <> ''
                            AND lower(btrim(working_name)) = lower(btrim(:seed))
                        )
                   )
                 ORDER BY id
                """
            ),
            {"spu_id": spu_id, "seed": seed},
        ).scalars().all()
        if len(matches) > 1:
            raise RuntimeError(
                "0046 停在颜色匹配这一步:商品 "
                f"{sku} 的身份种子 {seed!r} 在同一个 SPU 下匹配到 "
                f"{len(matches)} 个颜色。variant_code / working_name 有歧义,"
                "迁移不能替你任选一个;先把颜色编码或临时名称整理成唯一答案。"
            )
        if matches:
            bind.execute(
                sa.text(
                    "UPDATE products SET color_variant_id = :variant_id WHERE id = :product_id"
                ),
                {"variant_id": matches[0], "product_id": product_id},
            )

    # ---- 2. 回填不了的行:**停下,不跳过** ----
    #
    # 只数**真正需要身份**的那批:名下有 VARIANT 层属性行、或者有带变体标签
    # 的图片项。一件刚导进来、还没跑过识别的商品没有任何东西挂在它的变体
    # 身份上,拦下它只会让迁移无法执行,而它本来没有可丢的东西。
    #
    # 这一条是这条迁移全部安全性的所在:少了它,回填不上的商品会在
    # 第 3 步之后失去全部颜色属性,而迁移是绿的。
    stranded = bind.execute(
        sa.text(
            """
            SELECT p.sku
              FROM products p
             WHERE p.color_variant_id IS NULL
               AND (
                    EXISTS (
                        SELECT 1 FROM product_attribute_values v
                         WHERE v.owner_type = 'VARIANT'
                           AND v.is_current
                           AND v.owner_id = char_length(p.spu)::text || ':' || p.spu || '/'
                               || COALESCE(
                                      NULLIF(btrim(p.variant_key), ''),
                                      NULLIF(btrim(p.primary_color), ''),
                                      '\\x00'
                                  )
                    )
                 OR EXISTS (
                        SELECT 1
                          FROM listing_image_items i
                          JOIN listing_image_sets s ON s.id = i.image_set_id
                         WHERE i.variant_id IS NOT NULL
                           AND s.spu = p.spu
                           AND i.variant_id = COALESCE(NULLIF(p.variant_key, ''), p.primary_color)
                    )
               )
             ORDER BY p.sku
             LIMIT 50
            """
        )
    ).scalars().all()
    if stranded:
        raise RuntimeError(
            "0046 停在回填这一步:下面这些商品名下挂着 VARIANT 层属性或带变体标签的"
            "图片项,而它们的 color_variant_id 回填不上(SPU 下找不到同名颜色)。\n"
            "  " + ", ".join(stranded) + "\n"
            "这不是迁移能替你决定的事 —— 猜一个颜色的代价是那批属性挂到错的颜色上,"
            "而错误只在平台侧才会暴露。先去 SPU 下把颜色补齐或把这批行的 "
            "variant_key / primary_color 改成正确的颜色编码,再跑一次。"
        )

    # ---- 3. 属性 owner_id:命名空间形式 -> color_variants.id ----
    #
    # 逐行改写,不用一条 UPDATE ... FROM:owner_id 里的 SPU 是**变长前缀**
    # (`<len>:<spu>/<key>`),在 SQL 里切它要靠 `split_part` 加长度算术,
    # 而那一段写错不会报错,只会静静地匹配不上任何行。Python 侧按同一个
    # 函数拼一遍,拼得出来才改 —— 拼不出来的是 A44 之前的裸 owner_id,
    # 它们由 `orphaned_variant_owners()` 单独报告,这里不动。
    rows = bind.execute(
        sa.text(
            """
            SELECT p.id, p.spu, p.variant_key, p.primary_color, p.color_variant_id
              FROM products p
             WHERE p.color_variant_id IS NOT NULL
            """
        )
    ).all()
    for _pid, spu, key, colour, cv_id in rows:
        seed = (key or "").strip() or (colour or "").strip()
        if not seed or not (spu or "").strip():
            continue
        old_owner = _namespaced(spu.strip(), seed)
        bind.execute(
            sa.text(
                """
                UPDATE product_attribute_values
                   SET owner_id = :new
                 WHERE owner_type = 'VARIANT'
                   AND owner_id = :old
                """
            ),
            {"new": str(cv_id), "old": old_owner},
        )
        # ---- 4. 图片项变体标签:variant_key -> color_variants.id ----
        #
        # `variant_id` 在 0046 之前只是 SPU 内的 key,不是全局 key。
        # 必须沿 image_set -> spu 收窄;裸 `WHERE variant_id = :old` 会把另一个
        # SPU 的同名颜色一起改成当前 SPU 的 UUID,正好重演 §3.19 的串档。
        bind.execute(
            sa.text(
                """
                UPDATE listing_image_items i
                   SET variant_id = :new
                  FROM listing_image_sets s
                 WHERE s.id = i.image_set_id
                   AND s.spu = :spu
                   AND i.variant_id = :old
                """
            ),
            {"new": str(cv_id), "old": seed, "spu": spu.strip()},
        )

    # ---- 5. `variant_key` 退役 ----
    #
    # **不 drop 这一列。** 它是这次身份变更唯一的回头路:改写之后
    # 属性行上只剩 UUID,而"这个 UUID 当初是哪个 key"的答案只存在这一列里。
    # drop 掉的话 `downgrade()` 拼不出旧 owner_id,降级就成了一句空话。
    #
    # 代码侧已经不读它了(`identity_of()` 的第二级删了),所以留着它的
    # 唯一成本是一列没人读的数据 —— 而那正是 `audit_columns` 会点名的东西,
    # 台账上因此需要一行。收掉它的时机是"这次变更确认无误、不再需要回头路"
    # 那一天,单开一条迁移。
    op.execute(
        "COMMENT ON COLUMN products.variant_key IS "
        "'已退役(0046)。身份改由 color_variant_id 承担,这一列仅作降级回头路保留'"
    )


def downgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT p.spu, p.variant_key, p.primary_color, p.color_variant_id
              FROM products p
             WHERE p.color_variant_id IS NOT NULL
            """
        )
    ).all()
    for spu, key, colour, cv_id in rows:
        seed = (key or "").strip() or (colour or "").strip()
        if not seed or not (spu or "").strip():
            continue
        bind.execute(
            sa.text(
                """
                UPDATE product_attribute_values
                   SET owner_id = :old
                 WHERE owner_type = 'VARIANT'
                   AND owner_id = :new
                """
            ),
            {"old": _namespaced(spu.strip(), seed), "new": str(cv_id)},
        )
        bind.execute(
            sa.text(
                """
                UPDATE listing_image_items i
                   SET variant_id = :old
                  FROM listing_image_sets s
                 WHERE s.id = i.image_set_id
                   AND s.spu = :spu
                   AND i.variant_id = :new
                """
            ),
            {"old": seed, "new": str(cv_id), "spu": spu.strip()},
        )
    # `products.color_variant_id` **不清空** —— 见文件头。
