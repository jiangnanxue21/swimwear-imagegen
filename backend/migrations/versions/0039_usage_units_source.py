"""费用流水记下计费量是谁说的(A45-batch14-18 / 任务 9)。

## 这一列挡着什么

`billable_units` 从落地起就是**我们数出来的**:`max(provider_count, 1)`,
也就是"Provider 返回了几张候选图"。而 FASHN 在每一次 status 响应的
`x-fashn-credits-used` 头里报的是它自己实际扣了多少额度 —— 那个数被
`providers/fashn.py` 解析出来、抄在候选图的 metadata 上,然后**全仓再没有
第二处读它**。

两个数不是同一个量。官方参考表(`docs/vendor/fashn-skill/reference.md`
「Credits」)::

    tryon-max  balanced  1k:2  2k:3  4k:4     (× num_images)
               quality   1k:3  2k:4  4k:5

一张图 2 到 5 个额度,取决于 `FASHN_RESOLUTION` 与 `generation_mode` ——
**两个旋钮运营在设置页都能改**。所以台账少记的倍数不是一个常数,是一个跟着
配置浮动的系数,而方向恒定是**少记**:预算横幅一路绿着,账单是它的好几倍。

§10.2 第 5 条准入(「用量记录与 Provider 后台账单条数一致」)按这个写法
不可能对得上;对上了是运气。

## 为什么要落一列,而不是只在日志里说

对账的人发现某一行对不上时,第一个问题是"这个数是厂商说的还是我们猜的"。
前者对不上要去查厂商,后者对不上是我们算错了 —— 两条路完全相反。没有这一列,
每一行看起来同样权威,而在这一批之前**每一行都是猜的**。

## 存量回填成 `inferred`,这和迁移 0034 拒绝回填不矛盾

0034 的说明里写着「不回填历史行的 `billing_key`:回填等于替过去的账做一个
今天才定下来的判断」。这一条在那里成立,是因为那批行的正确取值**当时就
不可知**(哪一行才是真的取决于厂商账单)。

这里不一样:存量行的取值是**确定可知**的 —— 读厂商额度的代码在这一批之前
不存在,所以每一条历史流水的 `billable_units` 都出自 `max(provider_count, 1)`。
`inferred` 不是一个猜测,是对我们自己那条代码路径的陈述。

因此 `server_default` 写成 `'inferred'`、列取 NOT NULL:让"没说"这个状态
根本不可表示,免得后面每一处读它的地方都要先处理一次 NULL。

Revision ID: 0039
Revises: 0038
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: Union[str, None] = "0038"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

_TABLE = "provider_usage_records"
_COLUMN = "units_source"

#: 取值与 `app.core.enums.UnitsSource` 一一对应。**这里写死字符串是刻意的**:
#: 迁移是历史记录,import 一个会跟着演化的枚举等于让过去的迁移随今天的代码变。
_INFERRED = "inferred"
_ALLOWED = ("provider", "inferred")
_CHECK = "ck_provider_usage_records_units_source"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.String(16),
            nullable=False,
            server_default=_INFERRED,
        ),
    )
    # CHECK 而不是只靠应用层:这一列的读者是**对账的人**,而一个拼错的取值
    # (`"Provider"` / `"vendor"`)不会让任何代码报错,只会让一行流水从两个
    # 口径的统计里同时消失。写坏的那天就该拒绝,不该等到月底汇总时才发现。
    op.create_check_constraint(
        _CHECK,
        _TABLE,
        sa.column(_COLUMN).in_(_ALLOWED),
    )


def downgrade() -> None:
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.drop_column(_TABLE, _COLUMN)
