"""A45-batch14-26 的守卫:三条迁移 + 老建档路径切 SPU。

本批还清的是三笔**决定**类欠账(14-23 逐条点名过它们不是缺代码):

    §4.8 去重键          逾期六批
    evidence_class 存储列 + CHECK
    受众那条 NULL 兼容缝(老建档路径不写 spu_id)

守卫按 §3.33 的方向写:**防的是明天的调用点**,不是今天的实现。
每一条都先想"退化回去最省事的路径是什么",再钉那条路径 ——
钉实现细节的守卫会因为进步而变红(§3.31),而那种红会训练人去改守卫。
"""
from __future__ import annotations

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = BACKEND_ROOT / "app"
MIGRATIONS = BACKEND_ROOT / "migrations" / "versions"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _migration(name: str) -> Path:
    hits = sorted(MIGRATIONS.glob(f"{name}_*.py"))
    assert hits, f"迁移 {name} 不见了"
    return hits[0]


# --------------------------------------------------------------------------
# 决定 3:evidence_class 的回填不许在迁移里冻一份规则
# --------------------------------------------------------------------------


def test_the_evidence_class_migration_does_not_freeze_the_rules_as_sql():
    """**本批最重要的一条。**

    0045 只加列 + 加 CHECK,**不回填**。回填在
    `app/scripts/backfill_evidence_class.py` 里,因为脚本可以 import
    `derive_evidence_class` 而迁移不行(全仓 42 份没有一份 import `app.*`)。

    在迁移里回填只剩一条路:把四条规则冻成一段 SQL CASE。
    **那就是 PRD 风险表 D3 描述的第二个判定点** —— 规则今天还在演进,
    而冻进迁移的那份不会跟着演进,且停下来之后没有任何地方会报。

    0040 逐条权衡过同一道题并拒绝了,14-23 复述过一次。

    ## 退化回去最省事的路径

    不是"把回填写回来"(那太显眼),是**下一个人嫌脚本麻烦,在下一条迁移里
    补一句 UPDATE ... SET evidence_class = CASE ...**。所以这条守卫扫的是
    **全部迁移**,不只是 0045。
    """
    offenders: list[str] = []
    for path in sorted(MIGRATIONS.glob("*.py")):
        body = path.read_text(encoding="utf-8")
        if "evidence_class" not in body:
            continue
        lowered = body.lower()
        # 判据是"在同一份迁移里,既写 evidence_class 又出现 CASE/WHEN"。
        # 不判 `UPDATE`:CHECK 约束本身就要提到这一列,而那是对的。
        if "case" in lowered and "when" in lowered:
            offenders.append(path.name)
    assert not offenders, (
        f"这些迁移里出现了 CASE/WHEN 且提到 evidence_class:{offenders}。"
        "四条派生规则被冻进 SQL 了 —— 那是 D3 描述的第二个判定点。"
        "回填走 app/scripts/backfill_evidence_class.py,它 import 得到派生函数。"
    )


def test_the_backfill_script_goes_through_the_derive_function():
    """回填脚本必须**真的调**派生函数,而不是自己抄一份规则。

    上一条只证明了"迁移里没有第二份规则",它证明不了"脚本里那份是第一份"。
    两条合起来才是"全仓只有一个判定点"。

    钉的是 import **与**调用两件事:只 import 不调用的话,那份 import
    会在代码审查里看起来一切正常(§3.32 「算出来没人读的数,和没算是一回事」)。
    """
    path = APP_DIR / "scripts" / "backfill_evidence_class.py"
    assert path.exists(), "回填脚本不见了 —— 0045 的模块文档指着它"
    tree = _tree(path)

    imported = any(
        isinstance(n, ast.ImportFrom)
        and n.module == "app.media.evidence_rules"
        and any(a.name == "derive_evidence_class" for a in n.names)
        for n in ast.walk(tree)
    )
    assert imported, "回填脚本没有 import derive_evidence_class"

    called = any(
        isinstance(n, ast.Call)
        and (
            getattr(n.func, "id", None) == "derive_evidence_class"
            or getattr(n.func, "attr", None) == "derive_evidence_class"
        )
        for n in ast.walk(tree)
    )
    assert called, (
        "回填脚本 import 了派生函数但没有调用它 —— "
        "那份 import 在审查里看起来一切正常,而回填走的是别的路"
    )


def test_the_ai_invariant_is_written_identically_in_the_model_and_the_migration():
    """CHECK 的 SQL 在模型与迁移两处各写了一份,**两处必须逐字相同**。

    模型那份给 `create_all` 与新库用,迁移那份给存量库用。
    两者不一致时没有任何地方会报 —— 新库拦一种组合、老库拦另一种,
    而"这条不变式生效了吗"这个问题在两台机器上有两个答案。

    §3.31 记过同型的一次:「两处真相修不成一个补丁」。

    比对前把空白折叠:两处的换行与缩进由各自的行宽决定,那不是语义。
    """
    def _normalise(text: str) -> str:
        return " ".join(text.split())

    model_src = (APP_DIR / "models" / "media_asset.py").read_text(encoding="utf-8")
    mig_src = _migration("0045").read_text(encoding="utf-8")

    def _check_sql(tree: ast.Module) -> str:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", "") != "CheckConstraint":
                continue
            if node.args and isinstance(node.args[0], (ast.Constant, ast.BinOp, ast.JoinedStr)):
                return _normalise(ast.literal_eval(ast.unparse(node.args[0])))
        return ""

    model_sql = _check_sql(ast.parse(model_src))
    assert model_sql, "模型里找不到 CheckConstraint —— AI 不变式的库级那一半没了"

    # 迁移里它是一个模块级常量
    mig_tree = ast.parse(mig_src)
    mig_sql = ""
    for node in ast.walk(mig_tree):
        if (
            isinstance(node, ast.Assign)
            and any(getattr(t, "id", "") == "_AI_CHECK_SQL" for t in node.targets)
        ):
            mig_sql = _normalise(ast.literal_eval(ast.unparse(node.value)))

    assert mig_sql, "0045 里找不到 _AI_CHECK_SQL"
    assert model_sql == mig_sql, (
        "模型与迁移里的 AI 不变式 CHECK 不一致 —— 新库与存量库会拦住不同的组合。\n"
        f"  模型:{model_sql}\n  迁移:{mig_sql}"
    )


# --------------------------------------------------------------------------
# 决定 1 / 2:素材的 SPU 归属与去重键
# --------------------------------------------------------------------------


def test_the_asset_write_path_fills_both_the_fk_and_the_denormalised_code():
    """唯一构造点必须**同时**写 `spu_id` 与 `spu`。

    §4.4 定的口径是"外键权威、字符串是它的反规范化副本"。
    只写外键的话,`ix_media_assets_spu_role` 与几处按 SPU 码聚合的查询
    读到的是空;只写字符串的话,0044 的去重键分流会把这一行判成"没有 SPU",
    于是它落进兜底键 —— **而它明明有 SPU**。

    后者是本批引入的新失效方式,值得单独钉:去重键的覆盖面依赖
    `spu_id` 的空/非空,所以"忘了写外键"从此不只是少一列。
    """
    tree = _tree(APP_DIR / "media" / "service.py")
    ctor = next(
        (
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "MediaAsset"
        ),
        None,
    )
    assert ctor is not None, "`media/service.py` 里找不到 MediaAsset 的构造点"
    kwargs = {kw.arg for kw in ctor.keywords}
    for name in ("spu_id", "spu", "evidence_class"):
        assert name in kwargs, (
            f"MediaAsset 构造点没写 `{name}` —— "
            "14-22 为 `color_variant_id` 付过一次同样的账:"
            "列在、判定读着、而全仓没有写入路径"
        )


def test_nobody_resolves_a_spu_row_from_the_denormalised_code():
    """**不许从 `spu` 字符串码反查 SPU 行来补外键。**

    0043 的回填与 `media/service.py` 都刻意不这么做:那个码可能已经因为
    改名而与 SPU 行断开(那正是本批改用外键的理由),反查出来的 SPU 是错的。
    **错的外键比空的外键难发现得多** —— 空的会被兜底键接住并在清点里可见,
    错的会静默地把一批素材算进另一个款。

    退化回去最省事的路径:有人看到一批 `spu_id` 为空的素材,写一句
    `select(Spu).where(Spu.spu_code == asset.spu)` 把它们"修好"。
    """
    offenders: list[str] = []
    for path in APP_DIR.rglob("*.py"):
        body = path.read_text(encoding="utf-8")
        if "spu_code ==" not in body:
            continue
        for node in ast.walk(ast.parse(body)):
            if not isinstance(node, ast.Compare):
                continue
            left = node.left
            if getattr(left, "attr", "") != "spu_code":
                continue
            for comp in node.comparators:
                # 右边是某个对象的 `.spu` 属性 → 正是"从码反查"那个形状
                if getattr(comp, "attr", "") == "spu":
                    offenders.append(f"{path.relative_to(BACKEND_ROOT)}:{node.lineno}")
    assert not offenders, (
        f"有人从反规范化的 `spu` 字符串码反查 SPU 行:{offenders}。"
        "那个码可能已经因改名而断开,反查出来的外键是错的,且是静默的。"
    )


# --------------------------------------------------------------------------
# 决定 4:老建档路径不再产生 spu_id 为空的新行
# --------------------------------------------------------------------------


def test_the_legacy_create_path_resolves_a_spu_and_refuses_without_one():
    """`create_product` 必须解析出 `spu_id`,查不到就拒绝。

    这条缝的形状:受众在 SPU 层必填(§4.2),而走老路径建出来的商品
    `spu_id` 为空、`audience` 可空 —— **必填被绕过了**。

    ## 为什么钉"拒绝"而不只是钉"写了这一列"

    只钉写入的话,最省事的退化是 `product.spu_id = spu_row.id if spu_row else None`
    —— 那一行写了这一列,而缝原封不动。所以要同时钉:
    查不到 SPU 时这个函数**抛错**。

    ## 第一版被同一函数里另一处对的代码喂真

    第一版写的是 `assert "ValidationError" in ast.unparse(fn)`。变异 P1 把
    `if spu_row is None:` 改成 `if False:` —— 拒绝路径当场死掉,而函数里
    **另一处正确的代码**(`if not spu_code:` 那条)仍然含 `ValidationError`,
    于是断言被喂真,P1 报 GREEN。

    这与 14-22 的 L5 同型(§3.26 的那个新方向:不是文档喂假断言,
    是另一段对的代码喂真)。改成**算**而不是**读**:按 AST 找那条
    `if` 判定,断言它的条件真的在比 `spu_row` 与 `None`,且它的分支里抛错。
    """
    tree = _tree(APP_DIR / "services" / "product_service.py")
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "create_product"
    )
    body = ast.unparse(fn)

    assert "spu_id" in body, "`create_product` 不写 `spu_id` —— 受众必填仍然绕得过去"

    # 找「查不到 SPU 就抛错」那条判定。判据是三件事同时成立:
    #   条件里出现 spu_row、条件是与 None 比较、分支里 raise
    guarded = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        test_src = ast.unparse(node.test)
        if "spu_row" not in test_src or "None" not in test_src:
            continue
        if any(isinstance(inner, ast.Raise) for inner in ast.walk(node)):
            guarded = True
            break
    assert guarded, (
        "`create_product` 里没有「查不到 SPU 就抛错」那条判定 —— "
        "查不到时它多半把 spu_id 落成 None,那样这一列写了,而 §4.2 那条缝原封不动。\n"
        "注意:光看函数里有没有 ValidationError 是不够的,"
        "`if not spu_code` 那条会把这种断言喂真(变异 P1 演示过)。"
    )

    # 不许出现"查不到就算了"的三元回落
    assert " if spu_row else None" not in body and "if spu_row is not None else None" not in body, (
        "`create_product` 用三元把查不到 SPU 的情况回落成 None —— "
        "那正是这条守卫要防的那句话"
    )


def test_the_legacy_create_path_does_not_grow_a_second_spu_creation_route():
    """`create_product` 不许调 `create_spu`。

    两个理由,后一个更重要:

      1. `create_spu` 会按尺码模板展开一整套 SKU —— 从"建一个商品"里
         长出十几行商品来;
      2. 它会成为**第二条建档路径**。`seed_sample_data` 当初刻意不写第二条
         (14-22:「seed 走 spu_service.create_spu(),不另写建档路径」),
         理由是两条路径会各自演进,然后其中一条先漏掉某个必填项。
    """
    tree = _tree(APP_DIR / "services" / "product_service.py")
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "create_product"
    )
    calls = {
        getattr(n.func, "attr", None) or getattr(n.func, "id", None)
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
    }
    assert "create_spu" not in calls, (
        "`create_product` 调了 `create_spu` —— 那会展开一整套 SKU,"
        "并让仓库有第二条建档路径"
    )


def test_the_audience_is_copied_from_the_spu_not_taken_from_the_caller():
    """受众从 SPU 抄,不用入参那一份。

    §4.2 定的是 `spus.audience` 权威、`products.audience` 是反规范化副本。
    信入参的话,调用方可以用一个入参把商品行的受众改成和它 SPU 不同的值 ——
    而 `core/audience.category_code_for` 读的是**商品行**那一份,
    于是规则包键按一个受众算、模特与尺码表按另一个受众出。

    表现是"每一步单看都正常完成,错在最后才看得出来"(`create_spu` 原话)。
    """
    tree = _tree(APP_DIR / "services" / "product_service.py")
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "create_product"
    )
    assigns = [
        ast.unparse(n) for n in ast.walk(fn)
        if isinstance(n, ast.Assign)
        and any(
            ast.unparse(t).endswith("['audience']") or ast.unparse(t).endswith(".audience")
            for t in n.targets
        )
    ]
    assert any("spu_row.audience" in a for a in assigns), (
        "`create_product` 没有从 SPU 抄受众 —— 入参那一份可以与 SPU 不一致,"
        f"而两处不一致时规则包键与模特/尺码表会各按一个受众走。实际赋值:{assigns}"
    )
