"""颜色结构化字段 -> `color_variants.display_name` 的投影(§4.3,阶段 5 批次 5-4)。

**零依赖纯函数。**

## 它还的是哪一笔账

`ColorVariant.display_name` 的列注释从落列那天起就写着:

    投影列,**唯一写入点是属性服务**:VARIANT 层的颜色事实被 CONFIRMED 时,
    在同一个事务里写下来。

而**那个写入点一直不存在**。`audit_column_writers.LEDGER` 上记着这一条,
还款日阶段 5,理由是「要等 owner_id 切 UUID(阶段 1 剩余项)」。
那个前置在迁移 0046 就做完了:`validation.owner_for()` 今天对 VARIANT 层
字段**要求** owner 是 `color_variants.id`,拿不到就当场抛错。

后果是可见的:后端 5 处读它、前端 7 处显示它,而它恒为空字符串。

## 那个字段叫 `primary_color`,不叫 `standard_color_name`

五处注释与文档写着触发条件是「`standard_color_name` 被确认时」——**全仓没有这个字段**。
`grep` 只命中那五句话本身;注册表里的 VARIANT 层颜色字段是
`primary_color`(`registry._FIELDS`)。

这不是措辞问题:照着那句话去接线的人会先找那个字段,找不到,然后多半
得出"这一步还缺前置"的结论 —— 而那正是这笔账躲过几批的方式。
本模块用 `SOURCE_FIELD` 把它定死在代码里,并由守卫钉住它真的在注册表中
且真的是 VARIANT 层。

## 为什么是投影而不是直接读事实

`display_name` 是**反规范化副本**,事实源仍然是 `product_attribute_values`。
留一列的理由与 `products` 上那 8 个投影列一样:颜色名在六处被显示
(建档、图片绑定、方案面板、草稿预览、导出、发布),每一处都 join 一次
属性表是做不完也验不了的量。

代价是它会过期,所以**写入点必须只有一个**,而且必须在确认的**同一个事务**里。
分成两步(先确认、稍后同步)的话,中间那个窗口里界面显示的是旧名字,
而没有任何东西说明为什么。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: 触发投影的注册表字段名。**是 `primary_color`,不是 `standard_color_name`。**
#:
#: 后者只出现在五句注释里,全仓没有这个字段。守卫钉着这个常量真的在
#: 注册表中、且 owner_type 真的是 VARIANT —— 改注册表把颜色挪到 SPU 层时,
#: 这一条会当场变红,而不是安静地再也不写 `display_name`。
SOURCE_FIELD = "primary_color"

#: 投影列的长度上限(`ColorVariant.display_name` 是 String(128))。
#:
#: **今天这条截断走不到。** 拦超长值的是属性校验层的
#: `validation.MAX_TEXT_LENGTH`(同样是 128),`confirm()` 就拒了,到不了投影。
#:
#: 留着它有一个具体的理由,而且是**两个数必须相等**这条不变量:
#: 校验层哪天放宽到 256,投影会静默截断,于是界面上的颜色名与事实值
#: 不是同一个字符串 —— 而没有任何东西会报错。守卫钉着这两个数相等。
MAX_LENGTH = 128


def projected_name(value: Any) -> str | None:
    """从已确认的颜色事实里取出该写进 `display_name` 的那个字符串。

    返回 `None` 表示**不写** —— 与写空串不是一回事:

        None   这条事实不提供颜色名(值是空的、形状不认识)。保留现有显示名
        ""     会把界面上已有的颜色名擦掉,而运营并没有要求这件事

    这个区分是本模块最容易被简化掉的一处:`str(value or "")` 一行就能"跑通",
    而它在值为空时会静默清空一个正在被七个前端位置显示的字段。
    """
    text = _text_of(value)
    if not text:
        return None
    return text[:MAX_LENGTH]


def _text_of(value: Any) -> str:
    """事实值 -> 文本。**认两种形状**,其余一律当成"不提供"。

    属性值的 `value_json` 是 JSONB,历史上出现过两种写法:裸字符串,
    以及 `{"value": ...}` 的包装。两种都认不是纵容 —— 是因为读取方
    (投影)不该成为第三个决定"事实值长什么样"的地方;认不出的形状
    返回空串,由 `projected_name` 判成"不写",而不是把一个
    `{'value': 'black'}` 原样显示给运营(那正是本批开发时真实出现过的形状)。
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        inner = value.get("value")
        if isinstance(inner, str):
            return inner.strip()
    return ""


def needs_update(current: str | None, wanted: str | None) -> bool:
    """要不要真的写这一列。

    ## 这个函数**承重的只有 `None` 那一半**

    `current != wanted` 这一半不承重:SQLAlchemy 的脏检查本来就会把"赋一个和
    当前值相同的值"折叠掉,不发 UPDATE。拿掉它行为完全一样,没有任何守卫会红。

    留着它是因为它让**读代码的人**看得见这个函数在回答什么;
    但别把它当成一道防线,真正的防线是下面那一行:

    `wanted is None` 时返回 False。None 的含义是「这条事实不提供颜色名」,
    与「把它清空」不是一回事 —— 后者会擦掉一个正在被七个前端位置显示的字段。
    调用点(`service._project_colour_name`)在更前面还有一道同向的早返回,
    两道是刻意的:这一列的错写代价是"每个颜色显示错的名字而界面看起来正常"。
    """
    if wanted is None:
        return False
    return (current or "") != wanted


__all__ = ["MAX_LENGTH", "SOURCE_FIELD", "needs_update", "projected_name"]
