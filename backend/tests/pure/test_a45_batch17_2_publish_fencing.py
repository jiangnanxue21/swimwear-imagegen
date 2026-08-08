"""发布投递的落库归属校验(A45-batch17-2 / P2-1)。

## 这一组守的是什么

`publish_service._save()` 原来是"取三行、无条件写三行"。于是一次租约过期后
被重领、随后**迟到返回**的调用可以把较新的结论盖掉:

    DONE / SUCCEEDED / LISTED + external_spu_id  →  DEAD / UNKNOWN / SUBMIT_RESULT_UNKNOWN

批次链路的同型缺口在 A43 / BLOCK-02 用令牌关掉了(迁移 0026),发布这条
当时没跟着改。本批补上(迁移 0047),而这一组是它的离线看门人。

## 为什么必须静态钉

行为证据要真库双 session(`tests/test_publish_lease_concurrency_db.py`),
而那一组在没有 PostgreSQL 的机器上是 skip 的 —— 也就是本仓大部分时候的状态。
更要命的是这条路**只在出事时才走到**:正常跑一百次投递,`_save` 里有没有
那个 `if` 看不出任何差别;差别只在"调用耗时越过 LEASE_SECONDS"的那一次出现,
而那一次没有人在看屏幕。

## 每条守的是性质,不是做法(§3.31)

点名做法的守卫会因为进步而变红,而那种红会训练人去改守卫。所以下面钉的是:

    「落库前问过执行权」        不钉它用 SELECT FOR UPDATE 还是条件 UPDATE
    「令牌与租约一起还」        不钉赋值写在第几行
    「构造调用时带上令牌」      不钉令牌从哪来
    「归属判据不是时间戳」      这一条是**反向**断言,窗口必须封闭
"""
from __future__ import annotations

import ast

from tests.pure._helpers import BACKEND_ROOT, window

SERVICE = BACKEND_ROOT / "app" / "services" / "publish_service.py"
MODEL = BACKEND_ROOT / "app" / "models" / "publishing.py"
MIGRATIONS = BACKEND_ROOT / "migrations" / "versions"

_SRC = SERVICE.read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)


def _fn(name: str) -> ast.FunctionDef:
    hits = [n for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(hits) == 1, f"publish_service.py 里 {name} 有 {len(hits)} 个定义"
    return hits[0]


def _calls(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def _attr_targets(node: ast.AST, obj: str) -> list[str]:
    """`obj.x = ...` 里那些 x,按出现顺序。"""
    out: list[str] = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Assign):
            continue
        for tgt in sub.targets:
            if (
                isinstance(tgt, ast.Attribute)
                and isinstance(tgt.value, ast.Name)
                and tgt.value.id == obj
            ):
                out.append(tgt.attr)
    return out


# ---------------------------------------------------------------- 落库前先问执行权


def test_saving_an_outcome_asks_whether_it_still_has_the_right_to():
    """**这一组存在的头号理由。**

    `_save()` 里 `apply_outcome` 的调用必须被一次归属判定支配。没有它的表现
    不是报错,是一次迟到的调用把一个**已经上架成功**的商品改回"结果未知",
    而 `external_spu_id` 还留在库里 —— 界面说要人工处理,平台上东西好好的。

    钉法:归属判定的调用必须出现在 `apply_outcome` **之前**,并且中间有一条
    提前返回。只钉"出现了"是不够的 —— 算出来没人读和没算是一回事(§3.32)。
    """
    fn = _fn("_save")
    assert "_still_holds" in _calls(fn), (
        "`_save()` 没有问过执行权就往三张表上写。"
        "租约过期被重领之后,迟到的那次调用会盖掉较新的结论"
    )
    # 比的是**调用节点**的行号,不是 `ast.dump` 里字符串出现的先后:
    # docstring 也在 AST 里,而它恰好两个名字都提到了 —— 拿 dump 比,
    # 这条断言会被一段散文喂成假(或者更糟,喂成真)
    def _line(name: str) -> int:
        hits = [
            n.lineno
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == name
        ]
        assert hits, f"`_save()` 里找不到对 {name} 的调用"
        return min(hits)

    assert _line("_still_holds") < _line("apply_outcome"), (
        "归属判定排在 `apply_outcome` 后面 —— 那时三张表已经写完了,判定拦不住任何东西"
    )
    early_returns = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.If) and any(isinstance(s, ast.Return) for s in n.body)
    ]
    assert any("_still_holds" in ast.dump(n.test) for n in early_returns), (
        "判定的结果没有变成一条提前返回:算出来没人读,和没算是一回事"
    )


def test_a_stale_outcome_is_still_written_down_somewhere():
    """失去执行权 ≠ 这次调用没发生过。

    调用是真的发出去了(钱花了、平台那边可能真的动了),它的结论不再有资格
    决定这一行的状态,但**必须留下痕迹** —— 否则"平台那边到底发生了什么"
    在系统里没有任何落脚点。批次那边同一条理由(`_record_stale_outcome`)。

    只要求"落到一个追加的地方",不要求是审计还是别的表:那是做法。
    """
    fn = _fn("_save")
    assert "_record_stale_outcome" in _calls(fn), "结果被丢弃了,没有任何地方记下这次调用发生过"
    recorder = _fn("_record_stale_outcome")
    assert "record" in _calls(recorder), (
        "stale 结论只进了日志。日志会滚掉,而这是唯一还能回答"
        "「这次调用到底拿到了什么」的地方"
    )


# ---------------------------------------------------------------- 判据是令牌不是时间


def test_the_ownership_test_is_a_token_not_a_timestamp():
    """**反向断言,窗口封闭。**

    评审给的"退一步最小修法"是拿 `lease_until == 领取值` 当落库条件。
    那条在本仓是错的:`_renew_lease()` 在发请求**之前**就把库里的
    `lease_until` 改掉了,而 `_Call.lease_until` 不动 —— 真按它写,
    **正常路径也会一行都写不进去**,那比被覆写严重得多。

    所以这条钉的是:归属判据不许退回时间戳。
    """
    holds = window(_SRC, "def _still_holds(", "def _record_stale_outcome(", what="_still_holds")
    assert "lease_token" in holds, "归属判定没有用令牌"
    assert "lease_until" not in holds, (
        "归属判定又用回了 `lease_until`。它在续租那一刻就被我们自己改掉了,"
        "拿它当条件会让**正常路径**也落不了库"
    )

    renew = window(_SRC, "def _renew_lease(", "def _invoke(", what="_renew_lease")
    renew_where = window(renew, "update(PublishOutbox)", ".values(", what="_renew_lease 的 WHERE")
    assert "lease_token" in renew_where, (
        "续租的归属判据不是令牌 —— 两处判据必须是同一份,"
        "分叉的表现是「单机全绿、真库下偶尔覆盖」,最难查的那一类"
    )
    assert "lease_until" not in renew_where, (
        "续租的 WHERE 里还留着 `lease_until`:续租成功之后它就变了,"
        "第二次续租会判假"
    )


def test_a_missing_token_is_rejected_before_sqlalchemy_can_turn_it_into_is_null():
    """无令牌必须显式失败,不能依赖 ``column == None`` 的 SQL 语义。

    SQLAlchemy 会把后者编译成 ``IS NULL``。若没有显式分支,存量 NULL 行
    会被当成调用方仍持有,恰好把安全默认值翻成不安全的一侧。
    """
    for name in ("_renew_lease", "_still_holds"):
        fn = _fn(name)
        guards = [
            node
            for node in ast.walk(fn)
            if isinstance(node, ast.If)
            and "call.lease_token" in ast.unparse(node.test)
            and "is None" in ast.unparse(node.test)
            and any(isinstance(stmt, ast.Return) for stmt in node.body)
        ]
        assert guards, (
            f"{name} 没有显式拒绝空令牌 —— SQLAlchemy 会把 `== None` "
            "编译成 `IS NULL`,从而匹配存量无主行"
        )


# ---------------------------------------------------------------- 每条发请求的路都要领令牌


def test_every_call_that_goes_out_carries_a_token():
    """构造 `_Call` 的每一处都必须带 `lease_token`。

    今天有两处:`_lease()`(自动投递)与 `reconcile_unknown()`(人工确认)。
    第三处出现时忘了带令牌的表现**不是报错**,是这条路的结论从此永远落不了库 ——
    "点了确认、界面纹丝不动",而且它照样打日志、照样返回 200。

    这条钉的是数量关系(每一个构造点都带),不钉有几个构造点:
    多一条链路是进步,漏一把令牌不是。
    """
    sites = [
        n
        for n in ast.walk(_TREE)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_Call"
    ]
    assert sites, "一个 `_Call` 构造点都找不到 —— 这条守卫已经不设防了"
    missing = [s.lineno for s in sites if not any(k.arg == "lease_token" for k in s.keywords)]
    assert not missing, (
        f"第 {missing} 行构造 `_Call` 时没带 `lease_token`:"
        "这条路发出去的调用回来时会被判成 stale,结论只进审计而不进业务表"
    )


# ---------------------------------------------------------------- 令牌与租约一起还


def test_finishing_a_row_hands_back_the_token_together_with_the_lease():
    """落终态时 `lease_until` 与 `lease_token` 必须一起清。

    只清租约不清令牌,那一行会带着一个已经有结论的状态和一把还有效的令牌 ——
    另一个还拿着同一把令牌的执行体(不存在,除非将来有人复用 `_Call`)可以
    再写一次。反过来只清令牌不清租约,排查时会看见一个"有人持有"的假象。

    钉的是**成对**,不是各自出现几次:两者的赋值次数必须相等。
    """
    apply_fn = _fn("apply_outcome")
    targets = _attr_targets(apply_fn, "row")
    assert targets.count("lease_until") >= 3, (
        "`apply_outcome` 的三个分支(成功 / 重试 / 置死)不再各自归还租约"
    )
    assert targets.count("lease_token") == targets.count("lease_until"), (
        f"租约还了 {targets.count('lease_until')} 次、令牌还了 "
        f"{targets.count('lease_token')} 次 —— 有分支落了终态却没吊销令牌"
    )

    redeliver = _fn("redeliver_dead")
    assert "lease_token" in _attr_targets(redeliver, "row"), (
        "人工重新投递没有吊销令牌:还在飞的那次确认回来时仍然写得进去,"
        "而运营刚刚已经决定了要重发"
    )


# ---------------------------------------------------------------- 列与迁移对得上


def test_the_column_exists_in_both_the_model_and_a_migration():
    """模型上有这一列,迁移里也得有一条加它。

    只加模型不加迁移,表现是**测试全绿、真库 500** —— ORM 元数据对得上
    `create_all` 建出来的测试库,而生产库是 alembic 建的。这一条不判是哪一版
    迁移加的(那是做法),只判"两份真相都承认这一列"。
    """
    # 按 AST 找 `PublishOutbox` 类体里那条**带注解的赋值**,不查子串:
    # 这一列的上方就有一段解释它为什么存在的注释,子串检查会被那段散文
    # 喂成绿的 —— 列删掉了、解释还在,守卫照样 PASS(第一版就是这么写的,
    # 变异 M9 当场把它照了出来)
    model_tree = ast.parse(MODEL.read_text(encoding="utf-8"))
    outbox = [
        n
        for n in ast.walk(model_tree)
        if isinstance(n, ast.ClassDef) and n.name == "PublishOutbox"
    ]
    assert len(outbox) == 1, "publishing.py 里 PublishOutbox 不是恰好一个定义"
    declared = {
        s.target.id
        for s in outbox[0].body
        if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
    }
    assert "lease_token" in declared, "PublishOutbox 上没有 lease_token 列"
    adders = [
        p
        for p in MIGRATIONS.glob("*.py")
        if "publish_outbox" in (src := p.read_text(encoding="utf-8")) and "lease_token" in src
    ]
    assert adders, (
        "没有任何一版迁移给 publish_outbox 加 lease_token:"
        "ORM 建出来的测试库有这一列,alembic 建出来的生产库没有"
    )
