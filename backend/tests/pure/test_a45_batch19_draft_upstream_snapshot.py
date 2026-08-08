"""A45-batch19(阶段 5 批次 5-1):草稿的颜色维上游快照。

## 这一批做的是阶段 5 五项交付里的第三项的**判定那一半**

PRD §13 阶段 5 交付:颜色结构化字段注册表项与确认流;来源追溯;
`color_sku_image_map` 与 `upstream_versions` 快照;READY 门禁扩展;导出预览。

本批只落第三项,而且只落它的两列 + 迁移 + 零依赖判定层。接线(`build_draft`
写快照、`refresh_draft` 读它、READY 门禁按颜色轴推广)是 5-2。

排它第一是因为 5-2 与 5-5 都要读它,而**它是这台机器上唯一能真验的一层** ——
没有网络、没装后端依赖,pytest / ruff / lint-imports 一条都跑不了,
只有 `tools/run_pure_tests.py` 与 `tools/verify_delivery.py` 能跑。

## 守卫分五段,对应五种断法

    形状     快照能不能被逐项比较(排序稳定、schema 版本在、JSON 可落库)
    范围     §6.7 的 ACTIVE 口径 —— **反向那一半才是重点**:
             PLANNED 颜色怎么折腾都不许让草稿过期
    判定     颜色轴的每一种变化都说得出对象、字段、动作(§4.5.1)
    分工     每个轴只有一个人报 —— 这一条是反向断言,最容易静默失效
    欠账     两列今天没有写入点,判定层今天没有生产调用点

第四段值得单独说:`copies` / `spec_version` / `mapping_version` 三项按 §4.10
要落进新列,但它们已经由 `stale.diff_components` 负责解释。两边都比的话,
改一次文案版本会出两条过期提示,而运营看到「这份草稿有 2 个上游变了」——
**「变了几处」这个数字从此不可信,而且不会有任何东西报错。**
"""
from __future__ import annotations

import ast
import inspect
import json
from collections.abc import Mapping

from app.core.enums import SellableStatus
from app.listings.image_set_rules import ItemView, SetView, coverage
from app.workbench import upstream_snapshot as us
from app.workbench.stale import StaleChange, serialize
from tests.pure._helpers import BACKEND_ROOT

MODULE_SRC = (BACKEND_ROOT / "app" / "workbench" / "upstream_snapshot.py").read_text(
    encoding="utf-8"
)
MODEL_SRC = (BACKEND_ROOT / "app" / "models" / "listing_copy.py").read_text(encoding="utf-8")
MIGRATION = BACKEND_ROOT / "migrations" / "versions" / "0049_draft_upstream_snapshot.py"


# ---------------------------------------------------------------- 夹具


def _color(
    variant_id: str,
    *,
    code: str | None = None,
    status: str = SellableStatus.ACTIVE.value,
    facts: Mapping[str, str] | tuple[str, ...] = ("v-1",),
    sample: str | None = "s" * 64,
    plan: str | None = "p" * 64,
    image_set: tuple[str, int] | None = ("is-1", 3),
    skus: tuple[str, ...] = ("SW-001-BLK-M",),
) -> us.ColorUpstream:
    return us.ColorUpstream(
        variant_id=variant_id,
        # `code=""` 要能表达「这个颜色真的没有编码」—— 用 `code or ...` 的话
        # 空串会被默认值吃掉,于是那条回落用例永远走不到它要测的分支
        variant_code=variant_id.upper() if code is None else code,
        sellable_status=status,
        fact_versions=(
            dict(facts)
            if isinstance(facts, Mapping)
            else {version: f"field_{index}" for index, version in enumerate(facts, start=1)}
        ),
        sample_fingerprint=sample,
        plan_fingerprint=plan,
        image_set_id=None if image_set is None else image_set[0],
        image_set_version=None if image_set is None else image_set[1],
        sku_codes=skus,
    )


_COMPONENTS = {
    "attrs": ["v-a"],
    "copies": {"en": {"id": "c-1", "version": 2}},
    "manual": {},
    "spec_version": "7",
    "mapping_version": "3",
}


def _snapshot(*colors: us.ColorUpstream, shared=("f-1",), shared_sample="a" * 64) -> dict:
    shared_map = (
        dict(shared)
        if isinstance(shared, Mapping)
        else {version: f"shared_field_{index}" for index, version in enumerate(shared, start=1)}
    )
    return us.build_upstream_versions(
        components=_COMPONENTS,
        colors=colors,
        shared_fact_versions=shared_map,
        shared_sample_fingerprint=shared_sample,
    )


def _components(changes) -> list[str]:
    return sorted({c.component for c in changes})


def _code_only(source: str) -> str:
    """剥掉注释与 docstring 之后的源码。

    `ast.parse` 天然吃掉注释;docstring 是真的字符串节点,要显式摘。
    本仓为「解释性文字喂真了断言」付过四次账(§3.26 / 14-13 / 14-22 /
    18 的 C2),前三次的修法是把锚点钉得更死,第四次改成从输入里把注释去掉 ——
    这里沿用第四种,因为**一条守卫要讲清它守的是什么,就得说出那件事的名字**。
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


# ---------------------------------------------------------------- 一、形状


def test_the_snapshot_survives_a_round_trip_through_json():
    """快照要落进 JSONB 再读回来,所以它必须是纯 JSON 值。

    这条看着像废话,而它挡的是一类具体的错:builder 里顺手把
    `frozenset` / `tuple` / `UUID` 放进去。那些东西在内存里比较得好好的,
    落库那一刻被 `json.dumps` 拒掉或悄悄变成另一种类型 ——
    而后者会让「同一份事实算出同一个快照」在**跨进程**时不成立。
    """
    snap = _snapshot(_color("v-red"), _color("v-blk"))
    assert json.loads(json.dumps(snap, ensure_ascii=False)) == snap


def test_the_same_facts_always_produce_the_same_snapshot():
    """输入顺序换一遍,快照必须一模一样。

    快照要被逐项比较,而「同一份事实算出同一个快照」是比较能成立的前提。
    查询顺序变一次(换个 ORDER BY、换个 join)就重新算出一个不等的快照的话,
    草稿会在**没有任何上游变化**时被判过期 —— 而这个机制立刻变成噪音,
    然后就会有人去关掉它。
    """
    a, b, c = _color("v-red"), _color("v-blk"), _color("v-nvy")
    # **比字节,不比 dict。** `{"a":1,"b":2} == {"b":2,"a":1}` 在 Python 里为真,
    # 于是按 dict 比的话「排序没了」这件事一个字都不会说 —— 而落进 JSONB、
    # 进审计 payload、被人肉 diff 的是序列化之后的那串字节
    assert json.dumps(_snapshot(a, b, c), ensure_ascii=False) == json.dumps(
        _snapshot(c, a, b), ensure_ascii=False
    )


def test_the_snapshot_carries_its_own_shape_version():
    """形状版本必须在快照里,而且比较双方对不上时判「不可比」。

    少了它,形状一改,`diff_upstream` 会拿新形状的键去读旧快照 ——
    读不到的一律等于 None,于是**每个轴都「没变」**,存量草稿全部静默放行。
    这是本模块最安静的坏法:不报错、不抛异常,只是所有草稿突然都不过期了。
    """
    snap = _snapshot(_color("v-red"))
    assert snap["schema"] == us.UPSTREAM_SCHEMA_VERSION

    changes = us.diff_upstream({**snap, "schema": "0"}, snap)
    assert _components(changes) == ["upstream_snapshot"]
    assert changes[0].old_value == "0" and changes[0].new_value == us.UPSTREAM_SCHEMA_VERSION


def test_the_two_columns_have_independent_shape_versions():
    """只改图片映射形状时,不该把事实快照一起判成不可比,反向亦然。"""
    upstream = _snapshot(_color("v-red"))
    mapping = us.build_color_sku_image_map(colors=[_color("v-red")], coverage={})
    assert upstream["schema"] == us.UPSTREAM_SCHEMA_VERSION
    assert mapping["schema"] == us.IMAGE_MAP_SCHEMA_VERSION
    assert "SNAPSHOT_SCHEMA_VERSION" not in MODULE_SRC


def test_a_draft_built_before_the_columns_existed_is_not_silently_waved_through():
    """`stored=None` 判过期,不判「没变化」。

    0049 之前建的草稿一行都没算过颜色维。空列表在这里的含义会是
    「颜色维检查过了,没问题」—— 而实际含义是「没检查过」。
    与 `stale.copy_attrs_stale(plan_missing=True)` 同向:没有快照就无法
    证明这份草稿仍然成立,默认必须偏保守。
    """
    for empty in (None, {}):
        changes = us.diff_upstream(empty, _snapshot(_color("v-red")))
        assert _components(changes) == ["upstream_snapshot"]
        assert changes[0].action, "没有说该做什么 —— §4.5.1 要求提示带动作"


def test_every_component_this_module_emits_is_in_the_declared_closed_set():
    """本模块吐出的 `component` 不许有 `COMPONENTS` 之外的取值。

    前端与过期提示按 component 分组展示。散着写字符串字面量的话,
    改一个拼写不会有任何东西红 —— 表现是那一类提示**从分组里消失**,
    而不是报错。所以取值要写成封闭集合,并且真的被行使一遍。
    """
    emitted = set()
    emitted |= {c.component for c in us.diff_upstream(None, _snapshot(_color("v-red")))}
    emitted |= {
        c.component
        for c in us.diff_upstream(
            {**_snapshot(_color("v-red")), "schema": "0"}, _snapshot(_color("v-red"))
        )
    }
    emitted |= {
        c.component
        for c in us.diff_upstream(
            _snapshot(_color("v-red"), _color("v-old"), shared=("f-1",), shared_sample="a" * 64),
            _snapshot(
                _color("v-red", facts=("v-2",), sample="z" * 64, plan="q" * 64,
                       image_set=("is-2", 4)),
                _color("v-new"),
                shared=("f-2",),
                shared_sample="b" * 64,
            ),
        )
    }
    assert emitted <= set(us.COMPONENTS), f"冒出了封闭集合之外的取值:{emitted - set(us.COMPONENTS)}"
    assert emitted == set(us.COMPONENTS), (
        f"这几个取值一次都没被行使:{sorted(set(us.COMPONENTS) - emitted)} —— "
        "没被行使的取值等于没有实现,而它在集合里看起来像是有的"
    )


def test_the_changes_serialize_with_the_existing_serializer():
    """输出走 `stale.serialize`,不新建一个序列化器。

    颜色维是现有过期机制的**推广**(§6.7 原话),不是第二套机制。
    自建一个形状的表现是前端要认两种过期提示,而第二种多半会漏掉
    `action` 那一栏 —— 也就是 §4.5.1 点名不许发生的那件事。
    """
    changes = us.diff_upstream(None, _snapshot(_color("v-red")))
    rows = serialize(changes)
    assert set(rows[0]) == {"component", "message", "fields", "old_value", "new_value", "action"}
    assert all(isinstance(c, StaleChange) for c in changes)


# ---------------------------------------------------------------- 二、范围(§6.7)


def test_only_active_colors_get_versions_in_the_snapshot():
    """§6.7:只检查 `sellable_status=ACTIVE` 的颜色与其 SKU。"""
    snap = _snapshot(
        _color("v-red"),
        _color("v-plan", status=SellableStatus.PLANNED.value),
        _color("v-pause", status=SellableStatus.PAUSED.value),
        _color("v-dead", status=SellableStatus.DISABLED.value),
    )
    assert sorted(snap["colors"]) == ["v-red"]
    assert snap["inactive"] == {
        "v-dead": "DISABLED",
        "v-pause": "PAUSED",
        "v-plan": "PLANNED",
    }


def test_the_inactive_ledger_carries_a_status_and_nothing_else():
    """`inactive` 只记状态,**一个版本都不许记**。

    带上版本的话,一个 PLANNED 颜色的事实每被改一次草稿就过期一次 ——
    而 §6.7 写的正是「PLANNED/PAUSED/DISABLED 不阻塞、不参与映射完整性判定」。

    这一条是正向断言,下一条是它的反向那一半 —— 两条都要有:
    这里钉的是**记了什么**,那里钉的是**判定真的不看它**。
    """
    snap = _snapshot(_color("v-plan", status=SellableStatus.PLANNED.value))
    assert snap["inactive"] == {"v-plan": "PLANNED"}
    assert all(isinstance(v, str) for v in snap["inactive"].values())


def test_churning_a_planned_colour_never_makes_the_draft_stale():
    """**§6.7 的反向那一半。** PLANNED 颜色怎么折腾都不许让草稿过期。

    这是本文件里最重要的一条。正向的「ACTIVE 颜色变了要过期」漏掉时,
    有人会在导出文件里看见错的行;而反向漏掉时,一个还没到样的颜色
    会让整个 SPU 的草稿永远处在过期态 —— 运营重新生成一次,它又过期,
    **而提示里说的是一个他压根还没开始做的颜色**。

    第二种没有任何东西会报错,只会让人不再相信过期提示。
    """
    before = _snapshot(
        _color("v-red"),
        _color("v-plan", status=SellableStatus.PLANNED.value, facts=("v-1",), sample="1" * 64),
    )
    after = _snapshot(
        _color("v-red"),
        _color(
            "v-plan",
            status=SellableStatus.PLANNED.value,
            facts=("v-9", "v-8"),
            sample="9" * 64,
            plan="9" * 64,
            image_set=("is-9", 9),
        ),
        # 顺带新建一个从来没 ACTIVE 过的颜色 —— 同样不许触发
        _color("v-brand-new", status=SellableStatus.PLANNED.value),
    )
    assert us.diff_upstream(before, after) == []


def test_activating_or_pausing_a_colour_does_make_the_draft_stale():
    """§7.2:新增 ACTIVE 颜色 / 停用颜色 → 重新计算。

    比的是颜色集合的**并集**,不是交集 —— 与
    `scope_fingerprint.changed_scopes` 和 `generation_plan.image_set_stale_scopes`
    同一条口径同一个坑:只比交集的话,「某个颜色被停用」会因为那个键在
    current 里不存在而被漏掉,而它恰恰最该让草稿过期。
    """
    before = _snapshot(_color("v-red", code="RED"), _color("v-blk", code="BLK"))
    after = _snapshot(
        _color("v-red", code="RED"),
        _color("v-blk", code="BLK", status=SellableStatus.PAUSED.value),
        _color("v-nvy", code="NVY"),
    )
    changes = us.diff_upstream(before, after)
    assert _components(changes) == ["color_set"]

    paused = next(c for c in changes if "BLK" in c.message)
    added = next(c for c in changes if "NVY" in c.message)
    # 停用与删除对运营是两句不同的话 —— `inactive` 存在的全部理由就是这句
    assert "PAUSED" in paused.message and paused.new_value == "PAUSED"
    assert "新建了 ACTIVE" in added.message
    assert added.old_value == "不存在" and added.new_value == "ACTIVE"

    turned_active = us.diff_upstream(
        _snapshot(_color("v-plan", code="PLAN", status=SellableStatus.PLANNED.value)),
        _snapshot(_color("v-plan", code="PLAN")),
    )
    assert len(turned_active) == 1
    assert "从 PLANNED 转为 ACTIVE" in turned_active[0].message
    assert turned_active[0].old_value == "PLANNED"


def test_a_deleted_colour_reads_differently_from_a_paused_one():
    """颜色整个消失(既不在 colors 也不在 inactive)时,说的是「已不存在」。

    两者的动作恰好一样(重新生成草稿),但**话不一样**:运营看到
    「被改成 PAUSED」会去查是谁停的,看到「已不存在」会去查是谁删的。
    §4.5.1 要的是能指向下一步动作的提示,不是一个统一措辞。
    """
    before = _snapshot(_color("v-red", code="RED"), _color("v-gone", code="GONE"))
    after = _snapshot(_color("v-red", code="RED"))
    (change,) = us.diff_upstream(before, after)
    assert change.new_value == "已删除" and "已不存在" in change.message


# ---------------------------------------------------------------- 三、判定


def test_each_colour_axis_reports_separately_and_names_the_colour():
    """一个颜色的四个轴各报各的,而且每条都点名是哪个颜色。

    合并成一条「红色的上游变了」的话,运营不知道该去重新识别事实、
    还是去重新出图 —— 那正是 §4.5.1 说的「仅显示『已过期』三个字视为未完成」
    在颜色维上的形状。
    """
    before = _snapshot(_color("v-red", code="RED"))
    after = _snapshot(
        _color(
            "v-red",
            code="RED",
            facts=("v-2",),
            sample="z" * 64,
            plan="q" * 64,
            image_set=("is-2", 4),
        )
    )
    changes = us.diff_upstream(before, after)
    assert _components(changes) == [
        "color_facts",
        "color_image_set",
        "color_plan",
        "color_sample",
    ]
    for change in changes:
        assert "RED" in change.message, f"{change.component} 那条没点名颜色"
        assert change.action, f"{change.component} 那条没说该做什么"


def test_fact_changes_name_fields_instead_of_exposing_version_ids():
    """事实轴的 `fields` 必须是字段键,不能把数据库版本 UUID 推给运营。"""
    before = _snapshot(
        _color("v-red", code="RED", facts={"11111111-1111": "primary_color"}),
        shared={"aaaaaaaa-aaaa": "material"},
    )
    after = _snapshot(
        _color("v-red", code="RED", facts={"22222222-2222": "primary_color"}),
        shared={"bbbbbbbb-bbbb": "material"},
    )
    changes = us.diff_upstream(before, after)
    by_component = {change.component: change for change in changes}
    assert by_component["color_facts"].fields == ("primary_color",)
    assert by_component["shared_facts"].fields == ("material",)
    assert not any(
        "1111" in field or "2222" in field
        for change in changes
        for field in change.fields
    )


def test_a_colour_without_a_code_falls_back_to_a_short_id_not_an_empty_string():
    """没有 `variant_code` 时退回短 id,**不退回 `display_name`**。

    `display_name` 今天恒为默认值(`audit_column_writers` 台账上记着,
    还款日阶段 5)。拿它当标识的表现是每个颜色都显示成空 ——
    提示变成「颜色 的样品有变动」,而这不会报错。
    """
    before = _snapshot(_color("abcdef0123456789", code=""))
    after = _snapshot(_color("abcdef0123456789", code="", sample="z" * 64))
    (change,) = us.diff_upstream(before, after)
    assert "abcdef01" in change.message
    # 判据是**代码**,不是源码原文:`_color_label` 的文档里要讲清为什么不用
    # 那一列,而按原文比会把那句解释当成使用(与下面那条 caller 守卫同一个坑)
    assert "display_name" not in _code_only(MODULE_SRC), (
        "有人开始读 display_name 了 —— 那一列今天恒为默认值,拿它当标识"
        "会让每个颜色都显示成空,而且不报错"
    )


def test_the_shared_scope_is_a_separate_axis_from_the_colour_scopes():
    """共享作用域自己一个轴。**这是 AC-21 在草稿这一侧的形状。**

    §5.3 / AC-21:给颜色 A 补样品,共享事实与 A 色事实 stale,
    **B 色不受影响**。快照把 shared 与每个颜色分开存,就是为了让
    「只有 A 变了」在草稿提示里也说得出来 —— 合并成一个总指纹的话,
    这里只能说「上游变了」,而那正是 D1 那条风险的落点。
    """
    before = _snapshot(_color("v-red", code="RED"), _color("v-blk", code="BLK"))
    after = _snapshot(
        _color("v-red", code="RED", sample="z" * 64),
        _color("v-blk", code="BLK"),
        shared_sample="b" * 64,
    )
    changes = us.diff_upstream(before, after)
    assert _components(changes) == ["color_sample", "shared_sample"]
    assert not any("BLK" in c.message for c in changes), "B 色被误伤了 —— AC-21 的反面"


# ---------------------------------------------------------------- 四、映射与分工


def _set_with(*items: ItemView) -> SetView:
    return SetView(image_set_id="is-1", spu="SW-001", version=3, status="APPROVED", items=items)


def _item(asset: str, *, variant: str | None, primary=False, order=0, shared=False) -> ItemView:
    return ItemView(
        media_asset_id=asset,
        role="PRODUCT_MAIN",
        sort_order=order,
        variant_id=variant,
        is_primary=primary,
        shared_opt_in=shared,
    )


def test_the_map_fans_the_colour_images_out_over_every_sku_of_that_colour():
    """尺码不换图,所以一个颜色下每个 SKU 的图是一样的 —— 但要逐 SKU 落下来。

    只记到颜色一层的话,「这一行 SKU 用的是哪张主图」在出事之后要靠
    重新跑一遍映射才能回答,而那时上游可能已经变了。
    """
    image_set = _set_with(
        _item("a-main", variant="v-red", primary=True, order=0),
        _item("a-side", variant="v-red", order=1),
        _item("a-common", variant=None, order=2, shared=True),
    )
    cov = coverage(image_set, variant_ids=["v-red"])
    mapping = us.build_color_sku_image_map(
        colors=[_color("v-red", code="RED", skus=("SW-1-RED-S", "SW-1-RED-M"))],
        coverage=cov,
    )
    skus = mapping["colors"]["v-red"]["skus"]
    assert sorted(skus) == ["SW-1-RED-M", "SW-1-RED-S"]
    assert skus["SW-1-RED-S"] == {"primary": "a-main", "extras": ["a-side", "a-common"]}
    assert skus["SW-1-RED-S"] == skus["SW-1-RED-M"]
    assert us.primary_of(mapping, variant_id="v-red", sku="SW-1-RED-M") == "a-main"


def test_a_shared_image_never_lands_in_the_primary_slot():
    """§6.5:通用图只能进附图位,而且这个判断**不在本模块里重写**。

    通用图当主图是 BLOCK-02 定死的那条规矩;判定长在
    `image_set_rules.coverage()` 上(`shared` 与 `owned` 分开返回)。
    这里断言的是本模块**消费**了那个分法 —— 自己再判一次的表现是
    两处口径各自漂移,而漂移的方向恰好是「红色 SKU 挂黑色主图」。
    """
    image_set = _set_with(_item("a-common", variant=None, primary=True, order=0, shared=True))
    cov = coverage(image_set, variant_ids=["v-red"])
    mapping = us.build_color_sku_image_map(
        colors=[_color("v-red", skus=("SW-1-RED-S",))], coverage=cov
    )
    row = mapping["colors"]["v-red"]["skus"]["SW-1-RED-S"]
    assert row["primary"] is None
    assert row["extras"] == ["a-common"]


def test_the_map_check_only_covers_the_sku_axis_and_says_so():
    """`map_problems` 只查 SKU 轴,主图缺失归 `image_set_rules.validate_set`。

    在这里再判一次的表现是同一个问题出两条不同措辞的提示,而运营会
    以为是两个问题。分工写在函数文档里,这里钉两件事:**它真的不判主图**,
    以及它**真的查得出 SKU 那一轴**。
    """
    body = inspect.getsource(us.map_problems)
    assert "primary" not in body, (
        "`map_problems` 开始判主图了 —— 那是 §6.5 的格子,"
        "两处都判的话同一个问题会出两条提示"
    )

    mapping = us.build_color_sku_image_map(
        colors=[_color("v-red", code="RED", skus=("SW-1-RED-S",))],
        coverage={},
    )
    # 主图是 None(图片集里一张图都没有),而 map_problems 一个字都不说
    assert mapping["colors"]["v-red"]["skus"]["SW-1-RED-S"]["primary"] is None
    assert us.map_problems(
        mapping, colors=[_color("v-red", code="RED", skus=("SW-1-RED-S",))]
    ) == []

    # 换成 SKU 轴的问题,它就说话了
    problems = us.map_problems(
        mapping, colors=[_color("v-red", code="RED", skus=("SW-1-RED-S", "SW-1-RED-L"))]
    )
    assert problems == ["颜色 RED 的 SKU SW-1-RED-L 不在映射里"]


def test_ready_gate_conjoins_image_rules_and_sku_mapping_rules():
    """READY 不能在两路判定里二选一:缺主图与缺 SKU 必须能同时报出。"""
    color_at_build = _color("v-red", code="RED", skus=("SW-1-RED-S",))
    mapping = us.build_color_sku_image_map(colors=[color_at_build], coverage={})
    current = _color("v-red", code="RED", skus=("SW-1-RED-S", "SW-1-RED-M"))
    problems = us.ready_problems(
        mapping,
        colors=[current],
        image_set=_set_with(_item("a-side", variant="v-red", primary=False)),
    )
    assert any("MISSING_PRIMARY_FOR_VARIANT" in problem for problem in problems)
    assert any("SW-1-RED-M 不在映射里" in problem for problem in problems)


def test_the_map_check_refuses_to_pass_a_draft_that_never_computed_one():
    """没有映射快照 = 不放行,与 `diff_upstream(None, ...)` 同一档。"""
    assert us.map_problems(None, colors=[_color("v-red")]) != []
    assert us.map_problems({"schema": "0", "colors": {}}, colors=[_color("v-red")]) != []


def test_the_map_check_flags_a_colour_that_is_no_longer_active():
    """停用之后映射里还留着它 —— §6.7:不参与映射完整性判定,也不该留在映射里。"""
    mapping = us.build_color_sku_image_map(
        colors=[_color("v-red", code="RED"), _color("v-blk", code="BLK")], coverage={}
    )
    problems = us.map_problems(
        mapping,
        colors=[
            _color("v-red", code="RED"),
            _color("v-blk", code="BLK", status=SellableStatus.PAUSED.value),
        ],
    )
    assert problems == ["映射里还留着颜色 BLK,它已经不是 ACTIVE"]


def test_the_three_axes_that_components_already_owns_are_carried_but_never_compared():
    """**每个轴只有一个人报。** 这条是反向断言,而反向断言最容易静默失效。

    §4.10 点名 `upstream_versions_json` 要含 Copy 版本,所以它落进列里;
    值从 `components` 抄来(单一写入点)。但比较归 `stale.diff_components` ——
    两边都比的话,改一次文案版本会出两条过期提示,而运营看到的是
    「这份草稿有 2 个上游变了」,于是「变了几处」这个数字从此不可信。

    断言分两段:值真的落进去了(否则 §4.10 没兑现),以及改了它**一条
    变化都不出**(否则就是双报)。
    """
    before = _snapshot(_color("v-red"))
    assert before["copies"] == {"en": {"id": "c-1", "version": 2}}
    assert before["spec_version"] == "7" and before["mapping_version"] == "3"

    after = us.build_upstream_versions(
        components={
            **_COMPONENTS,
            "copies": {"en": {"id": "c-2", "version": 9}},
            "spec_version": "8",
            "mapping_version": "4",
        },
        colors=[_color("v-red")],
        shared_fact_versions={"f-1": "shared_field_1"},
        shared_sample_fingerprint="a" * 64,
    )
    assert after["copies"] != before["copies"]
    assert us.diff_upstream(before, after) == [], (
        "文案 / 模板 / 映射版本被比了第二遍 —— 那三个轴归 stale.diff_components"
    )


def test_the_snapshot_copies_those_three_axes_instead_of_collecting_them_again():
    """值必须是从 `components` 抄的,不许自己再采集一次。

    上一条断的是「不比较」,这一条断的是「不第二次采集」。两者是不同的错:
    自己采集一次的表现是**两列说的不是同一件事**,而那种不一致要等到
    有人拿导出文件和草稿详情页对账时才会被发现。
    """
    fn = next(
        n
        for n in ast.walk(ast.parse(MODULE_SRC))
        if isinstance(n, ast.FunctionDef) and n.name == "build_upstream_versions"
    )
    # `ast.unparse` 把字符串统一成单引号,所以这里钉的是它的输出形状,
    # 不是源码里那几个字 —— 拿源码原文比会在有人换个引号时假红
    body = ast.unparse(fn)
    for key in ("copies", "spec_version", "mapping_version"):
        assert f"components.get('{key}')" in body, (
            f"{key} 不再从 components 抄 —— 第二个采集点就是第二个漂移源"
        )


# ---------------------------------------------------------------- 五、欠账


def _imports_the_layer(node: ast.AST) -> bool:
    """这个节点是不是一条指向判定层的 import。

    两种写法都要认:`from app.workbench import upstream_snapshot`(别名进来)
    与 `from app.workbench.upstream_snapshot import build_upstream_versions`
    (直接取函数)。只认前一种的话,5-2 用后一种接线时这条欠账守卫
    **不会翻转** —— 一条还清了却不肯变红的欠账,和一条没人记的欠账等价。
    """
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if module.endswith("upstream_snapshot"):
            return True
        return any(alias.name == "upstream_snapshot" for alias in node.names)
    if isinstance(node, ast.Import):
        return any(alias.name.endswith("upstream_snapshot") for alias in node.names)
    return False


def test_the_two_draft_snapshot_columns_have_no_writer_yet():
    """**这条守卫记的是欠账,不是成绩。还款日:阶段 5。**

    还款日读作「交付阶段推进到 5 之前必须还清」。`DELIVERY_STAGE` 推到 5 的
    含义是「阶段 5 的交付项落码完毕」,而接线(批次 5-2)正是阶段 5 的批次 ——
    标记推上去那一刻这两列必须有人写。

    本批落的是列 + 迁移 + 判定层,写入点在 `workbench/service.build_draft`,
    读取点在 `refresh_draft` 与 READY 门禁,三处都排在 5-2。

    §3.38 那两次(`media_assets.color_variant_id` 躲五批、§4.8 去重键躲六批)
    都是事后被人逐条核出来的。这一批在落列的**同一批**就记账,
    并且记在两处:`audit_column_writers.LEDGER`(门禁读它)与这里
    (「欠账守卫都在还款日之内」读它)。

    还清的样子:两列在 `build_draft` 里被赋值,`LEDGER` 的两条随之失效
    并被审计点名要求删除 —— 那时这条守卫翻转成正向的,并补一句「欠账已还」。
    """
    build = (BACKEND_ROOT / "app" / "workbench" / "service.py").read_text(encoding="utf-8")
    for column in ("upstream_versions", "color_sku_image_map"):
        assert f"row.{column}" not in build, (
            f"{column} 有写入点了 —— 这条欠账已经还了,"
            "把它翻转成正向守卫并从 LEDGER 里删掉那一条"
        )


def test_the_decision_layer_has_no_production_caller_yet():
    """**这条守卫记的是欠账,不是成绩。还款日:阶段 5。**

    判定层本批已落码并被穷举测试,但 `app/` 下**零调用**。

    §3.43 把「上游算对了、没人接最后一跳」点成了一类测试看不见的缺陷:
    纯层全绿、门禁全绿,而功能整条不通。那条门禁(`WIRED_MODULES`)只看
    被登记的模块,所以一个没登记的新模块可以安静地躺很久 —— 本条替它记账。

    还清的样子:`verify_delivery.WIRED_MODULES` 里加上
    `app.workbench.upstream_snapshot`,由那条门禁接管,本条随之删除。
    """
    # **判据是 import,不是文本。** 第一版按文本扫,当场被自己咬:
    # `models/listing_copy.py` 的列注释里写着「形状与判定在
    # `app/workbench/upstream_snapshot.py`」—— 于是守卫报「已经接线了」,
    # 而那句话恰恰是在说它没接。同一个坑在本仓是第五次(§3.26 / 14-13 /
    # 14-22 / 18 的 C2),修法与上一次同向:**从输入里把注释去掉**,
    # 而 `ast` 天然做到这一点。
    hits = sorted(
        {
            path.name
            for path in (BACKEND_ROOT / "app").rglob("*.py")
            if path.name != "upstream_snapshot.py"
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if _imports_the_layer(node)
        }
    )
    assert not hits, (
        f"{hits} 已经引用了判定层 —— 这条欠账还上了,"
        "把模块登记进 WIRED_MODULES 并删掉本条"
    )


def test_the_migration_is_a_pure_column_add_with_no_backfill():
    """0049 只加列,不回填、不 import `app.*`。

    迁移不许 import `app.*`(全仓没有一份破例,0038 / 0040 / 0042 / 0045
    各写过为什么)。而回填这两列要按 §6.5 的混排规则挑图 —— 在迁移里做
    只剩「把那套规则冻成一段 SQL」一条路,也就是 0045 拒绝过的第二个判定点。
    """
    src = MIGRATION.read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "app" not in imported, "迁移 import 了 app.* —— 全仓 43 份没有一份这么做"
    assert 'revision: str = "0049"' in src and 'down_revision: str | None = "0048"' in src
    assert "UPDATE" not in src.upper().replace("UPDATED", ""), "0049 不该回填"

    upgrade = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "upgrade"
    )
    body = ast.unparse(upgrade)
    assert "nullable=True" in body, (
        "两列给了 NOT NULL / server_default —— `{}` 的含义是「算过,颜色维是空的」,"
        "而存量草稿一次都没算过。READY 门禁读到它会在颜色轴上静默放行"
    )
    assert "server_default" not in body


def test_the_two_columns_are_nullable_in_the_orm_too():
    """ORM 侧同样可空。两侧不一致时,SQLAlchemy 会在 flush 前就拦下 NULL。

    迁移和模型各说各话是一类很贵的错:`test_upgrade_matches_orm_metadata`
    只比**列名**,比不了可空性 —— 于是这种不一致要等到真库上第一次
    插入才暴露,而那时错误信息指向的是调用方。
    """
    for column in ("upstream_versions", "color_sku_image_map"):
        line = next(ln for ln in MODEL_SRC.splitlines() if ln.strip().startswith(f"{column}:"))
        assert "nullable=True" in line, f"{column} 在 ORM 侧不是可空的"
        assert "server_default" not in line
