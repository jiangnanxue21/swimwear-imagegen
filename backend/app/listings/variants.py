"""SPU 的变体口径 —— **全仓唯一一份定义**。

## 为什么要单独一个模块

「一个 SPU 有哪几个变体」这句话原来只存在于 `workbench/service.py`
的 `_canonical_with_skus()` 里,写成一个内联表达式:

    variant_id=(row.primary_color or row.sku)

而图片集的变体覆盖校验(`image_set_rules.validate_set` 的 `variant_ids`)
需要同一个答案。让校验那边再写一遍的话,两处会各自漂移 ——
表现是「导出的 SKU 行按颜色铺了 3 行,而批准时只检查了 1 个变体」,
两边都不会报错,只有平台会。

## A44:变体 id 不再由颜色推导

那个表达式的问题在 `docs/DECISIONS.md` §3.17 记着:`primary_color`
是运营可以改的一列,改一次,图片标签、属性 owner_id、导出变体列
三样东西同时换主键。A43 把属性分层接上去之后,这件事从「有诊断」
变成了「属性侧无声丢值」。

身份因此不再由任何可编辑字段推导出来。判定规则全部在零依赖的
`variant_key.py` 里,这个模块只负责查库和写库。

    variant_id_for()      身份。给属性 owner_id、图片标签、导出用
    variant_label_for()   名字。给界面用 —— 不要拿身份当显示值
    assign_variant_key()  分配。**只在创建商品时调用**

## A45-batch13:身份的权威从 key 换成外键

batch13 之后身份有三级:`products.color_variant_id` → `products.variant_key`
→ 种子表达式。顺序与理由写在 `variant_key.identity_of()`,**这个模块里
一个字都不重复** —— 上一版把"key 或种子"这句话在三个函数里各写了一份,
两级时三份恰好等价,所以谁都没发现写重了。

新建档路径(`services/spu_service.create_spu`)只写外键、不写 `variant_key`
(退役方向)。少了第一级的话,它建出来的九行 SKU 在每一个读取方眼里是
九个颜色 —— 那是 batch13 发出去的那个缺陷。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.listings import variant_key as vk
from app.models.product import Product


def _row(product: Product, *, variant_name: str = "") -> vk.VariantRow:
    """一行商品的判定投影。**只读列,不碰 relationship。**

    `variant_name` 由调用方传进来而不是在这里读 `product.color_variant`:
    这个函数被 `variant_id_for()` 在导出与属性识别的按行循环里调用,
    在里面碰一次关系属性就是一次惰性加载 —— 九行 SPU 一次查询变十次,
    而查询预算那条棘轮按 AST 看循环,看不见 ORM 惰性加载。

    需要名字的三个函数走 `_rows()`,那条路已经预取过了。
    """
    return vk.VariantRow(
        sku=product.sku,
        key=(product.variant_key or "").strip(),
        color=product.primary_color or "",
        uid=str(product.color_variant_id or ""),
        variant_name=variant_name,
    )


def variant_id_for(product: Product) -> str:
    """一个 SKU 行对应的变体 id。**这是身份,不是显示名。**

    三级顺序(外键 → `variant_key` → 种子)的定义在 `variant_key.identity_of()`,
    **这里不重写一遍**:同一句话原来在这个函数、`ids_of()`、`refs_of()` 里
    各有一份,两级时三份恰好等价,加进外键这一级就会分叉。

    掉到第三级的行会被改名带偏,`drift()` 的 `unassigned` 把它们点名列出来。
    """
    return vk.identity_of(_row(product))


def variant_label_for(product: Product) -> str:
    """界面上该显示的名字。**不是身份。**

    回落顺序见 `variant_key.label_for_row()`。单个商品对象这里拿不到颜色
    变体的名字(读它要碰 relationship,见 `_row()`),所以这条路只有两级;
    要三级完整的名字,用 `variant_directory()` —— 图片绑定界面用的就是它。
    """
    return vk.label_for_row(_row(product))


def _siblings(session: Session, spu: str) -> list[Product]:
    """同一个 SPU 下的全部商品行。**预取颜色变体。**

    `_rows()` 要读颜色变体的名字。不预取的话九行 SPU 一次查询变十次,
    而这条路径同时被图片集校验和属性完整度消费,每次批准都会走一遍。
    """
    return list(
        session.scalars(
            select(Product)
            .where(Product.spu == spu)
            .options(selectinload(Product.color_variant))
            .order_by(Product.sku)
        )
    )


def _rows(session: Session, spu: str) -> list[vk.VariantRow]:
    """判定用的行,**带上显示名**。目录、覆盖校验、诊断三处共用。

    显示名回落到 `variant_code`(A45-batch13-3 / R5):`working_name` 在建档
    表单上是可选的(schema 默认空串),只填了编码的颜色,三级回落会一路掉到
    身份 —— 目录里那一项就是一串 UUID。"BLK" 不是好名字,但它是运营自己填的、
    印在样品袋上的那个编码;十六进制谁也认不出。**编码只用于显示回落,
    不参与身份判定** —— 身份仍然只有 `identity_of()` 那三级。
    """
    return [
        _row(
            p,
            variant_name=(
                (p.color_variant.working_name or p.color_variant.variant_code)
                if p.color_variant
                else ""
            ),
        )
        for p in _siblings(session, spu)
    ]


def assign_variant_key(session: Session, product: Product) -> str:
    """给一件**新**商品分配稳定 key,并写回对象。返回分配到的值。

    ## 必须在 `session.add()` 之前调用

    它要按 SPU 查同门兄弟来决定「有没有同色的变体已经存在」。
    先 add 再调用的话,自己也会出现在兄弟里(flush 之后),
    于是「有同色的」永远成立,每一行都复用自己的 key —— 看起来正常,
    实际上第 2 行开始的复用逻辑从来没有被执行过。

    ## 已经有 key 的对象原样返回

    导入是幂等的(重复执行只跳过已存在的 SKU),但调用方不保证只调一次。
    重新分配一次的后果是把一个已经被属性和图片引用的身份换掉 ——
    那正是这一整轮要消灭的事。
    """
    existing = (product.variant_key or "").strip()
    if existing:
        return existing
    key = vk.mint_key(
        color=product.primary_color,
        sku=product.sku,
        siblings=[_row(p) for p in _siblings(session, product.spu)],
    )
    product.variant_key = key
    return key


def required_variant_ids(session: Session, spu: str) -> list[str]:
    """这个 SPU 需要被图片覆盖的全部变体,按稳定顺序。

    返回列表而不是集合:进审计与错误消息时顺序要可复现,
    否则同一份数据两次批准给出的违规文案顺序不同,像是数据变了。
    """
    return vk.ids_of(_rows(session, spu))


def variant_directory(session: Session, spu: str) -> list[vk.VariantRef]:
    """变体目录:每个变体的 key 与当前名字。

    图片绑定界面要的就是这张表 —— 让人从**当前颜色名**里挑,
    存进去的是 key。自由文本打字是 A43 那条 `unknown_tags` 的来源。
    """
    return vk.refs_of(_rows(session, spu))


def resolve_variant_ref(session: Session, spu: str, ref: str | None) -> str | None:
    """把入参里的变体称呼翻译成 key。翻不动返回 None。

    写入侧必须过这一层,理由在 `variant_key.resolve_ref()` 的注释里:
    人照着界面上的当前颜色名打字,而库里的身份可能是一个历史颜色名。
    """
    if ref is None or not ref.strip():
        return None
    return vk.resolve_ref(ref, variant_directory(session, spu))


def drift_report(session: Session, spu: str) -> dict[str, list[str]]:
    """稳定 key 带来的两种新异常 + 没分到 key 的行。**诊断,不阻断。**"""
    return vk.drift(_rows(session, spu))
