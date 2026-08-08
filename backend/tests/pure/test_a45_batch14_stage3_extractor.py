"""A45-batch14:阶段 3 第一批 —— 真实多模态抽取器与识别硬规则。

按 PRD v3.1 §13 阶段 3 的第一批交付:真实 vision 抽取器接进 `extractors`
注册表(复用 llm/ 传输层与 images 层,fail-closed 语义沿用)、固定 JSON Schema
与 `missing_reason`、不可见字段过滤。

## 这份守卫从哪一侧下断言

batch13-2 的教训写在它自己的文件头上:守卫挡得住"有人把决定删掉",
挡不住"决定只落了一半" —— 因为那份守卫断言的是**展开层**,而验收标准
说的是读取侧看见什么。

所以这里的断言尽量落在"下一个消费者看见什么"上:

    落库层看见几条证据、其中几条带 reason      不是"解析函数返回了什么"
    注册表报出来的 configured 来自哪里          不是"名单里写了什么"
    请求体里出现了哪些字段、没出现哪些          不是"适配器被调用了"

`run_extraction` 需要 Session,纯测试里没有数据库 —— 用一个记账用的
假 session(收集 `add()` 进来的对象;`begin_nested()` 给一个只计数的
假保存点,因为 batch14-20 起建行段包在保存点里)。它测的是**这个函数
决定写什么**,那正是缺陷发生的地方;"写进去了吗"由真库用例负责
(已写、未执行)。

素材从哪里来,由 `_EVIDENCE_ENTRY` 一处收口 —— 那个常量下面写着它为什么
不能再是四份散落的字面量。
"""
from __future__ import annotations

import ast
import pathlib

from app.core.enums import MissingReason
from tests.pure._helpers import BACKEND_ROOT, PROJECT_ROOT, expect_raises

EXTRACTOR_VISION = BACKEND_ROOT / "app" / "extractors" / "vision.py"
EXTRACTOR_REGISTRY = BACKEND_ROOT / "app" / "extractors" / "registry.py"
EXTRACTOR_SCHEMA = BACKEND_ROOT / "app" / "extractors" / "schema.py"
ATTR_SERVICE = BACKEND_ROOT / "app" / "attributes" / "service.py"
EVAL_VISION = BACKEND_ROOT / "app" / "evaluators" / "vision.py"
ENDPOINT_TRUST = BACKEND_ROOT / "app" / "llm" / "endpoint_trust.py"
MIGRATION_0036 = (
    BACKEND_ROOT / "migrations" / "versions" / "0036_extraction_missing_reason.py"
)
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def _func(path, name: str):
    for node in ast.walk(_tree(path)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里没有 {name}()")


# ================================================================ 假 session


class _FakeSavepoint:
    """`begin_nested()` 的返回值。只记账,不回滚任何东西。

    真实的保存点在 batch14-20 被加进 `run_extraction` 的建行段:幂等键
    撞唯一索引时整个事务会进 aborted,不开保存点就没法继续跑完这次识别。
    纯测试里撞不到约束(假 session 不落库),所以这两个方法都只是计数 ——
    它们存在是为了**让那一段能跑到**,不是为了验保存点的语义。
    """

    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _FakeSession:
    """只记账,不落库。`run_extraction` 在纯测试里唯一需要的东西。"""

    def __init__(self):
        self.added = []
        self.flushes = 0
        self.savepoints = []

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        self.flushes += 1

    def begin_nested(self):
        sp = _FakeSavepoint()
        self.savepoints.append(sp)
        return sp

    def scalars(self, *_a, **_k):
        return iter(())

    def scalar(self, *_a, **_k):
        return None

    def get(self, *_a, **_k):
        return None

    def execute(self, *_a, **_k):
        class _R:
            def all(self_inner):
                return []

        return _R()


class _FakeAsset:
    def __init__(self, ident: str):
        self.id = ident
        self.storage_path = f"products/{ident}.jpg"
        self.source = "MANUAL_UPLOAD"
        # `status` 少不得:batch14-7 给 `run_extraction` 接上 §5.1 白名单之后,
        # 判定会**复查** `status = READY`(`is_extraction_input` 的第一个条件)。
        # 这一行曾经缺失,而本文件恰好在缺 pydantic/sqlalchemy 的机器上整体
        # skip —— 于是四条用例带着"2148/2148 全绿"的记录红了一整批
        # (batch14-11 撞破)。生产路径由取数入口保证只给 READY 行,假对象
        # 必须复刻同一前提,否则测的是一条生产里到不了的分支。
        #
        # 那个入口 batch14-19 起是 `evidence_assets_for()`,不再是
        # `usable_assets()`(见 `_EVIDENCE_ENTRY`)—— 换名字那一轮,这份
        # 注释和下面四处打桩一起过期了,而跳过让它们又躺了一批。
        self.status = "READY"
        self.quality_json = None


class _FakeProduct:
    def __init__(self, audience="WOMEN"):
        self.id = "11111111-1111-1111-1111-111111111111"
        self.audience = audience
        self.spu = "SPU-SW-001"
        self.sku = "SPU-SW-001-BLK-S"
        self.variant_key = "black"
        self.color_variant_id = None
        self.primary_color = "BLACK"


class _ScriptedExtractor:
    """按脚本产出结果的抽取器。`billable` 可切,用来验那道付费上限。"""

    def __init__(self, results, *, name="scripted", billable=False):
        self._results = list(results)
        self.name = name
        self.billable = billable
        self.usage_provider = "scripted-vendor"
        self.calls = []

    def extract(self, *, image, target_fields, enum_defs, options):
        self.calls.append((image, tuple(target_fields), dict(options)))
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_the_fake_asset_still_passes_the_evidence_whitelist():
    """夹具与 §5.1 白名单不许再次静默脱钩。

    本文件的假素材要扮演的是"一条会真的进入识别输入的行"。白名单每收紧一个
    条件(batch14-7 收了 `status`,下一次可能是 `evidence_class` 存储列),
    这里就会先红,并把话说全 —— 而不是让台账/上限那几条用例一起红成
    "全部素材不能作为识别证据",让人误以为白名单误伤了生产。
    """
    from app.media import evidence_rules

    assert evidence_rules.asset_is_extraction_input(_FakeAsset("probe")), (
        "本文件的 _FakeAsset 不再满足 §5.1 白名单 —— 白名单新增了条件而夹具"
        "没跟上。去 evidence_rules.asset_is_extraction_input 看它现在读哪些"
        "字段,把缺的补进 _FakeAsset(生产语义:evidence_assets_for 给出的"
        " READY 人工上传行),不要反过来放宽白名单。"
    )


#: `run_extraction` 今天从 `media_service` 上取证据素材用的那个名字。
#:
#: **这里写成常量,是因为它曾经在这份文件里散成四份字面量而集体过期。**
#: batch14-19 把取数入口从 `usable_assets()` 换成 `evidence_assets_for()`
#: (§5.1「白名单查询助手成为唯一取数入口」),下面四条用例还在打旧名字的桩:
#: 假素材永远到不了 `run_extraction`,假 session 又查不出任何行,于是全部
#: 撞「该商品没有可用于识别的素材」。而本文件在缺 sqlalchemy 的机器上整体
#: skip —— 换名字那一轮没有任何地方变红。
#:
#: 这和 batch14-11 撞破的是**同一型事故**(见 `_FakeAsset.status` 那段注释):
#: 生产改了前提、夹具没跟上、跳过掩盖了红。补法也和那次一样 —— 不放宽被测
#: 的东西,而是把夹具和生产前提之间的那根线**接到会报警的地方**:
#: `test_the_patched_entry_point_is_the_one_run_extraction_calls` 直接读
#: `run_extraction` 的 AST,下一次换名字会在这里先红,并把新名字念出来。
_EVIDENCE_ENTRY = "evidence_assets_for"


class _evidence_returns:
    """让 `run_extraction` 的取数入口返回指定的假素材。

    用上下文管理器而不是四处 try/finally:打桩点只剩一处,常量只有一份,
    恢复不会漏。
    """

    def __init__(self, assets):
        self._assets = list(assets)
        self._original = None

    def __enter__(self):
        from app.media import service as media_service

        self._original = getattr(media_service, _EVIDENCE_ENTRY)
        setattr(media_service, _EVIDENCE_ENTRY, lambda *_a, **_k: list(self._assets))
        return self

    def __exit__(self, *_exc):
        from app.media import service as media_service

        setattr(media_service, _EVIDENCE_ENTRY, self._original)
        return False


def _media_service_calls_in(fn_name: str) -> set[str]:
    """`app/attributes/service.py` 的某个函数里,`media_service.X(...)` 的 X 集合。"""
    fn = _func(ATTR_SERVICE, fn_name)
    return {
        node.func.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "media_service"
    }


def test_the_patched_entry_point_is_the_one_run_extraction_calls():
    """打桩的名字必须是 `run_extraction` 真的用来取证据的那一个。

    没有这条,取数入口再改一次名,下面四条用例会再一次静默地打空气 ——
    在有 sqlalchemy 的机器上红成「该商品没有可用于识别的素材」,让人以为
    是白名单误伤;在没有的机器上连红都不红。

    `usable_asset_count` 不算数:它只在证据为空时被调来把错误话说准,
    **不返回行**(见 `media/service.py` 那个函数的文档)。所以这里断言的是
    「打的桩在集合里」而不是「集合只有一个元素」。
    """
    called = _media_service_calls_in("run_extraction")
    assert _EVIDENCE_ENTRY in called, (
        f"本文件打的桩是 media_service.{_EVIDENCE_ENTRY},而 run_extraction 现在调的是 "
        f"{sorted(called)} —— 取数入口改名了。把 _EVIDENCE_ENTRY 改成新名字,"
        "不要给 _FakeSession 加返回行的查询方法来绕过去"
    )
    assert "usable_assets" not in called, (
        "run_extraction 退回了 usable_assets —— 它返回未过滤的行,"
        "AI 生成图会被喂进付费抽取器(§5.1)"
    )


def _run(session, product, extractor, **kw):
    from app.attributes import service as attr_service

    return attr_service.run_extraction(
        session, product=product, extractor=extractor, **kw
    )


def _evidence_rows(session):
    from app.models.attribute import AttributeEvidence

    return [o for o in session.added if isinstance(o, AttributeEvidence)]


def _result(fields=(), unreadable=(), missing=()):
    from app.extractors.base import FieldMissing, FieldObservation, ImageExtractionResult

    return ImageExtractionResult(
        fields=tuple(
            FieldObservation(name=n, value=v, confidence=c) for n, v, c in fields
        ),
        unreadable_fields=tuple(unreadable),
        missing=tuple(FieldMissing(name=n, reason=r) for n, r in missing),
        model_name="scripted-model",
        prompt_version="scripted-1",
        duration_ms=5,
    )


# ================================================================ §6.3 编造过滤
#
# 这一节全部打在 `extractors/evidence_plan.py` 上 —— 那是判定的所在地,
# 零依赖,所以能在这台没有 sqlalchemy 的机器上真的跑起来。
# "服务层确实用了这个判定"由本文件末尾的接线守卫钉住。


def _plan(fields=(), unreadable=(), missing=(), targets=("primary_color",)):
    from app.extractors.evidence_plan import plan_evidence

    return plan_evidence(
        _result(fields=fields, unreadable=unreadable, missing=missing), targets
    )


def test_a_field_outside_the_requested_targets_never_becomes_evidence():
    """§6.3 硬规则一:目标清单外的字段过滤掉,不入确认队列。

    **判据是"这次问了什么",不是"注册表里有没有"。** 原来的检查是
    `observation.name not in REGISTRY` —— 注册过但这次没问它的字段照样落证据,
    而增量识别(`fields=` 指定几个字段)恰恰是最常见的调用形态:
    模型顺手多答一个 `pattern_type`,它会被当成本次识别的产物写进去,
    带着一个没人要过的值参与合并。
    """
    plan = _plan(
        fields=[("primary_color", "NAVY", 0.9), ("pattern_type", "STRIPE", 0.9)]
    )
    assert [e.field_name for e in plan.evidence] == ["primary_color"]
    assert plan.fabricated == 1
    assert plan.fabricated_names == ("pattern_type",)


def test_a_model_invented_field_name_is_counted_too():
    """模型自造一个注册表里根本没有的名字,同样计数而不是静默丢弃。"""
    plan = _plan(fields=[("no_such_field_at_all", "X", 0.9)])
    assert plan.evidence == ()
    assert plan.fabricated == 1


def test_a_value_for_a_field_the_model_says_it_cannot_see_is_fabrication():
    """既给了值又说"这个字段判断不了"时,**"没有值"胜出并计入编造**。

    这正是"不可见属性编造"的字面定义。让值胜出的话,一个模型亲口说
    看不清的值会进合并、进确认队列 —— 而它是这条指标存在的全部理由。
    """
    plan = _plan(
        fields=[("primary_color", "NAVY", 0.95)],
        missing=[("primary_color", "NOT_VISUALLY_DETERMINABLE")],
    )
    assert plan.fabricated == 1
    assert len(plan.evidence) == 1
    assert plan.evidence[0].is_missing
    assert plan.evidence[0].missing_reason == "NOT_VISUALLY_DETERMINABLE"


def test_the_fabricated_names_are_truncated_and_counted_not_stored_wholesale():
    """字段名可能是模型自造的任意字符串。计数进列,名字只进日志且截断 ——
    存原文等于让模型往我们的库里写自由文本。"""
    plan = _plan(fields=[("x" * 500, "V", 0.9)])
    assert plan.fabricated == 1
    assert all(len(n) <= 64 for n in plan.fabricated_names)


def test_the_fabrication_count_is_a_column_not_only_a_log_line():
    """§14.4 的"不可见属性编造率"要 SUM 它,所以它必须是一列真实列。

    只写日志的话那个指标永远是 0 —— 而一个恒为 0 的指标比没有指标更糟,
    它看起来在说"模型从不编造"。判据按 AST 读模型定义,不 import
    (这台机器没有 sqlalchemy)。
    """
    model = BACKEND_ROOT / "app" / "models" / "attribute.py"
    tree = _tree(model)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == (
            "fabricated_field_count"
        ):
            found = True
            call = node.value
            kwargs = {k.arg: k.value for k in call.keywords}
            assert kwargs["nullable"].value is False
            assert "server_default" in kwargs, (
                "存量识别记录没数过编造,加列时不给 server_default 会让迁移失败"
            )
    assert found, "product_attribute_extractions 上没有 fabricated_field_count 列"


# ================================================================ §4.5 missing_reason


def test_a_field_the_model_cannot_read_leaves_evidence_with_a_reason():
    """§4.5:落 evidence 不落 value。

    这条以前是反的 —— `unreadable_fields` **不落任何东西**。后果是
    "这张图上这个字段看不清"只活在模型响应里:运营在识别详情页看到的是
    一个字段凭空缺席,而它到底是没问、模型没答、还是答了看不清,
    三者长得一模一样,而三者对应三个不同的动作。
    """
    plan = _plan(missing=[("primary_color", "NOT_VISUALLY_DETERMINABLE")])
    assert len(plan.evidence) == 1
    item = plan.evidence[0]
    assert item.is_missing
    assert item.missing_reason == MissingReason.NOT_VISUALLY_DETERMINABLE.value
    assert item.value is None and item.confidence is None


def test_the_two_no_value_channels_land_in_one_shape():
    """v1 的 `unreadable_fields` 与 v2 的 `missing` 只有**一条**落库形状。

    两种形状意味着下游要认两种"没有值",而认漏一种的表现是
    某一类缺失在界面上不显示 —— 不报错,只是不在。
    """
    plan = _plan(
        unreadable=["primary_color"],
        missing=[("material", "AWAITING_SUPPLIER_DATA")],
        targets=("primary_color", "material"),
    )
    rows = {e.field_name: e for e in plan.evidence}
    assert set(rows) == {"primary_color", "material"}
    assert rows["primary_color"].missing_reason == MissingReason.INSUFFICIENT_EVIDENCE.value
    assert rows["material"].missing_reason == MissingReason.AWAITING_SUPPLIER_DATA.value
    assert all(e.is_missing for e in rows.values())


def test_an_unknown_missing_reason_degrades_instead_of_dropping_the_evidence():
    """模型编一个不认识的 reason 时,降级成"看不清",**不放弃"没有值"这件事**。

    降级方向是刻意的:reason 是诊断信息,写错只让人少一条线索;
    而把整条证据丢掉会让这个字段退回"凭空缺席"那种不可分辨的状态。
    """
    plan = _plan(missing=[("primary_color", "BECAUSE_I_SAID_SO")])
    assert len(plan.evidence) == 1
    assert plan.evidence[0].missing_reason == MissingReason.INSUFFICIENT_EVIDENCE.value


def test_saying_a_field_is_missing_does_not_smuggle_it_past_the_target_filter():
    """对没问过的字段宣布"没有值",不落证据。

    否则编造过滤有一条绕行缝:模型把清单外的字段塞进 `missing`,
    它就凭空出现在这次识别的证据里 —— 值是空的,但字段是它自己加的。
    """
    plan = _plan(missing=[("pattern_type", "INSUFFICIENT_EVIDENCE")])
    assert plan.evidence == ()
    # 没给出确定值,所以不算编造
    assert plan.fabricated == 0


def test_the_reason_strings_do_not_drift_from_the_enum():
    """`evidence_plan` 为了零依赖重复了一份 reason 字符串。两边漂移的表现是
    落库值与枚举对不上,而没有任何东西会报错。"""
    from app.extractors import evidence_plan

    assert evidence_plan.ALLOWED_REASONS == {m.value for m in MissingReason}


def test_missing_reason_is_a_column_so_the_ui_can_tell_two_nulls_apart():
    """`normalized_value` 为 NULL 有两种原因,少了这一列它们不可分辨:
    模型说看不清 vs 模型给了个归一不了的值。前者该补图,后者该看提示词。"""
    model = BACKEND_ROOT / "app" / "models" / "attribute.py"
    source = model.read_text(encoding="utf-8")
    assert "missing_reason: Mapped[str | None]" in source, "缺列,两种 NULL 不可分辨"


def test_the_service_materialises_the_plan_instead_of_deciding_again():
    """接线守卫:`run_extraction` 必须调 `plan_evidence`,不许自己再判一遍。

    自己判一遍的表现是两套规则并存,而其中一套没有测试 —— 上面那一整节
    的断言会变成在测一个没人调用的函数。
    """
    fn = _func(ATTR_SERVICE, "run_extraction")
    called = {
        node.func.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "plan_evidence" in called
    # 而且不许在服务层里重新出现目标过滤的判断
    source = ast.get_source_segment(ATTR_SERVICE.read_text(encoding="utf-8"), fn) or ""
    assert "not in target_set" not in source, "服务层又长出了第二份目标过滤"


# ================================================================ 付费上限


def test_a_paid_extractor_refuses_to_fan_out_over_the_ceiling():
    """识别还是同步逐图调用,一次点击就是 N 次付费调用。

    没有这道闸,"给一件传了 200 张图的商品点一次识别"= 200 次付费调用
    外加一个必然超时的请求,而点的人在界面上看到的只是一个按钮。
    """
    from app.core.errors import ValidationError

    session = _FakeSession()
    product = _FakeProduct()
    assets = [_FakeAsset(str(i)) for i in range(30)]
    extractor = _ScriptedExtractor([], billable=True)

    from app.attributes import service as attr_service

    with _evidence_returns(assets):
        error = expect_raises(
            ValidationError,
            attr_service.run_extraction,
            session,
            product=product,
            extractor=extractor,
        )
    assert extractor.calls == [], "已经开始调用了才拒绝 —— 钱已经花出去一部分"
    assert "分批" in str(error)


def test_the_ceiling_does_not_apply_to_the_mock():
    """Mock 不花钱,而它是 CI 与演练要跑的那条路径。

    上限按 `billable` 问抽取器自己,不维护"哪些抽取器要花钱"的名单 ——
    名单会和注册表分叉,分叉的表现是新接的付费抽取器不受限。
    """
    session = _FakeSession()
    assets = [_FakeAsset(str(i)) for i in range(30)]
    extractor = _ScriptedExtractor([_result() for _ in range(30)], billable=False)

    from app.attributes import service as attr_service

    with _evidence_returns(assets):
        row = attr_service.run_extraction(
            session, product=_FakeProduct(), extractor=extractor
        )
    assert row.image_count == 30


def test_the_call_budget_boundaries_are_exact():
    """上限的失败模式全部是边界,所以逐个钉住。

    第一版这条是"按 AST 看上限检查排在循环之前",而变异验证发现它抓不住
    把检查删掉 —— 判定搬进纯模块之后,边界能真的跑起来。
    """
    from app.extractors.call_budget import over_call_budget

    assert over_call_budget(image_count=12, ceiling=12, billable=True) is False
    assert over_call_budget(image_count=13, ceiling=12, billable=True) is True
    assert over_call_budget(image_count=1, ceiling=12, billable=True) is False


def test_the_budget_never_applies_to_a_free_extractor():
    """Mock 不花钱,而它是 CI 与演练要跑的那条路径。"""
    from app.extractors.call_budget import over_call_budget

    assert over_call_budget(image_count=10_000, ceiling=12, billable=False) is False


def test_a_zero_ceiling_falls_back_instead_of_meaning_unlimited():
    """把 0 理解成"不限"会让一次手滑的配置变成一张巨额账单。"""
    from app.extractors.call_budget import DEFAULT_MAX_IMAGES_PER_RUN, over_call_budget

    assert over_call_budget(image_count=500, ceiling=0, billable=True) is True
    assert over_call_budget(image_count=-1, ceiling=-5, billable=True) is False
    assert DEFAULT_MAX_IMAGES_PER_RUN > 0


def test_the_refusal_tells_the_operator_how_to_continue():
    """只说"太多了"的话,运营唯一能做的是删素材 —— 而素材本身没有问题。"""
    from app.extractors.call_budget import over_budget_message

    message = over_budget_message(image_count=40, ceiling=12)
    assert "40" in message and "12" in message
    assert "media_asset_ids" in message


def test_the_service_asks_the_budget_module_before_the_loop():
    """接线守卫:服务层调预算判定,且排在逐图循环之前。

    排在循环里的话,拒绝发生时钱已经花出去一部分。
    """
    fn = _func(ATTR_SERVICE, "run_extraction")
    budget_line = None
    loop_line = None
    for node in fn.body:
        for child in ast.walk(node):
            if (
                budget_line is None
                and isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "over_call_budget"
            ):
                budget_line = node.lineno
        if isinstance(node, ast.For) and loop_line is None:
            loop_line = node.lineno
    assert budget_line is not None, "服务层没有调用预算判定"
    assert loop_line is not None, "找不到逐图循环"
    assert budget_line < loop_line, "预算检查排在了循环之后,拒绝时钱已经花了"


def test_a_failed_call_still_records_usage():
    """结构判据:异常分支里也要记流水。

    厂商在收到请求那一刻就开始计费,超时和解析失败都不退钱 ——
    而"只给成功的记账"是这类台账最常见、也最难发现的漏法。
    """
    fn = _func(ATTR_SERVICE, "run_extraction")
    handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "逐图调用外面没有异常处理"
    reachable = False
    for handler in handlers:
        for node in ast.walk(handler):
            if not (
                isinstance(node, ast.If)
                and any(
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "_record_extraction_usage"
                    for call in ast.walk(node)
                )
            ):
                continue
            # **条件必须是一个真的变量。** 第一版只断言"调用出现在 except 里",
            # 而 `if False: _record_extraction_usage(...)` 照样满足它 ——
            # 变异验证当场抓到这条守卫在验错的东西(与 batch13 的 M7 同型)
            assert not isinstance(node.test, ast.Constant), (
                "记流水被一个常量条件挡住了,等于没记"
            )
            reachable = True
    assert reachable, "调用失败的那一路没有记流水,账单会对不上"


def test_the_ceiling_is_read_from_configuration_not_frozen_in_the_source():
    """上限必须可调:12 是默认值不是判据。运维改配置就该生效。"""
    source = ATTR_SERVICE.read_text(encoding="utf-8")
    assert "EXTRACTOR_MAX_IMAGES_PER_RUN" in source


# ================================================================ 花费台账


def test_every_paid_call_writes_a_usage_row_including_the_failed_ones():
    """厂商在收到请求那一刻就开始计费,超时和解析失败都不退钱。

    a25 之前评分调用完全不进流水表,`/spend` 漏掉的是开销里更大的一半。
    识别在**接后端的同一批**里堵上,不留"先跑起来再补记账"的窗口。
    """
    calls = []

    from app.attributes import service as attr_service

    original = attr_service._record_extraction_usage
    attr_service._record_extraction_usage = lambda session, **kw: calls.append(kw)
    try:
        session = _FakeSession()
        extractor = _ScriptedExtractor(
            [_result(fields=[("primary_color", "NAVY", 0.9)]), RuntimeError("boom")],
            billable=True,
        )
        assets = [_FakeAsset("a"), _FakeAsset("b")]
        with _evidence_returns(assets):
            attr_service.run_extraction(
                session,
                product=_FakeProduct(),
                extractor=extractor,
                fields=("primary_color",),
            )
    finally:
        attr_service._record_extraction_usage = original

    assert len(calls) == 2, f"两次调用只记了 {len(calls)} 条流水"
    assert [c["succeeded"] for c in calls] == [True, False]
    assert calls[1]["error_code"] == "RuntimeError"


def test_a_free_extractor_writes_no_usage_rows():
    """Mock 不该在花费台账里留下行 —— 那会让"这个月花了多少"变成假的。"""
    calls = []

    from app.attributes import service as attr_service

    original = attr_service._record_extraction_usage
    attr_service._record_extraction_usage = lambda session, **kw: calls.append(kw)
    try:
        with _evidence_returns([_FakeAsset("a")]):
            attr_service.run_extraction(
                session=_FakeSession(),
                product=_FakeProduct(),
                extractor=_ScriptedExtractor([_result()], billable=False),
                fields=("primary_color",),
            )
    finally:
        attr_service._record_extraction_usage = original
    assert calls == []


def test_the_usage_row_is_not_tagged_as_a_generation_submit():
    """识别流水的 operation 必须是它自己的,不能混进 submit。

    混了的话「这个月生成花了多少」会把识别的钱算进去,而两者的单价、
    调用频次、优化方向完全不同 —— 看板上会看见生成成本莫名翻倍。
    """
    fn = _func(ATTR_SERVICE, "_record_extraction_usage")
    operations = [
        kw.value.value
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "operation" and isinstance(kw.value, ast.Constant)
    ]
    assert operations == ["attribute_extract"], operations


def test_extraction_usage_does_not_pass_a_billing_key():
    """每一次识别调用都是真的又调了一次 —— 没有"同一笔消费记两行"的窗口。

    传 billing_key 的后果反过来:第二张图的调用会撞唯一约束,
    而它是一笔真实发生的消费。
    """
    fn = _func(ATTR_SERVICE, "_record_extraction_usage")
    keys = [
        kw.arg
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        for kw in node.keywords
    ]
    assert "billing_key" not in keys


# ================================================================ 注册表


def test_the_registry_offers_the_real_backend():
    from app.extractors.registry import get_extractor
    from app.extractors.vision import VisionAttributeExtractor

    assert isinstance(get_extractor("vision"), VisionAttributeExtractor)


def test_configured_traces_to_the_implementation_not_a_list():
    """硬规则 4 在这里第二次落地。

    a37 版按一张 `_SIMULATED_BACKENDS` 名单填 `configured`,注释自己承认
    「接真后端时这一行必须改成问它自己」。名单式写法在真后端接上之后
    会说"vision 已配置",而它可能连 base_url 都没填 —— 状态条据此告诉运营
    "识别已就绪",然后每一次识别都报未配置。
    """
    from app.extractors.registry import _BACKENDS, describe_extractors

    rows = {row["name"]: row for row in describe_extractors()}
    assert set(rows) == set(_BACKENDS)
    for name, row in rows.items():
        instance = _BACKENDS[name]()
        assert row["configured"] == bool(instance.is_configured()), name
        assert row["is_simulator"] == bool(_BACKENDS[name].is_simulator), name


def test_an_unconfigured_real_backend_reports_itself_as_not_configured():
    """空配置下 vision 必须报 False。

    这是上一条的行为版:上一条证明"两边一致",这一条证明那个一致的值
    **不是恒 True** —— 两条都在,才排除掉"is_configured 直接 return True"。
    """
    from app.extractors.vision import ExtractorModelConfig, VisionAttributeExtractor

    blank = VisionAttributeExtractor(ExtractorModelConfig())
    assert blank.is_configured() is False
    assert "EXTRACTOR_MODEL_BASE_URL" in blank.config.missing_fields()


def test_the_registry_never_falls_back_to_mock_for_an_unknown_backend():
    from app.core.errors import ConfigError
    from app.extractors.registry import get_extractor

    expect_raises(ConfigError, get_extractor, "no-such-backend")


def test_describing_extractors_survives_a_backend_that_explodes():
    """状态上报路径不该因为某个后端读配置炸掉而整个 500。

    报 `configured: False` 比报 500 诚实,也有用得多。
    """
    from app.extractors import registry

    class _Exploding:
        name = "boom"
        is_simulator = False

        def __init__(self):
            raise RuntimeError("配置读取炸了")

    registry._BACKENDS["boom"] = _Exploding
    try:
        rows = {row["name"]: row for row in registry.describe_extractors()}
        assert rows["boom"]["configured"] is False
    finally:
        del registry._BACKENDS["boom"]


# ================================================================ 配置独立


def test_the_extractor_config_does_not_read_a_single_vision_model_key():
    """识别配置**不回退**到评分器的键。

    静默共享的代价:换评分模型时识别模型跟着换,而识别的校准分箱按
    (字段 × 模型 × Prompt) 存 —— 分箱全部作废、置信度退回"未校准",
    没有任何提示。判据按 AST 取真实读到的字符串,注释里提 VISION 不算。
    """
    tree = _tree(EXTRACTOR_VISION)
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    # 文档字符串里会提到 VISION_MODEL_*,所以只看真的被当作配置键读的那些
    read_keys = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id.startswith("provider_")
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    leaked = {k for k in read_keys if k.startswith("VISION_MODEL")}
    assert not leaked, f"识别配置读了评分器的键:{sorted(leaked)}"
    assert any(k.startswith("EXTRACTOR_MODEL") for k in read_keys)
    assert literals  # 保证上面那个集合真的解析到了东西


def test_every_declared_setting_key_exists_on_settings():
    """设置页声明的键必须都是真实配置项。拼错一个字母,界面上那一项永远存不进去。"""
    import app.core.config as config_module
    from app.core.settings_schema import SETTING_GROUPS

    fields = {f.key for g in SETTING_GROUPS for f in g.fields if f.key.startswith("EXTRACTOR")}
    assert len(fields) >= 10, "识别分组的字段太少,像是没接上"
    source = config_module.__file__
    text = pathlib.Path(source).read_text(encoding="utf-8")
    for key in fields:
        assert f"{key}:" in text, f"{key} 在设置页声明了,但 Settings 上没有这个字段"


def test_the_env_example_documents_the_new_group():
    """`.env.example` 是运维唯一会照着填的那份清单。"""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for key in ("EXTRACTOR_BACKEND", "EXTRACTOR_MODEL_BASE_URL", "EXTRACTOR_MAX_IMAGES_PER_RUN"):
        assert key in text, f"{key} 没写进 .env.example"


# ================================================================ 共用而不是重写


def test_the_extractor_reuses_the_shared_transport_and_image_layers():
    """§5.1 与 M2 的验收判据:识别层不自己写一遍 HTTP 与图片准备。

    自己写的话,写出来的多半是 `httpx.post` 加一个 try —— 没有退避、
    不认 Retry-After、把 429 当永久失败,而 SSRF 那三层更是重写不出来。
    """
    imported = set()
    for node in ast.walk(_tree(EXTRACTOR_VISION)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "app.llm.transport" in imported
    assert "app.llm.images" in imported
    # `from app.llm import endpoint_trust` 记的模块是 `app.llm`,所以两种写法都认
    names = set()
    for node in ast.walk(_tree(EXTRACTOR_VISION)):
        if isinstance(node, ast.ImportFrom):
            names.update(a.name for a in node.names)
    assert "app.llm.endpoint_trust" in imported or "endpoint_trust" in names


def test_the_extractor_never_calls_an_http_client_directly():
    """往返只能经传输层。直接 import httpx 就是绕过了重试与错误归类。"""
    for node in ast.walk(_tree(EXTRACTOR_VISION)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(("httpx", "requests", "urllib.request")), (
                    f"识别层直接引了 {alias.name},退避与错误归类会被绕过"
                )
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith(("httpx", "requests", "urllib.request"))


def test_both_layers_ask_the_same_module_whether_a_key_is_required():
    """A45-#36 那个缺陷不该在第二份实现里重生。

    两层各写一份主机判定的表现是:某一天有人在其中一份里修好了
    `10.gpu.example.com`,另一份继续把它判成私网 —— 而两份都有测试。
    """
    from app.evaluators.vision import VisionEvaluatorConfig
    from app.extractors.vision import ExtractorModelConfig

    for host, expected in (
        ("https://10.gpu.example.com/v1", True),
        ("http://127.0.0.1:8000/v1", False),
        ("http://10.0.0.5:8000/v1", False),
        ("http://ollama:11434/v1", False),
        ("https://api.openai.com/v1", True),
    ):
        a = ExtractorModelConfig(base_url=host).requires_api_key
        b = VisionEvaluatorConfig(base_url=host).requires_api_key
        assert a == b == expected, f"{host}: 识别 {a} / 评分 {b},期望 {expected}"


def test_both_layers_bill_the_same_endpoint_to_the_same_vendor():
    """同一个端点的识别费和评分费必须落在 `/spend` 页的同一个厂商下。

    各推各的话,同一笔预算会被拆成两行,而"这家厂商这个月花了多少"
    再也答不上来。
    """
    from app.evaluators.vision import VisionEvaluatorConfig, VisionModelImageQualityEvaluator
    from app.extractors.vision import ExtractorModelConfig, VisionAttributeExtractor

    url = "https://api.openai.com/v1"
    left = VisionAttributeExtractor(ExtractorModelConfig(base_url=url)).usage_provider
    right = VisionModelImageQualityEvaluator(
        VisionEvaluatorConfig(base_url=url)
    ).usage_provider
    assert left == right == "openai"


def test_an_unrecognisable_endpoint_does_not_get_a_guessed_vendor_name():
    """推不出来时退回自己的名字,不猜 —— 猜出来的厂商名会让人去对不存在的账单。"""
    from app.extractors.vision import ExtractorModelConfig, VisionAttributeExtractor

    extractor = VisionAttributeExtractor(ExtractorModelConfig(base_url="http://gpu-box:8000"))
    assert extractor.usage_provider == "vision"


# ================================================================ Schema 与解析


def test_the_schema_carries_the_missing_channel():
    from app.extractors.schema import build_extraction_schema

    schema = build_extraction_schema(("primary_color",))
    assert "missing" in schema["required"]
    reasons = schema["properties"]["missing"]["items"]["properties"]["reason"]["enum"]
    assert "INSUFFICIENT_EVIDENCE" in reasons


def test_the_model_may_not_claim_a_field_awaits_supplier_data():
    """`AWAITING_SUPPLIER_DATA` 的意思是"看图永远判断不了"。

    而非视觉字段根本不进识别 Schema —— 模型没有资格对一个我们没问它的
    字段宣布"等供应商数据"。留在枚举里是给导入与人工标记那条路用的。
    """
    from app.extractors.schema import build_extraction_schema

    schema = build_extraction_schema(("primary_color",))
    reasons = schema["properties"]["missing"]["items"]["properties"]["reason"]["enum"]
    assert "AWAITING_SUPPLIER_DATA" not in reasons
    # 但枚举本身要有它,否则导入侧无处可落
    assert MissingReason.AWAITING_SUPPLIER_DATA.value == "AWAITING_SUPPLIER_DATA"


def test_the_api_ready_schema_drops_the_prompt_only_hints():
    """`_enum_hints` 是给提示词用的。发出去会被 strict 校验整个拒掉。"""
    from app.extractors.schema import api_ready_schema, build_extraction_schema

    schema = build_extraction_schema(("primary_color",))
    assert "_enum_hints" in schema
    assert "_enum_hints" not in api_ready_schema(schema)


def test_the_api_ready_schema_satisfies_strict_mode_required_rules():
    """strict json_schema 要求每层 required 列全部 properties。

    少一个的表现是整个请求被 400 拒掉,而 400 不重试 —— 一次识别当场失败,
    错误信息是厂商那句含糊的 schema 校验失败。
    """
    from app.extractors.schema import api_ready_schema, build_extraction_schema

    ready = api_ready_schema(build_extraction_schema(("primary_color",)))
    items = ready["properties"]["fields"]["items"]
    assert set(items["required"]) == set(items["properties"])


def test_the_api_ready_schema_does_not_mutate_its_input():
    """就地改的话,同一个进程里第二次构建拿到的是被改过的 Schema。"""
    from app.extractors.schema import api_ready_schema, build_extraction_schema

    schema = build_extraction_schema(("primary_color",))
    before = list(schema["properties"]["fields"]["items"]["required"])
    api_ready_schema(schema)
    assert schema["properties"]["fields"]["items"]["required"] == before
    assert "_enum_hints" in schema


def test_prose_is_rejected_instead_of_becoming_an_empty_result():
    """fail-closed:解析不了就抛错,**不返回空结果**。

    返回空结果的表现是"这张图识别成功但一个字段都没有",而它和
    "模型真的什么都没看出来"在库里长得一模一样 —— 前者该重试,后者该换图。
    """
    from app.extractors.schema import ExtractionParseError, parse_extraction_payload

    expect_raises(ExtractionParseError, parse_extraction_payload, "我觉得这件是藏青色的。")
    expect_raises(ExtractionParseError, parse_extraction_payload, "")
    expect_raises(ExtractionParseError, parse_extraction_payload, '{"fields": ')
    expect_raises(ExtractionParseError, parse_extraction_payload, '["not", "an", "object"]')


def test_a_fenced_json_block_is_tolerated():
    """```json 围栏是模型最常见的画蛇添足,不该因此判定整张图失败。"""
    from app.extractors.schema import parse_extraction_payload

    parsed = parse_extraction_payload(
        '```json\n{"fields": [{"name": "primary_color", "value": "NAVY", '
        '"confidence": 0.8}], "unreadable_fields": [], "missing": []}\n```'
    )
    assert parsed.fields[0].name == "primary_color"


def test_an_out_of_range_confidence_is_refused_not_clamped():
    """置信度越界是模型没按契约走,夹到 1.0 会把一个坏信号伪装成满分证据。"""
    from app.extractors.schema import ExtractionParseError, parse_extraction_payload

    expect_raises(
        ExtractionParseError,
        parse_extraction_payload,
        '{"fields": [{"name": "primary_color", "value": "NAVY", "confidence": 7}]}',
    )


def test_a_missing_confidence_does_not_kill_the_whole_image():
    """json_object / prompt_only 档位没有 Schema 兜着,漏一个 confidence
    不该毁掉整张图的其余字段 —— None 在合并层的含义是权重 0,
    fail-closed 语义没有松动。"""
    from app.extractors.schema import parse_extraction_payload

    parsed = parse_extraction_payload(
        '{"fields": [{"name": "primary_color", "value": "NAVY"}]}'
    )
    assert parsed.fields[0].confidence is None


def test_the_parser_does_not_quietly_drop_unknown_fields():
    """解析层**只解析**。过滤是 `run_extraction` 的事,它要数编造。

    在这里顺手丢掉的话,§6.3 的"不可见属性编造率"永远数不出来 ——
    被丢掉的东西没有留下痕迹。
    """
    from app.extractors.schema import parse_extraction_payload

    parsed = parse_extraction_payload(
        '{"fields": [{"name": "totally_made_up", "value": "X", "confidence": 0.9}]}'
    )
    assert [f.name for f in parsed.fields] == ["totally_made_up"]


# ================================================================ 请求形状


def _build_request(style="responses", fmt="json_schema"):
    from app.extractors.schema import api_ready_schema, build_extraction_schema
    from app.extractors.vision import ExtractorModelConfig, build_adapter
    from app.llm.images import PreparedImage

    config = ExtractorModelConfig(
        base_url="https://api.example.com/v1",
        model="m",
        api_key="k",
        api_style=style,
        response_format=fmt,
    )
    image = PreparedImage(
        role="PRODUCT_IMAGE",
        mime_type="image/png",
        byte_size=3,
        data_url="data:image/png;base64,AAA",
    )
    return build_adapter(config).build(
        prompt="p",
        image=image,
        schema=api_ready_schema(build_extraction_schema(("primary_color",))),
    )


def test_the_request_never_carries_known_values():
    """§6.1:不告诉模型已有的值。

    模型看到 `{"primary_color": "NAVY"}` 之后就会把图判成 NAVY ——
    所谓交叉验证只是复读我们喂进去的答案。比对必须由后端纯函数做。
    """
    import json

    for style in ("responses", "chat_completions"):
        body = json.dumps(_build_request(style=style).body, ensure_ascii=False)
        for leak in ("known", "current_value", "已有值"):
            assert leak not in body, f"{style} 的请求体里出现了 {leak}"


def test_both_api_styles_hit_their_own_endpoint():
    assert _build_request("responses").url.endswith("/responses")
    assert _build_request("chat_completions").url.endswith("/chat/completions")


def test_the_endpoint_does_not_double_the_version_segment():
    """运维习惯把 /v1 写进 base_url,而文档里的端点也带 /v1。拼出 /v1/v1 换来一个
    404,而 404 在这一层看起来像"模型名写错了",能查很久。"""
    assert "/v1/v1/" not in _build_request().url


def test_the_api_key_never_leaves_the_authorization_header():
    """Key 只能出现在头里。进了 body 就会跟着请求摘要写进日志和留档。"""
    import json

    request = _build_request()
    assert request.headers["Authorization"] == "Bearer k"
    body = json.dumps(request.body)
    assert "Bearer" not in body
    assert '"k"' not in body.replace('"model": "m"', "")


def test_the_safe_summary_hides_the_image_payload():
    """日志里不许出现 base64。这张表要长期留存。"""
    summary = _build_request().safe_body_summary()
    blob = repr(summary)
    assert "AAA" not in blob and "base64" not in blob


def test_json_object_mode_does_not_send_a_schema():
    """端点报"不支持 json_schema"时逐级降级,而降级必须真的不发 Schema ——
    发了会换来同一个 400,于是"降级"看起来无效。"""
    body = _build_request(fmt="json_object").body
    assert body["text"] == {"format": {"type": "json_object"}}
    chat = _build_request("chat_completions", "json_object").body
    assert chat["response_format"] == {"type": "json_object"}


def test_prompt_only_mode_sends_neither():
    body = _build_request(fmt="prompt_only").body
    assert "text" not in body
    assert "response_format" not in _build_request("chat_completions", "prompt_only").body


def test_reasoning_models_do_not_receive_temperature():
    """推理模型普遍拒收 temperature,无条件发送换来一个 400,而 400 不重试。"""
    from app.extractors.vision import ExtractorModelConfig, build_adapter
    from app.llm.images import PreparedImage

    image = PreparedImage(
        role="PRODUCT_IMAGE",
        mime_type="image/png",
        byte_size=3,
        data_url="data:image/png;base64,AAA",
    )
    config = ExtractorModelConfig(
        base_url="https://api.example.com/v1", model="m", api_key="k", reasoning_effort="low"
    )
    body = build_adapter(config).build(prompt="p", image=image, schema={}).body
    assert "temperature" not in body and body["reasoning"] == {"effort": "low"}

    plain = build_adapter(
        ExtractorModelConfig(
            base_url="https://api.example.com/v1",
            model="m",
            api_key="k",
            reasoning_effort="none",
        )
    ).build(prompt="p", image=image, schema={}).body
    assert plain["temperature"] == 0 and "reasoning" not in plain


# ================================================================ 迁移


def test_the_migration_chain_reaches_0036():
    text = MIGRATION_0036.read_text(encoding="utf-8")
    assert 'revision: str = "0036"' in text
    assert 'down_revision: Union[str, None] = "0035"' in text


def test_the_downgrade_removes_exactly_what_the_upgrade_added():
    """按 AST 比对升级加了什么、降级撤了什么。

    它挡得住漏写,挡不住写错 —— 后者只有真库能验(已写、未执行)。
    """
    up = _func(MIGRATION_0036, "upgrade")
    down = _func(MIGRATION_0036, "downgrade")

    def _ops(node, name: str):
        """取 (表名, 列名)。**按 AST 取,不按字符串找** —— 注释里出现列名不算。"""
        out = []
        for child in ast.walk(node):
            if not (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == name
            ):
                continue
            table = child.args[0].value
            second = child.args[1]
            if name == "add_column":
                # sa.Column("列名", ...) 的第一个位置参数
                column = second.args[0].value
            else:
                column = second.value
            out.append((table, column))
        return out

    added = _ops(up, "add_column")
    dropped = _ops(down, "drop_column")
    assert sorted(added) == sorted(dropped), f"升级 {added} / 降级 {dropped}"
    assert len(added) == 2


def test_the_new_columns_do_not_claim_history_they_never_had():
    """`missing_reason` 不许有默认值:给它一个等于替模型宣布
    "所有旧证据都说过看不清",而旧证据什么都没说过。"""
    up = _func(MIGRATION_0036, "upgrade")
    checked = False
    for node in ast.walk(up):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_column"
        ):
            continue
        column = node.args[1]
        if column.args[0].value != "missing_reason":
            continue
        checked = True
        kwargs = {k.arg for k in column.keywords}
        assert "server_default" not in kwargs, (
            "给 missing_reason 一个默认值,等于替所有旧证据宣布它们说过看不清"
        )
    assert checked, "找不到 missing_reason 那次 add_column"
    # **第一版这条按字符串切片取"两个列名之间的那段",而迁移的文档字符串里
    # 恰好按同样顺序提到这两个名字 —— 它比较的是一段文档。**
    # 变异验证当场抓到(与 batch13 的 M15 同型:比较的其实是两句注释)。


# ================================================================ 自身完整性


def test_this_file_is_not_accidentally_importing_pytest():
    """`tests/pure/` 里 import pytest 会让整个文件在导入期炸掉,而运行器
    只会把它记成**一条**失败 —— 9 条用例一条都没跑过,套件却显示只差一条。

    判据按 AST 看真正的 import 语句。第一版按子串找,而**这段文档字符串
    里就写着那三个字**,于是它比较的是一句注释 —— 与 batch13-2 里
    M15 抓到的那种"守卫在验错的东西"是同一个形状。
    """
    tree = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] != "pytest"
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] != "pytest"
