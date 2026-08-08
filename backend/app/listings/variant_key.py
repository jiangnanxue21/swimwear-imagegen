"""变体身份的**纯判定**(A44 / BLOCK-04 第一件)。零依赖。

## 这个模块要解决的那个 bug

A43 之前 `variant_id_for()` 是一个表达式:

    variant_id = primary_color or sku

`primary_color` 是运营在商品表单里**可以改的一列**。改一次颜色文案,
这个表达式的返回值就变了,而它同时是三样东西的主键:

    图片项的变体标签      → 旧标签变成 `unknown_tags`,有诊断,看得见
    属性值的 owner_id     → 已确认的颜色属性\"消失\",**没有等价诊断**
    导出 SKU 行的变体列   → 与前两者对不齐

A43 把属性分层接到了这个表达式上,于是第二条从「不存在」变成了
「存在且看不见」。`docs/DECISIONS.md` §3.17 把它记成本轮必须先处理的遗留。

## 修法:身份与名字分开

    variant_key    身份。**一次分配,此后不再由任何字段推导出来。**
    label          名字。永远是 `primary_color` 的当前值,随便改。

改名只动 label,不动 key。于是标签、属性、导出三者继续指着同一个东西。

## 为什么 key 的种子是旧表达式,而不是 UUID

分配那一刻的取值决定了迁移日会不会出事。

用 UUID:0027 跑完的瞬间,库里所有图片标签(自由文本,写的是颜色名)
和所有 `(VARIANT, 颜色名)` 上的属性值,全部指向不存在的 key ——
一次迁移把 A43 的那个 bug 在**每一个** SPU 上同时引爆一遍。

用旧表达式作种子:迁移日 `variant_key == primary_color or sku`,
与迁移前逐字节相同,存量数据一条都不用动。此后颜色再改,key 不跟着变。

代价是 key 看起来像颜色名,会有人想当然地去解析它。`label_of()` 存在
就是为了让\"要显示的那个字符串\"有一个正确的来源;直接把 key 打到界面上
的地方,在第一次改名之后就会显示旧名字。

## A45-batch13-2:身份上面又多了一级(外键)

batch13 建了 `color_variants` 表,新建档路径给每一行 SKU 写
`products.color_variant_id`,**而且不写 `variant_key`**(退役方向,守卫
`test_the_new_code_does_not_read_variant_key` 钉着)。

于是那批行两级回落全落空,掉到种子;而建档时 `primary_color` 是空的,
种子就是 sku,全库唯一 —— 一个三颜色九 SKU 的 SPU,在**每一个读取方**
眼里是九个颜色。阶段 1 的第一条验收在新表里成立,在读取侧不成立。

修法是把身份收成三级(`identity_of()`),而不是在建档路径上补一句
`assign_variant_key`:后者是往退役的方向再加一个依赖。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

#: `products.variant_key` 的列宽。消歧后缀要在这个预算里挤出来。
MAX_KEY_LENGTH = 64

#: 消歧后缀的分隔符。选 `~` 是因为它不出现在任何颜色文案里,
#: 也不是 SKU 编码的常用字符 —— 看到它就知道这个 key 被消过歧。
DISAMBIGUATOR = "~"


@dataclass(frozen=True)
class VariantRow:
    """参与判定的一行商品。

    只有判定与显示真正用得到的字段,而且全是标量:传 ORM 对象进来会让这个
    模块开始依赖数据库的形状,它的全部价值在于不依赖(同 `image_set_rules`)。

    A45-batch13-2 加了 `uid` 与 `variant_name` 两个 —— 它们是 `color_variants`
    那张表在这里的**投影**,不是新口径。加它们的理由见 `identity_of()`。
    """

    sku: str
    #: 已分配的稳定 key。未分配(存量行、还没 flush 的新行)时是空串
    key: str = ""
    #: `primary_color` 的**当前**值。这是名字,不是身份
    color: str = ""
    #: `products.color_variant_id` 的字符串形式。空串 = 这一行还没接上颜色变体表
    uid: str = ""
    #: 颜色变体自己的名字(`working_name`)。**只用于显示**,不参与身份判定。
    #: 建档刚做完那一刻 `color` 是空的,而这一列不是 —— 见 `label_for_row()`
    variant_name: str = ""


@dataclass(frozen=True)
class VariantRef:
    """一个变体对外的两个称呼。给 `resolve_ref()` 和界面用。"""

    key: str
    label: str


def normalize(text: str | None) -> str:
    """比较用的归一形式。**只用于比较,不落库。**

    做四件事,每一件都对应一种真实录入差异:

        NFKC        全角\"Ｒｅｄ\"与半角\"Red\"是同一个颜色
        casefold    \"RED\" / \"Red\" / \"red\"
        去空白      \"深 蓝\" 与 \"深蓝\"(中文录入里空格是随手打的)
        去分隔符    \"navy-blue\" / \"navy_blue\" / \"navy/blue\"

    **不做同义词映射。** \"藏青\"和\"navy\"是不是同一个颜色,这个模块答不了,
    猜错的后果是两个变体被悄悄并成一个 —— 那比多出一个变体严重得多。
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return "".join(ch for ch in folded if not ch.isspace() and ch not in "-_/\\")


def seed_for(*, color: str | None, sku: str) -> str:
    """A43 之前的那个表达式。**现在它只是种子,不再是身份。**

    保留原样(含 `or sku` 的回落)是迁移日零改动的前提,见模块文档。
    颜色为空时回落到 sku:存量里有一批商品还没跑属性识别,拿空串当
    变体 id 会把它们全并成一个变体,于是\"缺图\"永远检查不出来。
    """
    return (color or "").strip() or sku


def _fit(seed: str, suffix: str = "") -> str:
    """把 `seed + suffix` 塞进列宽。截 seed,不截后缀 —— 后缀是消歧的那一位。"""
    room = MAX_KEY_LENGTH - len(suffix)
    return seed[:room] + suffix


def split_disambiguator(key: str) -> tuple[str, str]:
    """把 key 拆成 (裸种子, 消歧后缀)。没有后缀时后缀是空串。

    只认"`~` + 纯数字"这一种形状,而且要求前面还剩点东西 —— 颜色名本身
    含 `~` 是合法的(比如某些促销色号),把它当后缀切掉会让一个正常变体
    平白变成"改过名"。
    """
    head, sep, tail = key.rpartition(DISAMBIGUATOR)
    if sep and head and tail.isdigit():
        return head, f"{DISAMBIGUATOR}{tail}"
    return key, ""


def key_matches_color(*, key: str, color: str | None, sku: str) -> bool:
    """这个 key 是不是"当前颜色现在该长的样子"。

    A-27:`drift()` 原来直接比 `normalize(color) != normalize(key)`,
    于是两类**没有改过名**的变体被恒判成 renamed:

        消歧后缀    key = "Red~2",颜色仍然是 "Red" -> 每次都报改名
        列宽截断    颜色超过 MAX_KEY_LENGTH,key 是它的前缀 -> 同样恒报

    不自己拼比较逻辑,而是**用同一组函数把 key 重新算一遍**:后缀原样带回、
    种子照 `seed_for` 取、长度照 `_fit` 截。铸造与判定共用一条路径,
    以后改铸造规则时判定自动跟上 —— 各写一份的下场见 §3.4。
    """
    _, suffix = split_disambiguator(key)
    expected = _fit(seed_for(color=color, sku=sku), suffix)
    return normalize(expected) == normalize(key)


def mint_key(*, color: str | None, sku: str, siblings: list[VariantRow]) -> str:
    """给一行新商品分配 key。**只在创建时调用一次。**

    三步,顺序不能换:

    1. **同 SPU 下已有同色的变体 → 复用它的 key。**
       这是唯一让\"新加一个尺码\"落到正确变体上的方式。少了这一步,
       同一个红色会因为两次创建拿到两个 key,图片按变体覆盖时红色永远缺一个。

    2. 没有同色的 → 用种子。迁移日的取值与 0027 的回填逐字节一致。

    3. 种子被占了、而占用者是**另一个**颜色 → 加消歧后缀。
       这一步平时不会走到:种子要么是颜色(步骤 1 已经处理了同色),
       要么是 SKU(全库唯一)。真正会撞的是这种:A 行没有颜色,key = 它的
       SKU \"SW-7\";之后 B 行的颜色文案恰好就叫 \"SW-7\"。荒谬但不违法,
       而两个变体共用一个 key 会让它们的属性互相覆盖 —— 这一步宁可丑。

    颜色为空**不复用**任何 key:空颜色是\"还不知道\",不是一种颜色。
    把两个未知归并成一个变体,等于宣称它们同色。
    """
    normalized = normalize(color)
    taken = {row.key for row in siblings if row.key}

    if normalized:
        for row in siblings:
            if row.key and normalize(row.color) == normalized:
                return row.key

    seed = seed_for(color=color, sku=sku)
    candidate = _fit(seed)
    if candidate not in taken:
        return candidate

    n = 2
    while True:
        suffix = f"{DISAMBIGUATOR}{n}"
        candidate = _fit(seed, suffix)
        if candidate not in taken:
            return candidate
        n += 1


def label_of(*, key: str, color: str | None) -> str:
    """界面上该显示的那个字符串。**当前颜色优先,不是 key。**

    key 是分配那一刻的颜色名。改名之后它就是一个历史值 ——
    把它直接打到界面上,运营会看到自己刚改掉的旧名字,并且再改一次。
    """
    return (color or "").strip() or key


#: `identity_source()` 的三个取值。**不要写字面量** —— 诊断分级按它分桶,
#: 各处各拼一份字符串的下场和 §3.4 一样。
SOURCE_VARIANT_UID = "color_variant_id"
# Kept for response/test compatibility while older clients still know the name.
# Runtime identity selection no longer emits this source after migration 0046.
SOURCE_VARIANT_KEY = "variant_key"
SOURCE_SEED = "seed"


def identity_of(row: VariantRow) -> str:
    """一行商品属于哪个变体。**全仓唯一一份定义,两级顺序不许各写各的。**

        1. `color_variant_id`   外键。batch13 起新建档路径写的就是它
        2. 种子表达式           `primary_color or sku`,仅供未迁移行的诊断

    ## 为什么必须收成一处

    加这一级之前,「key 或种子」这句话在 `ids_of` / `refs_of` /
    `variants.variant_id_for` 里各写了一份。两级时三份恰好等价,所以谁都
    没发现;加进外键这一级就会分叉,而分叉的表现正是本模块顶部警告的那个
    「导出铺三行、属性写一个变体」—— 两边都不报错,只有平台会。

    ## 为什么不再回落到 key

    0046 已把属性 owner_id 与图片标签和 `color_variant_id` 一起迁移。
    `variant_key` 的列和值只为 downgrade 保留;继续读取它会重新打开一条
    非 UUID 身份路径,并把正常的迁移后形状误报成 `identity_shadowed`。
    """
    uid = (row.uid or "").strip()
    if uid:
        return uid
    # ---- `variant_key` 这一级**退役了**(阶段 1,A45-batch14-28)----
    #
    # 它曾经在这里,而迁移 0046 已经把库里的两处消费点(属性 owner_id、
    # 图片项变体标签)从 key 改写成了 `color_variants.id`,并且把
    # `products.color_variant_id` 回填齐了 —— 那一次改写和回填是**同一个
    # 动作**,理由见 §3.23 与 0046 的文件头。
    #
    # ## 为什么是删掉而不是留着当第二级
    #
    # 留着它就还有一条路径能产出非 UUID 的身份,而 `owner_for()` 现在对
    # 非 UUID 的变体身份是**当场失败**的。两者并存的表现是:一批行在识别时
    # 报错,而错误信息指向"没回填",实际原因是它回落到了一个已经退役的级别。
    #
    # 更要紧的是留着它会让"退役"变成一句没有落点的话。0046 为了可降级
    # 刻意保留列和值,所以「同时有 uid 和 key」从此是正常迁移结果,
    # **不能**再由 drift 报成异常;代码侧唯一正确动作是完全不读这一列。
    return seed_for(color=row.color, sku=row.sku)


def identity_source(row: VariantRow) -> str:
    """这一行的身份是从哪一级来的。给诊断分级用,不参与判定。

    和 `identity_of()` 共用一套条件而不是各写一遍:两者一旦不同源,
    诊断会指着一个和实际身份无关的级别,而那比没有诊断更误导人。
    """
    if (row.uid or "").strip():
        return SOURCE_VARIANT_UID
    return SOURCE_SEED


def label_for_row(row: VariantRow) -> str:
    """界面上该显示的名字。**三级回落,身份排在最后。**

        1. `primary_color`   当前颜色。改名之后立刻生效,这是它该有的样子
        2. `variant_name`    颜色变体自己的名字(建档时填的 `working_name`)
        3. 身份              实在没有名字了才显示

    第二级是 batch13 加的,少了它就会这样:建档刚做完那一刻属性识别还没跑,
    `primary_color` 是空的,而身份已经是一个 UUID —— 于是界面上三个颜色
    显示成三串十六进制,运营认不出哪个是黑色。`label_of()` 的两级回落
    在 key 时代够用(key 长得像颜色名),换成外键之后就不够了。
    """
    # 叠两次 `label_of()`,不自己写三段回落:那个函数就是"前者非空就用它、
    # 否则回落"这一句话的定义,链再长也只是把同一句话叠起来。各写一份的话,
    # 以后改 strip 规则会漏掉这里 —— §3.4 那条的同一个形状
    named = label_of(key=identity_of(row), color=row.variant_name)
    return label_of(key=named, color=row.color)


def ids_of(rows: list[VariantRow]) -> list[str]:
    """这些商品行覆盖的全部变体 id,按传入顺序去重。

    返回列表而不是集合:进审计与错误消息时顺序要可复现,
    否则同一份数据两次批准给出的违规文案顺序不同,像是数据变了。
    """
    seen: dict[str, None] = {}
    for row in rows:
        seen.setdefault(identity_of(row), None)
    return list(seen)


def refs_of(rows: list[VariantRow]) -> list[VariantRef]:
    """变体目录:每个变体的 key 与当前名字。

    同一个 key 的多行(同色不同尺码)只出一条,名字取第一次遇到的那行 ——
    同 key 不同色是数据异常,由 `drift()` 的 `key_label_conflicts` 单独报,
    不在这里悄悄挑一个。

    A-28:这句承诺原来是**假的** —— `drift()` 那时报三类,一类都不是它。
    于是"这里挑第一个没关系,反正别处会报"这个理由不成立,而读代码的人
    没法从这里看出来。现在那一类真的会报了。
    """
    out: dict[str, VariantRef] = {}
    for row in rows:
        key = identity_of(row)
        if key not in out:
            out[key] = VariantRef(key=key, label=label_for_row(row))
    return list(out.values())


def resolve_ref(ref: str | None, refs: list[VariantRef]) -> str | None:
    """把\"用户写的那个变体名\"翻译成 key。翻不动时返回 None。

    ## 为什么写入侧必须过这一层

    图片项的 `variant_id` 是接口入参,自由文本。人看着界面上的**当前**
    颜色名打字,而库里的身份是 key(可能是一个历史颜色名)。不翻译的话:
    改过一次名的 SPU,新打的标签全部落进 `unknown_tags`,而旧标签还好好的 ——
    表现是\"越是最近绑的图越对不上\",没有人会往改名那边想。

    四级匹配,严格程度递减:

        1. key 完全相等          最常见,机器回填的那一路
        2. key 归一后相等        人从旧诊断里抄了一个 key,大小写/空格有出入
        3. label 归一后相等      人照着界面打的当前颜色名 —— 这一条是重点
        4. 都不中 → None         调用方决定是拒绝还是记成 unknown

    label 撞了两个变体(改名撞车)时**返回 None,不猜**:两个红色里
    挑一个的后果是图挂到错的颜色上,而那个错误在平台侧才会暴露。
    """
    if ref is None:
        return None
    raw = ref.strip()
    if not raw:
        return None

    for item in refs:
        if item.key == raw:
            return item.key

    target = normalize(raw)
    if not target:
        return None

    hits = [item.key for item in refs if normalize(item.key) == target]
    if len(hits) == 1:
        return hits[0]

    hits = [item.key for item in refs if normalize(item.label) == target]
    if len(hits) == 1:
        return hits[0]
    return None


def drift(rows: list[VariantRow]) -> dict[str, list[str]]:
    """报告 UUID 身份目录里的异常。**诊断,不阻断。**

    key 不再跟着颜色走,于是出现了两种 A43 不可能有的状态:

        renamed             兼容返回键。0046 后恒为空。
        label_collisions    两个身份的当前颜色一样了。这是**真问题**:
                            界面上两个变体同名,运营分不出该给哪个绑图,
                            而 `resolve_ref()` 遇到它会拒绝翻译。
                            成因通常是把\"正红\"和\"大红\"都改成了\"红色\" ——
                            那是一次合并,应当显式做,不该由改名顺手完成。
        unassigned          **身份只剩种子**的行 —— 没有颜色外键,不能安全
                            写入 UUID owner。
        key_label_conflicts 同一个身份下出现了两个不同的颜色(A-28)。
                            `refs_of()` 遇到它会**悄悄挑第一个**当目录名,
                            于是界面上这个变体叫什么取决于行的顺序。
                            成因是两行本该是两个变体却共用了身份,
                            或者其中一行的颜色被单独改过。
        identity_shadowed   0046 后保留的兼容键。为保持接口形状继续返回,
                            但必须恒为空:`variant_key` 只供 downgrade 使用,
                            同时有外键和值是迁移后的正常形状,不是异常。

    五个列表都按 sku / 身份排序,输出可复现。

    A-27:renamed 的判据从"key 和颜色字符串不相等"换成
    `key_matches_color()`。原来带消歧后缀的 key(`Red~2`)和被列宽截断的
    长颜色名**永远**在这张表里,而它们一次名都没改过 —— 诊断噪声会让
    真正改过名的那几行淹掉。

    A45-batch13-2:两条判据必须跟着三级身份改,否则新路径的每一行都会恒报。

        unassigned  判据原来是"没有 variant_key"。新建档路径不写 key(守卫
                    明令禁止),于是它建的每一行都会永远挂在这张表上 ——
                    而它们恰恰是唯一带真外键的那批。**恒报等于不报。**
        renamed     判据只在身份来自 `variant_key` 时才有意义:key 的种子
                    是颜色名,所以"对不上"说明改过名。外键是 UUID,拿它和
                    颜色名比永远不相等,同样恒报。
    """
    renamed: list[str] = []
    unassigned: list[str] = []
    shadowed: list[str] = []
    by_label: dict[str, set[str]] = {}
    colors_by_key: dict[str, set[str]] = {}

    for row in rows:
        source = identity_source(row)
        if source == SOURCE_SEED:
            unassigned.append(row.sku)
            continue
        identity = identity_of(row)
        # 颜色为空是"还不知道",不是"改过名" —— 与 mint_key 里
        # "空颜色不复用任何 key"是同一个立场
        # `variant_key` 已退役,不再参与身份与改名诊断。保留 renamed 返回键
        # 仅为接口兼容;新口径下它恒为空。
        by_label.setdefault(normalize(label_for_row(row)), set()).add(identity)
        if normalize(row.color):
            colors_by_key.setdefault(identity, set()).add(normalize(row.color))

    collisions: list[str] = []
    for keys in by_label.values():
        if len(keys) > 1:
            collisions.extend(keys)

    return {
        "renamed": sorted(set(renamed)),
        "label_collisions": sorted(set(collisions)),
        "unassigned": sorted(set(unassigned)),
        "key_label_conflicts": sorted(
            key for key, colors in colors_by_key.items() if len(colors) > 1
        ),
        "identity_shadowed": sorted(set(shadowed)),
    }
