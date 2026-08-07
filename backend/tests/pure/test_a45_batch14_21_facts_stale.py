"""A45-batch14-21:`facts_stale` 派生接线(PRD §4.5 / §5.3 / §6.3 完成条件)。

## 这一批还的是一笔跨了两个阶段的欠账

`scope_fingerprint.py` 从 batch14-9 起就写好并穷举验过。14-20 接上了
**计算**那一半(`fingerprints` / `views`),而**派生**那一半 ——
`facts_stale` 与 `changed_scopes` —— 全仓零调用点,直到本批。

后果具体到一句话:**系统回答不了「这条商品事实还成不成立」。**
`flow.py` 里那几个 `stale` 是 `CopyFacts` / `DraftFacts`,是文案和草稿的
下游过期,不是商品事实。AC-21(传 A 色图不 stale B 色事实)不是会答错,
是**没有任何地方在算**。

## 这个文件不重新证明 AC-21

那是一个全称命题(**任意**一次 A 色改动都不影响 B),由
`test_a45_batch14_9_scope_fingerprint.py` 穷举证过。判定一个字没动。

本文件钉的是**接线**,以及接线时那几个"不报错的坏法":

    一、事实的指纹不许取自 run 行            两者作用域不同,D1 从后门回来
    二、写入侧与读取侧必须同一个取数口径      漂移的表现是"刚确认完就显示过期"
    三、CHANNEL 不参与,而"不参与"不等于"比不上" 合并的表现是刊登属性集体过期
    四、过期字段不许说成"尚无可用值"          说错话会把运营送去做一件要花钱
                                              且确定无效的事
    五、完成条件要带上指纹那一句              少了它,换掉全部样品之后
                                              界面仍显示"已完成"
"""
from __future__ import annotations

import ast

from app.attributes import scope_fingerprint as fp
from app.attributes import validation as va
from app.core.enums import AttributeStatus, OwnerType
from app.workbench import flow
from tests.pure._helpers import BACKEND_ROOT

APP = BACKEND_ROOT / "app"


def _module(rel: str) -> ast.Module:
    return ast.parse((APP / rel).read_text(encoding="utf-8"))


def _func(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"找不到函数 {name}")


def _code(fn: ast.FunctionDef) -> str:
    """函数体,**剥掉 docstring**。

    §3.34 第二节那条规矩的第八次:文档里出现的关键字会把断言喂成平凡真,
    而口径真的改掉时守卫照样绿。本文件每一条读函数体的守卫都走这个助手。
    """
    return "\n".join(
        ast.unparse(node)
        for node in fn.body
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
    )


# ------------------------------------------------- 作用域判定(纯层,可穷举)


def test_every_owner_type_has_an_answer():
    """`OwnerType` 的每一个成员都要有一个作用域答案。**穷举。**

    漏掉一个的表现不是抛异常 —— `fingerprint_scope` 的最后一行是兜底
    `return SHARED_FINGERPRINT_SCOPE`,所以漏掉的那一层会静静地按共享
    作用域判过期。而"静静地按共享判"正是 D1 那个问题:一个颜色补图,
    另一层的事实跟着过期。

    所以这条不只是"别抛异常",它要求每一层的答案是**被想过的**:
    下面那张表逐条写出来,新增一个 `OwnerType` 成员时这里当场红。
    """
    expected = {
        OwnerType.SPU: (True, None),
        OwnerType.PRODUCT: (True, None),
        OwnerType.SKU: (True, None),
        OwnerType.CHANNEL: (False, None),
    }
    for owner_type in OwnerType:
        if owner_type is OwnerType.VARIANT:
            continue  # 下面单独一条,它的答案取决于 owner_id
        assert owner_type in expected, (
            f"`OwnerType.{owner_type.name}` 是新加的,而它的作用域没人想过 —— "
            "兜底会让它按共享判过期,不报错"
        )
        scope = va.fingerprint_scope(owner_type, "whatever")
        assert (scope.participates, scope.scope) == expected[owner_type], (
            f"{owner_type.name} 的作用域答案变了"
        )


def test_channel_facts_do_not_participate_at_all():
    """CHANNEL 不参与,而**不参与不等于比不上**。

    两者在 `fingerprint_for_owner()` 那里都返回 None,所以只看返回值的话
    分不开。合并的表现很具体:刊登属性(平台类目 id、站点特有字段)开始
    跟着补图集体过期 —— 它们和样品图没有任何关系,而运营会看到一批
    永远消不掉的待确认。

    所以判定必须回答**两个**问题,而不是一个可空的答案。
    """
    scope = va.fingerprint_scope(OwnerType.CHANNEL, "listing-123")
    assert scope.participates is False
    assert scope is va.NOT_FINGERPRINTED

    shared = va.fingerprint_scope(OwnerType.SPU, "SPU-1")
    assert shared.participates is True
    assert shared.scope is fp.SHARED, "共享作用域的键必须和指纹模块同一个约定"


def test_the_variant_scope_comes_out_of_the_namespaced_owner_id():
    """VARIANT 取的是 owner_id 里的变体段,不是整个 owner_id。

    owner_id 的格式是 `<len>:<spu>/<variant_id>`(A44 命名空间)。整个递给
    `fingerprints()` 去查的话永远查不到 —— 那张表的键是
    `media_assets.color_variant_id`,不带 SPU 前缀。

    查不到的后果**不是报错**:`facts_stale` 判过期,于是每一条颜色事实
    恒定显示"样品已变",运营点多少次都消不掉。一个不报错的死循环。
    """
    owner_id = va.variant_owner_id(spu="SPU-SW-001", variant_id="cv-black")
    scope = va.fingerprint_scope(OwnerType.VARIANT, owner_id)
    assert scope.participates is True
    assert scope.scope == "cv-black", "取出来的不是变体段 —— 它永远查不到指纹"


def test_a_variant_owner_id_that_cannot_be_split_falls_back_to_shared_not_to_skipping():
    """拆不出来的裸变体 id 朝**过期**那一侧倒,不朝"不参与"那一侧。

    A44 之前写下的行 owner_id 是裸变体 id,拆不出 SPU 命名空间。这些行由
    `orphaned_variant_owners()` 点名列着 —— 也就是说它们的身份**已经知道
    是可疑的**。

    返回"不参与"等于宣布这批事实永远成立,而它们恰恰是最需要有人重新看
    一眼的那一批。落共享的话它们跟着共享指纹走,补任何一张图都会待确认
    —— 吵,但方向对。这与 `facts_stale(stored=None) is True` 是同一条口径。
    """
    scope = va.fingerprint_scope(OwnerType.VARIANT, "black")
    assert scope.participates is True, (
        "拆不出来就跳过 = 宣布这批身份可疑的事实永远成立"
    )
    assert scope.scope is fp.SHARED


# ------------------------------------------------- 接线:写入侧


def test_the_fact_fingerprint_is_not_taken_from_the_run_row():
    """**这是本批最要紧的一条。**

    `ProductAttributeExtraction.input_fingerprint`(0040)记的是"这次 run
    吃进去的是哪批素材" —— 它是**共享作用域**那一个。
    `ProductAttributeValue.input_fingerprint`(0042)记的是"这条事实按哪批
    素材立起来"。

    拿前者填后者是让这条门禁"最省事变绿"的做法,而它的后果不是报错:
    共享事实与各颜色事实会共用同一个 64 位哈希,于是给颜色 A 补一张图,
    **B 色事实跟着 stale** —— D1 那个问题从后门原样回来,而每一条事实
    都带着一个看起来很像的指纹。

    欠账守卫当初逐字点名的就是这条捷径
    (`test_the_wired_half_is_registered_and_the_unwired_half_is_not`)。
    """
    body = _code(_func(_module("attributes/service.py"), "apply_evidence"))
    assert "extraction.input_fingerprint" not in body, (
        "事实的指纹取自 run 行 —— 共享与颜色事实会共用同一个答案"
    )
    assert "fingerprint_for_owner(" in body, (
        "没有按 owner 取指纹 —— 那是唯一能把共享事实与颜色事实分开的一步"
    )


def test_both_write_paths_record_a_fingerprint():
    """识别写值与人工确认**都**要记指纹,漏一条的表现完全不同。

        apply_evidence 漏  识别写出来的值一律无指纹 -> 恒过期 -> 每次识别完
                           所有字段立刻显示"样品已变"
        confirm 漏         §6.3 的完成条件是「CONFIRMED **且**指纹匹配」,
                           人工确认写的正是 CONFIRMED —— 不记指纹的话这条路
                           **永远满足不了完成条件**,运营点一次、刷新之后
                           那个字段又回到待确认,点多少次都一样

    第二个是一个没有任何地方会报错的死循环,所以两条都钉。
    """
    tree = _module("attributes/service.py")
    for name in ("apply_evidence", "confirm"):
        body = _code(_func(tree, name))
        assert "input_fingerprint=" in body, f"{name} 没有把指纹传给 set_value"
        assert "scope_fingerprints_for(" in body, (
            f"{name} 没有经过那个取数入口 —— 它是写入侧与读取侧唯一的共同口径"
        )


def test_set_value_is_still_the_only_writer_and_it_persists_the_column():
    """指纹只在唯一写入点上落库。

    `set_value` 自称"唯一允许写 `product_attribute_values` 的入口"。指纹
    要是在别处赋值(比如某个调用点拿到行之后 `row.input_fingerprint = ...`),
    那个入口就不再唯一 —— 而不唯一的表现是某一条路径写值时忘了指纹,
    于是那条路径写出来的事实恒过期,只有那一条。
    """
    tree = _module("attributes/service.py")
    body = _code(_func(tree, "set_value"))
    assert "input_fingerprint=input_fingerprint" in body, (
        "`set_value` 没有把指纹写进新行 —— 参数收下了,列还是空的"
    )
    src = (APP / "attributes" / "service.py").read_text(encoding="utf-8")
    assert ".input_fingerprint =" not in src, (
        "有人在 `set_value` 之外直接给属性行的指纹赋值 —— 唯一写入点不再唯一"
    )


# ------------------------------------------------- 接线:取数口径不许有两套


def test_the_fact_side_has_exactly_one_place_that_fetches_evidence():
    """写入侧与读取侧必须经过**同一个**取数函数。

    三个消费者(识别写值、人工确认、工作台判过期)各写一行
    `evidence_assets_for(...)` + `views()` + `fingerprints()` 的话,这个仓库
    已经付过一次学费的形状会原样回来(§5.1「唯一取数入口」)。

    而这里漂移的表现格外安静:**写入侧按 A 集合算指纹、读取侧按 B 集合算**,
    两边都不报错,现象是一条刚刚确认完的事实立刻显示"已过期" ——
    或者反过来,一条真该过期的事实永远显示正常。

    所以 `fingerprints(` 在 `app/` 下只许有两个调用点:run 身份那一处
    (`_run_identity`,它算的是 run 行的指纹)和事实侧这一处。
    """
    hosts: list[str] = []
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "fingerprints"
            ):
                hosts.append(path.relative_to(APP).as_posix())
    assert sorted(hosts) == ["attributes/service.py", "attributes/service.py"], (
        f"`fingerprints()` 的调用点变成了 {sorted(hosts)} —— "
        "它只该有两处:run 身份与事实侧取数入口,都在 attributes/service.py。"
        "多一处就是多一套集合口径,而漂移不报错"
    )


def test_the_fact_side_and_the_extraction_side_agree_on_trusting_imported_urls():
    """两侧的 `imported_url_trusted` 由**同一个常量**给出。

    不一致的后果是指纹按一个比取数更窄(或更宽)的集合算 —— 那批图真的
    进了识别、真的花了钱,却不参与"输入变没变",于是补一张图不会让事实
    过期。AC-21 静静地不成立。

    14-20 那条守卫钉的是 `run_extraction` 与 `_run_identity` 两处的字面量
    一致;事实侧一接线就变成四个调用点,而**字面量一致这件事没法靠断言
    维持四份**。所以本批把它收成一个常量,断言点它。
    """
    src = (APP / "attributes" / "service.py").read_text(encoding="utf-8")
    assert "EVIDENCE_IMPORTED_URL_TRUSTED = True" in src, (
        "口径常量不见了 —— 四个调用点会各写各的字面量"
    )
    body = _code(_func(_module("attributes/service.py"), "scope_fingerprints_for"))
    assert body.count("EVIDENCE_IMPORTED_URL_TRUSTED") == 2, (
        "取数与算指纹没有用同一个常量 —— 有一批图会花了钱却不进指纹"
    )
    # **事实侧也要经过 `views()`。** 14-9 那条守卫只钉了 `_run_identity`
    # 那一处,而本批把指纹带到了第二个宿主 —— 变异 Q3 撞出来的:
    # 事实侧把 `views()` 拿掉之后,常量数没变、调用点数没变、一条断言都
    # 不红,而算出来的是一个所有素材都长得一样的哈希。
    #
    # 这是 §3.34 第三节那条(判定的宿主会搬家)的一个变体:宿主不是搬家,
    # 是**多了一个**,而原来那条守卫只认得旧的那一个。
    assert "scope_fingerprint.views(" in body, (
        "事实侧的指纹没经过 `views()` —— 每一项都 getattr 到 None,"
        "算出来是一个恒定哈希,换了图也不 stale,而且不报错"
    )
    media = (APP / "media" / "service.py").read_text(encoding="utf-8")
    assert "_IMPORTED_URL_TRUSTED = True" in media, (
        "素材侧的口径不见了 —— 审计说某作用域变了,而事实侧按另一个集合算"
    )


def test_the_asset_snapshot_goes_through_views_because_orm_rows_are_alive():
    """`changed_scopes` 的 before/after 必须是**快照**,不是 ORM 行。

    SQLAlchemy 的身份映射会让"改之前取的那一批"和"改之后取的那一批"是
    同一批 Python 对象。直接留 ORM 行的话 before 与 after 逐字段相同,
    `changed_scopes()` 恒返回空集 —— 不报错,只是让 §8.1 第 2 行那一格
    恒定说"什么都没变"。

    `AssetView` 在构造时把每一项 getattr 下来,所以它是死的。
    """
    body = _code(_func(_module("media/service.py"), "_evidence_views"))
    assert "scope_fingerprint.views(" in body, (
        "快照没经过 `views()` —— ORM 行是活的,before/after 会恒相等"
    )


def test_the_three_asset_actions_all_report_which_scopes_changed():
    """隔离 / 放行 / 改角色三处都要记。**少一处的表现各不相同。**

        隔离漏   最常见的那次动作没有记录,而它恰恰是运营最会去追的那次
        放行漏   "我明明放行了,为什么事实还是过期"没有答案
        改角色漏 `role` 是指纹元素之一,改角色会让事实过期,而审计里
                 只写着"改了个角色"

    三处都必须在**赋值之前**取 before —— 取在之后的话拿到的是同一个值,
    而那不报错。
    """
    tree = _module("media/service.py")
    for name in ("quarantine", "release", "set_role"):
        body = _code(_func(tree, name))
        assert "_scope_change_payload(" in body, f"{name} 没有记作用域变化"
        assert "before = _evidence_views(" in body, f"{name} 没有取变更前的快照"


def test_the_shared_scope_is_named_in_the_audit_payload_not_left_as_null():
    """共享作用域在审计里写成 `"SHARED"`,不是 JSON 的 null。

    null 在日志检索时和"这个键不存在"长得一模一样,而两者含义相反:
    前者是"共享事实过期了",后者是"这次动作什么都没改"。

    断言里写的是**单引号**那一版:`ast.unparse` 会规范化引号,源码里的
    双引号到这里全变成单引号。§3.34 第二节末尾那条顺带记的规矩,
    第一次真的踩上 —— 而它的失效方向是**假红**,红在一个完全正确的代码上。
    """
    body = _code(_func(_module("media/service.py"), "_scope_change_payload"))
    assert "'SHARED'" in body, "共享作用域没有名字 —— 它在日志里会和「没有变化」混掉"
    assert "changed_scopes(" in body


# ------------------------------------------------- 读取侧:判定与措辞


def test_stale_is_always_a_subset_of_confirmed_by_construction():
    """`stale ⊆ confirmed` 这条不变式**长在 dataclass 上**,不靠调用点。

    这个仓库刚刚为「判据落在别人身上的守卫,守不住自己」付过一次学费
    (§3.37)。同一个形状在这里的样子是:判定侧只对 SETTLED 那一档算过期,
    于是消费侧写 `if name in facts.stale` 看起来是安全的 —— 而它安全的
    理由在**另一个模块**里。

    所以两个消费者都读 `stale_confirmed`,取交在属性里做:一个没确认过的
    字段无论怎么被塞进 `stale`,都不会走到"已确认的事实过期"那条分支上去
    说一句假话。
    """
    facts = flow.AttributeFacts(
        required_fields=("primary_color", "material"),
        confirmed=frozenset({"material"}),
        stale=frozenset({"material", "primary_color"}),  # 故意塞一个没确认的
    )
    assert facts.stale_confirmed == frozenset({"material"})
    # 那个没确认的字段照样欠着,但它欠的是"填一个值",不是"再确认一次"
    assert set(facts.missing_required) == {"primary_color", "material"}


def test_a_stale_confirmed_field_no_longer_counts_toward_completion():
    """§6.3 的完成条件是两句话,不是一句。

    「必填事实 `CONFIRMED`」**且**「其 `input_fingerprint` 匹配当前作用域
    指纹」。少了后半句,给一个颜色换掉全部样品之后,那个颜色的事实仍然
    带着旧结论进导出,而界面显示"已完成"。
    """
    fresh = flow.AttributeFacts(
        required_fields=("material",), confirmed=frozenset({"material"})
    )
    assert fresh.missing_required == ()

    stale = flow.AttributeFacts(
        required_fields=("material",),
        confirmed=frozenset({"material"}),
        stale=frozenset({"material"}),
    )
    assert stale.missing_required == ("material",), (
        "过期的已确认事实仍然算完成 —— 换掉全部样品之后界面会说「已完成」"
    )


def test_a_stale_field_is_not_reported_as_having_no_value():
    """过期字段的措辞不许落进"尚无可用值"。

    **说错话的代价很具体。** 「尚无可用值」的建议是"先跑一次属性识别",
    于是运营去跑一次**付费**识别 —— 而识别产出的是 CANDIDATE,盖不掉
    已有的 MANUAL 值(§6.2 规则 4),那个字段照样过期。花了钱,什么都没变。

    这与 §11 修 `ATTR_EVIDENCE_ONLY` 那条是同一个形状的第二次:
    两种处境的**下一步动作不同**,就不能共用一个问题码。
    """
    facts = flow.AttributeFacts(
        required_fields=("material",),
        confirmed=frozenset({"material"}),
        stale=frozenset({"material"}),
        extraction_count=1,
    )
    result = flow._evaluate_attribute(
        facts,
        material_ready=True,
        audience=flow.AudienceFacts(audience="WOMEN"),
    )
    codes = {issue.code for issue in result.issues}
    assert codes == {"ATTR_FACT_STALE"}, f"问题码是 {sorted(codes)}"
    issue = result.issues[0]
    assert issue.level is flow.IssueLevel.NEEDS_CONFIRM, (
        "过期报成阻断 —— 阻断数要能被信任,而这一条欠的是"
        "「人再看一眼」,与冲突同一档"
    )
    assert "尚无可用值" not in issue.message
    assert "不必重跑识别" in issue.hint, (
        "提示没有说清不用重跑 —— 运营会去付一次确定无效的钱"
    )


def test_a_stale_field_counts_as_waiting_for_a_human_not_as_unfinished_work():
    """只剩过期字段时,属性步是 NEEDS_CONFIRM 不是 IN_PROGRESS。

    这一档在 `STATE_PROGRESS` 里的分数不同,而分数决定列表排序。过期字段
    点进去**有一个可以确认的东西**(值就在那儿),这与 CANDIDATE 的处境
    相反 —— 那一档点进去什么都没有,§11 为此把它从 NEEDS_CONFIRM 里挪走了。

    所以它跟 `suggested` 归一档,不跟 `candidate_only`。
    """
    facts = flow.AttributeFacts(
        required_fields=("material",),
        confirmed=frozenset({"material"}),
        stale=frozenset({"material"}),
        extraction_count=1,
    )
    result = flow._evaluate_attribute(
        facts, material_ready=True, audience=flow.AudienceFacts(audience="WOMEN")
    )
    assert result.state is flow.StepState.NEEDS_CONFIRM


def test_the_summary_says_how_many_facts_went_stale():
    """分数的分子已经把过期字段扣掉了,**扣了就要说**。

    只扣不说的话运营看到"已确认 1/2",而他明明确认过 2 个 ——
    于是他会再点一次那个已经确认过的字段,以为自己刚才漏了。
    """
    facts = flow.AttributeFacts(
        required_fields=("material", "neckline"),
        confirmed=frozenset({"material", "neckline"}),
        stale=frozenset({"material"}),
        extraction_count=1,
    )
    result = flow._evaluate_attribute(
        facts, material_ready=True, audience=flow.AudienceFacts(audience="WOMEN")
    )
    assert "已确认 1/2" in result.summary
    assert "样品已变 1" in result.summary


def test_the_workbench_asks_the_single_judgement_and_does_not_re_derive():
    """工作台只递结果,不在那一层重新算一遍。

    在 `_attribute_facts` 里就地写"取指纹、按 owner 找作用域、比一比"的话,
    写入侧和读取侧就有两套集合口径。判定的单点是
    `attributes.service.stale_fields`,它也是 `facts_stale` 的唯一调用点。
    """
    body = _code(_func(_module("workbench/service.py"), "_attribute_facts"))
    assert "stale_fields(" in body, "工作台没有接上过期判定"
    for leak in ("facts_stale", "fingerprints(", "evidence_assets_for("):
        assert leak not in body, (
            f"`_attribute_facts` 里出现了 {leak} —— 判定在这一层被重新算了一遍"
        )


def test_the_stale_verdict_only_looks_at_settled_facts_via_the_policy_table():
    """档位判定走 `queue_policy`,不手写状态集合。

    手写一份 `{"CONFIRMED"}` 的下场是 `AttributeStatus` 新增一档时这里
    静静地漏掉它。那张表是穷举的、由守卫钉着。

    反向也钉一句:CANDIDATE 不许被算成过期 —— 那会让工作台对同一个字段
    同时说两句话(「有建议值待确认」和「已确认的事实过期了」),
    而后者是假的:那条事实从来没有立起来过。
    """
    body = _code(_func(_module("attributes/service.py"), "stale_fields"))
    assert "queue_policy.disposition(" in body, "档位是手写的状态集合"
    assert AttributeStatus.CONFIRMED.value not in body, (
        "状态字面量出现在判定里 —— 新增一档时这里会静静漏掉"
    )


def test_not_participating_is_asked_separately_from_the_fingerprint_lookup():
    """"不参与"与"查不到"必须分两次问。

    `fingerprint_for_owner()` 对两种情况都返回 None:CHANNEL 不参与,
    以及参与但那个作用域今天没有证据素材。后者**必须**判过期(一个颜色的
    最后一张证据图被删掉,它的事实最该被重新看一眼),前者必须不判。

    只看返回值的话两者分不开,而合并的表现是刊登属性跟着补图集体过期。
    """
    body = _code(_func(_module("attributes/service.py"), "stale_fields"))
    assert "participates" in body, (
        "没有单独问「参不参与」—— 靠 `current is None` 推的话,"
        "CHANNEL 与「颜色没图」会被合成一种处理"
    )


# ------------------------------------------------- 接口出参


def test_the_api_cannot_forget_to_compute_staleness():
    """`_out()` 的 `stale` 是必传的,没有默认值。

    给它默认值(无论 True 还是 False)的话,第三个端点写出来时会**忘记传**
    而不报错:默认 False 让全库事实显示正常,默认 True 让全库事实显示过期,
    两个方向都是一句没人算过的话。必传的话,忘记传是一个 TypeError。

    Schema 上的默认仍取 True —— 那一层的默认值管的是"没有人算过它",
    而"没算过"必须显示成"证明不了它仍然成立"。两个默认方向不同是刻意的。
    """
    fn = _func(_module("api/attributes.py"), "_out")
    assert fn.args.kwonlyargs and fn.args.kwonlyargs[0].arg == "stale", (
        "`stale` 不是关键字参数 —— 位置参数会被顺手漏掉"
    )
    assert fn.args.kw_defaults == [None], (
        "`stale` 有默认值 —— 忘记传会变成一句没人算过的话,而且不报错"
    )
    schema = (APP / "schemas" / "attribute.py").read_text(encoding="utf-8")
    assert "facts_stale: bool = True" in schema, (
        "出参默认成了「不过期」—— 一次忘记赋值的表现是全库事实显示正常"
    )


def test_list_attributes_reads_the_layered_values_not_just_the_legacy_bucket():
    """`effective_map` 必须带 `product=`,否则作用域判定退化成 SPU 级。

    不带的话只读 legacy 那一处(`owner_type=SPU, owner_id=product.id`),
    而拿 legacy 行去问"该比哪个颜色的指纹"会一律得到共享作用域 ——
    颜色事实的过期判断静静地退化成 SPU 级,D1 从后门回来。

    这一条是接线时才暴露出来的:在此之前这个端点不带 `product=` 也"能用",
    因为没有任何东西依赖 owner 这一维。
    """
    body = _code(_func(_module("api/attributes.py"), "list_attributes"))
    assert "product=product" in body, (
        "读的是 legacy 那一桶 —— 颜色事实的作用域会退化成 SPU 级"
    )
    assert "settled_only=False" in body, (
        "接口只报已确认那一档的过期 —— 它回答的是「这一行对不对得上」,"
        "不是「该不该催人」"
    )
