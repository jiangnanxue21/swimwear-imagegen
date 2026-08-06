"""AI 图伪装成原始样品(PRD §11 第二行 / 验收 AC-22)。**纯判定,零三方依赖。**

## 规格原文

    | 场景 | 预期行为 |
    | AI 候选文件被当作"原始样品"重新上传 | 同 SPU sha256 命中已带溯源的素材行
      → 拒绝并提示来源冲突,落隔离待人工放行 |

AC-22 是它的验收:「将某 AI 候选文件经上传通道重新提交为『原始样品』→
被溯源冲突拦下并隔离,不进入事实识别」。

## 这条开口今天真的开着,而且是 batch14-10 自己点的名

`ingest()` 的去重键是 `(product_id, sha256)`,而 `_fill_missing_role()`
允许在去重命中时补一次角色 —— AI 影子行正好是 `role=None` +
`role_source=UNSET`。于是运营把生成好的图下载下来当样品传回去,
命中的是那条 AI 行,而它拿到了 `role_source=HUMAN`。

一条 `source=AI_GENERATED` 的记录带着「人工确认过的正面图」这个标记留在库里,
是**两道门禁的共同前提被同时污染**:§6.2 的完整度门禁问「角色是不是人定的」,
§5.1 的白名单问「它是不是本商品的证据」,而这次污染让第一个问题答错了。
今天第二道还拦得住(`evidence_class` 由 `source` 派生,AI 图进不了
`PRODUCT_EVIDENCE`),所以它没有酿成事故 —— 但**一道门禁靠另一道门禁
碰巧还在,不叫防住了**。

## 作用域必须比去重键宽

去重键是 `(product_id, sha256)`,而 §11 写的是**同 SPU**。两者不是一回事,
而差出来的那一块正是最常见的走法:一个卖三色的款,运营把 A 色生成好的图
下载下来,当样品传给 B 色的 SKU。`product_id` 不同 → 去重不命中 →
新建一条 `source=MANUAL_UPLOAD` 的行 → 它派生成 `PRODUCT_EVIDENCE` →
**直接进识别输入,每张一次真实付费调用**,而且投的票是一张 AI 图看出来的结论。

所以判定要按 `spu + sha256` 找,不按去重键找。这是本模块存在的全部理由:
把「拿什么范围去问」这件事从 `ingest()` 的去重逻辑里分出来,
因为那两个范围**看起来应该一样,而它们不一样**。

## 判据是溯源事实,不是 `source` 一列

一条 AI 行可能因为历史原因、或者因为将来某次数据修复,`source` 被写成了
别的值。所以这里认三个信号,任一命中即算带溯源:

    generation_task_id / generation_candidate_id   §4.8 的溯源列(阶段 2)
    source = AI_GENERATED                           今天唯一被写入的信号
    legacy_kind = generation_candidate              影子写留下的来路

第三个是今天**唯一真的会命中**的那一路 —— 溯源列还没落库,而
`shadow_from_candidate()` 写的 `legacy_kind` 就在那里。少了它,这个模块
今天一条也拦不住,而两侧的测试全绿:那正是 batch14-10 第二节描述的
「靠尚未发生的事没出事」原样重演一遍。

## 为什么是隔离,不是抛异常

§11 写的是「拒绝并提示来源冲突,**落隔离待人工放行**」——两件事都要。
抛异常只做到前一半,而且更糟:影子写和业务写在同一个事务里
(`media/service.py` 顶部那段),抛出去会把这次上传整笔回滚,
于是**那条"待人工放行"的记录也一起没了**,运营手上只剩一句报错。
放行通道(`media.release()`)已经存在,隔离态是它认的输入。

判定不决定「怎么落」,只回答「这次入库和已有的溯源事实冲不冲突」。
落库那一半在 `media/service.py`,与 `evidence_rules` / `sample_completeness`
同一套分工。
"""
from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from app.core.enums import EvidenceClass, MediaSource
from app.media.evidence_rules import derive_evidence_class
from app.media.mapping import LEGACY_CANDIDATE


class Verdict(StrEnum):
    """这次入库的结论。"""

    #: 没有冲突,照常入库
    ACCEPT = "ACCEPT"
    #: 同 SPU 已有一条带 AI 溯源、且字节相同的素材 —— 这次自称原始样品
    SOURCE_CONFLICT = "SOURCE_CONFLICT"


#: 落隔离时写进 `quarantine_reason` 的那句话。**全仓一份。**
#:
#: 它要同时说清三件事:为什么被拦(来源冲突)、拦的依据是什么(同 SPU 有
#: 字节相同的 AI 产出)、以及下一步谁能解开(人工放行)。少掉第三件的
#: 表现是运营在素材页看见一条红的、然后去重新传一次 —— 而重传会撞上
#: 同一条规则,他会认为系统坏了。
QUARANTINE_REASON = (
    "来源冲突:同 SPU 下已有一张字节完全相同的 AI 生成图。"
    "这张图不会进入事实识别;确认它确实是实物样品照后,可在素材页人工放行"
)


def _text(value: Any) -> str:
    """枚举成员 / 字符串 / None 统一成一个可比较的字符串。

    与 `media/evidence_rules._text` 同一份口径 —— 两处对「空值算什么」
    必须一致,否则同一条素材在派生里算「没来源」、在这里算「来源是 'None'」。
    """
    if value is None:
        return ""
    return str(value).strip()


def carries_generation_provenance(asset: Any) -> bool:
    """这条素材行带着「它是本系统生成出来的」这个事实。

    鸭子类型,字段缺了按缺省算 —— 不 import ORM 模型,所以整个输入空间
    能在 `tests/pure/` 被穷举。三个信号见模块文档,任一命中即算。

    **两个溯源列用 getattr 取。**它们是阶段 2 的列,今天不存在;
    写成属性访问会在列落库前 AttributeError,写死 `None` 则会在列落库
    **之后**继续瞎 —— 后者不报错、不留痕。同 `evidence_rules
    .asset_is_extraction_input` 的处理。
    """
    if getattr(asset, "generation_task_id", None) is not None:
        return True
    if getattr(asset, "generation_candidate_id", None) is not None:
        return True
    if _text(getattr(asset, "source", None)) == MediaSource.AI_GENERATED.value:
        return True
    return _text(getattr(asset, "legacy_kind", None)) == LEGACY_CANDIDATE


def claims_product_evidence(
    *,
    source: Any,
    role: Any = None,
    legacy_asset_type: Any = None,
    imported_url_trusted: bool = False,
) -> bool:
    """这次入库自称是「关于本商品的原始证据」。

    判据复用 §5.1 那一个派生,**不重写条件**。重写第二套的后果在这里很具体:
    派生认的来源集合放宽一档时,这道拦截会对新放进来的那一档视而不见,
    而那一档恰恰是刚刚被允许成为识别输入的。

    生成链路自己回写候选图(`source=AI_GENERATED`)派生成
    `GENERATED_RESULT`,所以它**不算**自称原始样品 —— 那是正常的影子写,
    每跑一个生成任务都会发生一次。把它算成冲突的话,每一张候选图落盘
    都会隔离自己。
    """
    return (
        derive_evidence_class(
            source=source,
            role=role,
            legacy_asset_type=legacy_asset_type,
            imported_url_trusted=imported_url_trusted,
        )
        is EvidenceClass.PRODUCT_EVIDENCE
    )


def verdict(
    *,
    source: Any,
    same_digest_in_spu: Iterable[Any],
    role: Any = None,
    legacy_asset_type: Any = None,
    imported_url_trusted: bool = False,
) -> Verdict:
    """这次入库和同 SPU 已有的溯源事实冲不冲突。**AC-22 的落点。**

    `same_digest_in_spu` 是**同 SPU、同 sha256** 的已有素材行(可以为空)。
    取数那一半在 `media/service.py` —— 它必须按 spu 找而不是按去重键找,
    理由见模块文档「作用域必须比去重键宽」。

    两个条件缺一不可:

        这次自称是原始样品      否则是生成链路自己在回写候选图
        已有一条带溯源的同图    否则只是一张普通的重复图

    **顺序上先问「这次是什么」再遍历已有行**:反过来写的话,
    每一次候选图落盘都要把同 SPU 的同图全扫一遍才发现无事发生。
    """
    if not claims_product_evidence(
        source=source,
        role=role,
        legacy_asset_type=legacy_asset_type,
        imported_url_trusted=imported_url_trusted,
    ):
        return Verdict.ACCEPT
    for asset in same_digest_in_spu:
        if carries_generation_provenance(asset):
            return Verdict.SOURCE_CONFLICT
    return Verdict.ACCEPT


def may_fill_role(asset: Any) -> bool:
    """去重命中时,能不能给这条已有素材补一次角色。

    带溯源的行**不能**。`_fill_missing_role()` 的原意是「一条
    `role_source=UNSET` 的素材对下游是不可用的,补上它不会覆盖任何人的决定」
    —— 那句话对人工上传的行成立,对 AI 影子行不成立:AI 行的角色留空
    是一个**决定**(`shadow_from_candidate` 的注释写着「在这里猜一个等于用
    role_source=RULE 掩盖一次没有依据的推断」),不是一个待填的空。

    这一条和 `verdict()` 不重叠,两个都要:

        verdict()      管**新建行**那一路(同 SPU 不同 SKU,去重不命中)
        may_fill_role() 管**去重命中**那一路(同一件商品,不建新行)

    只做前者的话,把图传回原 SKU 仍然会给 AI 行盖上 `role_source=HUMAN`;
    只做后者的话,传给兄弟 SKU 会新建一条干净的 `PRODUCT_EVIDENCE`。
    """
    return not carries_generation_provenance(asset)
