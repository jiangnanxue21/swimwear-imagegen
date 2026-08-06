"""A45-batch14-18:计费量问厂商要,并记下这个数是谁说的(任务 9,阶段 1)。

## 这一批修的是什么

`billable_units` 从落地起就是**我们数出来的**:`max(provider_count, 1)`,
也就是"Provider 返回了几张候选图"。而 FASHN 每一次 status 响应都在
`x-fashn-credits-used` 头里报了它实际扣了多少额度 —— 那个数被
`providers/fashn.py` 解析出来、抄在候选图的 metadata 上,然后**全仓再没有
第二处读它**(本批之前 grep 只剩测试)。

两个数不是同一个量。官方参考表(`docs/vendor/fashn-skill/reference.md`
「Credits」)::

    tryon-max  balanced  1k:2  2k:3  4k:4     (× num_images)
               quality   1k:3  2k:4  4k:5

一张图 2 到 5 个额度,取决于 `FASHN_RESOLUTION` 与 `generation_mode` ——
**两个旋钮运营在设置页都能改**。所以台账少记的倍数不是常数,是跟着配置浮动
的系数,方向恒定是**少记**:预算横幅一路绿着,账单是它的好几倍。

§10.2 第 5 条准入(「用量记录与 Provider 后台账单条数一致」)按旧写法不可能
对得上,对上了是运气。

## 这一批能验到什么、验不到什么

判定(`settle_billable_units` / `usage_from_candidates`)是纯的、零依赖,
所以下面大半条守卫是**真的调函数**得出的结论,不是 AST 形状。

**验不到的是端点行为**:FASHN 到底会不会在每一次 status 响应上都带那个头、
带的是累计值还是本次值。这里守的是"拿到之后怎么算",不是"厂商真的会这么答"。
第一次接真端点时要盯的三件事写在 `docs/STATUS.md` 本批块。

## 反向断言一律走 AST,不切文本窗口

本文件里唯一的反向断言(`..._still_cannot_report_are_named_here`)第一版是切
源码的,被 `_helpers.window()` 挡了回来 —— 那个终点在文件里出现 21 次。
改成读调用节点的实参名单:一个精确集合,没有"切窄之后静默变真"这条失效路径。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from app.core.enums import UnitsSource
from app.providers.base import (
    GenerationCandidate,
    ImageGenerationProvider,
    ProviderUsage,
    settle_billable_units,
)
from app.providers.fashn import CREDITS_HEADER, FashnImageGenerationProvider
from app.providers.registry import PROVIDER_FACTORIES
from tests.pure._helpers import BACKEND_ROOT

APP = BACKEND_ROOT / "app"
MIGRATIONS = BACKEND_ROOT / "migrations" / "versions"

MODEL = APP / "models" / "generation.py"
LEDGER = APP / "services" / "generation_service.py"
TASKS = APP / "tasks" / "generation_tasks.py"
MIGRATION = MIGRATIONS / "0039_usage_units_source.py"

#: 本批落的那一列。名字在四个地方出现(ORM、迁移、判定、接线),
#: 写成常量是为了让"改名字"这件事在这份守卫里只需要改一处 ——
#: 改一处就能全绿的守卫才是钉性质的守卫。
COLUMN = "units_source"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _fn(path: Path, name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(_src(path))):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里找不到函数 {name}()")


def _candidate(prediction_id: str | None, credits: object, index: int = 0):
    """造一张 FASHN 形状的候选图。

    `metadata` 的三个键与 `fashn.fetch_results` 写的那三个一致 ——
    夹具比生产多写一个键的话,守卫会在生产少写一个键时仍然全绿。
    """
    metadata: dict[str, object] = {"provider": "fashn"}
    if prediction_id is not None:
        metadata["prediction_id"] = prediction_id
    if credits is not _MISSING:
        metadata["credits_used"] = credits
    return GenerationCandidate(
        external_id=f"{prediction_id}#{index}",
        image_url="https://example.invalid/x.png",
        local_path=None,
        metadata=metadata,
    )


class _Missing:
    pass


_MISSING = _Missing()


# ==========================================================
# 一、谁的数说了算
# ==========================================================


def test_the_amount_billed_prefers_what_the_provider_reported():
    """厂商报了数就用厂商的,推算值只是它没报时的退路。

    这一条是整批的落点。反过来(推算值优先)的表现是:接了线、解析了响应头、
    测试全绿,而台账上的数字一个都没变。
    """
    units, source = settle_billable_units(ProviderUsage(units=8), inferred=4)
    assert units == 8, f"厂商报了 8 个额度,台账记了 {units}"
    assert source == UnitsSource.PROVIDER.value


def test_a_silent_provider_falls_back_to_the_count_and_says_so():
    """厂商没报时退回推算,**并且如实标记**。

    只退回不标记的话,一张全是估算的表看起来和账单同源 —— 对账的人拿它
    逐行核,核不上再回来翻代码才发现问题出在口径上。
    """
    for usage in (ProviderUsage(), ProviderUsage(units=None), None):
        units, source = settle_billable_units(usage, inferred=4)
        assert units == 4, f"{usage!r} 应该退回推算的 4,得到 {units}"
        assert source == UnitsSource.INFERRED.value, (
            f"{usage!r} 退回了推算值却把来源标成 {source} —— "
            "一行估算冒充和账单同源,比数字错了更难查"
        )


def test_a_reported_zero_is_not_the_same_as_no_report():
    """厂商说"这次没收钱"和厂商没说话,是两件事。

    合并的表现:一次真的免费的调用被退回推算值记成 1,金额凭空出现;
    或者反过来,一次没读到数的调用被当成 0,金额凭空消失。后者更糟 ——
    少记钱这个方向的错误没有人会去发现。
    """
    units, source = settle_billable_units(ProviderUsage(units=0), inferred=3)
    assert (units, source) == (0, UnitsSource.PROVIDER.value), (
        "厂商报的 0 被当成了「没报」,于是退回推算记了一笔本来不存在的钱"
    )


def test_the_settlement_never_produces_a_negative_amount():
    """负数进不去台账。`UnitPrice.cost_for()` 对负数是抛异常的,
    而那个异常会在写流水的路径上炸掉 —— 收尾动作自己炸掉,正好毁掉它要修的东西
    (`spend._price_book_or_empty` 花了一整段说这件事)。
    """
    assert settle_billable_units(ProviderUsage(units=-5), inferred=2)[0] == 0
    assert settle_billable_units(None, inferred=-1)[0] == 0


def test_every_source_the_settlement_can_return_is_a_declared_one():
    """判定只吐得出 `UnitsSource` 里有的取值。

    第三种取值不会让任何代码报错,只会让一行流水从两个口径的统计里同时消失,
    而 CHECK 约束要到写库那一刻才拦得住 —— 那时错误已经跑过一整条链路了。
    """
    declared = {s.value for s in UnitsSource}
    cases = [
        (ProviderUsage(units=7), 3),
        (ProviderUsage(units=0), 3),
        (ProviderUsage(), 3),
        (None, 0),
    ]
    for usage, inferred in cases:
        _, source = settle_billable_units(usage, inferred=inferred)
        assert source in declared, f"{usage!r} 得出了未声明的来源 {source!r}"


# ==========================================================
# 二、FASHN:一条 prediction 的额度只能算一次
# ==========================================================


def test_one_predictions_credits_are_counted_once_not_once_per_image():
    """**本批最要紧的一条。**

    `fetch_results` 把每条 prediction 的额度抄在**它产出的每一张**候选图上
    (那是给排查用的现场,不是给求和用的)。于是 `num_images=4` 那一次,
    4 张候选各自带着同一个 per-prediction 总额 —— 直接 `sum()` 就是 4 倍高估。

    这个坑今天还没人踩进去,因为本批之前没有人读过这个字段。这条守卫存在的
    意义正是:读法只有一处,而这一处按 `prediction_id` 去重。
    """
    provider = FashnImageGenerationProvider()
    four_images_one_prediction = [_candidate("pred-1", 8, i) for i in range(4)]

    usage = provider.usage_from_candidates(four_images_one_prediction)
    naive_sum = sum(c.metadata["credits_used"] for c in four_images_one_prediction)

    assert usage.units == 8, (
        f"一条 prediction 报了 8 个额度,算出来是 {usage.units} —— "
        f"按张数求和会得到 {naive_sum},那是 4 倍高估"
    )
    assert naive_sum == 32, "夹具本身塌了:四张候选没有各自带着同一个总额"


def test_separate_predictions_each_contribute_their_own_credits():
    """反过来也要成立:`product-to-model` 那种 4 次独立提交要加起来。

    去重去过头(只取第一条)的表现是台账只记 1/4 的钱,而它和上面那条
    错在相反方向 —— 一条守卫只钉一边的话,修 A 会顺手打开 B。
    """
    provider = FashnImageGenerationProvider()
    four_predictions = [_candidate(f"pred-{i}", 2, 0) for i in range(4)]
    assert provider.usage_from_candidates(four_predictions).units == 8


def test_a_prediction_that_produced_images_without_reporting_voids_the_whole_total():
    """有一条没带额度,整笔就不算厂商自述。

    偏小的和看起来和真数一模一样,而少记钱这个方向没有人会去发现。
    所以退回推算并标 `inferred`,让对账的人知道这一行要自己去查。
    """
    provider = FashnImageGenerationProvider()
    cases = {
        "缺 credits_used": [_candidate("a", 2), _candidate("b", _MISSING)],
        "credits_used 是 None": [_candidate("a", 2), _candidate("b", None)],
        "credits_used 不是整数": [_candidate("a", 2), _candidate("b", "3")],
        "缺 prediction_id(分不了组)": [_candidate("a", 2), _candidate(None, 2)],
    }
    for label, candidates in cases.items():
        usage = provider.usage_from_candidates(candidates)
        assert usage.units is None, (
            f"{label}:仍然报出了 {usage.units} —— 那是一个偏小的和,"
            "而它看起来和真数一模一样"
        )
        assert settle_billable_units(usage, inferred=2) == (
            2,
            UnitsSource.INFERRED.value,
        )


def test_no_candidates_at_all_is_not_a_vendor_reported_zero():
    """一张图都没有时不冒充厂商权威。

    文档写着「失败的预测不计费」,所以"全失败 = 0 额度"多半是对的。
    但**我们没读到那个 0**,是自己推出来的。冒充厂商去写一个 0,
    和写一个偏小的数是同一类错误,只是更彻底。
    """
    usage = FashnImageGenerationProvider().usage_from_candidates([])
    assert usage.units is None
    assert settle_billable_units(usage, inferred=1) == (1, UnitsSource.INFERRED.value)


def test_the_credits_header_the_parser_reads_is_the_documented_one():
    """响应头名字和厂商文档对得上。

    改错一个字母的表现是:解析恒为 None、每一笔都退回推算、
    **而且没有任何地方报错** —— 台账安静地退回本批之前的样子。
    """
    reference = (
        BACKEND_ROOT.parent / "docs" / "vendor" / "fashn-skill" / "reference.md"
    ).read_text(encoding="utf-8")
    assert CREDITS_HEADER == "x-fashn-credits-used"
    assert CREDITS_HEADER in reference, (
        f"{CREDITS_HEADER!r} 在官方参考里查不到 —— "
        "本文件每一个字段名都必须来自 docs/vendor,不许凭记忆写"
    )


def test_a_provider_that_does_not_override_the_hook_never_claims_vendor_authority():
    """没覆写钩子的 Provider 一律报"没报"。

    和 `is_simulator = True` 是同一个取舍,两个方向的错误代价不对称:

        新 Provider 忘了覆写   台账退回推算并标 `inferred`,对账时看得见
        默认冒充厂商权威       一张全是猜的表被当成和账单同源,而没有任何
                               地方会说它不是
    """
    assert ImageGenerationProvider.usage_from_candidates(
        ImageGenerationProvider, []
    ).units is None

    overriding = {
        name
        for name, factory in PROVIDER_FACTORIES.items()
        if getattr(factory, "usage_from_candidates", None)
        is not ImageGenerationProvider.usage_from_candidates
    }
    assert overriding == {"fashn"}, (
        f"覆写了用量钩子的 Provider 是 {sorted(overriding)},预期只有 fashn。"
        "新增一家时:先给它写一条自己的守卫,再来改这一行 —— "
        "改这一行让它变绿是最省事的做法,而那等于删掉了这条守卫"
    )


# ==========================================================
# 三、接线:算好了要有人调、有人写
# ==========================================================


def test_the_ledger_call_after_persisting_passes_both_the_amount_and_its_source():
    """落库之后那条流水必须同时传金额与来源。

    只传金额的表现:厂商的真数进了库,而来源留在默认的 `inferred` ——
    一行**数对了但标错来源**的流水,比两个都错更难查。
    """
    fn = _fn(TASKS, "_await_and_collect")
    submit_calls = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and "record_usage" in ast.unparse(node.func)
        and any(
            kw.arg == "operation" and ast.unparse(kw.value) == "'submit'"
            for kw in node.keywords
        )
    ]
    assert submit_calls, "找不到 operation='submit' 的用量流水,这份守卫的假设过期了"
    for call in submit_calls:
        passed = {kw.arg for kw in call.keywords}
        assert {"billable_units", COLUMN} <= passed, (
            f"submit 流水少传了 {{'billable_units', COLUMN}} - passed = "
            f"{ {'billable_units', COLUMN} - passed}"
        )


def test_the_wiring_asks_the_provider_before_falling_back_to_counting():
    """接线必须**先问厂商**,而不是直接把推算值塞进去。

    `settle_billable_units()` 是唯一做这个选择的地方。绕过它自己判一遍的
    表现是:两处口径迟早分叉,而分叉的那天两边都还是绿的。
    """
    body = ast.unparse(_fn(TASKS, "_await_and_collect"))
    assert "usage_from_candidates" in body, (
        "接线没有问过 Provider 自己报了多少 —— "
        "解析响应头的代码还在,只是又一次没有人读它"
    )
    assert "settle_billable_units" in body, (
        "接线绕过了唯一那处判定,自己判了一遍谁说了算"
    )


def test_record_usage_defaults_to_inferred_so_a_forgotten_wire_up_stays_honest():
    """`record_usage` 的 `units_source` 默认是 `inferred`。

    默认成 `provider` 的表现:任何一个没接线的调用点(轮询、取结果、评分、
    属性识别 —— 今天有四类)都会安静地声称自己和账单同源。
    """
    fn = _fn(LEDGER, "record_usage")
    defaults = dict(
        zip(
            [a.arg for a in fn.args.kwonlyargs],
            [None if d is None else ast.unparse(d) for d in fn.args.kw_defaults],
        )
    )
    assert COLUMN in defaults, f"record_usage 没有 {COLUMN} 这个关键字参数"
    assert defaults[COLUMN] is not None, f"{COLUMN} 没有默认值,忘了传就会炸在写库那一刻"
    assert "INFERRED" in defaults[COLUMN], (
        f"{COLUMN} 的默认值是 {defaults[COLUMN]} —— 默认必须是"
        "「我们数的」,不是「厂商说的」"
    )


def test_the_update_path_moves_the_source_along_with_the_amount():
    """同一笔消费第二次记账时,来源跟着金额一起更新。

    `billing_key` 那条路径是「有则更新」。恢复那一遍如果拿到了厂商自述而
    第一遍没有,金额会变成厂商的数而来源留在 `inferred` —— 又是一行
    **数对了但标错来源**的流水,且它恰好出现在恢复过的那批任务上,
    也就是最需要被看清楚的一批(NEW-01 的原话)。
    """
    fn = _fn(LEDGER, "record_usage")
    updated = {
        ast.unparse(target)
        for node in ast.walk(fn)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if ast.unparse(target).startswith("record.")
    }
    assert f"record.{COLUMN}" in updated, (
        f"更新路径没有改 record.{COLUMN};已改的字段:{sorted(updated)}"
    )


# ==========================================================
# 四、列:ORM 与迁移必须逐字对得上
# ==========================================================


def test_the_orm_column_and_the_migration_agree_on_the_units_source_column():
    """ORM 声明的列和迁移建的列必须同名、同默认值、同 CHECK 名。

    两边不一致时 `create_all()` 建出来的表(测试夹具走的正是那条路)和迁移
    建出来的表会长得不一样,而下一次 autogenerate 会提议 DROP 其中一个 ——
    这正是 `ix_provider_usage_records_created_provider` 那条索引踩过的坑,
    模型注释里还写着。

    静态比对而不是跑 alembic:这台机器没有 sqlalchemy,而
    `test_upgrade_matches_orm_metadata`(真库)只比列名,兜不住默认值与约束。
    """
    model = _src(MODEL)
    migration = _src(MIGRATION)

    assert f"{COLUMN}: Mapped[str]" in model, f"ORM 里找不到 {COLUMN} 列"
    assert f'"{COLUMN}"' in migration or f"_COLUMN = \"{COLUMN}\"" in migration, (
        f"迁移里找不到 {COLUMN} 列"
    )

    check_name = "ck_provider_usage_records_units_source"
    assert check_name in model, "ORM 的 __table_args__ 里没有那条 CHECK"
    assert check_name in migration, "迁移里没有那条 CHECK"

    for value in (UnitsSource.PROVIDER.value, UnitsSource.INFERRED.value):
        assert value in migration, (
            f"迁移的取值清单里没有 {value!r} —— "
            "CHECK 会把一个合法取值当成非法的拒掉"
        )

    assert f"server_default={UnitsSource.INFERRED.value!r}" in model.replace(
        "server_default=UnitsSource.INFERRED.value",
        f"server_default={UnitsSource.INFERRED.value!r}",
    ), "ORM 的 server_default 不是 inferred"
    assert "_INFERRED = \"inferred\"" in migration, (
        "迁移的存量回填值不是 inferred —— 存量行全部出自 max(provider_count, 1),"
        "标成别的等于替过去的账做一个今天才定下来的判断"
    )


def test_the_migration_hangs_off_the_previous_head_instead_of_forking():
    """0039 挂在 0038 后面。

    单 head 由 `verify_delivery.py` 守着,这里钉的是**挂对了地方** ——
    重新挂到更早的节点上照样是单 head,而那会让两条线的列互相盖掉。
    """
    migration = _src(MIGRATION)
    revision = re.search(r'^revision:\s*str\s*=\s*"([^"]+)"', migration, re.M)
    down = re.search(r'^down_revision:[^=]*=\s*"([^"]+)"', migration, re.M)
    assert revision and revision.group(1) == "0039"
    assert down and down.group(1) == "0038", (
        f"0039 的 down_revision 是 {down.group(1) if down else None},预期 0038"
    )


def test_the_migration_does_not_backfill_by_guessing():
    """存量回填走 `server_default`,不去猜历史行的真实来源。

    这一条和迁移 0034 拒绝回填 `billing_key` **不矛盾**:那批行的正确取值
    当时就不可知,而这里是确定可知的 —— 读厂商额度的代码在本批之前不存在,
    所以每一条历史流水都出自 `max(provider_count, 1)`。

    钉的是"不许出现一条按业务条件挑行回填的 UPDATE":那种回填等于替过去的账
    做一个今天才定下来的判断。
    """
    upgrade = ast.unparse(_fn(MIGRATION, "upgrade"))
    assert "server_default" in upgrade, "没有 server_default,存量行会违反 NOT NULL"
    assert "UPDATE" not in upgrade.upper(), (
        f"upgrade() 里有一条 UPDATE:\n{upgrade}\n"
        "存量来源由 server_default 一次性写定,按条件挑行回填是在猜"
    )


# ==========================================================
# 五、记账:今天还报不出来的那几类,点名留着
# ==========================================================


def test_the_paid_paths_that_still_cannot_report_are_named_here():
    """**这条守卫是一笔账,不是一条不变式。**

    今天只有 FASHN 的生成调用报得出 `provider`。另外四类付费调用仍然全是
    `inferred`,而它们各自的理由不一样:

        轮询 / 取结果      厂商每次真的又调了一次,但那几次不单独计费
        评分(vision)      响应体里有 usage,**没接** —— 见下
        属性识别(vision)  同上

    vision 那两类刻意不在本批接:它们的计价单位是 token 而不是"次",
    而价目表 `PROVIDER_PRICE_BOOK` 里 `attribute_extract` / `vision_score`
    今天配的是**每次调用**的价。改成 token 会在不改任何配置的情况下
    **静默改变已配价目表的含义** —— 金额一夜之间差几个数量级,
    而没有任何地方会说为什么。那是另一批的活。

    接线那天这条会红,那时把 STATUS 的欠账表改掉并删掉它。

    ## 为什么用 AST 而不是切一段源码

    第一版是 `window(source, "record_usage(\\n", "\\n    )")`,被 `_helpers.window()`
    当场挡了回来:那个终点在文件里出现 21 次。这正是它存在的理由 ——
    切窄之后**反向断言会静默变真**,而这条守卫整条就是一个反向断言。
    调用的实参名单本来就是 AST 上的一个精确集合,没必要退回文本匹配。
    """
    for path in (
        APP / "attributes" / "service.py",
        APP / "services" / "evaluation_service.py",
    ):
        calls = [
            node
            for node in ast.walk(ast.parse(_src(path)))
            if isinstance(node, ast.Call)
            and ast.unparse(node.func).split(".")[-1] == "record_usage"
        ]
        assert calls, f"{path.name} 里找不到 record_usage 调用,这笔账的假设过期了"
        for call in calls:
            passed = {kw.arg for kw in call.keywords}
            assert COLUMN not in passed, (
                f"{path.name} 已经开始传 {COLUMN} 了 —— 这笔账该销了:"
                "更新 docs/STATUS.md 的欠账表,并删掉这条守卫"
            )
