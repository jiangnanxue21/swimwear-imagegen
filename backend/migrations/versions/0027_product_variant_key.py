"""商品变体身份 `variant_key`(A44 / BLOCK-04 第一件)。

## 这一列消灭的那个 bug

在它之前,变体 id 是一个表达式:

    variant_id = primary_color or sku

`primary_color` 是运营在商品表单里可以改的一列。改一次颜色文案,
这个表达式同时是三样东西的主键,三样一起换:

    图片项的变体标签    → 旧标签变成 unknown_tags(有诊断)
    属性值的 owner_id   → 已确认的颜色属性\"消失\"(**没有诊断**)
    导出的变体列        → 与前两者对不齐

A43 把属性分层接到了这个表达式上,于是第二条从「不存在」变成了
「存在且无声」。`docs/DECISIONS.md` §3.17 把它记成必须先处理的遗留。

## 回填取值:**逐字节等于迁移前的表达式**

这是整个迁移唯一需要想清楚的地方。

给 UUID 的话,这一句 UPDATE 跑完的瞬间,库里所有图片标签(自由文本,
写的是颜色名)和所有 `(VARIANT, 颜色名)` 上的属性值,全部指向不存在的
key —— 一次迁移把上面那个 bug 在**每一个** SPU 上同时引爆一遍。

所以回填是:

    COALESCE(NULLIF(BTRIM(primary_color), ''), sku)

也就是旧表达式本身。升级完成的那一刻,所有 id 与升级前完全相同,
存量数据一条都不用动;此后颜色再改,这一列不跟着变。

`BTRIM` 不能省:`variant_key.seed_for()` 对齐的是 Python 侧的
`(color or "").strip() or sku`。少了它,颜色是 `'  '` 的行在库里回填成
两个空格,而应用层算出来是 SKU —— 两边对不齐,且只在这一小撮行上对不齐。

## 这一版还顺手补了一个更严重的洞:VARIANT 属性跨 SPU 串档

`product_attribute_values` 的唯一索引是
`(owner_type, owner_id, field_name) WHERE is_current` —— **里面没有 SPU**。

A43 把 VARIANT 层的 owner_id 直接写成变体 id(取值是颜色名),于是全库
只要有两个 SPU 都有"黑色",它们就共用同一行属性:给 SPU-A 的黑色确认一次
`primary_color`,SPU-B 的黑色跟着变,没有任何提示。

这比"属性不见了"更糟:值还在,只是属于别人。而且它在单 SPU 的测试里永远
复现不出来 —— 要两个 SPU 同色才会出现,而样例数据每个 SPU 一个颜色。

所以这一版同时做两件事:

    owner_id 加宽到 160     命名空间形式是 `<len>:<spu>/<variant_id>`,
                            64 位塞不下(spu 与 variant_id 各自可达 64)
    存量 VARIANT 行改写     裸 owner_id -> 带命名空间的形式

改写**只处理能唯一定位到一个 SPU 的行**。一个裸 owner_id 对应多个 SPU 时,
它本来就是被两个 SPU 共用的那一行 —— 归给谁都是猜,而猜错的结果是把别人的
颜色写进这个 SPU。这类行原样留着,由 `orphaned_variant_owners()` 的
`legacy_bare` 报出来,人工决定。

## 索引

`(spu, variant_key)`。分配新 key 时要按 SPU 查同门兄弟,批准图片集时
要列出该 SPU 的全部变体 —— 两条都是「给定 spu 取一批」,而 `ix_products_spu`
已经能定位;加上 `variant_key` 做覆盖索引,让\"这个 SPU 有哪几个变体\"
不用回表。**不加唯一约束**:同一个变体下有多个尺码行,本来就该重复。

## 为什么可空

回填是全表的,升级后不会有 NULL。留可空是为了 downgrade 之后再 upgrade
的中间态,以及测试里裸构造的 `Product` 对象。应用层的回落
(`variant_id_for()`)对着这个可空性写,并由 `drift_report()` 的
`unassigned` 把漏网的行点名。

Revision ID: 0027
Revises: 0026
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

_TABLE = "products"
_COLUMN = "variant_key"
_INDEX = "ix_products_spu_variant_key"
_ATTR_TABLE = "product_attribute_values"

#: 命名空间形式 `<len>:<spu>/<variant_id>`。两段各自可达 64,
#: 加上长度前缀与分隔符,160 是有余量的下一个整数
_OWNER_ID_WIDTH = 160
_OWNER_ID_OLD_WIDTH = 64


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(length=64), nullable=True))
    # 回填 = 迁移前的表达式,一字不改。理由见文件头
    op.execute(
        sa.text(
            f"UPDATE {_TABLE} "
            f"SET {_COLUMN} = COALESCE(NULLIF(BTRIM(primary_color), ''), sku) "
            f"WHERE {_COLUMN} IS NULL"
        )
    )
    op.create_index(_INDEX, _TABLE, ["spu", _COLUMN])

    # ---- VARIANT 属性的 owner_id 加上 SPU 命名空间
    #
    # 先加宽再改写:反过来的话新值写不进去,而 varchar 加宽在 PostgreSQL 里
    # 不需要重写表(只增不减时是元数据变更),放在这里代价可以忽略
    op.alter_column(
        _ATTR_TABLE,
        "owner_id",
        existing_type=sa.String(length=_OWNER_ID_OLD_WIDTH),
        type_=sa.String(length=_OWNER_ID_WIDTH),
        existing_nullable=False,
    )
    # A45-#9:`BTRIM(v.owner_id)` 不能省 —— 与回填那一句同一个理由。
    #
    # Python 侧 `variant_owner_id()` 对 spu 与 variant_id **两段都 `.strip()`**,
    # 而这条 SQL 原来直接拼 `v.owner_id` 的原值。带首尾空白的存量行迁移之后,
    # 库里是 `7:SPU-1/ 黑色 `,运行期算出来的是 `7:SPU-1/黑色` —— 差一个空白位,
    # 查不到、也没有诊断会报:`split_variant_owner_id` 拆得开,所以它不进
    # `legacy_bare`,它只是永远匹配不上。
    #
    # `BTRIM` 在 `variant_key` 回填那一句写了,这一句漏了 —— 同一个文件里的
    # 两句话对同一个语义做了两种处理。匹配侧(CTE 的 `bare`)本来就归一过,
    # 只有写入侧漏了。
    #
    # A45-#30:`owner_spu` 这个 CTE 按 bare 值**全库**分组。某 SPU 的颜色文案
    # 与另一 SPU 的 SKU 字面撞名时(A 的颜色叫 "SW-7",而 "SW-7" 恰好是 B 的
    # SKU),`COUNT(DISTINCT spu) = 1` 会失真,该行被跳过改写、落进 `legacy_bare`。
    # 概率极低,但"SKU 全库唯一所以不会撞"这个推理有缝 —— 撞的不是两个 SKU,
    # 是一个颜色名和一个 SKU。落进 legacy_bare 是**安全侧**的失败(不猜归属),
    # 由 `tools/repair_variant_owners.py` 人工处理。
    #
    # 只改写能唯一定位到一个 SPU 的行。`HAVING COUNT(DISTINCT spu) = 1`
    # 是这条语句的全部要点 —— 多个 SPU 共用的那一行归给谁都是猜,
    # 而猜错等于把别人的颜色写进这个 SPU
    #
    # ## B-07:这条 CTE 有一个已知的覆盖缺口,**不是可以修的**
    #
    # `bare` 用的是**当前** `primary_color`,而 `owner_id` 里那个字符串是
    # **写入当时**的颜色名。中间改过一次颜色文案的行,两者对不上 ->
    # 匹配不到 -> 不改写 -> 那一批行的跨 SPU 串档在这次迁移里根本没修,
    # 而迁移本身是绿的。
    #
    # 补不回来:库里只剩一个裸字符串,没有任何列记着它当初属于哪件商品
    # (属性表没有 product_id)。硬猜的话就回到了这条 CTE 一开始要避免的
    # 那件事 —— 把别人的颜色写进这个 SPU。
    #
    # 所以处理方式是**把它们交给人**:`tools/repair_variant_owners.py`
    # 列出全库的裸 VARIANT owner 及其字段/取值/时间,由人指定归属后
    # `--apply`。`attributes.service.orphaned_variant_owners()` 的
    # `legacy_bare` 是同一批行的按 SPU 视图。
    op.execute(
        sa.text(
            f"""
            WITH owner_spu AS (
                SELECT COALESCE(NULLIF(BTRIM(primary_color), ''), sku) AS bare,
                       MIN(spu) AS spu
                FROM {_TABLE}
                GROUP BY 1
                HAVING COUNT(DISTINCT spu) = 1
            )
            UPDATE {_ATTR_TABLE} AS v
            SET owner_id = LENGTH(o.spu) || ':' || o.spu || '/' || BTRIM(v.owner_id)
            FROM owner_spu AS o
            WHERE v.owner_type = 'VARIANT'
              AND v.owner_id = o.bare
            """
        )
    )


def downgrade() -> None:
    # 先把 owner_id 还原成裸形式,再窄回去 —— 顺序反了会截断,
    # 而截断之后没有任何办法知道原值是什么
    # C-13:**按长度前缀切,不按第一个 `/` 切。**
    #
    # 形式是 `<len>:<spu>/<variant_id>`,而那个长度前缀存在的唯一理由就是
    # SPU 可以含 `/`(自由文本,`schemas/product.py` 只校验长度)。原来这里
    # 写的是 `POSITION('/' IN owner_id)` —— 取第一个斜杠,也就是**绕开了
    # 长度前缀**,把它当成不存在:
    #
    #     spu="AB/CD"  owner_id="5:AB/CD/Red"
    #     按第一个 `/` 切  ->  "CD/Red"     ← 错的,而且看起来像个合法值
    #     按长度前缀切     ->  "Red"        ← 对的
    #
    # 错值会直接写进 `owner_id`,降级完成后再 upgrade 也回不来 ——
    # 原值已经没了。这是本文件里唯一一处**会损坏数据**的地方。
    #
    # 算式:跳过 `<len>` 这几位、冒号、SPU 本身、以及分隔符。
    # `SPLIT_PART(owner_id, ':', 1)` 取第一个冒号之前的那段,正则
    # `^[0-9]+:` 已经保证它是纯数字;SPU 自己含冒号也不影响,因为取的
    # 是**第一个**冒号。口径与 Python 侧 `validation.split_variant_owner_id()`
    # 完全一致 —— 那边一直是对的,错的只有这条 SQL。
    op.execute(
        sa.text(
            f"""
            UPDATE {_ATTR_TABLE}
            SET owner_id = SUBSTRING(
                owner_id
                FROM POSITION(':' IN owner_id)
                     + CAST(SPLIT_PART(owner_id, ':', 1) AS INTEGER)
                     + 2
            )
            WHERE owner_type = 'VARIANT'
              AND owner_id ~ '^[0-9]+:'
            """
        )
    )
    op.alter_column(
        _ATTR_TABLE,
        "owner_id",
        existing_type=sa.String(length=_OWNER_ID_WIDTH),
        type_=sa.String(length=_OWNER_ID_OLD_WIDTH),
        existing_nullable=False,
    )
    # 降级会丢掉身份:降回去之后变体 id 重新由 `primary_color or sku` 推导,
    # 而降级前如果有人改过颜色名,那些行的 id 会**变回改名后的值** ——
    # 也就是回到 A43 的行为。这不是数据损坏,是功能回退,符合降级的语义。
    #
    # 裸 owner_id 还原之后跨 SPU 串档也一起回来了。降级不是安全操作,
    # 只是可执行操作 —— `make check` 的 alembic 往返验的是"能跑",不是"没损失"。
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, _COLUMN)
