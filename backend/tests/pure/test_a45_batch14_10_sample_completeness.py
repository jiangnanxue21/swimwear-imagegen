"""A45-batch14-10:样品完整度门禁的单点派生(PRD §6.2)。

## 这道门禁今天是对的,但它对得没有道理

`workbench/service.py` 那句 `frozenset(r.role for r in usable if r.role)` 不问
角色是谁标的,也不问那张图是不是本商品的证据。它至今没漏,靠的是两件
**尚未发生**的事:`shadow_from_candidate()` 把候选图的角色留空,以及
M3 的角色识别还没接。

这两件事都写在代码注释里当成**计划**。M3 落地那天,每一张 AI 候选图都会
拿到一个模型给的角色,然后满足完整度 —— 而**没有任何测试会变红**:
那时的代码和现在逐字相同,变的只是库里的数据。

所以这一批的守卫有一个共同任务:**把「今天为什么没出事」写成断言,
而不是让它继续当一条没人知道的运气。**

## 三处穷举

判定是纯函数、输入空间可枚举,那就没有理由挑样本:

    每一个 `RoleSource` 成员         是不是被显式决定过
    每一个 `MediaSource` 成员         同上
    九个角色的**所有子集**            缺组判定与「有没有一张够格样品」等价

第三条是本批唯一一条全称命题(「组内任一角色即满足」),挑样本证明不了它。
"""
from __future__ import annotations

import ast
import itertools
import re

from app.core.enums import MediaSource, MediaStatus, RoleSource
from app.media import sample_completeness as sc
from app.workbench import batch as b
from app.workbench import flow
from app.workbench.flow import (
    AttributeFacts,
    AudienceFacts,
    CopyFacts,
    DraftFacts,
    FlowStep,
    ImageSetFacts,
    IssueLevel,
    MaterialFacts,
    NextActionCode,
    ProductFlow,
    StepState,
)
from tests.pure._helpers import BACKEND_ROOT  # noqa: F401

APP = BACKEND_ROOT / "app"
FRONTEND = BACKEND_ROOT.parent / "frontend" / "src"

#: `MediaRole` 的全部取值。写成字面量而不是 import 枚举,是因为下面那条
#: 穷举要拿它当**角色空间**,而角色空间将来长一个成员时,这条守卫应当
#: 由人来决定新成员进不进满足组 —— 自动跟着枚举走等于没有人做那个决定。
ALL_ROLES: tuple[str, ...] = (
    "PRODUCT_FRONT",
    "PRODUCT_BACK",
    "MODEL_FRONT",
    "MODEL_BACK",
    "DETAIL",
    "SIZE_CHART",
    "FLAT_LAY",
    "PACKAGING",
    "OTHER",
)


class _Asset:
    """假素材行。字段名与判定读的那几个对齐,**不 import ORM 模型**。

    默认值是「一张人工上传、人工标了正面图、状态 READY 的照片」——
    也就是唯一应当无条件通过门禁的那一种。每条用例只改它关心的那一个字段,
    于是「是哪一个条件让它挂了」在用例里是看得见的。
    """

    def __init__(
        self,
        *,
        role: str | None = "PRODUCT_FRONT",
        role_source: str | None = RoleSource.HUMAN.value,
        source: str = MediaSource.MANUAL_UPLOAD.value,
        status: str = MediaStatus.READY.value,
        color_variant_id: str | None = None,
        generation_candidate_id: str | None = None,
    ):
        self.role = role
        self.role_source = role_source
        self.source = source
        self.status = status
        self.color_variant_id = color_variant_id
        self.generation_candidate_id = generation_candidate_id


def _ai_shadow(**kwargs) -> _Asset:
    """`shadow_from_candidate()` 写进库的那一行。

    注意它**不是**一条畸形数据:`source=AI_GENERATED` + `status=READY` 是
    生成链路每跑完一个任务就产出的正常形状。角色留空由 `media/service.py`
    那句注释规定(「留 UNSET,由 M3 的角色识别或人工来定」)。
    """
    base = dict(
        source=MediaSource.AI_GENERATED.value,
        role=None,
        role_source=RoleSource.UNSET.value,
        generation_candidate_id="cand-1",
    )
    base.update(kwargs)
    return _Asset(**base)


REQUIRED = ("primary_color", "garment_type")


def _flow(**kwargs) -> ProductFlow:
    """一件除素材外处处就绪的商品。素材那一层由每条用例自己给。"""
    base = dict(
        product_id="p1",
        sku="SW-001-BLK-S",
        spu="SW-001",
        audience=AudienceFacts(audience="WOMEN"),
        material=MaterialFacts(),
        attribute=AttributeFacts(
            required_fields=REQUIRED, confirmed=frozenset(REQUIRED), extraction_count=1
        ),
        image_set=ImageSetFacts(exists=True, status="APPROVED", version=1, item_count=3),
        copy=CopyFacts(exists=True, status="APPROVED", version=1, locale="en"),
        draft=DraftFacts(exists=True, status="VALIDATED"),
    )
    base.update(kwargs)
    return ProductFlow(**base)


def _material(assets, **extra) -> MaterialFacts:
    """把一组假素材翻译成 `MaterialFacts`,**走的是 service 那条同样的路**。

    翻译写在这里而不是让用例手填 `gate_roles`,是因为手填等于把接线那一步
    重新绕过一次 —— 而接线正是这一批最容易白做的部分。
    """
    usable = [a for a in assets if a.status == MediaStatus.READY.value]
    return MaterialFacts(
        total=len(assets),
        usable=len(usable),
        quarantined=sum(1 for a in assets if a.status == MediaStatus.QUARANTINED.value),
        usable_roles=frozenset(a.role for a in usable if a.role),
        gate_roles=sc.gate_roles(usable),
        confirmable_roles=sc.confirmable_roles(usable),
        **extra,
    )


def _read(path) -> str:
    return path.read_text(encoding="utf-8")


# ==========================================================
# A 组:角色来源白名单(§6.2 本体)
# ==========================================================


def test_a_human_confirmed_front_photo_satisfies_the_gate():
    """基线正例。它挂了说明整条判定坏了,别先去看下面那些。"""
    assert sc.gate_roles([_Asset()]) == frozenset({"PRODUCT_FRONT"})
    assert sc.missing_role_groups(sc.gate_roles([_Asset()])) == ()


def test_a_model_guessed_role_does_not_satisfy_the_gate():
    """**M3 落地那天要拦的就是这一条。**

    今天全仓没有任何一行写着 `role_source=MODEL`,所以这条守卫今天
    「什么也没拦住」。它存在的意义正是如此:等到有写入点时再补,
    要拦的数据已经在库里了。
    """
    guessed = _Asset(role_source=RoleSource.MODEL.value)
    assert sc.gate_roles([guessed]) == frozenset()
    assert sc.missing_role_groups(sc.gate_roles([guessed])) != ()


def test_a_rule_inferred_role_does_not_satisfy_the_gate():
    """规则推断(文件名、上传位)也不算。

    它确实比模型可靠,但它可靠的是**批量导入那一刻**,不是「有人看过这张图
    并且认可它是正面图」。§6.2 给的是白名单不是排除法,门禁问的是后一件事。
    """
    assert sc.gate_roles([_Asset(role_source=RoleSource.RULE.value)]) == frozenset()


def test_the_whitelist_is_a_whitelist_not_an_exclusion_list():
    """将来新增的来源取值**默认不合格**。

    两种写法今天等价(`RULE` / `MODEL` 一个写入点都没有),分歧在将来:
    排除法之下新增任何取值都默认合格,而新增取值时没有人会想起这道门禁。
    这里拿一个**不存在**的取值当探针 —— 它正是「将来新增的那一个」。
    """
    for probe in ("SOME_FUTURE_SOURCE", "IMPORTED", "hUmAn", "human", ""):
        assert not sc.role_source_is_confirmed(probe), (
            f"{probe!r} 被判成已确认 —— 白名单被改成了排除法,"
            "而新增来源取值时没有人会想起这道门禁"
        )


def test_every_role_source_enum_member_is_decided_explicitly():
    """**穷举 `RoleSource` 的四个成员。**

    挑样本的写法漏掉一个成员时不会有任何征兆:那个成员今天没有写入点,
    所以「漏了」和「拦住了」在本机看起来一模一样。
    """
    expected = {
        RoleSource.HUMAN: True,
        RoleSource.RULE: False,
        RoleSource.MODEL: False,
        RoleSource.UNSET: False,
    }
    assert set(expected) == set(RoleSource), (
        f"`RoleSource` 变成了 {sorted(m.value for m in RoleSource)} —— "
        "新成员进不进 §6.2 的白名单要由人来决定,补进上面这张表"
    )
    for member, allowed in expected.items():
        assert sc.role_source_is_confirmed(member.value) is allowed
        assert sc.role_source_is_confirmed(member) is allowed, (
            f"{member} 直接传枚举成员时结论不同 —— ORM 列上是裸字符串,"
            "而调用方两种都可能传进来"
        )


def test_an_empty_role_source_is_not_confirmed():
    """NULL / 空串**不算**已确认。

    开这条缝的代价很具体:存量素材的 `role_source` 大面积是空的,
    放行等于让这道门禁从落地第一天起就只对新数据生效 ——
    而它要拦的恰恰是存量那一批。
    """
    for blank in (None, "", "   "):
        assert not sc.role_source_is_confirmed(blank)
    assert sc.gate_roles([_Asset(role_source=None)]) == frozenset()


def test_confirmed_is_in_the_whitelist_but_not_yet_in_the_enum():
    """**这条守卫钉的是一个凑合状态。**

    §6.2 写 `role_source ∈ {HUMAN, CONFIRMED}`,而 `RoleSource` 只有
    `HUMAN / RULE / MODEL / UNSET`。和 batch14-8 撞上的 `MODEL_REFERENCE`
    是同一种东西:规格里有、枚举里没有。

    白名单里**保留**这个字符串而不是删掉:删掉的话,将来「人工确认模型建议」
    落地时没有人会记得规格写过它,那条路径会被重新发明一次 ——
    而重新发明的那一版多半直接放行 `MODEL`。

    它成为枚举成员那天这条会红。那时该做的是删掉本守卫,不是改白名单。
    """
    assert "CONFIRMED" in sc.CONFIRMED_ROLE_SOURCES, (
        "白名单里的 CONFIRMED 被删掉了 —— 它是 §6.2 的原文,"
        "留着它是为了让「人工确认模型建议」落地时有个能接住的取值"
    )
    assert "CONFIRMED" not in {m.value for m in RoleSource}, (
        "`RoleSource` 长出了 CONFIRMED —— 凑合状态结束了:"
        "确认动作可以落库了,删掉本守卫"
    )
    assert sc.CONFIRMED_ROLE_SOURCES == frozenset({"HUMAN", "CONFIRMED"}), (
        "白名单被改动了。§6.2 只给了这两个取值,多一个就是一条没人决定过的放行"
    )


# ==========================================================
# B 组:证据那一半(复用 §5.1,不重写)
# ==========================================================


def test_an_ai_candidate_never_satisfies_the_gate_even_when_a_human_confirms_its_role():
    """**第二节那条开口的字面复现。**

    `ingest()` 的去重键是 `(product_id, sha256)`,而 `_fill_missing_role()`
    允许在去重命中时补一次角色 —— AI 影子行正好是 `role=None` +
    `role_source=UNSET`。运营把生成好的图下载下来、当样品传给另一个颜色
    (这条流水线上会发生的事),命中的是那条 AI 行,于是一条
    `source=AI_GENERATED` 的记录拿到了 `role_source=HUMAN` 和一个正面图角色。

    这条**不用等 M3**。它今天就能发生。
    """
    filled = _ai_shadow(role="PRODUCT_FRONT", role_source=RoleSource.HUMAN.value)
    assert sc.counts_for_gate(filled) is False
    assert sc.gate_roles([filled]) == frozenset()
    assert sc.missing_role_groups(sc.gate_roles([filled])) != ()

    # **这一行也说明了为什么需要两个条件。** 它的 `role_source` 是 HUMAN,
    # 白名单放它过去 —— 挡住它的是证据那一半。反过来,一张人工上传但角色
    # 由模型猜的图,证据那一半放它过去,挡住它的是白名单。各挡各的一半。
    from app.media.evidence_rules import asset_is_extraction_input

    assert sc.role_source_is_confirmed(filled.role_source) is True
    guessed = _Asset(role_source=RoleSource.MODEL.value)
    assert asset_is_extraction_input(guessed) is True
    assert sc.counts_for_gate(guessed) is False


def test_both_conditions_are_load_bearing_neither_alone_is_enough():
    """2×2 穷举:只有两个条件同时成立才算数。

    删掉任一条件的变异都会在这张表的某一格上撞红,而单看某一行或某一列
    都撞不到 —— 这正是「两个条件」这类修复最常见的坏法:
    改完之后三格是对的,没人发现第四格。
    """
    table = {
        (True, True): True,
        (True, False): False,
        (False, True): False,
        (False, False): False,
    }
    for (is_evidence, is_confirmed), expected in table.items():
        asset = (
            _Asset(role="PRODUCT_FRONT")
            if is_evidence
            else _ai_shadow(role="PRODUCT_FRONT")
        )
        asset.role_source = (
            RoleSource.HUMAN.value if is_confirmed else RoleSource.MODEL.value
        )
        assert sc.counts_for_gate(asset) is expected, (
            f"证据={is_evidence} 已确认={is_confirmed} 时判定是 "
            f"{sc.counts_for_gate(asset)},应当是 {expected}"
        )


def test_a_quarantined_photo_does_not_satisfy_the_gate():
    """被隔离的图不算数 —— 它可能是水印、logo 或露出。

    这一条由 §5.1 白名单的 `status = READY` 承担,本模块不自己再判一次:
    再判一次就是给「这张图能不能用」造第二个事实源,而人工放行会改状态。
    """
    held = _Asset(status=MediaStatus.QUARANTINED.value)
    assert sc.counts_for_gate(held) is False
    assert sc.gate_roles([held]) == frozenset()


def test_every_media_source_is_decided_explicitly():
    """**穷举 `MediaSource` 的五个成员。**

    `PLATFORM_SYNC`(从平台回抓)那一格值得单看:它是渠道现状,不是
    这件商品的原始样品。判定由 §5.1 的证据等级承担,这里只是钉住结论 ——
    因为「回抓图算不算样品」是一个会被人凭直觉改掉的判断。
    """
    expected = {
        MediaSource.MANUAL_UPLOAD: True,
        MediaSource.SUPPLIER_FEED: True,
        MediaSource.IMPORTED_URL: False,   # 要显式声明可信,默认不算
        MediaSource.AI_GENERATED: False,
        MediaSource.PLATFORM_SYNC: False,
    }
    assert set(expected) == set(MediaSource), (
        f"`MediaSource` 变成了 {sorted(m.value for m in MediaSource)} —— "
        "新来源算不算样品要由人来决定,补进上面这张表"
    )
    for member, allowed in expected.items():
        asset = _Asset(source=member.value)
        assert sc.counts_for_gate(asset) is allowed, f"{member} 的结论变了"


def test_confirmable_roles_never_points_at_an_ai_image():
    """「去确认这张图的角色」不能指着一张 AI 图。

    指错的后果不是白跑一趟:运营点完确认,门禁**仍然**过不去 ——
    因为挡住它的是来源不是角色。而界面上没有任何东西会说明这一点,
    于是那件商品变成一个点不动的死结。
    """
    ai = _ai_shadow(role="PRODUCT_FRONT", role_source=RoleSource.MODEL.value)
    assert sc.confirmable_roles([ai]) == ()

    real = _Asset(role="FLAT_LAY", role_source=RoleSource.MODEL.value)
    assert sc.confirmable_roles([ai, real]) == ("FLAT_LAY",)


# ==========================================================
# C 组:满足组(本批**放宽**的那一处)
# ==========================================================


def test_a_flat_lay_alone_satisfies_the_front_photo_requirement():
    """§6.2 原文是「至少一张 PRODUCT_FRONT/FLAT_LAY」,而旧代码只认前者。

    旧枚举 `GARMENT_CUTOUT` 有损映射成 `FLAT_LAY`(`media/mapping.py` 自注),
    于是**只有透明底商品图的商品被永久判「缺少正面图」**。
    """
    assert sc.missing_role_groups(sc.gate_roles([_Asset(role="FLAT_LAY")])) == ()

    # 组内是「或」不是「且」。改成「且」比原来更严:一件只有正面图的正常
    # 商品会被判缺平铺图,而运营会去传一张凑数的平铺图 ——
    # 假阻断换了个方向,坏处一模一样。
    assert sc.missing_role_groups(frozenset({"PRODUCT_FRONT"})) == ()
    assert sc.missing_role_groups(frozenset({"PRODUCT_FRONT", "FLAT_LAY"})) == ()


def test_the_cutout_only_product_is_no_longer_permanently_blocked():
    """同一件事走完整条判定 —— 因为假阻断是在**界面上**发生的。

    旧口径下运营解除它的唯一办法是把平铺图改标成正面图,而被改坏的是
    素材库的角色标注,下游每一处按角色取图的地方跟着错。
    """
    result = flow.evaluate(_flow(material=_material([_Asset(role="FLAT_LAY")])))
    assert result.step_state(FlowStep.MATERIAL) is StepState.DONE
    assert result.blocking_count == 0
    assert result.next_action.code is not NextActionCode.UPLOAD_MATERIAL


def test_missing_groups_is_empty_exactly_when_some_role_matches():
    """**全称命题,所以穷举九个角色的全部 512 个子集。**

    「组内任一角色即满足」说的是**任意**一组角色。挑三组样本证明不了它,
    只能证伪。而这是纯函数、输入空间可枚举,那就没有理由不证到底。
    """
    checked = 0
    for size in range(len(ALL_ROLES) + 1):
        for combo in itertools.combinations(ALL_ROLES, size):
            have = frozenset(combo)
            missing = sc.missing_role_groups(have)
            for group in sc.REQUIRED_ROLE_GROUPS:
                satisfied = bool(set(group) & have)
                assert (group in missing) is not satisfied, (
                    f"角色集合 {sorted(have)} 对组 {group} 的判定不自洽"
                )
            checked += 1
    assert checked == 2 ** len(ALL_ROLES), f"只跑了 {checked} 个子集,穷举失效了"


def test_a_missing_group_names_every_role_in_it():
    """提示语必须说「正面图**或**平铺图」。

    只报第一个角色的后果是本批要修的那个坏循环原样回来:运营为了消掉
    那句话,会把平铺图改标成正面图。`ref` 只带第一个是**跳转依据**,
    不是文案 —— 两者分开正是这条守卫要钉的。
    """
    result = flow.evaluate(_flow(material=_material([_Asset(role="DETAIL")])))
    blocking = [i for i in result.issues if i.code == "MATERIAL_MISSING_REQUIRED_ROLE"]
    assert len(blocking) == 1
    assert "正面图" in blocking[0].message
    assert "平铺图" in blocking[0].message, (
        "缺组提示只报了组里第一个角色 —— 运营会把平铺图改标成正面图来消掉它"
    )
    assert blocking[0].ref == "PRODUCT_FRONT", "`ref` 是跳转依据,只带组里第一个"


def test_the_group_label_reads_as_a_choice_not_a_list():
    """组标签按**传入顺序**拼,不按集合迭代序。

    八个元素是刻意的:两个元素时乱序碰巧等于有序的概率是 1/2,
    这条守卫会一半时间假通过。八个是 1/8!。

    顺序飘的表现没有人会报成 bug:同一件商品刷新两次,提示语里的角色
    先后颠倒 —— 只会让人觉得这个界面有点飘。
    """
    assert sc.group_label(("PRODUCT_FRONT", "FLAT_LAY")) == "正面图或平铺图"
    assert sc.group_label(ALL_ROLES[:8]) == "或".join(
        sc.role_label(r) for r in ALL_ROLES[:8]
    )
    assert sc.role_label("SOME_NEW_ROLE") == "SOME_NEW_ROLE", (
        "认不出的角色写成了「其它」—— 那把「它是谁写进来的」这条线索抹掉了"
    )


# ==========================================================
# D 组:双作用域
# ==========================================================


def test_the_colour_scope_only_counts_photos_of_that_colour():
    """颜色 A 的门禁不认颜色 B 的图。"""
    assets = [
        _Asset(role="PRODUCT_FRONT", color_variant_id="A"),
        _Asset(role="PRODUCT_BACK", color_variant_id="B"),
    ]
    assert sc.variant_gate_roles(assets, "A") == frozenset({"PRODUCT_FRONT"})
    assert sc.variant_gate_roles(assets, "B") == frozenset({"PRODUCT_BACK"})


def test_an_unattributed_photo_proves_nothing_about_any_colour():
    """通用素材只进 SPU 作用域,不进任何颜色子集。

    另一种写法是让它进**每一个**颜色子集 —— 那等于「一张没有归属的图
    证明了所有颜色都有正面照」,而 §6.2 按颜色上传存在的全部理由就是
    不接受这个推论。与 `scope_fingerprint` 的 D1 处理同一条口径。
    """
    generic = [_Asset(role="PRODUCT_FRONT", color_variant_id=None)]
    assert sc.variant_gate_roles(generic, "A") == frozenset()
    assert sc.gate_roles(generic) == frozenset({"PRODUCT_FRONT"})


def test_the_spu_scope_counts_colour_attributed_photos_too():
    """SPU 作用域是**全部**证据素材,含各颜色与通用。

    按颜色筛的表现是全库没有一件商品能过 SPU 门禁,而那种坏法在
    今天的库上完全隐形 —— 今天一条素材都没有 `color_variant_id`。
    """
    assets = [
        _Asset(role="PRODUCT_FRONT", color_variant_id="A"),
        _Asset(role="DETAIL", color_variant_id="B"),
        _Asset(role="PRODUCT_BACK", color_variant_id=None),
    ]
    assert sc.gate_roles(assets) == frozenset({"PRODUCT_FRONT", "DETAIL", "PRODUCT_BACK"})


def test_the_two_scope_words_mean_the_same_thing_as_in_the_fingerprint_module():
    """作用域这个词在两个模块里必须是同一个意思。

    不一致的表现是一件商品「按门禁算 A 色齐了、按指纹算 A 色没变」——
    两句话都对,而合起来说不通。归一化(空串等于未归属)是最容易分叉的
    那一处,所以单独钉住。
    """
    from app.attributes import scope_fingerprint as fp

    for blank in (None, "", "   "):
        asset = _Asset(color_variant_id=blank)
        assert sc._in_variant(asset, "A") is False
        assert fp._text_or_none(blank) is sc._text_or_none(blank), (
            "两个模块对「未归属」的归一化不一致 —— "
            "同一条素材会在门禁里算某个颜色的、在指纹里算通用的"
        )


# ==========================================================
# E 组:确认动线
# ==========================================================


def test_a_model_guessed_front_photo_becomes_a_confirmable_role():
    """模型猜的角色进不了门禁,但它是一条**有下一步**的阻断。

    这正是这条动线存在的理由:确认一个角色是点一下,重新拍一张图是一天。
    """
    guessed = _Asset(role="FLAT_LAY", role_source=RoleSource.MODEL.value)
    assert sc.confirmable_roles([guessed]) == ("FLAT_LAY",)


def test_confirmable_roles_is_empty_once_the_gate_is_satisfied():
    """**这个集合非空就意味着门禁没过。**

    少了这条性质,一件已经有人工正面图的商品会因为多了一张模型猜的平铺图
    而显示「有素材待确认角色」—— 一条没有下一步的待办,
    因为点完确认什么也不会改变。
    """
    assets = [
        _Asset(role="PRODUCT_FRONT", role_source=RoleSource.HUMAN.value),
        _Asset(role="FLAT_LAY", role_source=RoleSource.MODEL.value),
    ]
    assert sc.missing_role_groups(sc.gate_roles(assets)) == ()
    assert sc.confirmable_roles(assets) == ()


def test_the_unconfirmed_role_issue_is_needs_confirm_not_a_reminder():
    """级别不是三种颜色,是三种含义。

    降成 REMINDER 之后行为几乎不变:步骤照样 BLOCKED、下一步照样是
    「确认素材角色」,既有断言全部点头。变的是**可见度** —— 而 REMINDER
    的语义是「可以带着它继续走」,对一件走不下去的商品说这句话本身就是错的。
    """
    guessed = _Asset(role="FLAT_LAY", role_source=RoleSource.MODEL.value)
    result = flow.evaluate(_flow(material=_material([guessed])))
    issue = next(i for i in result.issues if i.code == "MATERIAL_ROLE_UNCONFIRMED")
    assert issue.level is IssueLevel.NEEDS_CONFIRM, (
        "降成 REMINDER 的话前端 `ISSUE_LEVEL_LABEL` 给它 `badge: false`,"
        "列表页根本不计数 —— 唯一能解开这条阻断的事情在看板上一处都不显示"
    )
    assert issue.target_step is FlowStep.MATERIAL

    # 级别对而**计数不动**是可能的(`summarize()` 只数 NEEDS_CONFIRM),
    # 所以两件事都断言 —— 运营看的是那个数字,不是级别字段。
    assert result.pending_count >= 1
    assert result.step_state(FlowStep.MATERIAL) is StepState.BLOCKED
    assert result.blocking_count >= 1, "缺图那条阻断必须还在 —— 确认之前走不下去"


def test_confirming_a_role_is_offered_before_shooting_a_new_photo():
    """有可确认角色时,唯一下一步是「确认素材角色」而不是「补充素材」。

    推荐补图的后果是运营真的去传了一张新图,**而新图默认同样没确认**,
    于是他再撞一次同一堵墙 —— 这一次他会认为系统坏了。
    """
    guessed = _Asset(role="FLAT_LAY", role_source=RoleSource.MODEL.value)
    result = flow.evaluate(_flow(material=_material([guessed])))
    assert result.next_action.code is NextActionCode.CONFIRM_ASSET_ROLE
    assert result.next_action.step is FlowStep.MATERIAL
    assert "平铺图" in result.next_action.reason

    # 没有可确认角色时才回到补图
    empty = flow.evaluate(_flow(material=_material([])))
    assert empty.next_action.code is NextActionCode.UPLOAD_MATERIAL


def test_confirmable_roles_come_out_sorted():
    """**八个元素。** 理由与 `test_the_group_label_reads_as_a_choice` 同一条。

    集合迭代序由字符串哈希决定,而字符串哈希每个进程都不一样。
    表现是同一件商品刷新两次、提示语里的角色先后颠倒,`ref`(前端跳转依据)
    跟着换一个。谁都不会把这件事报成 bug。

    此前每条用例都只有一个可确认角色,一条都撞不到 —— 和 batch14-9 的 H1
    是同一个教训:**构造了一对输入,不等于构造了一对会分叉的输入。**
    """
    roles = ALL_ROLES[:8]
    original = sc.REQUIRED_ROLE_GROUPS
    try:
        sc.REQUIRED_ROLE_GROUPS = (roles,)
        assets = [
            _Asset(role=r, role_source=RoleSource.MODEL.value) for r in reversed(roles)
        ]
        assert sc.confirmable_roles(assets) == tuple(sorted(roles))
    finally:
        sc.REQUIRED_ROLE_GROUPS = original


def test_the_gate_column_has_no_lenient_default():
    """`gate_roles` 空集时**不回落**到 `usable_roles`。

    给新字段一个宽松默认值,漏填的表现是悄悄放行 —— 硬规则 4 第二次事故
    的形状正是如此:缺一列不报错,判定一路走到最宽的一档,而两侧的测试全绿。

    代价是接线那天所有旧夹具当场变红。那是设计在起作用,不是回归。
    """
    assert MaterialFacts().gate_roles == frozenset()
    assert MaterialFacts().confirmable_roles == ()

    # 库里有一张正面图,但它没过门禁:判定必须按**没过**算
    facts = MaterialFacts(
        total=1, usable=1, usable_roles=frozenset({"PRODUCT_FRONT"}), gate_roles=frozenset()
    )
    result = flow.evaluate(_flow(material=facts))
    assert result.step_state(FlowStep.MATERIAL) is StepState.BLOCKED, (
        "`gate_roles` 空集时回落到了 `usable_roles` —— 漏填一列会悄悄放行"
    )


# ==========================================================
# F 组:接线与联动
# ==========================================================


def test_material_facts_hands_the_rows_to_the_gate():
    """**判定验得再干净,和它有没有被调用完全无关。**

    不接线的话本套件照样 37/37 全绿,而 `_material_facts` 仍然用那句
    「有 role 就算数」判定完整度。用 AST 读源码而不是 import:
    `workbench/service.py` 顶层 import 了 sqlalchemy,这批用例刻意零三方依赖。
    """
    tree = ast.parse(_read(APP / "workbench" / "service.py"))
    func = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_material_facts"
    )
    wired = {
        kw.arg: ast.unparse(kw.value)
        for call in ast.walk(func)
        if isinstance(call, ast.Call)
        for kw in call.keywords
        if kw.arg in ("gate_roles", "confirmable_roles")
    }
    assert wired.get("gate_roles") == "sample_completeness.gate_roles(usable)", (
        f"`gate_roles` 现在是 {wired.get('gate_roles')!r} —— "
        "门禁必须是那个判定的裸调用,不许换成别的表达式"
    )
    assert wired.get("confirmable_roles") == "sample_completeness.confirmable_roles(usable)"
    assert "usable" in wired.get("gate_roles", ""), (
        "递进去的不是 `usable` —— 传 `rows` 的话,白名单将来放宽状态条件时"
        "被隔离的素材会满足完整度门禁"
    )


def test_confirm_asset_role_has_its_own_exception_category():
    """不并进 `NO_USABLE_MATERIAL`。

    那个码的建议是「补图或放行隔离素材」,而这条动线上两个动作都是错的:
    补进来的新图默认同样没确认,而被隔离的可能是 0 条。
    分档理由与 `AUDIENCE_UNCONFIRMED` 当初一字不差。
    """
    assert (
        b.PRECHECK_CODE_BY_ACTION[NextActionCode.CONFIRM_ASSET_ROLE]
        is b.BatchErrorCode.ASSET_ROLE_UNCONFIRMED
    )
    meta = b.ERROR_REGISTRY[b.BatchErrorCode.ASSET_ROLE_UNCONFIRMED]
    assert meta.category is FlowStep.MATERIAL
    assert meta.needs_human is True
    assert meta.retryable is False, "重试一百次结果相同 —— 要人去点确认"
    assert "确认" in meta.advice
    other = b.ERROR_REGISTRY[b.BatchErrorCode.NO_USABLE_MATERIAL].advice
    assert meta.advice != other, "两个码的建议一样,那就没有分开的意义"

    for code in NextActionCode:
        assert code in b.PRECHECK_CODE_BY_ACTION, f"{code} 没有对应的异常类别"


def test_the_next_step_reason_repeats_the_issue_text_verbatim():
    """按钮理由**直接取 issue 文案,不从 `ref` 重拼**。

    这条是写码过程中被自己的守卫咬红那一次:`ref` 按设计只带组里第一个角色,
    从它重拼的结果是判定说「正面图**或**平铺图」、按钮理由说「缺少正面图」——
    本批要修的那个坏循环原样回来。
    """
    result = flow.evaluate(_flow(material=_material([_Asset(role="DETAIL")])))
    issue = next(i for i in result.issues if i.code == "MATERIAL_MISSING_REQUIRED_ROLE")
    assert issue.message in result.next_action.reason, (
        f"按钮说「{result.next_action.reason}」,判定说「{issue.message}」—— "
        "两句话不一样时,运营会去迁就其中较严的那一句"
    )
    assert "平铺图" in result.next_action.reason


def test_the_new_action_code_is_mirrored_in_every_frontend_table():
    """四处镜像。**漏掉最后一处的表现是首页一处都不显示。**

    卡在这一步的商品于是对运营是隐形的,他看完首页的结论是「今天没事干」。
    通用契约测试已经覆盖前三张表,这里点名钉住第四处 ——
    「其余待办」那一行不属于任何一张枚举表,通用覆盖看不见它。
    """
    workbench_ts = _read(FRONTEND / "api" / "workbench.ts")
    batch_ts = _read(FRONTEND / "api" / "batch.ts")
    today = _read(FRONTEND / "pages" / "TodayPage.tsx")

    assert "'CONFIRM_ASSET_ROLE'" in workbench_ts, "类型联合里没有这个码"
    assert re.search(r"CONFIRM_ASSET_ROLE:\s*'确认素材角色'", workbench_ts)
    assert re.search(r"CONFIRM_ASSET_ROLE:\s*'material'", workbench_ts)
    assert re.search(r"ASSET_ROLE_UNCONFIRMED:\s*'[^']+'", batch_ts)

    block = re.search(r"const SECONDARY_ACTIONS:[^=]*=\s*\[(.*?)\]", today, re.S)
    assert block, "找不到 SECONDARY_ACTIONS"
    assert "CONFIRM_ASSET_ROLE" in block.group(1), (
        "「其余待办」漏了这个码 —— 卡在这一步的商品在首页一处都不显示"
    )


def test_the_action_code_does_not_reuse_someone_elses_words():
    """**说错话比不说话更难查。**

    复用 `UPLOAD_MATERIAL` 的话按钮会说「补充素材」,而运营不需要补任何素材;
    复用 `RELEASE_QUARANTINE` 的话文案会说「N 条素材被隔离」,而 N 可能是 0。
    """
    label = flow.ACTION_LABELS[NextActionCode.CONFIRM_ASSET_ROLE]
    assert label == "确认素材角色"
    assert label != flow.ACTION_LABELS[NextActionCode.UPLOAD_MATERIAL]
    assert label != flow.ACTION_LABELS[NextActionCode.RELEASE_QUARANTINE]
    assert flow.ACTION_STEP[NextActionCode.CONFIRM_ASSET_ROLE] is FlowStep.MATERIAL

    guessed = _Asset(role="FLAT_LAY", role_source=RoleSource.MODEL.value)
    reason = flow.evaluate(_flow(material=_material([guessed]))).next_action.reason
    assert "隔离" not in reason, "这条动线上被隔离的可能是 0 条"


# ==========================================================
# G 组:欠账与凑合状态
# ==========================================================


def test_the_variant_scope_is_wired_now_that_the_attribution_column_exists():
    """**这条守卫原来记的是欠账,A45-batch14-15 把它收了。**

    原文:「颜色子集要按 `color_variant_id` 分,而那一列是阶段 2 的归属外键,
    今天不存在。拿 `variant_hint` 分子集等于让模型猜出来的值决定运营能不能
    往下走,而 §6.2 收紧 role 口径要禁的正是这件事。」

    那一列(迁移 0037)落库之后,这条改成钉**接线本身**:

        列还在                      —— 它没了,下面两条就该一起红
        `_material_facts` 真的按颜色分了桶
        分桶读的是归属外键,不是 hint  —— 这条最要紧,它是当初拒绝接线的理由

    第三条为什么单独钉:两列长得像,而拿 hint 顶上不会报错、不会变红,
    只会让一个模型猜出来的颜色决定「这件商品能不能往下走」。
    """
    model = _read(APP / "models" / "media_asset.py")
    block = model[model.index("class MediaAsset") : model.index("class MediaDerivative")]
    columns = set(re.findall(r"^    (\w+): Mapped", block, re.M))
    assert {"role", "role_source", "status", "source"} <= columns
    assert "color_variant_id" in columns, (
        "归属外键没了 —— 颜色作用域的接线会退回一个恒空的 dict,"
        "而每个颜色的完整度会静静地变成「合格」"
    )

    facts = _read(APP / "workbench" / "service.py")
    tree = ast.parse(facts)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_material_facts"
    )
    calls = {
        n.func.attr
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "variant_gate_roles" in calls, "颜色作用域算好了没人调"
    attrs = {
        n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)
    } | {
        c.value for c in ast.walk(fn)
        if isinstance(c, ast.Constant) and isinstance(c.value, str)
    }
    assert "color_variant_id" in attrs, "分桶读的不是归属外键"
    assert "variant_hint" not in attrs, (
        "分桶读了 `variant_hint` —— 那是模型猜出来的值,而它现在决定着"
        "一件商品能不能往下走。§4.8 明写 hint 是识别建议位"
    )


def test_the_role_label_table_lives_in_exactly_one_place():
    """全仓只有一份角色文案表。

    两份的表现是改了一处之后判定说「平铺图」、按钮说 `FLAT_LAY`,
    而两处都不会报错。这张表从 `workbench/flow.py` 搬到素材层,
    因为门禁的提示语要拼「正面图或平铺图」,而角色本身是素材层的概念。
    """
    hits = [
        path.relative_to(APP).as_posix()
        for path in APP.rglob("*.py")
        if '"PRODUCT_FRONT": "正面图"' in _read(path)
    ]
    assert hits == ["media/sample_completeness.py"], (
        f"角色文案表出现在 {hits} —— 全仓只能有一份"
    )
    assert flow.ROLE_LABELS is sc.ROLE_LABELS, (
        "`flow.ROLE_LABELS` 不再是同一个对象 —— 有人又抄了一份回去"
    )


def test_the_required_roles_constant_is_gone_from_the_flow_layer():
    """`flow.REQUIRED_ROLES` 必须消失,不能留一个没人读的副本。

    留着它的坏处不是死代码:下一个人改必备角色时会改那一个 ——
    它就在素材步旁边,而真正生效的表在另一个模块里。改完没有任何反馈。
    """
    assert not hasattr(flow, "REQUIRED_ROLES"), (
        "`flow.REQUIRED_ROLES` 还在。判定已经搬到 `sample_completeness`,"
        "留一个同名常量在这里,下一个人会改错那一份"
    )
    assert flow.RECOMMENDED_ROLES == ("PRODUCT_BACK", "DETAIL"), (
        "推荐角色**不进门禁**,它们读的是 `usable_roles`。改动这一行前"
        "先读 `MaterialFacts` 上那段说明"
    )


def test_recommended_roles_still_read_the_loose_set_not_the_gate():
    """提醒类问题读 `usable_roles`,**不读 `gate_roles`**。

    读门禁集合的表现是:一件有背面图、但那个角色是模型猜的商品,
    会显示「没有背面图」—— 那是假的。而假提醒会训练人忽略这一列,
    然后真正缺图的那一件也没人看了。
    """
    facts = MaterialFacts(
        total=2,
        usable=2,
        usable_roles=frozenset({"PRODUCT_FRONT", "PRODUCT_BACK", "DETAIL"}),
        gate_roles=frozenset({"PRODUCT_FRONT"}),
    )
    result = flow.evaluate(_flow(material=facts))
    reminders = {
        i.ref for i in result.issues if i.code == "MATERIAL_MISSING_RECOMMENDED_ROLE"
    }
    assert reminders == set(), (
        f"库里有背面图与细节图,却提醒缺 {sorted(reminders)} —— "
        "提醒读错了集合,读成了门禁那一份"
    )


def test_the_whitelist_blocks_nothing_today_and_that_is_the_point():
    """**这条收紧今天不改变任何一件商品的判定。**

    全仓只有两处写 `role_source`:`shadow_from_product_asset` 写 `HUMAN`、
    `shadow_from_candidate` 写 `UNSET` 且角色为空。`RULE` 与 `MODEL`
    一个写入点都没有。

    把它当成「反正没影响」而省掉,等于把落地时机排在 M3 之后 ——
    而那时要拦的数据已经在库里了。这条守卫钉住那个前提:
    出现第三个写入点时它会红,那时要有人回来看一眼写的是哪个取值。
    """
    source = _read(APP / "media" / "service.py")
    written = set(re.findall(r"role_source=RoleSource\.(\w+)", source))
    assert written == {"HUMAN", "UNSET"}, (
        f"`media/service.py` 现在写 {sorted(written)} 到 role_source。"
        "多出来的那个取值进不进 §6.2 的白名单,要由人来决定"
    )
    assert "由 M3 的角色识别或人工来定" in source, (
        "那句注释没了 —— 它是「AI 候选图角色留空」这条前提的唯一书面依据,"
        "而本批第二节整段推理都架在它上面"
    )
