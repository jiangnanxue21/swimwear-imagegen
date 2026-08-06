"""SPU → 颜色 × 尺码 → SKU 的展开规则 —— **全仓唯一一份定义,零三方依赖。**

PRD v3.1 §13 阶段 1 的第一条验收是「可构造三颜色九 SKU 的 SPU」。这个模块
就是那句话的可执行形式:给定 SPU 编码、若干颜色变体编码、一个尺码模板,
算出该建哪些 SKU 行,以及每一行的 `sku` / `size` / `size_group`。

## 为什么 SKU 编码是算出来的,不是人填的

建一个三颜色的 SPU 要落 9 行 `products`。让人逐行填 `sku`,就是 9 次打字机会,
而**打错的 SKU 和正确的 SKU 在库里长得一模一样** —— `uq_products_sku` 只保证
不重复,不保证对。等到发现时,那一行已经挂上了素材、生成任务和图片集。

算出来之后,「这一行属于哪个颜色、哪个尺码」在编码里就是可读的,而真正的
归属仍然由外键(`color_variant_id`)负责 —— 编码是给人看的,不是给程序推断用的
(§4.4:`spu` 字符串降级为反规范化读列,禁止作为查询权威;这里同理)。

## 分隔符:只有后两段禁用 —— 和 owner_id 命名空间是同一类事故,解法不同

`attributes/validation.variant_owner_id()` 顶部记着一次真实的歧义:

    spu="A"    variant="B/C"   →  "A/B/C"
    spu="A/B"  variant="C"     →  "A/B/C"

两个不同的身份拼成同一个字符串,而两边都不报错。那里的解法是加长度前缀
(SPU 是自由文本,收不动)。这里的编码是本期新建的,可以收输入 —— 但**不能
一刀切**:仓库里既有的 SKU 长这样

    SPU-SW-001 + BLK + S   →   SW-001-BLK-S      (sample-data 里那 10 件)

第一段自己就带横线。禁掉全部分隔符等于要求运营把用了很久的编码改掉,
而那个编码同时印在样品袋和供应商单据上。

所以规则是**后两段禁用,第一段允许**:颜色编码与尺码里没有分隔符,于是
从右边数,最后两段一定是"尺码"和"颜色",剩下的全是 SPU 编码。给定一个
拼出来的 SKU,三段的切法**唯一** —— 两个不同的三元组拼不出同一个字符串:

    spu="A-B" variant="C" size="S"  →  "A-B-C-S"
    要用别的三元组拼出 "A-B-C-S",得让 variant="B-C" 或 size="C-S",
    而那两段的字符集里没有分隔符 —— 建不出来

**这不是"宽松一点"** ,是把不变式挪到了真正需要它的那两段上。
歧义仍然在结构上不可能,而且不用带一个解析器。

## 尺码模板

「尺码模板」在阶段 1 的交付清单里(§13)。它同时解决两件事:

    一、9 行 SKU 的尺码不用逐行敲;
    二、给 `products.size_group` 一个**真实来源**。那一列从 M6 起就在,
        取值一直是自由文本 —— 于是同一个 SPU 里 "ALPHA" 和 "alpha_4" 并存,
        而按尺码组聚合的地方(渠道 spec、导出)只能各自猜。

`ONE_SIZE` 是一个只有一个成员的模板,**不是** "size 留空"。这不是洁癖:
`UNIQUE(color_variant_id, size)` 在 PostgreSQL 下对 NULL 不生效(NULL 彼此不相等),
于是尺码留空的行可以在同一个颜色下重复任意多次,而唯一约束一声不吭。
给它一个真实取值 `"OS"`,那条约束才管得住它。
"""
from __future__ import annotations

from dataclasses import dataclass

#: SKU 编码的分段分隔符。见模块文档:它**不许**出现在任何一段编码里面。
SEPARATOR = "-"

#: 编码允许的字符。刻意不含小写:`SW-001-blk-S` 与 `SW-001-BLK-S` 在人眼里
#: 是同一个 SKU,在 `uq_products_sku` 眼里是两个 —— 大小写不敏感的唯一约束
#: 要么改列类型(citext),要么在入口归一化。选后者,因为它对存量零影响。
_ALLOWED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")

#: SPU 编码额外允许分隔符,理由见模块文档("后两段禁用,第一段允许")。
_ALLOWED_IN_SPU_CODE = _ALLOWED | {SEPARATOR}

#: 各段编码的长度上限。`products.sku` 是 VARCHAR(64),三段加两个分隔符必须装得下;
#: 这里的三个数字加起来是 32 + 16 + 8 + 2 = 58,留了余量。
MAX_SPU_CODE = 32
MAX_VARIANT_CODE = 16
MAX_SIZE_CODE = 8

#: 一个 SPU 最多几个颜色、一个模板最多几个尺码、一次展开最多落几行。
#:
#: 取值按「业务上明显不可能达到」定,与 `core/field_limits` 顶部那段同一口径:
#: 上限是用来把手滑和脚本失控挡在门外的,不是容量规划。一次建档落 500 行
#: `products` 不是大生意,是有人把循环写错了。
MAX_VARIANTS_PER_SPU = 24
MAX_SIZES_PER_TEMPLATE = 20
#: 行数上限**必须小于**「颜色上限 × 最长模板」,否则它一次都不会触发。
#: 第一版写的是 200,而 24 × 6(最长模板 ALPHA_6)= 144 —— 一条永远不会
#: 生效的守卫,比没有守卫更糟:它看起来在保护,实际什么都不做。
#: `test_the_row_limit_can_actually_be_reached` 钉着这个关系。
MAX_SKUS_PER_EXPANSION = 120

#: 尺码模板:名字 -> 有序尺码。**名字同时是 `products.size_group` 的取值**,
#: 所以它是业务标识,不是展示文案 —— 界面文字在 `SIZE_TEMPLATE_LABELS`。
#:
#: 顺序是展示顺序,不是字典序:S/M/L/XL 按字典序排出来是 L/M/S/XL,
#: 而尺码表是运营和平台都要看的东西。`expand()` 原样保留这个顺序。
SIZE_TEMPLATES: dict[str, tuple[str, ...]] = {
    # ALPHA_3 是 `sample-data/` 里那 10 件示例商品用的尺码段(S/M/L),
    # 也是 PRD §13 阶段 1 那句「三颜色九 SKU」的构造:3 × 3 = 9
    "ALPHA_3": ("S", "M", "L"),
    "ALPHA_4": ("S", "M", "L", "XL"),
    "ALPHA_6": ("XS", "S", "M", "L", "XL", "XXL"),
    "NUMERIC_EU": ("34", "36", "38", "40", "42", "44"),
    "ONE_SIZE": ("OS",),
}

SIZE_TEMPLATE_LABELS: dict[str, str] = {
    "ALPHA_3": "字母码 S–L",
    "ALPHA_4": "字母码 S–XL",
    "ALPHA_6": "字母码 XS–XXL",
    "NUMERIC_EU": "欧码 34–44",
    "ONE_SIZE": "均码",
}


class SkuPlanError(ValueError):
    """展开规则被违反。带上是哪一段编码出的问题,好让接口层原样回给运营。

    继承 `ValueError` 而不是仓库的 `ValidationError`:这个模块要能被
    无三方依赖的纯测试直接导入,而 `core/errors` 拉着 FastAPI 的状态码语义。
    翻成 422 是服务层的事(`spu_service` 里那一处 except)。
    """

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


@dataclass(frozen=True)
class PlannedSku:
    """展开出来的一行。**这不是 ORM 对象**,只是「该建什么」的答案。

    服务层拿它去建 `Product`。分开的理由:展开规则要能在没有数据库、
    没有 sqlalchemy 的环境里被验证,而这正是本仓库 1800 多条纯测试的前提。
    """

    sku: str
    variant_code: str
    size: str
    size_group: str


def normalize_code(
    value: str | None, *, field: str, max_length: int, allow_separator: bool = False
) -> str:
    """把一段编码归一化成规范形式,不合法就抛。

    `allow_separator` 只对 SPU 编码开(见模块文档)。默认关掉,所以新写的
    调用点得**显式**说"我这一段允许横线" —— 反过来(默认允许、需要时收紧)
    的话,漏写一次的后果是颜色编码里混进分隔符,而那正是唯一性赖以成立的前提。

    归一化只做两件事:去首尾空白、转大写。**不做**去掉非法字符这种"善后":
    悄悄把 `BLK-1` 变成 `BLK1` 之后,运营看到的 SKU 和他填的不一样,
    而他不会收到任何提示 —— 那比直接报错难查得多。
    """
    text = (value or "").strip().upper()
    if not text:
        raise SkuPlanError(field, f"{field} 不能为空")
    if len(text) > max_length:
        raise SkuPlanError(field, f"{field} 最长 {max_length} 个字符,当前 {len(text)}")
    if SEPARATOR in text and not allow_separator:
        # 单独报这一条,不和"非法字符"合并:分隔符是**最可能**被填进来的那个,
        # 而且原因不显然(见模块文档)。合并之后运营只会看到"含非法字符",
        # 然后把 `SPU-SW-001` 里的横线也一起删掉。
        raise SkuPlanError(
            field,
            f"{field} 里不能含 {SEPARATOR!r} —— 它是 SKU 编码的分段分隔符。"
            f"只有 SPU 编码那一段允许含它(那一段在拼出来的 SKU 里排最前,"
            f"从右边数得出边界);颜色与尺码含它会让两个不同的商品拼出同一个 SKU",
        )
    if allow_separator and (text.startswith(SEPARATOR) or text.endswith(SEPARATOR)):
        # 这一条**不是**唯一性要求 —— `"A-"` 作为 SPU 编码拼出来的 SKU
        # 仍然切得开。它挡的是手滑:多打一个横线不会有任何下游报错,
        # 只会让每一行 SKU 都长一个双横线,而那时已经建了九行
        raise SkuPlanError(
            field, f"{field} 不能以 {SEPARATOR!r} 开头或结尾(多半是多打了一个)"
        )
    allowed = _ALLOWED_IN_SPU_CODE if allow_separator else _ALLOWED
    illegal = sorted(set(text) - allowed)
    if illegal:
        extra = f" {SEPARATOR}" if allow_separator else ""
        raise SkuPlanError(
            field,
            f"{field} 含不允许的字符 {''.join(illegal)!r}(只允许 A-Z 0-9 _{extra})",
        )
    return text


def normalize_spu_code(value: str | None) -> str:
    """SPU 编码的归一化入口。**调用方一律走这个,不要自己传 `allow_separator`。**

    有两个调用点(`expand()` 与 `spu_service.create_spu`),而"SPU 那一段允许
    横线"是一个容易被下一个调用点漏掉的例外。包成一个函数之后,漏掉的表现是
    NameError,不是"某个入口悄悄比别的严一点"。
    """
    return normalize_code(
        value, field="spu_code", max_length=MAX_SPU_CODE, allow_separator=True
    )


def sku_code(*, spu_code: str, variant_code: str, size: str) -> str:
    """三段拼成 SKU。**唯一一处拼接点。**

    在这之前,样例数据里的 `SW-001-BLK-S` 只是一个约定 —— 约定不会在被打破时
    报错。放进函数里之后,「SKU 长什么样」这句话有了一个能被测试钉住的答案。
    """
    return SEPARATOR.join((spu_code, variant_code, size))


def sizes_of(template: str) -> tuple[str, ...]:
    """模板里的尺码。模板不存在时抛 —— 不回落到"空列表"或某个默认模板。

    回落的后果是一个 SPU 一行 SKU 都没有(空列表),或者尺码全错(默认模板),
    两种都在建档那一步看起来"成功了"。
    """
    name = (template or "").strip().upper()
    sizes = SIZE_TEMPLATES.get(name)
    if sizes is None:
        raise SkuPlanError(
            "size_template",
            f"没有名为 {template!r} 的尺码模板,可选:{', '.join(sorted(SIZE_TEMPLATES))}",
        )
    return sizes


def expand(
    *,
    spu_code: str,
    variant_codes: list[str] | tuple[str, ...],
    size_template: str,
) -> tuple[PlannedSku, ...]:
    """颜色 × 尺码 展开成 SKU 行。顺序:先颜色(按传入顺序),再尺码(按模板顺序)。

    三颜色 × 四尺码 = 12 行;PRD 那句「三颜色九 SKU」是三颜色 × 三尺码,
    取哪个模板由建档时选。

    ## 重复颜色编码在这里就被挡住

    `UNIQUE(spu_id, variant_code)` 是最终裁判,但等到 flush 才报错的话,
    错误信息是一句数据库约束名,而运营填的是第 2 个还是第 5 个颜色重复了
    没人知道。这里报,能点名。
    """
    spu = normalize_spu_code(spu_code)
    sizes = sizes_of(size_template)
    template_name = (size_template or "").strip().upper()

    if not variant_codes:
        raise SkuPlanError("variant_codes", "至少要有一个颜色变体")
    if len(variant_codes) > MAX_VARIANTS_PER_SPU:
        raise SkuPlanError(
            "variant_codes",
            f"一个 SPU 最多 {MAX_VARIANTS_PER_SPU} 个颜色变体,当前 {len(variant_codes)}",
        )
    if len(sizes) > MAX_SIZES_PER_TEMPLATE:  # pragma: no cover - 模板是常量,守的是将来加模板
        raise SkuPlanError("size_template", f"尺码模板最多 {MAX_SIZES_PER_TEMPLATE} 个尺码")

    normalized: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(variant_codes):
        code = normalize_code(raw, field=f"variant_codes[{index}]", max_length=MAX_VARIANT_CODE)
        if code in seen:
            raise SkuPlanError(
                f"variant_codes[{index}]",
                f"颜色编码 {code} 在同一个 SPU 里重复了 —— 它是 SPU 内的稳定标识,必须唯一",
            )
        seen.add(code)
        normalized.append(code)

    total = len(normalized) * len(sizes)
    if total > MAX_SKUS_PER_EXPANSION:
        raise SkuPlanError(
            "variant_codes",
            f"一次展开最多 {MAX_SKUS_PER_EXPANSION} 行 SKU,"
            f"当前 {len(normalized)} 个颜色 × {len(sizes)} 个尺码 = {total} 行",
        )

    rows: list[PlannedSku] = []
    for code in normalized:
        for size in sizes:
            # 尺码来自模板常量,不是用户输入 —— 但仍然过一遍归一化。
            # 理由是**将来**:模板迟早会变成运营可配置的,那时这里是唯一的闸,
            # 而"当时是常量所以不用校验"这句话不会跟着改。
            size_code = normalize_code(size, field="size", max_length=MAX_SIZE_CODE)
            sku = sku_code(spu_code=spu, variant_code=code, size=size_code)
            if len(sku) > 64:
                # `products.sku` 是 VARCHAR(64)。三段各自的上限已经保证装得下,
                # 这一条守的是有人把上限调大之后忘了这里 —— 那时的表现是
                # flush 阶段一句 StringDataRightTruncation,行号在批量插入里丢掉
                raise SkuPlanError(
                    "sku",
                    f"拼出来的 SKU {sku!r} 超过 64 字符(products.sku 的列宽)",
                )
            rows.append(
                PlannedSku(
                    sku=sku, variant_code=code, size=size_code, size_group=template_name
                )
            )
    return tuple(rows)
