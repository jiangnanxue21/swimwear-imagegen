"""a51:建档试算 + SPU 停用/恢复。

## 这一批修的两件事,以及它们为什么是同一件事

    一、建档的四类失败(编码字符集、重复颜色、行数上限、编码已存在)
        **全部**只在三步走完点「建档」时才报得出来 —— 而其中三类错在
        第二步的输入框上
    二、`SpuStatus.DISABLED` 从落枚举那天就在,注释写着「停掉了。**不删**」,
        而全仓**没有任何一条路径迁得到它**;`spusApi` 的四个写方法
        (update / addColorVariant / updateColorVariant / createSkus)
        **全树零调用点**

两者都是"后端做完了、动线断在最后一跳"。第一件断在"规则只在提交时才被问",
第二件断在"端点写完了没人调"。

## 这里覆盖的是哪一半

纯层与**读源码**那一半:规则报出去的就是常量本身、试算与建档共用同一个
函数、闸表收了新端点、前端真的接了线。

**落库那一半没有跑**(本机无 PostgreSQL):停用的平台闸、`row_version`
进位、`include_disabled` 的分页一致性,归 `tests/*_db.py` 那一侧。
分开写是刻意的 —— 判定写错和事务写错的修法完全不同。
"""
from __future__ import annotations

import ast
import pathlib
import re
import textwrap

from app.listings import sku_matrix
from tests.pure._helpers import BACKEND_ROOT, PROJECT_ROOT, expect_raises

SKU_MATRIX = BACKEND_ROOT / "app" / "listings" / "sku_matrix.py"
SPU_SERVICE = BACKEND_ROOT / "app" / "services" / "spu_service.py"
SPU_API = BACKEND_ROOT / "app" / "api" / "spus.py"
ACTION_GATE = BACKEND_ROOT / "app" / "api" / "action_gate.py"
PRODUCT_SERVICE = BACKEND_ROOT / "app" / "services" / "product_service.py"
CREATE_PAGE = PROJECT_ROOT / "frontend" / "src" / "pages" / "SpuCreatePage.tsx"
DETAIL_PAGE = PROJECT_ROOT / "frontend" / "src" / "pages" / "SpuDetailPage.tsx"
SPUS_TS = PROJECT_ROOT / "frontend" / "src" / "api" / "spus.ts"
CLIENT_TS = PROJECT_ROOT / "frontend" / "src" / "api" / "client.ts"
WRITE_ERROR_TS = PROJECT_ROOT / "frontend" / "src" / "hooks" / "useWriteError.ts"


def _fn(path: pathlib.Path, name: str) -> str:
    """一个函数的源码。按名字取,取不到就是 AssertionError 而不是空串 ——
    改名之后守卫必须变红,不能变成"在空串里找不到,断言照旧成立"。
    """
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return ast.get_source_segment(text, node) or ""
    raise AssertionError(f"{path.name} 里没有 {name}()")


def _code(path: pathlib.Path, name: str) -> str:
    """同上,但**去掉文档字符串与 `#` 注释**。

    专供反向断言(`assert X not in ...`)用。本仓库的注释密度是刻意的,
    而这些注释里经常原样写着"不许出现的那个东西"——
    `disable_spu` 的 docstring 里就有一句「在这里自己写一遍 `notin_(...)`
    的话」。拿带注释的源码下反向断言,结果是守卫在**正确的代码上**变红,
    而修法多半是把注释删掉 —— 那正好删掉了最值钱的那部分。

    这与 `tools/audit_source_guards.py` 防的是同一件事的另一面:
    那边管"反向断言不许吃切窄的源码",这边管"不许吃注释"。
    """
    source = _fn(path, name)
    tree = ast.parse(textwrap.dedent(source))
    body = tree.body[0]
    assert isinstance(body, ast.FunctionDef | ast.AsyncFunctionDef)
    stripped = ast.get_docstring(body) is not None
    lines = textwrap.dedent(source).splitlines()
    if stripped:
        # 文档字符串一定是第一条语句,砍到它结束那一行
        first = body.body[0]
        lines = lines[: first.lineno - 1] + lines[first.end_lineno :]
    return "\n".join(
        line for line in lines if not line.lstrip().startswith("#")
    )


#: 块注释与行注释。**只用于反向断言**,理由同 `_code()`。
_TS_COMMENT = re.compile(r"/\*.*?\*/|(?<![:\w])//[^\n]*", re.S)


def _ts_code(path: pathlib.Path) -> str:
    """TS/TSX 源码,去掉注释。反向断言专用。"""
    return _TS_COMMENT.sub("", path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 一、规则下发


def test_the_reported_rules_are_the_constants_themselves():
    """`code_rules()` 报出去的每一项**就是**本模块的常量。

    这一条守的是"第二个版本"。规则端点存在的全部理由是让前端不必内置一份;
    如果这个函数自己写死了 `16`,那它就成了第二份定义,而这个模块的
    模块文档第一句是「全仓唯一一份定义」。
    """
    rules = sku_matrix.code_rules()
    assert rules["separator"] == sku_matrix.SEPARATOR
    assert rules["max_spu_code"] == sku_matrix.MAX_SPU_CODE
    assert rules["max_variant_code"] == sku_matrix.MAX_VARIANT_CODE
    assert rules["max_size_code"] == sku_matrix.MAX_SIZE_CODE
    assert rules["max_variants_per_spu"] == sku_matrix.MAX_VARIANTS_PER_SPU
    assert rules["max_skus_per_expansion"] == sku_matrix.MAX_SKUS_PER_EXPANSION
    assert rules["size_templates"] == {
        name: list(sizes) for name, sizes in sku_matrix.SIZE_TEMPLATES.items()
    }


def test_the_reported_rules_contain_no_literals_of_their_own():
    """`code_rules()` 的返回里**一个数字字面量都没有**。

    这一条是变异 A 教出来的,而它教的东西比它本身值钱:
    上面那条守卫比的是**值**,而 `"max_variant_code": 16` 与
    `"max_variant_code": MAX_VARIANT_CODE` 的值恰好相等 —— 把常量抄成
    字面量,守卫照旧是绿的。而"抄成字面量"正是第二个版本长出来的方式,
    也正是这个函数存在要防的那件事。

    值比不出出处,所以这一条走 AST:每个值要么是名字(常量引用)、
    要么是从常量算出来的表达式,**不许是裸的 `Constant`** ——
    `normalizes_to_uppercase: True` 是唯一的例外,它描述的是
    `normalize_code()` 的行为,而"行为"没有对应的常量可指。
    """
    source = SKU_MATRIX.read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "code_rules"
    )
    returned = next(n for n in ast.walk(fn) if isinstance(n, ast.Return))
    assert isinstance(returned.value, ast.Dict)
    literals = [
        ast.literal_eval(key)
        for key, value in zip(returned.value.keys, returned.value.values, strict=True)
        if isinstance(value, ast.Constant)
    ]
    assert literals == ["normalizes_to_uppercase"], (
        f"这些项是写死的字面量,不是常量引用:{literals}"
    )


def test_the_reported_charset_is_exactly_what_normalize_code_accepts():
    """报出去的字符集与 `normalize_code()` 真正接受的集合**逐字符**一致。

    反平凡:逐个字符试一遍,而不是比对两个常量。比对常量的话,
    改了 `_ALLOWED` 却忘了改 `normalize_code` 的判据(或反过来)不会被发现 ——
    而运营看到的会是"提示里说允许的字符,填进去被拒"。
    """
    charset = sku_matrix.code_rules()["allowed_charset"]
    assert isinstance(charset, str) and charset
    for ch in charset:
        assert sku_matrix.normalize_code(ch, field="x", max_length=4) == ch
    # 分隔符不在里面 —— 它只对 SPU 那一段开
    assert sku_matrix.SEPARATOR not in charset
    assert sku_matrix.code_rules()["spu_code_allows_separator"] is True


def test_the_charset_is_sorted_so_the_hint_does_not_change_between_refreshes():
    """字符集排过序。

    `_ALLOWED` 是 `frozenset`,迭代顺序不保证稳定,而这个值直接显示在
    建档表单的提示文案里 —— 每次刷新换一个顺序会让运营以为规则变了。
    """
    charset = sku_matrix.code_rules()["allowed_charset"]
    assert list(charset) == sorted(charset)


def test_uppercase_is_reported_because_the_form_mirrors_it():
    """报 `normalizes_to_uppercase`,而这确实是 `normalize_code` 的行为。

    前端据此在失焦时做同一个归一化。报错(比如哪天后端改成大小写敏感)
    而前端照旧转大写的话,运营填的 `blk` 会被界面改成 `BLK` 然后被后端
    当成另一个编码 —— 一个界面自己制造出来的不一致。
    """
    assert sku_matrix.code_rules()["normalizes_to_uppercase"] is True
    assert sku_matrix.normalize_code(" blk ", field="x", max_length=8) == "BLK"


# ---------------------------------------------------------------- 二、试算与建档共用同一段判定


def test_expand_delegates_the_colour_rules_instead_of_keeping_its_own_copy():
    """`expand()` **调** `normalize_variant_codes()`,不是自己再写一遍。

    这是整个试算端点成立的前提:两条路径共用同一段判定、同一句错误文案、
    同一个 `field` 下标口径。`expand()` 哪天把这段抄回去,试算就成了
    第二个版本,而先过期的那一个会让合法输入在界面上被拒。
    """
    assert "normalize_variant_codes(" in _fn(SKU_MATRIX, "expand")
    # 反平凡:那三条判定的字面量**不许**再出现在 expand 里。
    # 吃去注释的源码 —— 见 `_code()` 的文档
    code = _code(SKU_MATRIX, "expand")
    assert "至少要有一个颜色变体" not in code
    assert "在同一个 SPU 里重复了" not in code


def test_the_extracted_rules_still_reject_what_they_used_to_reject():
    """抽出来之后,四类颜色码错误一条不少。"""
    assert expect_raises(
        sku_matrix.SkuPlanError, sku_matrix.normalize_variant_codes, []
    ).field == "variant_codes"
    dup = expect_raises(
        sku_matrix.SkuPlanError, sku_matrix.normalize_variant_codes, ["BLK", "blk"]
    )
    # 点名第几行 —— 这个下标是前端高亮的唯一依据
    assert dup.field == "variant_codes[1]"
    sep = expect_raises(
        sku_matrix.SkuPlanError, sku_matrix.normalize_variant_codes, ["BL-K"]
    )
    assert sku_matrix.SEPARATOR in sep.message
    too_many = expect_raises(
        sku_matrix.SkuPlanError,
        sku_matrix.normalize_variant_codes,
        [f"C{i:03d}" for i in range(sku_matrix.MAX_VARIANTS_PER_SPU + 1)],
    )
    assert too_many.field == "variant_codes"


def test_expand_still_answers_the_stage_one_acceptance():
    """反平凡:抽取之后「三颜色九 SKU」照旧成立。"""
    rows = sku_matrix.expand(
        spu_code="SW-001", variant_codes=["BLK", "RED", "BLU"], size_template="ALPHA_3"
    )
    assert len(rows) == 9
    assert rows[0].sku == "SW-001-BLK-S"


def test_preview_reuses_the_same_four_judgements_as_create():
    """试算里没有一条新写的判定。

    四类失败各自对应一个既有函数,而 `preview_spu` 只是**提前**问了一次。
    这条守卫按名字点名那四个调用 —— 有人在试算里手写一个 `if len(...) >`
    就会红。
    """
    source = _fn(SPU_SERVICE, "preview_spu")
    for call in (
        "normalize_spu_code(",
        "normalize_variant_codes(",
        "sku_matrix.expand(",
        "_code_taken(",
    ):
        assert call in source, f"试算没走 {call}"
    # 与建档同一条错误路径:`loc` 逐字相同,前端一套高亮两处都能用
    assert "_translate(" in source


def test_preview_writes_nothing():
    """试算不写库。

    它拿着 session(要查编码占用),所以"不写"这件事必须被钉住 ——
    一个 `session.add()` 在这里是完全能编译过的。
    """
    code = _code(SPU_SERVICE, "preview_spu")
    for forbidden in ("session.add(", "session.commit(", "session.flush(", "audit.record("):
        assert forbidden not in code, f"试算里出现了 {forbidden}"


def test_preview_reports_an_unknown_row_count_as_none_not_zero():
    """没给尺码模板时 `sku_count` 是 `None`。

    退化成 0 的话,界面上"算不出"和"零行"长得一样,而后者会被读成
    "这次建档一行 SKU 都不会产生"。
    """
    source = _fn(SPU_SERVICE, "preview_spu")
    assert '"sku_count": len(planned) if template is not None else None' in source


def test_code_taken_is_a_flag_not_an_error():
    """编码占用回布尔,不抛。

    运营是边打字边触发试算的:`SW-0` 打到一半就红一次的话,他会先学会
    忽略这个提示,然后连真的那次也一起忽略。
    """
    assert '"code_taken": _code_taken(session, spu_code)' in _fn(SPU_SERVICE, "preview_spu")
    assert "DuplicateError" not in _code(SPU_SERVICE, "preview_spu")


# ---------------------------------------------------------------- 三、停用是状态迁移,不是删除


def test_there_is_still_no_delete_route_for_spus():
    """没有 `DELETE /spus/{id}`,而且不该有。

    SPU 底下挂着颜色与若干行 SKU,而每一行 SKU 又被九张表引着 ——
    `product_service.archive_product` 的 docstring 把这笔账算过了。
    """
    assert "@router.delete" not in SPU_API.read_text(encoding="utf-8")


def test_disable_asks_the_one_definition_of_still_listed():
    """停用的平台闸走 `product_service.live_listings_for()`。

    在 `spu_service` 里自己写一遍 `notin_(...)` 的话,`_PUBLISH_SETTLED`
    就有了两个读者而只有一个会跟着改 —— 而它**已经**被改过一次
    (DELISTED 是后补进去的)。
    """
    # **正向断言也吃去注释的源码。** 这一条是变异 E 教出来的:
    # `disable_spu` 的 docstring 里原样写着 `product_service.live_listings_for()`,
    # 于是把整句调用换成 `live = []` 之后守卫**照旧是绿的** —— 它命中的是
    # 那句注释。`tools/audit_source_guards.py` 只管反向断言,这个方向没人管。
    code = _code(SPU_SERVICE, "disable_spu")
    assert "product_service.live_listings_for(" in code
    # 反向同理:那句 docstring 里也原样写着「自己写一遍 `notin_(...)`」
    assert "notin_(" not in code
    # 拒绝时点名是哪几个 SKU 与哪个渠道站点 —— 只说"不能停用"的话,
    # 运营下一步不知道该去哪个后台下架
    assert "which" in code and "where" in code


def test_disable_is_idempotent_because_double_clicks_are_not_conflicts():
    """已经停用的再停一次直接返回。

    与 `archive_product` 同一条:连点两下或超时后重试,不该拿到 409。
    """
    source = _fn(SPU_SERVICE, "disable_spu")
    assert "if spu.status == SpuStatus.DISABLED.value:\n        return spu" in source


def test_disable_takes_a_reason_and_restore_does_not():
    """停用要理由(进审计),恢复不要。

    后者是撤销,前者是决定 —— 只有决定需要在三个月后答得出"为什么"。
    """
    assert "reason: str" in _fn(SPU_SERVICE, "disable_spu")
    assert "reason" not in _fn(SPU_SERVICE, "restore_spu").split('"""')[2]


def test_disable_does_not_cascade_into_archiving_the_skus():
    """停用**不**连带归档底下的 SKU。

    归档一行有它自己的闸、理由和审计记录;一个按钮背后迁移十几行的话,
    那十几条审计记录的理由全是同一句,而其中某一行可能正卡在平台驳回
    回流上。
    """
    code = _code(SPU_SERVICE, "disable_spu")
    assert "archive_product(" not in code
    assert "ProductStatus.ARCHIVED" not in code


def test_a_disabled_spu_stops_growing():
    """停用之后不能再加颜色或 SKU。

    不拒的话「停用」就只是一个标签:界面上写着"这个款不做了",而它照旧
    长出新的颜色和新的 SKU 行,没有任何地方会报错。

    闸放在 service 层而不是接口层 —— 批量导入也会落 SKU,那条路径不经过
    `api/spus.py`。
    """
    for name in ("add_color_variant", "create_skus"):
        assert "_assert_open_for_building(spu)" in _fn(SPU_SERVICE, name), name
    assert "SpuStatus.DISABLED.value" in _fn(SPU_SERVICE, "_assert_open_for_building")


def test_the_list_and_the_count_share_one_filter():
    """列表与总数用同一个过滤条件。

    各写一遍 `where` 的下场是分页错位:总数把停用的算进去、这一页没有,
    于是最后一页是空的,而空态写着"还没有商品"。
    """
    assert "_visible(select(Spu), include_disabled)" in _fn(SPU_SERVICE, "list_spus")
    assert "_visible(select(func.count(Spu.id)), include_disabled)" in _fn(
        SPU_SERVICE, "count_spus"
    )


def test_the_new_write_endpoints_declared_whether_they_pass_the_gate():
    """三个新写端点都在闸表里,而且各带一句理由。

    `test_a45_batch31_action_gate` 那三条守卫会在漏一行时变红,这一条
    多钉一层:理由**不许是空白**(一个没有理由的豁免下次会被当成
    "以前就这样")。
    """
    source = ACTION_GATE.read_text(encoding="utf-8")
    for endpoint in ("spus:preview_spu", "spus:disable_spu", "spus:restore_spu"):
        match = re.search(rf'"{endpoint}":\s*"([^"]+)"', source)
        assert match, f"{endpoint} 不在闸表里"
        assert len(match.group(1)) > 8, f"{endpoint} 的理由太短,说不出它为什么不过闸"


def test_live_listings_for_takes_a_batch_not_a_row():
    """`live_listings_for` 一次 `IN` 查询,不按行循环。

    SPU 停用要问的是"底下十几行里有没有一行还挂着",按行查就是 A-06
    那条「聚合的循环里不许有库读」的同一个形状。
    """
    source = _fn(PRODUCT_SERVICE, "live_listings_for")
    assert ".in_(ids)" in source
    # 空列表短路:`IN ()` 在部分方言下是语法错误
    assert "if not ids:\n        return []" in source


# ---------------------------------------------------------------- 四、前端真的接了线


def test_the_form_asks_the_backend_before_each_step_instead_of_copying_rules():
    """建档表单每一步的"下一步"都过一次试算,而**不是**内置一份规则。

    这两条必须一起断言:只断言"调了 preview"的话,一个既调试算又顺手
    抄了一份正则的页面照样绿 —— 而抄的那一份会先过期。
    """
    source = CREATE_PAGE.read_text(encoding="utf-8")
    assert source.count("runPreview(") >= 4, "试算调用点少了 —— 三步加一颗重新试算"
    assert "spusApi.preview(" in source
    assert "rules.data.allowed_charset" in source
    assert "rules.data.max_variant_code" in source
    # 反平凡:字符集与上限一律来自 `rules.data`,页面里不许出现它们的字面量
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in _ts_code(CREATE_PAGE)


def test_the_form_highlights_the_row_the_backend_named():
    """后端点名的 `loc` 被用来高亮到具体那一行。

    后端为这件事付过一次代价:`_api_loc()` 专门把 `variant_codes[1]` 翻成
    `color_variants[1].variant_code`,而那个函数的 docstring 里记着上一版
    翻错时的表现 ——「前端高亮不到任何一行」。翻它的唯一理由就是这里。
    """
    source = CREATE_PAGE.read_text(encoding="utf-8")
    assert "color_variants" in source and "rowProblems" in source
    assert "useWriteError(noRefresh, onFields)" in source
    # 指向某一行时跳回第二步 —— 人停在第三步,错在第二步的输入框上
    assert "setCurrent(1)" in source


def test_the_failure_alert_carries_the_reason_itself():
    """那条 Alert 不再说「上面的提示里有具体原因」。

    "上面的提示"是一条会自己消失的 toast。运营读到 Alert 时那句原因
    大概率已经不在屏幕上了 —— 一句页面自己做不到的话。
    """
    # 那句话现在只出现在**注释**里(讲这段历史),不许再出现在界面上
    assert "上面的提示里有具体原因" not in _ts_code(CREATE_PAGE)
    assert "{failure}" in CREATE_PAGE.read_text(encoding="utf-8")


def test_the_third_step_shows_the_backend_computed_skus():
    """第三步显示的 SKU 是后端算的。

    上一版只显示 `颜色数 × 尺码数`,理由是"前端不许拼 SKU 编码"——
    那个理由是对的,而它当时的结论是**当时唯一的选项**。试算端点补上
    之后,列表走的是 `expand()`,前端仍然一个字符都没拼。
    """
    assert "preview.skus" in CREATE_PAGE.read_text(encoding="utf-8")
    # 反平凡:前端不许自己乘出那个总数
    assert "colours.length * sizes.length" not in _ts_code(CREATE_PAGE)


def test_field_problems_keep_their_structure():
    """`fieldProblems()` 回结构,不回拼过的字符串。

    `describeError` 那份 `` `${loc}: ${msg}` `` 是给技术详情折叠面板的;
    拼过之后再解析回来是不行的 —— `msg` 里本来就可能含冒号。
    """
    source = CLIENT_TS.read_text(encoding="utf-8")
    assert "export function fieldProblems(" in source
    assert "loc: String(f.loc ?? '')" in source


def test_on_fields_is_optional_and_on_unknown_is_not():
    """两个回调可选性相反,而且是有理由的。

    `onUnknown` 强制:每个写请求都可能"结果未知",而那时正确的下一步
    每页不同,给不出默认值。`onFields` 可选:只有带表单的写请求才有
    字段可高亮,强制其余 20 个调用点传空函数只会得到噪音。
    """
    source = WRITE_ERROR_TS.read_text(encoding="utf-8")
    assert "onUnknown: () => void," in source
    assert "onFields?: (fields: FieldProblem[]) => void," in source
    # 无条件调,哪怕空数组 —— 页面据此清掉上一轮的红框
    assert "if (onFields) onFields(fieldProblems(err))" in source


def test_the_detail_page_finally_calls_the_four_orphaned_methods():
    """那四个"写完没人调"的方法有了调用点。

    `spusApi.update` / `addColorVariant` / `updateColorVariant` 与新加的
    `disable` / `restore` —— 后端端点齐了、前端客户端也写了,而在此之前
    **全树零调用点**。和 `GenerationPlanPanel`、`facts_stale` 是同一个形状。
    """
    source = DETAIL_PAGE.read_text(encoding="utf-8")
    for method in ("spusApi.update(", "spusApi.addColorVariant(",
                   "spusApi.updateColorVariant(", "spusApi.disable(", "spusApi.restore("):
        assert method in source, f"{method} 仍然没有调用点"


def test_the_detail_page_sends_the_row_version_it_read():
    """乐观锁的版本号从出参带上,不是写死或省略。"""
    source = DETAIL_PAGE.read_text(encoding="utf-8")
    assert "expected_version: spu!.row_version" in source
    assert "expected_version: row.row_version" in source


def test_the_variant_code_is_not_editable():
    """颜色编码在详情页**不可编辑**。

    它已经拼进了这个颜色底下每一行 SKU 的编码里(`SW-001-BLK-S`)。
    改它要么让界面和库里的 SKU 对不上,要么要重命名一整组已经挂着素材、
    生成任务和图片集的行 —— 那不是编辑,是重建。
    """
    source = DETAIL_PAGE.read_text(encoding="utf-8")
    # 编辑态那几个受控输入分别绑在这三个字段上,`variant_code` 不在其中
    assert "colourDraft.working_name" in source
    assert "colourDraft.supplier_color_code" in source
    assert "colourDraft.sellable_status" in source
    assert "colourDraft.variant_code" not in _ts_code(DETAIL_PAGE)


def test_the_button_says_disable_not_delete():
    """按钮叫「停用」,不叫「删除」。

    它做的确实不是删除:颜色、SKU、素材、属性、审计一行都不动。
    写成「删除」的话,第一个点它的人会以为库里那些行没了。
    """
    source = DETAIL_PAGE.read_text(encoding="utf-8")
    assert "停用这个款" in source
    code = _ts_code(DETAIL_PAGE)
    assert "删除这个款" not in code and "删除 SPU" not in code


def test_the_spu_out_schema_carries_notes_because_the_form_edits_it():
    """`SpuOut` 带 `notes`,否则那个编辑框是在覆盖。

    读不到就改,表单打开时是空的,保存一次就把别人写的备注抹掉了,
    而没有任何地方会报错。
    """
    schema = (BACKEND_ROOT / "app" / "schemas" / "spu.py").read_text(encoding="utf-8")
    body = schema.split("class SpuOut(BaseModel):")[1].split("class ")[0]
    assert "notes: str | None" in body
    assert "notes: string | null" in SPUS_TS.read_text(encoding="utf-8")
