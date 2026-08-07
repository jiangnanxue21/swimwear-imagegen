"""`evidence_class` 落存储列 + 库级 CHECK(A45-batch14-26,§4.8 / §5.1)。

## 落列的理由不是省 CPU

`derive_evidence_class` 从 14-8 起就是唯一写入点、四条规则穷举验过。
落成存储列是因为 **§5.1 的抽取输入白名单需要在 SQL 里过滤**:派生量
过滤不了,于是白名单今天只能把全部素材取出来再在 Python 里筛。素材上万
之后,"先全取"本身就是问题,而且那个问题会先表现为识别变慢,
排查方向指向模型侧。

## 风险表 D3 说这一列不该存,为什么现在存

D3 原文:设计成**独立赋值**的存储列时它会与 `source` / 溯源列漂移,
而一张 AI 图被误标成 `PRODUCT_EVIDENCE` 就穿透了整个白名单。

关键词是**独立赋值**。这一列落库之后仍然不接受赋值:

    应用层   唯一写入路径仍是 `derive_evidence_class`,
             由 `media/service.py` 在落库前调一次
    库级     本迁移的 CHECK,`violates_ai_check` 的孪生,
             拦绕过派生函数的直写
    源码级   `tests/pure` 一条 AST 守卫钉住"路由层与脚本不许直接赋值"

所以新增的这一列是**缓存**,不是第二个事实源 —— 区别在于它没有独立的
写入路径。三件套从"派生 + CHECK"变成"派生 + 存储 + CHECK",D3 描述的
那个漂移需要有人先绕过全部三层。

## 为什么不在这里回填 —— 本批三条迁移里唯一一条不回填的

0043 回填了,因为它的答案**在库里**(素材挂商品、商品挂 SPU),
迁移只是把它抄过来。

这一条的答案**要算**:四条规则,顺序不能换(见 `derive_evidence_class`
的模块文档)。而迁移不许 import `app.*` —— 全仓 42 份没有一份破例,
0038 / 0040 / 0042 各自写过为什么。于是在迁移里回填只剩一条路:
把四条规则冻成一段 SQL CASE。

**那就是 D3 描述的第二个判定点。** 规则今天会演进(§4.8 那张表还在长),
而 SQL CASE 不会跟着演进,它会安静地停在 2026 年 8 月这一版。
更糟的是它停下来之后没有任何地方会报 —— 回填过的行看起来完全正常。

0040 逐条权衡过同一道题并拒绝了,14-23 复述过一次。本批**不推翻那个
结论,而是绕开它**:回填走脚本(`app/scripts/backfill_evidence_class.py`),
脚本可以 import 派生函数,于是回填与运行时走同一段代码。

`app/scripts/backfill_media_assets.py` 早就确立了"大回填走脚本"的先例,
它当时给的理由(迁移跑在一个事务里、几十万行 UPDATE 长时间持锁、
脚本可分批可中断可干跑)在这里逐条成立。

## 存量行留 NULL 的读取口径

**NULL 当作"没算过",不当作 `REFERENCE_ONLY`。**

兜底成 REFERENCE_ONLY 会让一批存量商品证据凭空退出识别输入,
表现是"这个款怎么识别不出属性",而排查方向指向识别侧。
与 0042 留 NULL 是同一条规矩的又一次应用,只是方向相反 ——
那一条兜底成"过期"(不放行),这一条兜底成"没算过"(不假装算过)。
两者的共同点是:**默认值不许让任何人被静默放行或静默拦下。**

## CHECK 放行 NULL

`evidence_class IS NULL OR NOT (...)`。存量行还没回填,而回填是脚本的事,
两者之间有一段时间。这不是缺口 —— NULL 在读取侧进不了白名单。

Revision ID: 0045
Revises: 0044
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0045"
down_revision: Union[str, None] = "0044"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

_TABLE = "media_assets"
_CHECK = "ck_media_assets_ai_not_product_evidence"

#: 与 `app/models/media_asset.py` 里的 `CheckConstraint` 逐字相同。
#: 两处都要有:模型那份是给 `create_all` 与新库用的,这份是给存量库用的。
#: **它们不一致时没有任何地方会报** —— 守卫
#: `test_a45_batch14_26_*.py::test_the_ai_check_is_written_identically_in_both_places`
#: 按 AST 取出两处的字符串比对,这是那条守卫存在的理由。
_AI_CHECK_SQL = (
    "evidence_class IS NULL "
    "OR NOT ("
    "  (source = 'AI_GENERATED'"
    "   OR generation_task_id IS NOT NULL"
    "   OR generation_candidate_id IS NOT NULL)"
    "  AND evidence_class = 'PRODUCT_EVIDENCE'"
    ")"
)


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("evidence_class", sa.String(length=24), nullable=True),
    )
    op.create_check_constraint(_CHECK, _TABLE, _AI_CHECK_SQL)
    # **刻意不回填。** 见模块文档「为什么不在这里回填」。
    # 回填:python -m app.scripts.backfill_evidence_class --apply


def downgrade() -> None:
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.drop_column(_TABLE, "evidence_class")
