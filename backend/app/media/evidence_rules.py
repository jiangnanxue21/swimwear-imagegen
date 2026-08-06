"""一张图算不算「关于本商品的证据」。**纯判定,零三方依赖。**

PRD §4.8 的 `evidence_class` 派生规则与 §5.1 的抽取输入白名单,判定那一半。

## 为什么单独一个模块

`evidence_class` 是 PRD 风险表 D3 点名的那一条:**设计成独立赋值的存储列时,
它会和 `source` / 溯源列漂移,而一张 AI 图被误标成 `PRODUCT_EVIDENCE`
就穿透了整个 §5.1 白名单。**对策是「派生 + 单写入点 + 库级 CHECK」三件套,
而这个模块是其中的第一件:它是唯一算得出 `evidence_class` 的地方。

判定留在这里而不是长在 `media/service.py` 里,理由和
`extractors/evidence_plan.py` 顶部那条一模一样:这里零依赖,所以整个输入空间
能在 `tests/pure/` 被**穷举**;搬进服务层之后,覆盖「AI 图会不会漏进识别输入」
就要起一个 PostgreSQL,而那种测试没人会为一个分支去写 —— 于是这条正在
真实烧钱的规则,分支覆盖会退化成零。

服务层只负责把这里算出来的值**物化**成列,并由库级 CHECK 兜底。
两件事分开的好处在 batch13-2 的 F1 上有过教训:判定和落库混在一起时,
「决定只落了一半」不会被任何守卫看见。

## 派生规则(PRD §4.8 原文,与本模块的差异见下)

    generation_task_id IS NOT NULL 或 source = AI_GENERATED → GENERATED_RESULT
    role = MODEL_REFERENCE                                  → REFERENCE_ONLY
    source = PLATFORM_SYNC                                  → CHANNEL_DERIVATIVE
    source ∈ {MANUAL_UPLOAD, SUPPLIER_FEED, 可信 IMPORTED_URL} 且以上皆非
                                                            → PRODUCT_EVIDENCE

## 落码时和 PRD 原文对不上的五处

**一、`role = MODEL_REFERENCE` 按字面写是一条死分支。**
`MODEL_REFERENCE` 不是 `MediaRole` 的取值,是**旧** `AssetType` 的取值;
`MediaRole` 里根本没有这个成员。而 `media/mapping.py` 的
`LEGACY_ASSET_TYPE_TO_ROLE` 把 `AssetType.MODEL_REFERENCE` 映射成
`MediaRole.MODEL_FRONT` —— 那个映射自己的注释就写着它是**有损**的。

后果是:按字面写,这条永不触发;改成认 `MODEL_FRONT`,则每一张
「模特穿着本商品」的正经图都会被降成 `REFERENCE_ONLY`,而那恰恰是识别
最想要的那类图。两条路都是错的,所以这里认的是一个**标记集合**
(`MODEL_REFERENCE_MARKERS`),同时喂 `role` 与 `legacy_asset_type` 两个入口。

今天真正让这条规则生效的是 `legacy_asset_type` 那一路 —— 也就是说,
**一张模特参考图和识别输入之间,现在只隔着调用方记不记得传这个参数。**
`shadow_from_product_asset()` 手里有 `asset.asset_type`,传得出来;
新建路径(`ingest()`)手里没有,因为新模型里这个概念还没有落点。
补 `MediaRole.MODEL_REFERENCE` 之后 `role` 那一路才会活过来,
在那之前 `tests/pure` 有一条守卫钉着这个状态,别让它无声退化。

**二、「可信 IMPORTED_URL」在本仓没有定义。**
全仓没有任何可信域名注册表 / 白名单。把「可信」实现成「凡是 IMPORTED_URL
都算」,等于谁贴一条外链谁就产出一条商品证据 —— 那是把 §5.1 的口子换个
地方重新开一遍。所以这里要求调用方**显式**声明可信,默认 `False`,
与 `MediaConsent` 那套 fail-closed 的姿态一致。

**三、PRD 的规则表没有 else 分支。**
不可信的 `IMPORTED_URL`、以及任何认不出来的 source(空值、历史脏数据、
将来新增但这里还没跟上的枚举值),在原文里落不到任何一行。这里补上
兜底:一律 `REFERENCE_ONLY`。选它不是因为语义最贴切 —— 它本意是
「模特参考图」那类 —— 而是因为四个取值里只有它表示「可以看,但不算证据」,
而认不出来的东西必须落在不算证据的那一边。这处语义合并是**明知故犯**,
写在这里以免下一个人把它当成 bug 顺手"修好"。

**四、`generation_candidate_id` 在 PRD 的 CHECK 里漏了。**
CHECK 原文只点了 `generation_task_id`。但一条只有 `generation_candidate_id`
的记录同样是 AI 产物,而它能合法地通过那条 CHECK 成为 `PRODUCT_EVIDENCE`。
这里两个溯源列都认;落库时那条 CHECK 也应当两个都写。

**五、规则表漏了 `BACKGROUND_REFERENCE`。**
它和 `MODEL_REFERENCE` 是同一类东西 —— 生成链路的**输入参考**,
不是关于这件泳衣的证据。但 PRD 只点了模特参考图,而旧枚举映射把背景参考图
落成 `MediaRole.OTHER`,`OTHER` 在来源判定里畅通无阻。于是一张背景布的照片
会成为商品证据,识别从它身上读出的「主色」会去和真图的证据投票。

这一条不是读 PRD 读出来的,是 `tests/pure` 那条**穷举**旧枚举的守卫撞出来的
(挑样本的写法不会撞到它 —— 没人会专门挑背景参考图来验)。

## 两个**刻意不作为**输入的东西

**`role_source`(人工确认状态)。**它只可能影响「降级」这一侧 —— 升进
`PRODUCT_EVIDENCE` 只看 source,不看 role。而降级要不要等人工确认,
答案是不等:让「模型觉得这是模特参考图」也能降级,代价是少几张证据图;
反过来要求人工确认才降级,代价是**在人来点确认之前,每一轮识别都照付**。
§5.1 存在的全部理由就是消灭那笔开销,所以这一侧必须 fail closed。

**`status`。**「这张图能不能用」由白名单里的 `status = READY` 回答,
不由 `evidence_class` 回答。把状态编进证据等级,等于给「能不能用」
造出第二个事实源 —— 而人工隔离/放行会改状态,却不该改证据等级:
一张被隔离的商品正面图,放行之后仍然是商品正面图。
"""
from __future__ import annotations

from typing import Any

from app.core.enums import AssetType, EvidenceClass, MediaSource

#: 认得出「这张图讲的不是本商品的事」的标记。
#:
#: 同时比对 `role` 与 `legacy_asset_type` 两个入口 —— 原因见模块文档第一条。
#: 这里存字符串而不是枚举成员:`MediaRole` **没有**这两个成员,而
#: `AssetType` 有,写成枚举就只能写一半。
#:
#: `BACKGROUND_REFERENCE` 是 PRD 规则表漏掉的第二个(见模块文档第五条):
#: 它和 `MODEL_REFERENCE` 是同一类东西 —— 生成链路的**输入参考**,
#: 不是关于这件泳衣的证据。旧枚举把它映射成 `MediaRole.OTHER`,
#: 而 `OTHER` 在来源判定里畅通无阻。
REFERENCE_ONLY_MARKERS: frozenset[str] = frozenset(
    {AssetType.MODEL_REFERENCE.value, AssetType.BACKGROUND_REFERENCE.value}
)

#: 本身就构成商品证据的来源。
#:
#: `IMPORTED_URL` **不在这里** —— 它要额外的 `imported_url_trusted=True`,
#: 见模块文档第二条。
EVIDENCE_SOURCES: frozenset[str] = frozenset(
    {MediaSource.MANUAL_UPLOAD.value, MediaSource.SUPPLIER_FEED.value}
)


def _text(value: Any) -> str:
    """把枚举成员 / 字符串 / None 统一成一个可比较的字符串。

    `MediaSource` 是 `StrEnum`,所以 `str()` 拿到的就是取值本身;
    但调用方也可能直接传 ORM 列上的裸字符串。两种都要认。
    """
    if value is None:
        return ""
    return str(value).strip()


def _is_reference_material(role: Any, legacy_asset_type: Any) -> bool:
    """这张图是不是「参考素材」那一类:模特参考图、背景参考图。

    两个入口任一命中即可。今天只有 `legacy_asset_type` 那一路真的会命中 ——
    见模块文档第一条,以及 `tests/pure` 里钉住这个状态的那条守卫。
    """
    return (
        _text(role) in REFERENCE_ONLY_MARKERS
        or _text(legacy_asset_type) in REFERENCE_ONLY_MARKERS
    )


def derive_evidence_class(
    *,
    source: Any,
    generation_task_id: Any = None,
    generation_candidate_id: Any = None,
    role: Any = None,
    legacy_asset_type: Any = None,
    imported_url_trusted: bool = False,
) -> EvidenceClass:
    """算出这条素材的证据等级。**这是唯一的写入点。**

    路由层、脚本、迁移一律不许直接给 `evidence_class` 赋值 ——
    `tests/pure` 有一条 AST 守卫按投影列那套模式钉着这件事。

    ## 四条规则的判定顺序,以及为什么顺序不能换

    1. **溯源与 AI 来源先于一切。** 这一条同时是库级 CHECK 的不变式
       (`AI 来源 → evidence_class <> PRODUCT_EVIDENCE`),所以它必须排第一:
       排在后面的话,一张 `role=PRODUCT_FRONT` 的 AI 图会先被第四条捞成
       `PRODUCT_EVIDENCE`,而那正是 D3 描述的那次穿透。
    2. **参考素材先于来源。** 一张人工上传的模特参考图,`source` 是
       `MANUAL_UPLOAD`,第四条会把它捞成商品证据 —— 然后识别会从
       一张「模特长什么样」的图上读出这件泳衣的领型。背景参考图同理,
       只是读出来的会是背景布的颜色。
    3. **平台回抓先于来源判定。** `PLATFORM_SYNC` 不在 `EVIDENCE_SOURCES` 里,
       所以这一条即使删掉,结果也只是从 `CHANNEL_DERIVATIVE` 变成兜底的
       `REFERENCE_ONLY` —— 两者都进不了识别输入,**变异跑起来不会变红**。
       它存在的意义是让「渠道现状」和「来路不明」在素材库里可分辨,
       这是人看的,不是白名单看的。
    4. 剩下的按来源判。认不出来的一律兜底,见模块文档第三条。
    """
    src = _text(source)

    if generation_task_id is not None or generation_candidate_id is not None:
        return EvidenceClass.GENERATED_RESULT
    if src == MediaSource.AI_GENERATED.value:
        return EvidenceClass.GENERATED_RESULT

    if _is_reference_material(role, legacy_asset_type):
        return EvidenceClass.REFERENCE_ONLY

    if src == MediaSource.PLATFORM_SYNC.value:
        return EvidenceClass.CHANNEL_DERIVATIVE

    if src in EVIDENCE_SOURCES:
        return EvidenceClass.PRODUCT_EVIDENCE
    if src == MediaSource.IMPORTED_URL.value and imported_url_trusted:
        return EvidenceClass.PRODUCT_EVIDENCE

    return EvidenceClass.REFERENCE_ONLY


def violates_ai_check(
    *,
    evidence_class: Any,
    source: Any,
    generation_task_id: Any = None,
    generation_candidate_id: Any = None,
) -> bool:
    """库级 CHECK 的 Python 孪生:AI 来源的图不许是 `PRODUCT_EVIDENCE`。

    ## 为什么要在 Python 里再写一遍

    CHECK 只在有 PostgreSQL 的地方拦得住。这个函数让**同一条不变式**能在
    纯层被穷举 —— `tests/pure` 拿 `derive_evidence_class` 的整个输入空间
    喂给它,证明派生**永远**产不出被 CHECK 拒绝的组合。

    这不是重复:一个是运行时防线(拦住绕过派生函数的直写),
    一个是设计时证明(派生函数自己不会产出违规值)。
    少了后者,「派生 + CHECK」这套组合只能在真库上验,而真库用例池里
    已经压着 60 条没跑过的了 —— 再往里加一条,等于把这条规则的验证
    推给下一台机器。

    落库时那条 CHECK 要**两个溯源列都写**,不能只写 `generation_task_id`
    (PRD 原文只点了一个,见模块文档第四条)。
    """
    if _text(evidence_class) != EvidenceClass.PRODUCT_EVIDENCE.value:
        return False
    return (
        generation_task_id is not None
        or generation_candidate_id is not None
        or _text(source) == MediaSource.AI_GENERATED.value
    )


def is_extraction_input(*, evidence_class: Any, status_is_ready: bool) -> bool:
    """这条素材能不能进属性识别的输入。§5.1 白名单的**判定那一半**。

    白名单原文有四个条件,这里只落两个,另外两个刻意不在:

        status = READY                      → `status_is_ready` 参数
        evidence_class = PRODUCT_EVIDENCE   → 这里
        source IN (...)                     → 已经被派生规则吃掉了。再查一遍
                                              等于给 source 造第二个判定点,
                                              而两个判定点漂移时没人会发现
        generation_task_id IS NULL          → 同上:溯源列非空必然派生成
                                              GENERATED_RESULT,`violates_ai_check`
                                              在纯层穷举证明了这一点

    取数那一半(`media.evidence_assets_for(spu_id, scope)` 的 SQL)留给
    有真库的机器。**判定已经在这里验过了**,那边只是把它翻译成 WHERE 子句。
    """
    return status_is_ready and _text(evidence_class) == EvidenceClass.PRODUCT_EVIDENCE.value


def asset_is_extraction_input(asset: Any, *, imported_url_trusted: bool = False) -> bool:
    """一条素材行能不能进识别输入。**§5.1 白名单的完整判定。**

    鸭子类型:只要求 `source`,其余字段缺了按缺省算 —— 不 import ORM 模型,
    所以这个函数在没有 sqlalchemy 的机器上能拿假对象穷举。

    ## 为什么这层「翻译」也要在纯层

    把 `derive_evidence_class(...)` 那一串入参拼装留在 `run_extraction` 里,
    结果是判定可穷举、而**喂给判定的东西不可穷举** —— 少传一个入参、
    传错一个字段,派生函数依然人畜无害地返回一个合法值,而守卫只能靠
    AST 看「调用有没有出现」。那种守卫抓不到「算出来了但没用」和
    「条件写反了」这两类,而它们恰恰是接线最常见的坏法。

    ## 两个溯源列用 getattr 取

    `generation_task_id` / `generation_candidate_id` **已经是真列了**
    (A45-batch14-16 / 迁移 0038)。这里保留 `getattr` 不是因为列可能缺,
    是因为本函数按鸭子类型接任何"素材行"——纯层的假对象、未来的投影对象
    都要能进来,而属性访问会让它们在缺列时 AttributeError。

    原来这里写着「是阶段 2 的列,今天不存在」。那句话在 14-16 之后过期了,
    A45-batch14-19 改掉 —— **一句过期的注释会让下一个人以为那条路还没通**,
    于是他不会去用它(粗筛用的正是这两列)。

    ## `legacy_asset_type` 取不到

    它在旧 `product_assets` 行上,要 join 才拿得到。于是「模特参考图 /
    背景参考图」那一路在这个入口还没生效,那半条随归属外键一起收。
    这里显式传 None 而不是假装它不存在。
    """
    return is_extraction_input(
        evidence_class=derive_evidence_class(
            source=getattr(asset, "source", None),
            generation_task_id=getattr(asset, "generation_task_id", None),
            generation_candidate_id=getattr(asset, "generation_candidate_id", None),
            role=getattr(asset, "role", None),
            legacy_asset_type=None,
            imported_url_trusted=imported_url_trusted,
        ),
        status_is_ready=_text(getattr(asset, "status", None)) == "READY",
    )
