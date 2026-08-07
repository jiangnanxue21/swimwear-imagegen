"""A45-batch14-22:素材颜色归属的写入路径 + 新结构样例数据(PRD §13 阶段 1 / 阶段 2)。

## 这一批还的是什么

上一轮逐条核阶段 1-3 时,核出一处**上一版清单里没有的欠账**:

    `media_assets.color_variant_id` 落库了(0037)、被两处判定读着
    (`sample_completeness._in_variant` / `scope_fingerprint._element`),
    **而全仓没有任何写入路径。**

这是"读得到、判得了、写不进"的第三例(前两例是阶段 4 的 `shared_opt_in`
与 `angle`)。它的失效方式和那两例一样安静,但后果更靠上游:

    颜色维整个塌成一维 —— 每一张图都算通用图

于是「给 A 色补图」和「补一张通用图」在指纹上完全一样,**AC-21 那条全称
命题在真数据上永远平凡成立**。上一批刚接的 `facts_stale` 会一路绿着,
而它证明的是一件没有内容的事。

## 顺带还的:样例数据

同一个洞的另一半。阶段 1 验收「可构造三颜色九 SKU 的 SPU」——「可构造」
和「构造出来了」是两件事,而在此之前样例数据里 10 个 SPU 各 1 个 SKU。
"""
from __future__ import annotations

import ast
import json

from tests.pure._helpers import BACKEND_ROOT, PROJECT_ROOT

APP = BACKEND_ROOT / "app"
SAMPLE = PROJECT_ROOT / "sample-data"


def _module(rel: str) -> ast.Module:
    return ast.parse((APP / rel).read_text(encoding="utf-8"))


def _func(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"找不到函数 {name}")


def _code(fn) -> str:
    """函数体,剥掉 docstring。§3.34 第二节。"""
    return "\n".join(
        ast.unparse(n)
        for n in fn.body
        if not (
            isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
        )
    )


def _kwargs(fn) -> set[str]:
    return {a.arg for a in fn.args.args + fn.args.kwonlyargs}


# ------------------------------------------------- 归属的写入路径


def test_the_attribution_column_has_a_writer_all_the_way_from_the_endpoint():
    """**从接口到落库,整条路都收得下颜色。**

    断言整条链而不是只断言最里面那一层,是因为这个洞的形状就是"链断在中间"
    —— `ingest()` 收得下但 `shadow_from_product_asset()` 不传,表现和完全
    没接一模一样:列恒为 NULL,而每一层单看都是对的。

    链上任意一环少一个参数,这条当场红。
    """
    chain = [
        ("api/products.py", "upload_asset"),
        ("services/asset_service.py", "upload_asset"),
        ("media/service.py", "shadow_from_product_asset"),
        ("media/service.py", "ingest"),
    ]
    for rel, name in chain:
        fn = _func(_module(rel), name)
        assert "color_variant_id" in _kwargs(fn), (
            f"{rel}::{name} 收不下颜色归属 —— 链断在这里,而表现和完全没接一样:"
            "那一列恒为 NULL,颜色维塌成一维"
        )

    # 最里面那一层必须真的写进 `MediaAsset(...)`,不是收下就丢。
    #
    # **按 AST 找那次构造,不按字符串找。** 第一版写的是
    # `assert "color_variant_id=color_variant_id" in body` —— 而 `ingest` 里
    # 还有一句 `_fill_missing_colour(existing, color_variant_id=color_variant_id)`,
    # 于是把构造那一行整个删掉,断言照样成立(变异 L5)。
    #
    # §3.26 第四节那句话的第 N 次:按字符串在源码里找东西,找到的从来不保证
    # 是你以为的那个位置。这次的失效方向是**假绿** —— 列恒为 NULL 而守卫全绿。
    ingest = _func(_module("media/service.py"), "ingest")
    constructed = [
        {kw.arg for kw in node.keywords}
        for node in ast.walk(ingest)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "MediaAsset"
    ]
    assert constructed, "`ingest` 里 `MediaAsset` 的构造点不见了 —— 这条守卫失去了对象"
    assert all("color_variant_id" in kws for kws in constructed), (
        "`ingest` 收下了归属却没写进 `MediaAsset` —— 参数在,列还是空的,"
        "而颜色维塌成一维不会报错"
    )


def test_each_hop_forwards_it_rather_than_dropping_it():
    """中间两层必须**往下传**,不是各自留着。

    收得下不等于传得下去 —— 这是上一批 `collect()` 那条查询预算守卫的
    同一个形状:「取了一次却不往下传,等于白取」。
    """
    for rel, name in (
        ("api/products.py", "upload_asset"),
        ("services/asset_service.py", "upload_asset"),
        ("media/service.py", "shadow_from_product_asset"),
    ):
        body = _code(_func(_module(rel), name))
        assert "color_variant_id=color_variant_id" in body, (
            f"{rel}::{name} 收下了归属但没往下传"
        )


def test_a_repeat_upload_cannot_silently_move_an_image_to_another_colour():
    """去重命中时只**补**空归属,不改已定的。

    改归属是一次有后果的动作:§5.3 明写「修改素材颜色归属 A→B → 原颜色、
    新颜色、共享三个指纹同时变化」,也就是三批事实同时过期。

    让一次**重复上传**顺手触发它,是最难查的一类数据变更 —— 运营做的动作
    是"再传一次同一张图",看到的结果是"另外两个颜色的事实全部退回待确认"。
    两件事在界面上毫无关联。

    与 `_fill_missing_role` 同一条口径,而那一条是被 AC-22 换来的。
    """
    fn = _func(_module("media/service.py"), "_fill_missing_colour")
    body = _code(fn)
    assert "if asset.color_variant_id is not None:" in body, (
        "去重命中时会覆盖已有归属 —— 一次重复上传会让三批事实同时过期"
    )
    assert "return" in body
    # 去重那一路真的调了它
    ingest = _code(_func(_module("media/service.py"), "ingest"))
    assert "_fill_missing_colour(" in ingest, (
        "去重命中那一路不补归属 —— 先传成通用图再补颜色的那条动线走不通"
    )


def test_a_colour_from_another_spu_is_refused_before_anything_is_written():
    """跨 SPU 的颜色 id 要在写之前拦下。

    §4.3:颜色名在 SPU 内唯一,**跨 SPU 同名是常态**(几乎每个款都有黑色)。
    所以光看一个 UUID 分不出它属于谁,而传错的后果不是报错:

        这张图进不了本商品那个颜色的完整度门禁(传了图还说缺图)
        它却会让**另一个 SPU** 的颜色事实过期(一个没人动过的款
        突然一批字段回到待确认)

    两个现象都不指向"上传时选错了颜色",而且发生在两个不同的页面上。
    """
    guard = _code(_func(_module("services/product_service.py"), "assert_colour_belongs_to"))
    assert "variant.spu_id != product.spu_id" in guard, (
        "没有比 SPU —— 一个 UUID 分不出它属于哪个款"
    )
    assert "product.spu_id is None" in guard, (
        "没处理「商品没有 spu_id」那一档 —— 老建档路径建的商品给不出"
        "「本商品所在 SPU」这个答案,而放行等于允许挂到查不出关系的颜色上"
    )
    endpoint = _code(_func(_module("api/products.py"), "upload_asset"))
    assert "assert_colour_belongs_to(" in endpoint, "接口没有调那道校验"
    assert endpoint.index("assert_colour_belongs_to") < endpoint.index(
        "asset_service.upload_asset"
    ), "校验排在写入之后 —— 拦下来的时候字节已经落存储了"


def test_the_hint_and_the_foreign_key_are_kept_apart():
    """`variant_hint` 与 `color_variant_id` 不许合流。

    前者是模型猜出来的字符串,后者是人给的外键。混用正是 §6.2 收紧 role
    口径要禁的那件事 —— 让模型的猜测决定一份**已确认**的事实什么时候过期。

    `scope_fingerprint.AssetView` 的文档里逐字写着这一条,而那边只是不取;
    真正的防线要在写入侧:归属列不许从 hint 赋值。
    """
    src = (APP / "media" / "service.py").read_text(encoding="utf-8")
    for bad in (
        "color_variant_id=variant_hint",
        "color_variant_id = variant_hint",
        "color_variant_id=asset.variant_hint",
    ):
        assert bad not in src, (
            f"归属列从 `variant_hint` 取值({bad}) —— 模型的猜测会决定"
            "已确认事实什么时候过期"
        )


# ------------------------------------------------- 新结构样例数据


def _manifest() -> dict:
    path = SAMPLE / "spus.json"
    assert path.exists(), (
        "sample-data/spus.json 不见了 —— 阶段 1「测试数据与 Fixture 重置为"
        "新结构」失去了对象,而验收只能靠手工造数据演示"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_sample_can_actually_build_a_three_colour_nine_sku_spu():
    """**阶段 1 验收那一条,在样例数据上有对象。**

    「可构造三颜色九 SKU 的 SPU」——「可构造」和「构造出来了」是两件事。
    后端的展开规则早就算得出 3 × 3 = 9(`test_a45_batch13_stage1_identity`
    里有穷举守卫),而在这之前样例数据里 **10 个 SPU 各 1 个 SKU**。

    于是这条验收在演示时没有对象,真库用例只能自己造夹具 —— 而自己造的
    夹具证明的是"代码能建",不是"这个包交出去之后演示得起来"。
    """
    from app.listings import sku_matrix

    nine = [
        spec
        for spec in _manifest()["spus"]
        if len(spec["color_variants"]) == 3
        and len(
            sku_matrix.expand(
                spu_code=sku_matrix.normalize_spu_code(spec["spu_code"]),
                variant_codes=[v["variant_code"] for v in spec["color_variants"]],
                size_template=spec["size_template"],
            )
        )
        == 9
    ]
    assert nine, "样例数据里没有三颜色九 SKU 的 SPU"


def test_the_sample_keeps_a_single_colour_control_group():
    """留一个单色 SPU 作对照。

    **AC-21 要两组才验得出来。** 多色那件证明「A 色变了 B 色不变」,
    单色这件证明共享作用域本身还在动 —— 只有多色样本时,一个把颜色维
    整个关掉的缺陷会表现成「全都不过期」,而那看起来很像正常。
    """
    specs = _manifest()["spus"]
    assert any(len(s["color_variants"]) == 1 for s in specs), "没有单色对照组"
    assert any(len(s["color_variants"]) > 1 for s in specs), "没有多色样本"


def test_the_sample_covers_more_than_one_audience():
    """样例要盖住两个受众。

    §18.3:必填属性集按受众条件化 —— 女装问领型、男装问腰头。只有 WOMEN
    的话「受众决定必填集」在演示里永远看不出来,而它错了的表现是运营被问到
    一个这件商品根本没有的字段。
    """
    audiences = {s["audience"] for s in _manifest()["spus"]}
    assert len(audiences) >= 2, f"样例只覆盖了 {sorted(audiences)}"


def test_the_manifest_carries_no_projection_columns():
    """样例里不许出现 `display_name`。

    它是投影列,唯一写入点是属性服务在 VARIANT 层 `standard_color_name`
    被确认时(§4.3)。样例里填一个"正式名称"等于绕过那个写入点,而绕过它
    的值没有来源、没有证据、不会被复核 —— 演示时看起来一切正常,
    而属性确认那条动线在样例上就没有对象了。
    """
    for spec in _manifest()["spus"]:
        for variant in spec["color_variants"]:
            assert "display_name" not in variant, (
                f"{spec['spu_code']}/{variant['variant_code']} 填了 display_name —— "
                "那是投影列,填它等于绕过属性服务那个唯一写入点"
            )


def test_the_seed_goes_through_the_same_service_the_endpoint_uses():
    """播种走 `spu_service.create_spu()`,不另写一条展开逻辑。

    另写一条的下场是**样例数据能建出接口建不出来的东西** —— 比如一个跳过
    SKU 展开规则的 SPU,而演示时看不出来,直到有人照着样例的形状去调接口。

    与 §5.1「唯一取数入口」同一条规矩在写入侧的样子。
    """
    body = _code(_func(_module("scripts/seed_sample_data.py"), "seed_new_structure"))
    assert "spu_service.create_spu(" in body, (
        "播种没走建档服务 —— 样例能建出接口建不出来的东西"
    )
    for hand_rolled in ("sku_matrix.expand(", "Product(", "ColorVariant("):
        assert hand_rolled not in body, (
            f"播种脚本里出现了 {hand_rolled} —— 那是第二条建档路径"
        )


def test_the_seed_attaches_images_by_colour_not_by_sku():
    """新结构的素材按颜色挂,而且每个颜色只挂一次。

    逐个尺码各传一份的话,`(product_id, sha256)` 去重键挡不住(product_id
    不同),于是同一张图在库里有三份 —— 而 §5.3 的指纹会把它们当成三张
    不同的证据,一个颜色的"证据数量"凭空乘以尺码数。
    """
    body = _code(_func(_module("scripts/seed_sample_data.py"), "_seed_colour_images"))
    # 归属经内嵌的 `_upload` 传下去 —— 钉的是**它真的到了上传调用**,
    # 不是某个变量名长什么样。第一版断言写的是 `color_variant_id=variant.id`,
    # 而真实写法是 `_upload(product, path, variant.id)`:断言红在一个完全
    # 正确的代码上(§3.34 第二节说的假红那一侧)
    assert "color_variant_id=variant_id" in body, "上传时没有把颜色归属递下去"
    assert "_upload(product, path, variant.id)" in body, "颜色图没有带该颜色的 id"
    assert "_upload(shared_host, path, None)" in body, (
        "通用图没有落点,或者带上了颜色 —— 少了它,「补通用图不该 stale 掉"
        "颜色事实」这条在样例上验不到"
    )
    # **两个取数点各钉一次,不能只钉 `_first_sku(` 这个前缀。**
    #
    # 变异 S6/S8 撞出来的:把 `_first_sku(...)` 的结果换成 None,循环体
    # 一行不动 —— 于是上面那三条断言全部成立,而那一批图一张也不会被挂上去。
    # 前缀断言被另一处调用喂成了平凡真,和 L5 是同一个形状。
    assert "_first_sku(variant.id)" in body, (
        "颜色图没有取到该颜色的 SKU 行 —— 循环还在,而它一次也不会执行"
    )
    assert "_first_sku(None)" in body, "通用图没有取到落点行"


def test_the_sample_verifier_owns_the_stage_one_acceptance():
    """那条验收由 `verify_sample_data` 盯着,不只写在文档里。

    §3.34:**欠账要么写成会变红的守卫,要么就不要写。** 验收也一样 ——
    「样例数据里要有一个三颜色九 SKU 的 SPU」如果只写在 STATUS 里,
    下一个人重整样例数据时不会知道有这条约束,而删掉它不会有任何征兆。
    """
    src = (BACKEND_ROOT / "tools" / "verify_sample_data.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    checks = next(
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "CHECKS" for t in n.targets)
    )
    registered = {
        ast.unparse(el.elts[1])
        for el in checks.elts
        if isinstance(el, ast.Tuple) and len(el.elts) == 2
    }
    for owed in (
        "check_a_three_colour_nine_sku_spu_exists",
        "check_every_colour_has_its_sample_images",
        "check_colour_images_are_distinct_across_colours",
    ):
        assert owed in registered, f"{owed} 写好了没挂进 CHECKS —— 它从来不会被执行"


def test_the_missing_dependency_path_still_checks_what_it_can():
    """缺 pydantic 时**只跳字段那一层**,展开规则照验。

    整条跳过是最省事的写法,而它的表现是:一个 schema 全错、SKU 一个也
    展开不出来的样例文件,在没有 pydantic 的机器上显示绿色 —— 而这个仓库
    最常被执行的那条路(`make check-offline`)恰好就没有 pydantic。

    这条口径本来就写在 `verify_sample_data` 的模块文档里(第二节:
    「没有图就说没有图,让人自己判断,而不是替他判断」),本批只是让它
    在新增的那条检查上也成立。
    """
    src = (BACKEND_ROOT / "tools" / "verify_sample_data.py").read_text(encoding="utf-8")
    fn = _func(ast.parse(src), "check_the_new_structure_sample_is_accepted_by_the_real_service")
    body = _code(fn)
    assert "_expand_all(sku_matrix)" in body, (
        "缺依赖时整条跳过了 —— 展开规则那一半不需要 pydantic,跳掉它意味着"
        "一个展不开的样例文件在离线门禁上显示绿色"
    )
    assert "ModuleNotFoundError" in body, "没有区分「缺依赖」和「样例有问题」"
