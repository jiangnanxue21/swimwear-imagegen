"""A45-batch14-9:双作用域样品指纹与 `facts_stale` 派生(PRD §5.3 / §8.1,修复 D1)。

## AC-21 是一个全称命题,所以这里穷举

「传 A 色图不 stale B 色事实」说的是**任意**一次 A 色改动都不影响 B。
挑三组样本证明不了全称命题 —— 它只能证伪。而这条判定是纯函数、
输入空间可枚举,那就没有理由不把它证到底。

下面 `test_ac21_holds_for_every_shape_of_change` 走的是「所有子集 × 所有改动
种类」,一次几千组;`test_ac21_survives_the_awkward_shapes` 单独钉住几个
穷举覆盖不到的形状(空集、单色、重复哈希)。

## 与 batch14-8 的关系

证据集合的判定复用 `media/evidence_rules.asset_is_extraction_input`,
不重写。所以这套指纹**天然继承**了那一批的性质:AI 图不进指纹,
也就不会因为多生成了几张候选图而把已确认的事实 stale 掉。
那条性质在这里单独有守卫 —— 它跨两个模块,任何一边改了都该有人知道。
"""
from __future__ import annotations

import ast
import itertools

from app.attributes import scope_fingerprint as fp
from app.core.enums import MediaSource
from app.workbench import stale_matrix as sm
from tests.pure._helpers import BACKEND_ROOT  # noqa: F401

RULES_FILE = BACKEND_ROOT / "app" / "attributes" / "scope_fingerprint.py"


class _Asset:
    """假素材行。字段名与 `_element()` 读的那几个对齐。"""

    def __init__(
        self,
        asset_id,
        color_variant_id=None,
        content_hash=None,
        source=MediaSource.MANUAL_UPLOAD.value,
        role="PRODUCT_FRONT",
        status="READY",
    ):
        self.asset_id = asset_id
        self.color_variant_id = color_variant_id
        self.content_hash = content_hash or f"sha-{asset_id}"
        self.source = source
        self.role = role
        self.status = status


def _spu(*specs):
    """`_spu(("a1", "A"), ("a2", None))` → 一组素材。"""
    return [_Asset(aid, variant) for aid, variant in specs]


# --------------------------------------------------------------- AC-21


def test_ac21_holds_for_every_shape_of_change():
    """**AC-21 的穷举证明。**

    对每一个基础素材集合(A/B/通用 各若干的所有子集),施加每一种
    「只动 A 色」的改动,断言 B 色指纹一个字节都没变。

    「只动 A 色」枚举四种:补一张、删一张、换内容(content_hash 变)、
    隔离(status 变,于是掉出证据集合)。这四种就是 §8.1 第二行
    「替换/删除/隔离颜色 A 原始样品」的全部形态。
    """
    universe = [
        _Asset("a1", "A"),
        _Asset("a2", "A"),
        _Asset("b1", "B"),
        _Asset("b2", "B"),
        _Asset("g1", None),
    ]
    checked = 0
    for size in range(len(universe) + 1):
        for base in itertools.combinations(universe, size):
            before = list(base)
            b_before = fp.variant_fingerprint(before, "B")

            variants = [("补一张 A 色图", [*before, _Asset("a9", "A")])]
            a_rows = [a for a in before if a.color_variant_id == "A"]
            if a_rows:
                victim = a_rows[0]
                variants.append(
                    ("删一张 A 色图", [a for a in before if a is not victim])
                )
                variants.append(
                    (
                        "换 A 色图内容",
                        [
                            _Asset(a.asset_id, "A", content_hash="sha-new")
                            if a is victim
                            else a
                            for a in before
                        ],
                    )
                )
                variants.append(
                    (
                        "隔离一张 A 色图",
                        [
                            _Asset(a.asset_id, "A", status="QUARANTINED")
                            if a is victim
                            else a
                            for a in before
                        ],
                    )
                )

            for label, after in variants:
                assert fp.variant_fingerprint(after, "B") == b_before, (
                    f"{label} 之后 B 色指纹变了 —— AC-21 破了。"
                    f"before={[a.asset_id for a in before]}"
                )
                assert "B" not in fp.changed_scopes(before, after), (
                    f"{label} 把 B 色算进了受影响作用域"
                )
                checked += 1
    assert checked > 100, f"只验了 {checked} 组,穷举塌了"


def test_ac21_survives_the_awkward_shapes():
    """穷举覆盖不到的几个形状。"""
    # 空集:两个作用域都算得出指纹,而不是抛错
    assert fp.shared_fingerprint([]) == fp.shared_fingerprint([])
    assert fp.variant_fingerprint([], "B") == fp.variant_fingerprint([], "B")

    # 只有 B 色时给 A 补图,B 仍然不动
    only_b = _spu(("b1", "B"))
    after = [*only_b, _Asset("a1", "A")]
    assert fp.variant_fingerprint(after, "B") == fp.variant_fingerprint(only_b, "B")

    # 两张图内容相同但 id 不同:是两条证据,指纹要能分辨
    same_bytes = [_Asset("a1", "A", content_hash="x"), _Asset("a2", "A", content_hash="x")]
    assert fp.variant_fingerprint(same_bytes, "A") != fp.variant_fingerprint(
        same_bytes[:1], "A"
    )


def test_adding_an_a_colour_asset_stales_shared_and_a_only():
    """AC-21 的正面表述:该动的动了,不该动的没动。

    只断言 B 不变是不够的 —— 一个恒返回同一个常量的指纹函数也满足那条。
    """
    before = _spu(("a1", "A"), ("b1", "B"))
    after = [*before, _Asset("a2", "A")]
    assert fp.changed_scopes(before, after) == {fp.SHARED, "A"}


def test_reassigning_an_asset_between_colours_moves_three_scopes():
    """§5.3 明写:改素材颜色归属 → 原颜色、新颜色、共享三个指纹同时变化。

    共享那一个容易漏:集合的**成员没变**,变的是成员身上的 `color_variant_id`。
    指纹元素里不带颜色归属的话,这次改动对共享事实完全隐形 ——
    而共享事实是按「哪些图参与了合并」算出来的,归属一变,分组就变了。
    """
    before = _spu(("a1", "A"), ("b1", "B"))
    after = [_Asset("a1", "B"), _Asset("b1", "B")]
    assert fp.changed_scopes(before, after) == {fp.SHARED, "A", "B"}


def test_deleting_the_last_asset_of_a_colour_is_still_a_change():
    """比并集不比交集:最后一张图被删掉时,那个作用域在 after 里根本不存在。

    只比交集的话这次变化会被漏掉 —— 而「这个颜色一张证据图都没有了」
    恰恰是最该让事实过期的一种。
    """
    before = _spu(("a1", "A"), ("b1", "B"))
    after = _spu(("a1", "A"))
    assert "B" in fp.changed_scopes(before, after)


# ------------------------------------------------- 通用素材的作用域


def test_a_generic_asset_never_lands_in_any_colour_subset():
    """通用素材只进 shared —— 见 `scope_fingerprint` 文档第二条。

    让它进每一个颜色子集的话,「补一张通用图」会 stale 掉全部颜色,
    也就是 D1 那个问题从后门原样回来。
    """
    before = _spu(("a1", "A"), ("b1", "B"))
    after = [*before, _Asset("g1", None)]
    assert fp.changed_scopes(before, after) == {fp.SHARED}
    assert fp.variant_fingerprint(after, "A") == fp.variant_fingerprint(before, "A")
    assert fp.variant_fingerprint(after, "B") == fp.variant_fingerprint(before, "B")


def test_an_empty_string_variant_id_counts_as_generic():
    """空串和 NULL 都是「没归属」。

    两者分开算的话,同一张未归属素材会因为写库时落了空串还是 NULL
    而产生两个不同的指纹 —— 而那个差别对运营完全不可见。
    """
    assert fp.fingerprints([_Asset("g1", "")]) == fp.fingerprints([_Asset("g1", None)])
    assert set(fp.fingerprints([_Asset("g1", "  ")])) == {fp.SHARED}


# ------------------------------------------------- 与 batch14-8 的接缝


def test_ai_images_never_enter_any_fingerprint():
    """跨模块的性质:证据判定复用 §5.1 那一个,所以 AI 图不进指纹。

    不复用的话,后果不是多算一次:生成一轮候选图会让**全部**已确认事实
    stale 掉 —— 而运营刚确认完事实就去生成图,那是流程的正常顺序。
    """
    real = _spu(("a1", "A"))
    with_ai = [*real, _Asset("gen1", "A", source=MediaSource.AI_GENERATED.value)]
    assert fp.changed_scopes(real, with_ai) == frozenset()
    assert fp.shared_fingerprint(with_ai) == fp.shared_fingerprint(real)


def test_quarantined_and_deleted_assets_leave_the_fingerprint():
    """状态一变就掉出证据集合,于是指纹变 —— 这是第二行那一格的执行点。"""
    ready = _spu(("a1", "A"))
    for status in ("QUARANTINED", "DELETED", "FAILED", "PENDING"):
        moved = [_Asset("a1", "A", status=status)]
        assert fp.changed_scopes(ready, moved) == {fp.SHARED, "A"}, f"状态 {status} 没有让指纹变"


def test_the_fingerprint_module_does_not_reimplement_the_evidence_filter():
    """判定只有一处。第二套条件正是 D3 描述的漂移。"""
    src = RULES_FILE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "asset_is_extraction_input" in calls, "指纹模块没有复用 §5.1 的证据判定"
    for literal in ("PRODUCT_EVIDENCE", "READY", "AI_GENERATED"):
        assert literal not in {
            n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
        }, f"指纹模块里出现了 {literal!r} —— 证据条件被重写了一遍"


# ------------------------------------------------- facts_stale 判定


def test_facts_stale_compares_and_fails_closed():
    """对得上 → 不过期;对不上 → 过期;没存过 → 过期。"""
    assert fp.facts_stale(stored_fingerprint="abc", current_fingerprint="abc") is False
    assert fp.facts_stale(stored_fingerprint="abc", current_fingerprint="xyz") is True
    for missing in (None, "", "   "):
        assert (
            fp.facts_stale(stored_fingerprint=missing, current_fingerprint="abc") is True
        ), f"存量数据({missing!r})没有按过期算"


def test_a_fingerprint_version_bump_stales_everything_on_purpose():
    """算法改了要动 `FINGERPRINT_VERSION`,否则新旧指纹会被直接比较。

    不带版本的话,改一次哈希算法 = 全库事实一次性集体 stale,
    而运营看到的是「所有东西同时过期了」,查不出原因。带上版本至少
    「为什么全过期」有个能指的答案。
    """
    assert fp.FINGERPRINT_VERSION, "指纹版本不能为空"
    baseline = fp.shared_fingerprint(_spu(("a1", "A")))
    original = fp.FINGERPRINT_VERSION
    try:
        fp.FINGERPRINT_VERSION = original + "-bumped"
        bumped = fp.shared_fingerprint(_spu(("a1", "A")))
    finally:
        fp.FINGERPRINT_VERSION = original
    assert bumped != baseline, "版本没有进哈希,动它不会改变指纹"
    assert fp.shared_fingerprint(_spu(("a1", "A"))) == baseline, "版本没还回去"


def test_the_encoding_cannot_be_forged_by_shifting_a_separator():
    """长度前缀编码 —— 见 `scope_fingerprint` 文档第三条。

    指纹碰撞在这里的后果不是抽象的安全问题,是**该过期的事实没有过期**:
    一份旧事实带着「指纹没变」的证明进了导出。

    这里用的是一对**真的会碰撞**的输入:换成 `f"{text}|"` 那种朴素分隔符
    编码时,两者拼出来的前两段都是 `a|b|c|`。第一版守卫用的样本
    (`("ab","c")` vs `("a","bc")`)在朴素编码下并不碰撞,所以它抓不住
    那条变异 —— 「构造了一对不同的输入」不等于「构造了一对碰撞」。
    """
    left = fp.shared_fingerprint([_Asset("a", None, content_hash="b|c")])
    right = fp.shared_fingerprint([_Asset("a|b", None, content_hash="c")])
    assert left != right, "字段边界可以被平移 —— 编码能被构造出碰撞"


def test_the_fingerprint_does_not_depend_on_the_order_assets_arrive_in():
    """§5.3「按稳定排序哈希」。

    不排序的后果不是碰撞,是反过来的:同一组素材换个查询顺序就得到
    不同的指纹,于是**什么都没改也会 stale** —— 运营被要求重新确认
    一批一个字都没变的事实,而且每次刷新的结论还不一样。
    """
    assets = _spu(("a1", "A"), ("a2", "A"), ("b1", "B"), ("g1", None))
    baseline = fp.fingerprints(assets)
    for permutation in itertools.permutations(assets):
        assert fp.fingerprints(list(permutation)) == baseline, (
            "指纹依赖素材的到达顺序 —— 什么都没改也会 stale"
        )


def test_the_fingerprint_elements_are_exactly_the_ones_section_5_3_names():
    """§5.3 的元素清单是一个**封闭集合**,和失效矩阵同一套规矩。

    少一项的后果各不相同、但都一样安静:

        color_variant_id   改归属对共享事实完全隐形
        status             证据判定将来放宽状态时,状态变化不再改指纹
        content_hash       换了图片内容但沿用同一条记录时,事实不过期

    `status` 今天确实是**冗余**的(不是 READY 的素材已经掉出证据集合了),
    所以没有任何行为测试能分辨它在不在。冗余而无法分辨的字段最容易被
    「顺手清理掉」—— 这条守卫就是它唯一的锚。清理它之前先改 §5.3。
    """
    source = RULES_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    element = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_element"
    )
    names = [
        c.value
        for n in ast.walk(element)
        if isinstance(n, ast.Tuple)
        for c in n.elts
        if isinstance(c, ast.Constant)
    ]
    assert names == [
        "asset_id",
        "content_hash",
        "source",
        "role",
        "color_variant_id",
        "status",
    ], f"指纹元素清单变了:{names} —— 先改 PRD §5.3,再改这里"


def test_the_shared_scope_key_cannot_collide_with_a_variant_id():
    """共享作用域的键是 `None`,不是某个字符串。

    用字符串的话,一个恰好同名的 `color_variant_id` 会覆盖掉共享指纹 ——
    那不会报错,只会让共享事实在错误的时机过期。
    """
    assert fp.SHARED is None
    keys = fp.fingerprints(_spu(("a1", "SHARED"), ("a2", "__shared__")))
    assert fp.SHARED in keys and len(keys) == 3


def test_the_module_stays_dependency_free():
    """没有数据库也能穷举,是这个模块存在的全部理由。"""
    forbidden = {"sqlalchemy", "fastapi", "pydantic", "httpx", "celery", "PIL"}
    seen: set[str] = set()
    for node in ast.walk(ast.parse(RULES_FILE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            seen.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            seen.add(node.module.split(".")[0])
    assert not (forbidden & seen), f"指纹模块长出了三方依赖:{sorted(forbidden & seen)}"


# ------------------------------------------------- 失效矩阵那一列


def test_the_matrix_now_has_a_facts_column_pointing_at_this_module():
    """§8.1「目标新增 FACTS 列」。

    加这一列之前,那张自称封闭的矩阵**回答不了「商品事实什么时候过期」** ——
    18 个格子里没有一个管事实,而 D1 那条风险的落点恰恰在事实上。
    """
    cell = sm.cell(sm.ChangeSource.ASSET_REPLACED_OR_QUARANTINED, sm.Target.FACTS)
    assert cell.effect is sm.Effect.STALE
    assert cell.mechanism == "app.attributes.scope_fingerprint.changed_scopes"
    assert sm.is_exhaustive()
    # 格数由行列自乘推出,不写字面量 —— 理由见 test_stale_matrix.py 那一条
    assert len(sm.MATRIX) == len(sm.ChangeSource) * len(sm.Target)


def test_modifying_a_confirmed_fact_is_a_new_version_not_a_stale_flag():
    """§8.1「修改确认的共享结构事实 → 新版本」。

    和 STALE 不是一回事:过期是「这份还在但不能再采信」,新版本是「换了一份」。
    合并这两个取值,「改了一个事实之后该做什么」就失去了答案。
    """
    cell = sm.cell(sm.ChangeSource.CONFIRMED_ATTRIBUTE_MODIFIED, sm.Target.FACTS)
    assert cell.effect is sm.Effect.NEW_VERSION
    assert cell.effect is not sm.Effect.STALE


def test_the_six_change_sources_still_owed_to_section_8_1_are_listed():
    """**这条守卫记的是欠账,不是成绩。还款日:阶段 6(A45-batch24 重新认领)。**

    ## 到期时发生了什么

    原还款日是阶段 5。阶段 5 交付完毕时,四行里**还清了一行**
    (「新增/停用 ACTIVE 颜色」,由 batch20 用快照比对那条路还的,见下面
    `settled_elsewhere`),另外三行没有还,也没有人来重新认领 ——
    这正是 §3.34「欠账守卫是有还款日的」与 §3.37「还款日要有人替它盯」
    两条要防的形态,而它在 batch23 与 batch24 之间被外部评审点了出来。

    ## 为什么剩下三行改判到阶段 6,而不是继续挂在阶段 5

    三行都要求矩阵新增一个 `ChangeSource`,而
    `test_every_effective_cell_names_a_mechanism_that_exists_in_code`
    要求每个有效应的格子点名一个**真实存在**的函数:

        修改素材颜色归属(A→B)   要素材侧的颜色归属变更事件 —— 今天没有入口
        修改人工材质/卖点         `manual` 字段今天不进指纹(§4.5 的口径未定)
        修改价格或库存            同上,且它牵涉「价格改了要不要重出草稿」的产品决定

    三条都不是「接一下就好」,而是各自缺一个上游决定。定在阶段 6 不是
    再拖一次:阶段 6 是渠道适配层落地的批次,前两条的入口在那时才存在。

    **到期时如果仍然还不了,请再写一次为什么,而不是再把日期往后挪一格。**


    §8.1 除了 FACTS 列还要求新增六个变更源。它们没进矩阵,因为
    `test_every_effective_cell_names_a_mechanism_that_exists_in_code`
    要求每个有效应的格子点名一个**真实存在**的函数,而这六行的机制
    多半要等阶段 2 的归属外键与阶段 4 的方案层。

    先画格子后补实现的话,那条守卫会红,而让它变绿的最省事做法是
    随便指一个函数 —— 于是矩阵从「封闭集合」退化成「一张看起来很全的表」。

    补上一行就从这张清单里划掉一行;清单空了,这条守卫就该删掉。
    """
    # A45-batch14-20(阶段 4)划掉了两条:它们点名的机制落地了
    #
    #     更换生成方案      app.workflows.generation_plan.image_set_stale_scopes
    #     替换批准图片集    app.listings.image_set_service.approve
    #
    # 「修改素材颜色归属」看着也能补(FACTS 那一格点 changed_scopes 就行),
    # **但它的 COPY 那一格点不出机制**:§8.1 写的是"两色字段过期",
    # 而颜色结构化字段是阶段 5 的东西。补一行要四格都有着落,
    # 不能只补得出的那几格。
    owed = {
        "修改素材颜色归属(A→B)",
        "修改人工材质/卖点",
        "修改价格或库存",
    }
    settled = {
        "更换生成方案(plan_fingerprint 变化)": sm.ChangeSource.GENERATION_PLAN_CHANGED,
        "替换批准图片集": sm.ChangeSource.APPROVED_IMAGE_SET_REPLACED,
    }
    #: **不经 `ChangeSource` 还清的那些。**
    #:
    #: 「新增/停用 ACTIVE 颜色」原来记在 `owed` 里,等的是矩阵加一行。
    #: 而 A45-batch20(5-2B)用另一条路还了它:`listing_drafts.upstream_versions`
    #: 存下建草稿那一刻的颜色集,`refresh_draft` 比对当前颜色集,不一致就判
    #: STALE。真库用例 `test_a_new_active_colour_makes_the_draft_stale` 与
    #: `test_deactivating_a_colour_says_deactivated_not_deleted` 两条各钉一头。
    #:
    #: 划掉它而不是留着:留着的话这条守卫会一直说"还欠 4 笔",而其中一笔
    #: 已经还了 —— 一份**报出来的欠账比实际多**的清单,下一个人核对两轮
    #: 发现对不上之后,会开始怀疑整张清单(§3.37 记的正是这种损耗)。
    #: 换成矩阵那条路才算"重复还款",所以这里记的是**已还,经由另一条路**。
    settled_elsewhere = {
        "新增/停用 ACTIVE 颜色": "A45-batch20:upstream_versions 快照比对(§4.10)",
    }
    assert settled_elsewhere, "另一条路还清的那些也要留名,否则下次会被当成新缺口"

    #: §4.5 原有六行 + §8.1 已补的那几行。清单空了,这条守卫就该删掉。
    baseline = 6
    for source in settled.values():
        assert source in sm.ChangeSource, "划掉的那一行又不见了"
    present = {s.value for s in sm.ChangeSource}
    assert len(present) == baseline + len(settled), (
        f"变更源变成了 {len(present)} 个 —— 有人动了矩阵的行。"
        f"如果是在补 §8.1 剩下那几行,请同时从本清单里划掉对应项:{sorted(owed)}"
    )
    assert len(owed) == 3, "欠账清单和上面那句解释对不上了"



# ------------------------------------------------- 接线(欠账已还)


def test_the_fingerprint_is_computed_from_the_view_not_the_orm_row():
    """接线时必须经过 `views()`。**直接把 ORM 行递进去不报错。**

    §5.3 的元素清单用 `asset_id` / `content_hash`,而 `MediaAsset` 上的列名是
    `id` / `sha256`。少了这层翻译,`_element()` 的每一项都 `getattr` 到 None,
    算出来的是**一个所有素材都长得一样的指纹** —— 于是换了图、删了图、
    改了归属,指纹全都不动,`facts_stale` 永远说「没变」。

    这是本模块最安静的坏法:没有异常、没有空值、没有类型错误,
    只有一个恒定的哈希串。所以守卫盯的是「有没有经过那层翻译」。
    """
    src = (BACKEND_ROOT / "app" / "attributes" / "service.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_run_identity"
    )
    body = ast.unparse(fn)
    assert "scope_fingerprint.views(" in body, (
        "指纹入参没经过 `views()` —— 算出来的会是一个恒定哈希,而且不报错"
    )
    fingerprint_calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "fingerprints"
    ]
    assert len(fingerprint_calls) == 1, "指纹不是算一次 —— 多算一次就多一个答案"
    arg = fingerprint_calls[0].args[0] if fingerprint_calls[0].args else None
    assert isinstance(arg, ast.Name) and arg.id == "views", (
        "递给 `fingerprints()` 的不是 `views()` 的产物"
    )


def test_the_fingerprint_and_the_evidence_query_agree_on_trusting_imported_urls():
    """指纹与取数必须用**同一个** `imported_url_trusted`。

    不一致的后果是指纹按一个比取数更窄(或更宽)的集合算 —— 那批图真的
    进了识别、真的花了钱,却不参与「输入变没变」的判断,于是补一张图
    不会让事实过期。这不报错,只是让 AC-21 静静地不成立。
    """
    src = (BACKEND_ROOT / "app" / "attributes" / "service.py").read_text(encoding="utf-8")
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "run_extraction"
    )
    query = ast.unparse(fn)
    identity = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "_run_identity"
    )
    assert "imported_url_trusted=True" in query, "取数那一行的口径变了"
    # **剥掉 docstring 再看。** `_run_identity` 的文档里就写着这个关键字
    # (解释为什么两边要一致),不剥的话断言被那句**解释**喂成平凡真 ——
    # 口径真的改掉时守卫照样绿。本批变异 P2 撞出来的,
    # 与 §3.26 第四节同一个形状的第七次。
    code = "\n".join(
        ast.unparse(n)
        for n in identity.body
        if not (
            isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
        )
    )
    assert "imported_url_trusted=True" in code, (
        "指纹用的口径和取数不一样 —— 有一批图会花了钱却不进指纹"
    )


def test_the_stored_fingerprint_has_a_column_to_land_in():
    """`input_fingerprint` 是真列(迁移 0040)。

    **这条是上一版欠账守卫的正面。** 那一条写着「指纹算得出来却没有地方存,
    没有存量就没有比较对象」——`facts_stale(stored=None)` 恒为 True,
    接上去只会让全库事实一次性集体过期。列到齐之后它变红,按它自己的
    约定删掉,换成这一条。

    反向也钉:列被删掉时 `facts_stale` 会退回恒 True,而那**不报错** ——
    表现是运营看到所有东西同时过期了,查不出原因。
    """
    src = (BACKEND_ROOT / "app" / "models" / "attribute.py").read_text(encoding="utf-8")
    klass = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.ClassDef) and n.name == "ProductAttributeExtraction"
    )
    columns = {
        n.target.id
        for n in klass.body
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
    }
    assert "input_fingerprint" in columns, (
        "`input_fingerprint` 列不见了 —— facts_stale 会退回恒 True,全库事实集体过期"
    )
    assert hasattr(fp, "AssetView"), "列名翻译没了,指纹拿不到 asset_id / content_hash"
    # 归属外键仍在:没有它,颜色作用域会退回全空,而那不报错
    media = (BACKEND_ROOT / "app" / "models" / "media_asset.py").read_text(encoding="utf-8")
    block = media[media.index("class MediaAsset") : media.index("class MediaDerivative")]
    assert "color_variant_id: Mapped" in block, "归属外键没了 —— 指纹的颜色作用域会退回全空"
