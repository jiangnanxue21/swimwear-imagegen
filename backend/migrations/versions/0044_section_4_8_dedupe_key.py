"""§4.8 去重键(A45-batch14-26)。**逾期六批的那一笔,本批还清。**

PRD §4.8 行 257 逐字写着:

    去重键 | 改为 `UNIQUE(spu_id, COALESCE(color_variant_id,''), sha256)`:
    同图上传到两个颜色 → 命中提示,要求人工确认后才各自成行(§11 场景 1)

今天是 `UNIQUE(product_id, sha256)`(0011)。0037 自己写过"这一版**不含**
新去重键",随后六批没有一批补上。

## 按字面落有一个洞,所以本迁移落的是两条索引

`spu_id` 可空(老建档路径的存量商品没有 SPU,见 0043 的回填)。而在唯一
索引里 **`NULL <> NULL`** —— 一条式落法的后果是:

    所有 spu_id 为空的素材,`(NULL, ..., sha256)` 彼此永不相等,
    于是**它们彻底失去去重**。同一个供应商把同一张图推十次,进十行。

那比今天更糟,而且是静默的:约束在,只是不拦任何东西。这类"看起来落了、
实际覆盖零行"的失效,与 14-24 记的那次「0 列都答得出,而退出码是 0」
是同一型。

所以落两条**互斥且合起来覆盖全表**的局部唯一索引:

    uq_media_assets_spu_colour_sha256   WHERE spu_id IS NOT NULL   §4.8 那个键
    uq_media_assets_product_sha256      WHERE spu_id IS NULL       兜底,即旧键

任何一行的 `spu_id` 要么空要么不空,所以**恰好落在一条键下**,不存在
两条都不管的行。这一点是这个落法成立的全部前提,守卫里有一条正向断言
钉着它(不是断言"有两条索引",是断言两个谓词互补)。

## 第二条不是历史遗留,它会自净

老建档路径从本批起必带 `spu_id`(`product_service.create_product`,
同批),所以**新行不再落进兜底键**。存量回填完成之后它覆盖零行,那时:

    spu_id 收 NOT NULL  →  第二条索引的谓词恒假  →  删掉它

三件事是同一个动作。守卫记了这笔账(还款日:阶段 5),红的时候要做的是
删索引,不是把 `spu_id` 改回可空。

## COALESCE 的类型

§4.8 写的是 `COALESCE(color_variant_id,'')` —— 空字符串。而这一列是 UUID,
`COALESCE(uuid, '')` 在 PostgreSQL 里类型不匹配直接报错。落法是先转文本:

    COALESCE(color_variant_id::text, '')

语义与 PRD 一致(通用素材归到同一个桶),差别只在表达式的类型。**这不是
对 PRD 的偏离**,是那一行伪代码在真类型系统里的写法。

## 这条键实际拦住的是什么

§11 场景 1:同一张图上传到两个颜色。旧键 `(product_id, sha256)` 对两个
颜色的两行商品是**两个不同的键**,重复不会命中,人工确认那一步不会发生。
新键把 SPU 内的同一张图收在一起,于是:

    - 同 SPU 同颜色同图 → 命中,不成第二行(与旧行为一致)
    - 同 SPU 不同颜色同图 → **命中**,走人工确认(§11 场景 1 的落点)
    - 同 SPU 通用图与颜色图同图 → 命中(COALESCE 把通用收进 '' 桶)
    - 跨 SPU 同图 → 不命中,各自成行(角色/授权/隔离都是按 SPU 算的)

## 这条迁移可能失败,而那是对的

存量库里如果已经有"同 SPU 跨颜色的同一张图",建唯一索引会失败。
**不要加 `ON CONFLICT` 或者先去重再建。** 那批行正是 §11 场景 1 要求人工
确认的对象,自动挑一行留下等于替运营做了那个决定。失败信息会指出冲突行,
处理完再跑。

Revision ID: 0044
Revises: 0043
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: Union[str, None] = "0043"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

_TABLE = "media_assets"
_OLD_KEY = "uq_media_assets_product_sha256"
_NEW_KEY = "uq_media_assets_spu_colour_sha256"


def upgrade() -> None:
    # 旧键是 0011 建的**表级 UNIQUE 约束**,不是索引 —— 先拆约束,
    # 再以局部索引的形式重建同名对象。同名是刻意的:
    # 它在两个世界里是同一件事(没有 SPU 时按商品去重),
    # 换个名字会让 0011 之后所有引用这个名字的排查笔记失效。
    op.drop_constraint(_OLD_KEY, _TABLE, type_="unique")

    op.create_index(
        _NEW_KEY,
        _TABLE,
        ["spu_id", sa.text("COALESCE(color_variant_id::text, '')"), "sha256"],
        unique=True,
        postgresql_where=sa.text("spu_id IS NOT NULL"),
    )
    op.create_index(
        _OLD_KEY,
        _TABLE,
        ["product_id", "sha256"],
        unique=True,
        postgresql_where=sa.text("spu_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(_OLD_KEY, table_name=_TABLE)
    op.drop_index(_NEW_KEY, table_name=_TABLE)
    # 回到 0011 的表级约束。注意这一步在"存量含同商品重复行"时会失败 ——
    # 那种行只可能由新键放行进来(不同 SPU 同商品是不可能的组合),
    # 所以实践中不会发生;真发生了说明有人手工改过 spu_id。
    op.create_unique_constraint(_OLD_KEY, _TABLE, ["product_id", "sha256"])
