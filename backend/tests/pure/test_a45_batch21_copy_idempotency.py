"""A45-batch21(阶段 5 批次 5-3):文案生成的幂等单元。

## 这一批收的是一笔正在花钱的账

`listing_copies` 从第一版起就是 SPU 粒度的,而入口是 SKU 粒度的。
一个 S/M/L 三码的 SPU,运营在三行上各点一次「生成文案」= 三次真实 LLM 调用、
三个版本,输入完全相同。批量那条路早就按 SPU 去重了
(`batch.ACTION_SCOPE[GENERATE_COPY] == "spu"`),**缺口只长在单件入口上** ——
而单件入口恰恰是运营日常用的那一个。

## 守卫分四段

    键的成分   尺码/SKU/product_id **不许**进键(这是本模块存在的全部理由)
    键的稳定   同一份输入必然同一个键;事实集合的顺序与重复不影响它
    复用口径   失败态不占槽位;存量 NULL 判「重新生成」不判「复用」
    接线       判定真的挡在付费调用之前,而不是算完了没人看
"""
from __future__ import annotations

import ast

from app.workflows import copy_idempotency as ci
from tests.pure._helpers import BACKEND_ROOT

LAYER = BACKEND_ROOT / "app" / "workflows" / "copy_idempotency.py"
SERVICE = BACKEND_ROOT / "app" / "workbench" / "service.py"
MIGRATION = BACKEND_ROOT / "migrations" / "versions" / "0050_copy_idempotency_key.py"


def _unit(**over):
    base = dict(
        spu="SPU-1",
        channel="GENERIC",
        site="ANY",
        locale="en",
        fact_versions=("v1", "v2"),
    )
    base.update(over)
    return ci.unit_for(**base)


# ---------------------------------------------------------------- 一、键的成分


def test_the_size_never_reaches_the_key():
    """**本文件的第一条。** 尺码不进键 —— 这是整个批次存在的理由。

    构造上就做不到:`unit_for()` 的签名里没有尺码这个位置。所以这条钉的是
    那个签名,而不是"传了尺码算出同一个键"(传不进去)。

    退化的样子很具体:有人为了"让不同尺码各有一版文案"给它加一个
    `sku=` 或 `product_id=` 参数。加上那一刻,一个 S/M/L 三码的 SPU
    又变回三次付费调用,而**测试全绿** —— 键仍然是稳定的、复用仍然工作,
    只是每个尺码各占一个单元。
    """
    import inspect

    params = set(inspect.signature(ci.unit_for).parameters)
    for banned in ("size", "sku", "product_id", "product"):
        assert banned not in params, (
            f"`unit_for()` 收了 `{banned}` —— 幂等单元一旦按尺码分裂,"
            "S/M/L 三行又会各花一次钱,而这件事不会有任何测试变红"
        )
    fields = set(ci.CopyUnit.__dataclass_fields__)
    assert fields == {
        "spu",
        "channel",
        "site",
        "locale",
        "fact_versions",
        "color_variant_id",
    }, f"单元的字段集变了:{fields}"


def test_two_sizes_of_the_same_spu_land_in_one_unit():
    """同一 SPU 的 S/M/L 算出同一个键(AC-11 的正面表述)。

    这里不构造尺码 —— 构造不出来。它验的是同一份输入的**幂等**:
    三次调用传的是同一个 SPU、同一语言、同一事实集,于是同一个键。
    """
    assert _unit().key() == _unit().key()


def test_the_fact_set_is_a_set_not_a_list():
    """事实集合的顺序与重复不影响键。

    按列表算的话,换一次 `ORDER BY` 就算出一个新键,于是"上游没变"
    也重新付费生成一次 —— 与 `upstream_snapshot.active_colors` 排序
    那条是同一个坑。
    """
    assert _unit(fact_versions=("v2", "v1")).key() == _unit().key()
    assert _unit(fact_versions=("v1", "v2", "v1")).key() == _unit().key()
    assert _unit(fact_versions=("v1", "v2", " ")).key() == _unit().key()


def test_every_axis_that_should_move_the_key_moves_it():
    """该变的都要变。少一个轴的表现是**拿另一件东西的文案复用**。"""
    base = _unit().key()
    assert _unit(spu="SPU-2").key() != base, "换个 SPU 复用了同一版文案"
    assert _unit(locale="zh").key() != base, "换个语言复用了同一版文案"
    assert _unit(channel="SHEIN").key() != base
    assert _unit(site="BR").key() != base
    assert _unit(fact_versions=("v1", "v3")).key() != base, (
        "已确认事实改了却复用旧文案 —— AC-10 要的隔离在这里破掉"
    )
    assert _unit(color_variant_id="c-1").key() != base, (
        "颜色维文案启用后与 SPU 级文案算出同一个键"
    )


def test_the_key_carries_its_own_algorithm_version():
    """换算法必须换版本号,否则新旧键混在一张表里而查不出区别。"""
    assert ci.COPY_UNIT_VERSION in ("1",) or ci.COPY_UNIT_VERSION
    assert _unit().key().startswith("c1-")
    assert ci.COPY_UNIT_VERSION in LAYER.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 二、复用口径


def test_a_rejected_version_never_occupies_the_slot():
    """失败态不占幂等槽位。

    `save_copy` 会把规则硬失败的输出也落库(好让运营看见模型吐了什么)。
    拿它当复用对象的话,这个 SPU 的文案**永远修不好** —— 每次点生成都
    返回那一版失败的,现象是"点了没反应"。

    与 `run_state.unique_index_predicate()` 拒绝全表唯一是同一条规矩。
    """
    key = _unit().key()
    assert ci.decide(
        existing_key=key, existing_status="REJECTED", wanted_key=key
    ) is ci.CopyUnitVerdict.GENERATE
    assert "REJECTED" not in ci.REUSABLE_STATUSES
    assert "ARCHIVED" not in ci.REUSABLE_STATUSES, (
        "归档版本可复用 = 回滚到一个被更高版本挤下去的旧稿"
    )


def test_an_approved_version_is_reused_without_spending():
    key = _unit().key()
    verdict = ci.decide(existing_key=key, existing_status="APPROVED", wanted_key=key)
    assert verdict is ci.CopyUnitVerdict.REUSE
    assert not ci.spends_money(verdict)


def test_a_row_from_before_the_column_regenerates_rather_than_reuses():
    """存量行(键为 NULL)判**重新生成**,不判复用。

    方向与 `upstream_snapshot` 两列的 NULL 相反,而两者同源:
    **默认值的方向按代价选,不按对称选。**
    那边放行的代价是缺了整个颜色的草稿被导出;这边复用的代价是拿一版
    按早就变过的事实生成的文案当成"这次要的那一版"。
    """
    assert ci.decide(
        existing_key=None, existing_status="APPROVED", wanted_key=_unit().key()
    ) is ci.CopyUnitVerdict.GENERATE


def test_a_forced_regeneration_is_not_deduped():
    """单字段重生与强制重生是运营的明确意图,不是重复点击。"""
    key = _unit().key()
    verdict = ci.decide(
        existing_key=key, existing_status="APPROVED", wanted_key=key, force=True
    )
    assert verdict is ci.CopyUnitVerdict.FORCED
    assert ci.spends_money(verdict)


def test_spending_is_decided_by_naming_the_free_verdict():
    """`spends_money` 按「等于 REUSE」判,不按「不等于 GENERATE」判。

    后者在新增一个判定取值时**默认把它算成花钱的**,而新取值多半是
    新的复用形态 —— 默认方向反了,表现是台账上凭空多出一批调用。
    """
    source = ast.unparse(
        next(
            n
            for n in ast.walk(ast.parse(LAYER.read_text(encoding="utf-8")))
            if isinstance(n, ast.FunctionDef) and n.name == "spends_money"
        )
    )
    assert "REUSE" in source, "`spends_money` 没有点名那个免费的取值"


# ---------------------------------------------------------------- 三、接线


def test_the_verdict_gates_the_paid_call_and_the_content_plan():
    """判定必须挡在**生成器调用**与**内容计划**两者之前。

    内容计划那一半单独钉:`_content_plan_or_raise` 每次都 `session.add`
    一行 `ContentPlan` 并写一条审计。放在判定之前的话,复用路径会安静地
    攒垃圾行,而"复用了几次"在审计里看起来和"生成了几次"一模一样 ——
    也就是说这笔账**看起来还是没省下来**。
    """
    fn = next(
        n
        for n in ast.walk(ast.parse(SERVICE.read_text(encoding="utf-8")))
        if isinstance(n, ast.FunctionDef) and n.name == "generate_copy"
    )
    # **把 docstring 从输入里去掉。** 第一版没去,当场被自己咬:
    # `generate_copy` 的 docstring 里写着「`_content_plan_or_raise` 必须留在
    # 判定之后」,于是 `_first("_content_plan_or_raise")` 返回 0(那句话本身),
    # 断言拿一句**解释**当成了代码位置。
    #
    # 同一个坑在本仓是第七次(§3.26 / 14-13 / 14-22 / batch18 C2 /
    # batch19 caller 守卫 / batch20 B2)。修法每次都一样:解释性文字
    # 本来就不该参与判定。
    statements = list(fn.body)
    if (
        statements
        and isinstance(statements[0], ast.Expr)
        and isinstance(statements[0].value, ast.Constant)
        and isinstance(statements[0].value.value, str)
    ):
        statements = statements[1:]
    body = [ast.unparse(stmt) for stmt in statements]
    joined = "\n".join(body)

    def _first(needle: str) -> int:
        return next(i for i, line in enumerate(body) if needle in line)

    assert "copy_idempotency.decide" in joined, "`generate_copy` 里没有幂等判定"
    verdict_at = _first("copy_idempotency.decide")
    assert _first("spends_money") > verdict_at
    plan_at = _first("_content_plan_or_raise")
    assert plan_at > _first("spends_money"), (
        "内容计划建在判定之前 —— 复用路径会攒 ContentPlan 垃圾行与审计噪音"
    )
    generator_at = _first("generator.generate") if "generator.generate" in joined else None
    if generator_at is not None:
        assert generator_at > verdict_at, "生成器调用排在判定之前 —— 钱已经花掉了"


def test_the_key_is_persisted_by_the_writer():
    """算出来的键真的落库。不落的话下一次必然不命中,等于没有幂等。"""
    service = SERVICE.read_text(encoding="utf-8")
    assert "idempotency_key=unit.key()" in service, (
        "`save_copy` 没有收到键 —— 每一次生成都会写一行没有键的版本,"
        "而没有键的版本判 GENERATE,于是幂等永远不命中"
    )
    copy_service = (BACKEND_ROOT / "app" / "listings" / "copy_service.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(copy_service)
    ctor = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "ListingCopy"
    )
    assert "idempotency_key" in {kw.arg for kw in ctor.keywords}, (
        "`ListingCopy` 的构造点没写 `idempotency_key` —— "
        "14-22 为 `color_variant_id` 付过一次同样的账:列在、判定读着、没有写入路径"
    )


def test_concurrent_generation_is_serialised_on_the_unit():
    """并发必须串行化在**单元**上,不是在商品上。

    锁在 product 上的话,S 行与 M 行的两个并发请求各自拿到自己的锁,
    各判 GENERATE(那一刻库里还没有任何一版),各调一次生成器 ——
    AC-11 的「并发不产生第二次付费生成」当场不成立。
    """
    service = SERVICE.read_text(encoding="utf-8")
    assert 'try_advisory_xact_lock(session, "listing_copy_unit", unit.key())' in service, (
        "并发没有按幂等单元串行化"
    )


# ---------------------------------------------------------------- 四、迁移


def test_the_migration_adds_no_unique_index_on_the_key():
    """**这一列上不许有唯一索引。**

    一眼看去 `UNIQUE(idempotency_key)` 很自然,而 `REJECTED` 的那一版会
    永久占住这个键 —— 这个 SPU 的文案再也生成不出来,现象是"点了没反应"。
    """
    src = MIGRATION.read_text(encoding="utf-8")
    tree = ast.parse(src)
    assert 'revision: str = "0050"' in src and 'down_revision: str | None = "0049"' in src
    imported = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "app" not in imported, "迁移 import 了 app.*"

    upgrade = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "upgrade"
    )
    body = ast.unparse(upgrade)
    assert "create_index" in body
    assert "unique=True" not in body, (
        "键上加了唯一索引 —— REJECTED 的那一版会永久占住它"
    )
    assert "nullable=True" in body, "键不可空的话,存量行会挡住这次迁移"


def test_the_column_is_nullable_in_the_orm_too():
    model = (BACKEND_ROOT / "app" / "models" / "listing_copy.py").read_text(
        encoding="utf-8"
    )
    line = next(
        ln for ln in model.splitlines() if ln.strip().startswith("idempotency_key:")
    )
    assert "nullable=True" in line
