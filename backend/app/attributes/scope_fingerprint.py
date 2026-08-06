"""双作用域样品指纹与 `facts_stale` 派生(PRD §5.3 / §8.1,修复 D1)。**零依赖。**

## 它修的是什么

PRD 风险表 D1:**指纹作用域过宽**。v3.0 只定义了单一 SPU 级样品指纹,
后果是「给颜色 A 补传一张图,颜色 B 的事实与图片也会被判 stale」——
多颜色 SPU 会被反复无谓返工。一个卖三个颜色的款,给其中一个补图,
另外两个颜色已经确认过的事实全部退回待确认,而运营看不出为什么。

对策是把指纹拆成两个作用域(§5.3):

    shared_fingerprint(spu)            该 SPU 全部证据素材(含各颜色与通用)
    variant_fingerprint(spu, variant)  该颜色的证据素材子集

验收 AC-21 就是这条的判据:「给颜色 A 补传样品后:共享事实与 A 色事实 stale,
B 色事实、B 色图片集、B 色草稿映射均不受影响」。

## 为什么是纯模块

这是一条**纯派生**:一组素材进,一组指纹出,不查库。放在这里的理由和
`media/evidence_rules.py`、`extractors/evidence_plan.py` 顶部那两条一样 ——
零依赖所以能穷举。而 AC-21 恰恰是那种「靠挑样本验不出来」的性质:
它说的是**任意**一次 A 色改动都不影响 B,那是一个全称命题,
挑三组样本证明不了它,穷举可以。

`stale_matrix` 的 `ASSET_REPLACED_OR_QUARANTINED / FACTS` 那一格点名的就是
本模块的 `changed_scopes`。

## 四处需要说明的判断

**一、证据集合的判定不在这里,复用 §5.1 那一个。**
`shared` 的集合定义是「全部 PRODUCT_EVIDENCE + READY 素材」,那正是
`evidence_rules.asset_is_extraction_input` 回答的问题。这里直接调它,
**不重写一遍条件** —— 重写第二套正是 PRD 风险表 D3 描述的漂移,
而漂移在这里的表现特别隐蔽:指纹用一套集合、识别用另一套,
于是「事实是根据哪些图算出来的」和「事实什么时候该过期」不是同一个问题的答案。

**二、通用素材(`color_variant_id` 为空)只进 shared,不进任何颜色子集。**
另一种写法是让它进**每一个**颜色子集 —— 那会让「补一张通用图」stale 掉
全部颜色,也就是 D1 那个问题从后门原样回来。
与 §6.3「颜色字段只在该颜色证据子集内合并」也对得上:通用图不参与颜色事实的
合并,那它就不该决定颜色事实什么时候过期。

**三、编码必须防拼接歧义。**
把字段用分隔符拼起来再哈希,是一个能被构造出碰撞的写法:
`("ab", "c")` 和 `("a", "bc")` 用 `"|"` 拼出来都是 `ab|c` 的变体族。
指纹碰撞在这里的后果不是抽象的安全问题,是**该过期的事实没有过期** ——
一份旧事实带着「指纹没变」的证明进了导出。所以每个字段按
「长度 + 冒号 + 内容」编码,长度前缀让拼接唯一可逆。

**四、算不出来就算过期。**
没有存过指纹(存量数据、迁移中途)时返回「过期」,不是「不过期」。
与 `workbench/stale.py` 的 `copy_attrs_stale(plan_missing=True) is True`
同一条口径:证明不了它仍然成立,就不能拿它去发布。
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

from app.media.evidence_rules import asset_is_extraction_input

#: 共享作用域的键。用 `None` 而不是某个字符串常量:任何字符串都可能
#: 和一个真实的 `color_variant_id` 撞上,而撞上的表现是「共享事实的指纹
#: 被某个颜色的指纹覆盖」—— 一个不会报错、只会让事实在错误的时机过期的 bug。
SHARED: None = None

#: 指纹版本。算法或元素集合有任何改动都要动它 —— 不动的话,
#: 老指纹和新指纹会被直接比较,结果是**全库事实一次性集体 stale**,
#: 而运营看到的是「所有东西同时过期了」,查不出原因。
#: 带上版本之后,至少「为什么全过期了」有个能指的答案。
FINGERPRINT_VERSION = "v1"


def length_prefixed(value: Any) -> str:
    """长度前缀编码。见模块文档第三条。

    **公开的**,因为它有第二个消费者:`attributes/run_state.py` 的幂等键
    要防同一种拼接歧义。抄一份过去的代价不是重复,是**两个编码器会漂移** ——
    改了这边的人不会知道那边也有一个,而两边都不报错。
    与 `field_limits` 那条规矩同源:一个东西有第二个消费者的那一刻,
    它就该有名字。
    """
    text = "" if value is None else str(value)
    return f"{len(text)}:{text}"


#: 老名字。本模块内部几十处调用照旧,改名只是为了让它能被外面引用
_field = length_prefixed


def _element(asset: Any) -> str:
    """一条素材在指纹里的样子(§5.3 的元素清单)。

    `status` 留在元素里是刻意的**冗余**:今天集合已经按 READY 过滤过,
    所以这一项在集合内恒等。但 `asset_is_extraction_input` 将来若放宽状态,
    不带 status 的指纹会对「素材从 READY 变成 QUARANTINED」视而不见 ——
    而那种失效不报错,只是让一份该过期的事实继续有效。

    「文件版本」这一项由 `content_hash` 承担:素材是内容寻址的,
    换了文件就是换了哈希。单独留一个 `file_version` 字段会制造第二个
    「这张图变没变」的答案。
    """
    return "".join(
        _field(getattr(asset, name, None))
        for name in ("asset_id", "content_hash", "source", "role", "color_variant_id", "status")
    )


def _digest(elements: Iterable[str]) -> str:
    """稳定排序后哈希。

    排序在**编码之后**做:按对象排序要求元素可比较,而 `role` / 变体 id
    都可能是 None,`None < str` 在 Python 3 直接抛 TypeError。
    编码之后全是字符串,排序总是成立的。
    """
    payload = _field(FINGERPRINT_VERSION) + "".join(sorted(elements))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _evidence(assets: Iterable[Any], *, imported_url_trusted: bool = False) -> list[Any]:
    """过成证据集合。判定复用 §5.1 那一个,见模块文档第一条。"""
    return [
        a
        for a in assets
        if asset_is_extraction_input(a, imported_url_trusted=imported_url_trusted)
    ]


class AssetView:
    """素材行 -> 指纹元素的入参(A45-batch14-15)。

    §5.3 的元素清单用的是 `asset_id` / `content_hash`,而 `MediaAsset` 上的
    列名是 `id` / `sha256`。**翻译放在这里,不放调用点** —— 与
    `media/evidence_rules.py` 那条同一个理由:判定可穷举而喂给判定的东西
    不可穷举的话,守卫就只能靠 AST 看「调用出现了没有」。

    `color_variant_id` 直接取(迁移 0037 之后它是真列)。**不取
    `variant_hint`**:那是模型猜出来的值,而指纹决定的是「一份已确认的事实
    什么时候过期」—— 让模型的猜测去让人工确认过的事实过期是反的。
    """

    __slots__ = ("asset_id", "content_hash", "source", "role", "color_variant_id", "status")

    def __init__(self, asset: Any) -> None:
        self.asset_id = getattr(asset, "id", None)
        self.content_hash = getattr(asset, "sha256", None)
        self.source = getattr(asset, "source", None)
        self.role = getattr(asset, "role", None)
        self.color_variant_id = getattr(asset, "color_variant_id", None)
        self.status = getattr(asset, "status", None)


def views(assets: Iterable[Any]) -> list[AssetView]:
    """一批素材行 -> 一批指纹入参。"""
    return [AssetView(a) for a in assets]


def shared_fingerprint(assets: Iterable[Any], *, imported_url_trusted: bool = False) -> str:
    """该 SPU 全部证据素材的指纹。供 SPU 共享事实的 `input_fingerprint` 比较。"""
    return _digest(
        _element(a) for a in _evidence(assets, imported_url_trusted=imported_url_trusted)
    )


def variant_fingerprint(
    assets: Iterable[Any], variant_id: str, *, imported_url_trusted: bool = False
) -> str:
    """某个颜色的证据素材子集的指纹。

    通用素材(`color_variant_id` 为空)**不在**子集里,见模块文档第二条。
    """
    return _digest(
        _element(a)
        for a in _evidence(assets, imported_url_trusted=imported_url_trusted)
        if _text_or_none(getattr(a, "color_variant_id", None)) == variant_id
    )


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def fingerprints(
    assets: Iterable[Any], *, imported_url_trusted: bool = False
) -> dict[str | None, str]:
    """一次算完所有作用域:`{None: 共享指纹, 颜色id: 颜色指纹, ...}`。

    颜色这一维**由素材自己带出来**,不需要调用方传一份颜色清单。
    传清单的写法有个具体的坏处:清单漏了一个颜色时,那个颜色的事实
    永远不会被判过期 —— 而「漏了一个」不会有任何征兆。

    代价是没有素材的颜色不出现在结果里。那由 `changed_scopes` 兜住:
    它比的是两次结果的并集,所以「最后一张图被删掉」也算一次变化。
    """
    kept = _evidence(assets, imported_url_trusted=imported_url_trusted)
    result: dict[str | None, str] = {SHARED: _digest(_element(a) for a in kept)}
    for variant in {
        v for v in (_text_or_none(getattr(a, "color_variant_id", None)) for a in kept) if v
    }:
        result[variant] = _digest(
            _element(a)
            for a in kept
            if _text_or_none(getattr(a, "color_variant_id", None)) == variant
        )
    return result


def changed_scopes(
    before: Iterable[Any], after: Iterable[Any], *, imported_url_trusted: bool = False
) -> frozenset[str | None]:
    """哪些作用域的指纹变了。**这是 AC-21 的落点。**

    给颜色 A 补一张图 → `{None, "A"}`,**不含 B**。
    改素材的颜色归属 A→B → `{None, "A", "B"}`:共享集合的元素里带着
    `color_variant_id`,所以归属一改,共享指纹也变(§5.3 明写这一条)。

    比的是两次结果的**并集**,不是交集:只比交集的话,「某个颜色的最后
    一张证据图被删掉」会因为那个键在 after 里不存在而被漏掉 ——
    而那恰恰是最该 stale 的一种变化。
    """
    old = fingerprints(before, imported_url_trusted=imported_url_trusted)
    new = fingerprints(after, imported_url_trusted=imported_url_trusted)
    return frozenset(
        scope for scope in set(old) | set(new) if old.get(scope) != new.get(scope)
    )


def facts_stale(*, stored_fingerprint: Any, current_fingerprint: Any) -> bool:
    """一条事实的 `input_fingerprint` 和当前作用域指纹对不对得上。

    **不落状态列**(§8.2「下游过期一律派生比较,不写 STALE 列」)。
    写列的话就有两个事实源:列说不过期、指纹说过期,而没人知道该信哪个。

    存量数据没有指纹时返回 True,见模块文档第四条 —— 证明不了它仍然成立,
    就不能拿它去发布。
    """
    stored = _text_or_none(stored_fingerprint)
    current = _text_or_none(current_fingerprint)
    if stored is None:
        return True
    return stored != current
