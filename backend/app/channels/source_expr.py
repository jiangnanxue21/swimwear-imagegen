"""spec 里 `source:` 表达式的受限 DSL(§9.3)。

**零依赖纯函数。** 没有 `eval`,没有 `getattr`,没有属性穿透,没有函数调用,
没有索引表达式。七个白名单前缀,每个用显式分支解析。

## 为什么不能用一个通用的取值器

`source: product.spu` 看起来只是一次属性访问,写成 `getattr` 一行就完事。
但 spec 是**外部配置文件**,而配置文件在真实项目里会被运营改、会从平台文档
复制粘贴、将来还可能从数据库读。一旦解析器接受任意路径,
`product.__class__.__init__.__globals__` 就是一条从 YAML 到任意代码执行的路。

显式分支的代价是加一种来源要改这个文件。那正是想要的效果 ——
加一种字段来源本来就该是一次会出现在 diff 里的决定。

## 字段来源矩阵(§9.4)

    product.<field>            商品库字段        运营录入 / 导入
    attr.<field>               已确认属性        阶段 9 + 人工确认
    variant.<field>            变体层字段        商品库(仅 SKU 行上下文可用)
    copy.<locale>.<field>      文案字段          阶段 10 + 校验
    images.<role>              图片集中该角色    已批准图片集
    manual.<key>               运营手填          渠道配置页
    const.<name>               spec 内常量       spec 自身

**价格、库存、备货时效一律走 `manual`,不允许模型生成。**
让大模型给一个会真的向消费者收款的数字,是这条链路里唯一不可挽回的错误。
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class SourceKind(StrEnum):
    PRODUCT = "product"
    ATTR = "attr"
    VARIANT = "variant"
    COPY = "copy"
    IMAGES = "images"
    MANUAL = "manual"
    CONST = "const"


#: 只在 SKU 行上下文里可用的前缀。在 SPU 级 header 上引用它们是配置错误,
#: 而不是运行期取不到值 —— 后者会安静地产出一个空单元格。
ROW_ONLY_KINDS: frozenset[SourceKind] = frozenset({SourceKind.VARIANT})


class SourceExprError(ValueError):
    """表达式非法。**启动期**抛出,不留到运行期。

    spec 加载时全量校验,一条不合法就拒绝启动。理由和 `CHANNEL_SPEC_INCOMPLETE`
    一样:一个解析不了的 source 在运行期的表现是「这个字段是空的」,
    而空字段要到平台驳回时才会被发现。
    """


#: 三个正则**互不重叠**,而且都用 ^$ 锚定。
#: 不用一个大正则加可选分组:那样 `copy.en.title.upper()` 这类输入
#: 很容易因为某个分组恰好匹配而蒙混过关。
#:
#: 字段名必须以小写字母开头。这一条不是为了好看 —— `__class__`、
#: `__globals__` 全都是「小写字母加下划线」,用 `[a-z0-9_]+` 会原样放行。
#: 当前的 `resolve()` 只对 Mapping 做 `.get()`,拿不到那些属性,
#: 但解析器的职责是**在这里**就把它挡掉:下游哪天换成对象取值,
#: 这个洞就会安静地变成一条从 YAML 到任意属性的路。
_SIMPLE = re.compile(r"^(product|attr|variant|manual|const)\.([a-z][a-z0-9_]*)$")
_COPY = re.compile(r"^copy\.([a-z]{2}(?:-[A-Z]{2})?)\.([a-z][a-z0-9_]*)$")
_IMAGES = re.compile(r"^images\.([A-Z][A-Z_]*)$")


@dataclass(frozen=True)
class SourceRef:
    """解析后的引用。**不含任何可执行的东西。**"""

    kind: SourceKind
    key: str
    locale: str | None = None

    @property
    def is_row_only(self) -> bool:
        return self.kind in ROW_ONLY_KINDS

    def __str__(self) -> str:
        if self.kind is SourceKind.COPY:
            return f"copy.{self.locale}.{self.key}"
        return f"{self.kind.value}.{self.key}"


def parse_source(expr: str) -> SourceRef:
    """解析一条 `source` 表达式。非法一律抛 `SourceExprError`。"""
    if not isinstance(expr, str):
        raise SourceExprError(f"source 必须是字符串,收到 {type(expr).__name__}")
    text = expr.strip()
    if not text:
        raise SourceExprError("source 为空")
    if text != expr:
        # 前后空白多半是 YAML 里手抖。不静默接受:静默接受意味着
        # `" attr.x"` 和 `"attr.x"` 是两个字符串却是同一个引用,
        # 而 spec 的去重和覆盖检查是按字符串做的
        raise SourceExprError(f"source 前后不得有空白:{expr!r}")

    match = _COPY.match(text)
    if match:
        return SourceRef(kind=SourceKind.COPY, locale=match.group(1), key=match.group(2))

    match = _IMAGES.match(text)
    if match:
        return SourceRef(kind=SourceKind.IMAGES, key=match.group(1))

    match = _SIMPLE.match(text)
    if match:
        return SourceRef(kind=SourceKind(match.group(1)), key=match.group(2))

    raise SourceExprError(
        f"无法解析的 source 表达式:{expr!r}。"
        "只允许 product./attr./variant./manual./const.<字段>、"
        "copy.<locale>.<字段>、images.<ROLE>"
    )


@dataclass(frozen=True)
class ResolveContext:
    """取值上下文。**只暴露只读映射,不暴露任何对象。**

    参数全是 `Mapping`,不是 ORM 实例 —— 这样解析器不可能穿透到
    数据库会话、不可能触发懒加载,也就不可能在一个本该是纯函数的
    映射过程里发出 SQL。
    """

    product: Mapping[str, Any]
    attrs: Mapping[str, Any]
    manual: Mapping[str, Any]
    consts: Mapping[str, Any]
    copies: Mapping[str, Mapping[str, Any]]      # locale -> 字段表
    images: Mapping[str, Any]                    # ROLE -> 图片引用
    variant: Mapping[str, Any] | None = None     # 仅 SKU 行上下文


#: 取不到值时的返回。刻意不用 None:None 是一个**合法的字段值**
#: (「这个字段确实没有值」),而「这个来源里根本没有这个键」是配置错误。
#: 两者混在一起时,拼错一个字段名的表现是安静地产出空单元格。
class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 只在调试输出里出现
        return "<MISSING>"


MISSING = _Missing()


def resolve(ref: SourceRef, ctx: ResolveContext) -> Any:
    """按引用取值。每个分支单独实现,没有统一的反射入口。

    取不到返回 `MISSING`,由调用方决定这是 `CHANNEL_FIELD_MISSING`
    还是一个可以留空的选填字段。
    """
    if ref.kind is SourceKind.PRODUCT:
        return ctx.product.get(ref.key, MISSING)
    if ref.kind is SourceKind.ATTR:
        return ctx.attrs.get(ref.key, MISSING)
    if ref.kind is SourceKind.MANUAL:
        return ctx.manual.get(ref.key, MISSING)
    if ref.kind is SourceKind.CONST:
        return ctx.consts.get(ref.key, MISSING)
    if ref.kind is SourceKind.IMAGES:
        return ctx.images.get(ref.key, MISSING)
    if ref.kind is SourceKind.COPY:
        bucket = ctx.copies.get(ref.locale or "")
        if bucket is None:
            return MISSING
        return bucket.get(ref.key, MISSING)
    if ref.kind is SourceKind.VARIANT:
        if ctx.variant is None:
            # 在 SPU 级上下文里引用变体字段。这是 spec 写错了,不是数据缺失
            raise SourceExprError(
                f"{ref} 只能在 SKU 行上下文里使用,当前是 SPU 级字段"
            )
        return ctx.variant.get(ref.key, MISSING)
    raise SourceExprError(f"未实现的来源类型:{ref.kind}")  # pragma: no cover


# ---------------------------------------------------------------- 变换白名单

#: 允许在 spec 里声明的变换。**不允许在表达式里做运算。**
#:
#: `map` 只能引用 `dict/<name>.yaml`,不允许内联字典:内联字典会让
#: 「巴西站的颜色词表」散落在十几个字段声明里,改一次要找十几处。
TRANSFORMS: Mapping[str, tuple[str, ...]] = {
    "truncate": ("max_length",),
    "join": ("separator",),
    "upper": (),
    "lower": (),
    "map": ("dict",),
    "date_format": ("format",),
    "unit_convert": ("from", "to"),
    "pad": ("length", "fill"),
}


def validate_transform(spec: Mapping[str, Any]) -> None:
    """校验一条 transform 声明。启动期调用。"""
    name = spec.get("name")
    if name not in TRANSFORMS:
        raise SourceExprError(
            f"未知的 transform:{name!r}。白名单:{', '.join(sorted(TRANSFORMS))}"
        )
    missing = [p for p in TRANSFORMS[name] if p not in spec]
    if missing:
        raise SourceExprError(f"transform {name} 缺少参数:{', '.join(missing)}")
