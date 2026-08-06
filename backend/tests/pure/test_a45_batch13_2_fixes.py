"""A45-batch13-2:对 batch13 阶段 1 第一批的走读修复。

## 这一批修的五条

    F1  新建档路径造出来的九行 SKU,在每一个读取方眼里是九个颜色
    F2  `GET /spus` 的 `sku_count` 恒为 0
    F3  `_translate()` 的注释说保留 `field`,代码把它丢了
    F4  `spu_service` 把事务边界记到了 `get_session` 头上,而那个依赖不提交
    F5  `GET /spus` 序列化 `color_variants` 时按行惰性加载

F1 是唯一一条**已经随包发出去**的缺陷,其余四条在 batch13 里就存在。

## 为什么 F1 那样的东西能带着 42 条守卫发出去

batch13 的守卫里有 `test_three_colors_and_three_sizes_make_nine_skus`,
但它断言的是 `sku_matrix.expand()` —— 展开层。验收标准那句话是「可构造
**三颜色**九 SKU 的 SPU」,而"三颜色"这半句在读取侧一条断言都没有。
守卫挡得住「有人把决定删掉」,挡不住「决定只落了一半」。

所以这份文件里 F1 那一节全部从**读取方**下断言:`ids_of` 看见几个变体、
目录里显示什么名字、诊断报不报。不再从建档那一侧数行数。
"""
from __future__ import annotations

import ast
import dataclasses
import pathlib
import re

from app.core.errors import ErrorCode, FieldProblem, ValidationError
from app.listings import variant_key as vk
from tests.pure._helpers import BACKEND_ROOT, PROJECT_ROOT

SPU_SERVICE = BACKEND_ROOT / "app" / "services" / "spu_service.py"
SPU_API = BACKEND_ROOT / "app" / "api" / "spus.py"
VARIANTS = BACKEND_ROOT / "app" / "listings" / "variants.py"
DB_SESSION = BACKEND_ROOT / "app" / "db" / "session.py"
MAIN = BACKEND_ROOT / "app" / "main.py"
IMAGE_SETS_TS = PROJECT_ROOT / "frontend" / "src" / "api" / "imageSets.ts"


# ---------------------------------------------------------------- AST 取数


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def _func(path, name: str):
    """按名字取一个函数。**同时认 `async def`** —— `main.py` 的异常处理器
    全是异步的,只认 `ast.FunctionDef` 会把它们全部漏掉,而漏掉的表现是
    这条守卫报「找不到」,不是报「断言不成立」。"""
    for node in ast.walk(_tree(path)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里没有 {name}()")


def _identifiers(node: ast.AST) -> set[str]:
    """代码里用到的名字。注释与文档字符串不算(同 batch13 那份守卫的理由)。"""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
        elif isinstance(child, ast.keyword) and child.arg:
            names.add(child.arg)
    return names


def _new_path_rows() -> list[vk.VariantRow]:
    """`create_spu` 建出来的那九行在判定层的样子。

    三个特征缺一不可,而它们**全都是刻意的**:

        有 `color_variant_id`   建档路径写外键(batch13 的交付项)
        没有 `variant_key`      守卫明令禁止新代码碰它(退役方向)
        没有 `primary_color`    视觉属性不在建档入参上(阶段 1 验收项)

    三个刻意的决定叠在一起,就是 F1。
    """
    return [
        vk.VariantRow(
            sku=f"SPU-SW-001-{code}-{size}",
            uid=f"11111111-0000-0000-0000-00000000000{index}",
            variant_name=code,
        )
        for index, code in enumerate(("BLK", "RED", "NVY"))
        for size in ("S", "M", "L")
    ]


# ================================================================ F1 身份


def test_nine_skus_in_three_colours_are_three_variants_to_every_reader():
    """阶段 1 第一条验收,**从读取侧**断言一次。

    **退化成什么样:** 身份少了外键这一级,九行各自掉到种子表达式,而种子
    是 sku(建档时颜色为空),全库唯一 —— 于是属性层把 S 码确认的主色写进
    一个只有 S 码看得见的桶,图片集要求九个变体各有一张图,导出铺九行。
    三处都不报错。
    """
    ids = vk.ids_of(_new_path_rows())
    assert len(ids) == 3, f"九行 SKU 在读取方眼里是 {len(ids)} 个变体,应当是 3 个:{ids}"


def test_the_identity_order_is_the_foreign_key_then_the_key_then_the_seed():
    """三级顺序。**外键在最前,种子在最后。**

    **退化成什么样:** 谁把 `variant_key` 挪到外键前面,新路径的行会全部
    掉到种子(它们没有 key),F1 原样回来;挪到种子后面则是存量行掉回
    "会被改名带偏"。
    """
    row = vk.VariantRow(sku="SW-1", key="Red", color="正红", uid="uuid-1")
    assert vk.identity_of(row) == "uuid-1"
    assert vk.identity_of(vk.VariantRow(sku="SW-1", key="Red", color="正红")) == "Red"
    assert vk.identity_of(vk.VariantRow(sku="SW-1", color="正红")) == "正红"
    assert vk.identity_of(vk.VariantRow(sku="SW-1")) == "SW-1"


def test_the_source_label_agrees_with_the_identity_it_describes():
    """`identity_source()` 和 `identity_of()` 必须同源。

    **退化成什么样:** 两者各写一套条件,诊断会指着一个和实际身份无关的
    级别 —— 比 没有诊断更误导人,因为它看起来是可信的。
    """
    cases = [
        (vk.VariantRow(sku="SW-1", key="Red", color="红", uid="u"), vk.SOURCE_VARIANT_UID, "u"),
        (vk.VariantRow(sku="SW-1", key="Red", color="红"), vk.SOURCE_VARIANT_KEY, "Red"),
        (vk.VariantRow(sku="SW-1", color="红"), vk.SOURCE_SEED, "红"),
        (vk.VariantRow(sku="SW-1"), vk.SOURCE_SEED, "SW-1"),
    ]
    for row, source, identity in cases:
        assert vk.identity_source(row) == source, row
        assert vk.identity_of(row) == identity, row


def test_whitespace_only_identities_do_not_count_as_assigned():
    """空白串不是身份。

    **退化成什么样:** 用 `if row.uid:` 而不是 strip 之后判断,一列被写进
    空格的外键会让整行伪装成"有身份",而它翻译不到任何变体。
    """
    row = vk.VariantRow(sku="SW-1", key="   ", color="", uid="  ")
    assert vk.identity_of(row) == "SW-1"
    assert vk.identity_source(row) == vk.SOURCE_SEED


def test_rows_that_carry_both_identities_are_reported_as_shadowed():
    """外键赢过 key 的那一刻必须有人喊一声。

    这是三级身份**自己打开的**缺口:同时有外键和 key 的行,身份从 key 翻成
    外键,而属性 owner_id 与图片标签当初写的是 key。今天不存在这种行
    (两个集合不相交),给存量行回填外键的那一刻会全库都是。

    **退化成什么样:** 回填被当成一次普通的数据迁移做掉,全部颜色事实和
    图片绑定同时指向不存在的变体 —— 而回填本身跑得很干净,没有任何报错。
    """
    both = vk.VariantRow(sku="SW-9", key="Red", color="红", uid="uuid-9")
    assert vk.drift([both])["identity_shadowed"] == ["SW-9"]
    only_key = vk.VariantRow(sku="SW-8", key="Red", color="红")
    only_uid = vk.VariantRow(sku="SW-7", uid="uuid-7", variant_name="RED")
    assert vk.drift([only_key, only_uid])["identity_shadowed"] == [], (
        "只有一种身份的行不该报 shadowed —— 那是今天全库的常态,恒报等于不报"
    )


def test_new_path_rows_are_not_permanently_flagged():
    """新建档路径的行不该恒挂在 `unassigned` 和 `renamed` 上。

    **退化成什么样:** 判据仍然是"没有 variant_key"/"key 和颜色对不上",
    而新路径**不写** key、身份是 UUID —— 它建的每一行都会永远出现在诊断里。
    恒报等于不报:真正需要人看的那几行会被淹掉。
    """
    report = vk.drift(_new_path_rows())
    assert report["unassigned"] == [], f"带外键的行被报成没有身份:{report['unassigned']}"
    assert report["renamed"] == [], f"UUID 身份被拿去和颜色名比对:{report['renamed']}"


def test_an_identified_row_with_a_uuid_identity_is_never_called_renamed():
    """识别跑完之后的那个状态 —— 有外键身份**并且**有颜色名。

    上一条用的是建档刚做完的行,`primary_color` 是空的,而 `renamed` 本来
    就跳过空颜色。于是那一条测不出这里的问题:属性识别把颜色填上之后,
    `key_matches_color()` 会拿一个 UUID 去和"曜石黑"比,永远不相等。

    **退化成什么样:** `renamed` 少了"身份来自 variant_key 才判"这一级 ——
    每一个跑完识别的新 SPU,它的每一个变体都常驻改名列表。那张表是给人
    看"哪些变体确实改过名"的,全都在上面等于它不再有信息。
    """
    identified = [
        vk.VariantRow(sku="SPU-1-BLK-S", uid="uuid-blk", color="曜石黑", variant_name="BLK"),
        vk.VariantRow(sku="SPU-1-RED-S", uid="uuid-red", color="正红", variant_name="RED"),
    ]
    assert vk.drift(identified)["renamed"] == [], (
        "UUID 身份被拿去和颜色名比对了 —— 识别一跑完就全员改名"
    )


def test_rows_with_no_identity_at_all_are_still_reported():
    """判据换了,不是删了。

    **退化成什么样:** 为了让上一条绿,有人把 `unassigned` 整个改成恒空 ——
    于是真正会被改名带偏的存量行彻底隐身。
    """
    seeded = [vk.VariantRow(sku="OLD-1", color="红"), vk.VariantRow(sku="OLD-2")]
    assert vk.drift(seeded)["unassigned"] == ["OLD-1", "OLD-2"]


def test_a_renamed_row_is_still_reported_when_its_identity_is_the_key():
    """A-27 那条判据在 key 这一级上原样有效。

    **退化成什么样:** `renamed` 被收窄成恒空,改过名的存量行不再列出来,
    而"key 看着像颜色名"的人仍然会去解析它。
    """
    renamed = vk.VariantRow(sku="SW-1", key="Red", color="Crimson")
    assert vk.drift([renamed])["renamed"] == ["Red"]


def test_the_label_prefers_the_variant_name_over_a_hex_identity():
    """建档刚做完那一刻,界面上不许出现一串十六进制。

    **退化成什么样:** 只有"当前颜色 → 身份"两级(`label_of()` 就是两级)。
    key 时代够用,因为 key 长得像颜色名;换成 UUID 之后,属性识别跑完之前
    三个颜色在界面上显示成三串 UUID,运营认不出哪个是黑色。
    """
    row = vk.VariantRow(sku="SW-1", uid="11111111-2222", variant_name="BLK")
    assert vk.label_for_row(row) == "BLK"
    row = vk.VariantRow(sku="SW-1", uid="u", variant_name="BLK", color="曜石黑")
    assert vk.label_for_row(row) == "曜石黑"
    assert vk.label_for_row(vk.VariantRow(sku="SW-1", uid="11111111-2222")) == "11111111-2222"
    labels = [ref.label for ref in vk.refs_of(_new_path_rows())]
    assert labels == ["BLK", "RED", "NVY"], f"目录里显示的是身份而不是名字:{labels}"


def test_the_fallback_chain_is_written_down_exactly_once():
    """三级顺序只有 `identity_of()` 一处定义。

    **退化成什么样:** `variants.py` 里再写一份"外键或 key 或种子"。两级时
    三份副本恰好等价,所以上一版写重了三处而没人发现;加进外键这一级就会
    分叉,表现是导出铺三行、属性写一个变体 —— 两边都不报错,只有平台会。
    """
    names = _identifiers(_tree(VARIANTS))
    assert "identity_of" in names, "variants.py 不再走 identity_of()"
    assert "seed_for" not in names, (
        "variants.py 里又出现了 seed_for —— 回落顺序被复制了第二份"
    )


def test_the_identity_projection_does_not_touch_the_relationship():
    """`_row()` 只读列,不碰 `color_variant`。

    **退化成什么样:** 为了拿显示名在 `_row()` 里读一次关系属性。它被
    `variant_id_for()` 在导出与属性识别的按行循环里调用,于是九行 SPU 一次
    查询变十次 —— 而查询预算那条棘轮按 AST 看循环,看不见 ORM 惰性加载。
    """
    names = _identifiers(_func(VARIANTS, "_row"))
    assert "color_variant_id" in names, "_row() 不再读外键列"
    assert "color_variant" not in names, (
        "_row() 里碰了 color_variant 关系属性 —— 那是按行循环里的一次惰性加载"
    )


# ================================================================ 前后端同源


def _frontend_drift_keys() -> set[str]:
    text = IMAGE_SETS_TS.read_text(encoding="utf-8")
    start = text.index("drift: Partial<")
    end = text.index("tagged:", start)
    return set(re.findall(r"\|\s*'(\w+)'", text[start:end]))


def test_every_drift_key_is_declared_in_the_frontend_union():
    """后端 `drift()` 的键和前端联合类型一一对应。

    **退化成什么样:** 后端加一个键、前端不加。`drift` 是
    `Partial<Record<...>>`,TS 一声不吭,那一类异常在界面上彻底不存在。
    反过来前端多一个键则是承诺了后端不会给的东西。

    这条守卫是这一批自己欠的:F3 骂的就是"注释和代码对不上",而改
    `unassigned` 的判据却不动前端那句说明,是同一个毛病换个文件重演一次。
    """
    backend = set(vk.drift([]))
    frontend = _frontend_drift_keys()
    assert backend == frontend, (
        f"后端多出:{sorted(backend - frontend)};前端多出:{sorted(frontend - backend)}"
    )


def test_the_frontend_no_longer_describes_unassigned_as_missing_a_key():
    """前端那句说明必须跟着判据改。

    **退化成什么样:** 注释停在"还没分配 key 的行(0027 之前建的)",而判据
    已经是"身份只剩种子"。读注释的人会以为新建档路径的行都在这张表里。
    """
    text = IMAGE_SETS_TS.read_text(encoding="utf-8")
    assert "还没分配 key 的行" not in text, (
        "imageSets.ts 里 unassigned 的说明还是旧判据"
    )


# ================================================================ F2 sku_count


def test_the_list_serializer_has_no_default_sku_count():
    """`_to_out()` 的 `sku_count` **不许有默认值**。

    **退化成什么样:** 默认值一在,忘记数 SKU 就是一个安静的 0 —— 上一版
    正是这样:详情页对、列表页每个 SPU 都报 0 个 SKU。硬规则 4 点名的
    就是这个形状,为凑接口形状填的常量不是从真实来源推出来的。
    """
    fn = _func(SPU_API, "_to_out")
    args = [a.arg for a in fn.args.args]
    assert "sku_count" in args, "_to_out 不再收 sku_count"
    assert not fn.args.defaults, "_to_out 又有默认值了 —— 忘记传就会安静地报 0"


def test_the_list_route_passes_a_real_count_for_every_row():
    """列表路径必须真的传第二个实参。

    **退化成什么样:** 签名改对了、调用点没改。这条按调用点断言,不看签名。
    """
    fn = _func(SPU_API, "list_spus")
    calls = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_to_out"
    ]
    assert calls, "列表路径不再调用 _to_out"
    for call in calls:
        assert len(call.args) + len(call.keywords) >= 2, (
            "_to_out 在列表路径上只传了一个实参 —— sku_count 又回到常量了"
        )
    assert "sku_counts_for" in _identifiers(fn), "列表路径没有去数 SKU"


def test_the_counts_are_grouped_in_the_database():
    """一次分组查询,不在推导式里按行查。

    **退化成什么样:** `{row.id: len(skus_of(session, row.id)) for row in rows}`
    —— 结果正确,而一页 200 个 SPU 就是 200 次查询。A-06 那条「聚合的循环里
    不许有库读」是同一个形状,只是换了一个端点。
    """
    fn = _func(SPU_SERVICE, "sku_counts_for")
    names = _identifiers(fn)
    assert "group_by" in names, "sku_counts_for 没有分组,大概是在 Python 里数的"
    assert "spu_id" in names, "sku_counts_for 按 spu 字符串数 —— §4.4 禁止它做查询权威"
    loops = [
        n
        for n in ast.walk(_func(SPU_API, "list_spus"))
        if isinstance(n, (ast.For, ast.comprehension))
    ]
    for loop in loops:
        assert "sku_counts_for" not in _identifiers(loop), "在循环里逐行数 SKU"
        assert "skus_of" not in _identifiers(loop), "在循环里逐行查 SKU"


# ================================================================ F3 错误字段


def test_a_validation_error_carries_the_field_to_the_client():
    """`AppError.to_payload()` 现在真的送得出字段位置。

    **退化成什么样:** 回到只吐 code 和 message。前端 `client.ts` 读的是
    `error.fields`,读不到就只能整条消息弹一个横幅,而建档表单有一个颜色
    列表 —— 运营要自己一行行试出来是哪一个编码不合法。
    """
    err = ValidationError(
        "颜色编码里不许有分隔符",
        code=ErrorCode.INPUT_INVALID,
        http_status=422,
        fields=[FieldProblem(loc="color_variants[0].variant_code", msg="不许有分隔符")],
    )
    payload = err.to_payload()["error"]
    assert payload["fields"] == [
        {"loc": "color_variants[0].variant_code", "msg": "不许有分隔符"}
    ], payload


def test_an_error_without_a_field_omits_the_key_entirely():
    """没有字段位置时**不出现**这个键,而不是给一个空数组。

    **退化成什么样:** 恒发 `fields: []`。前端两种写法都吃得下,但
    "这个错误没有字段位置"和"有位置但一条都没算出来"从此长得一模一样。
    """
    assert "fields" not in ValidationError("受众必填").to_payload()["error"]


def test_the_field_shape_is_the_one_the_422_handler_already_uses():
    """形状抄的是既有的那一份,不是新造的第二种。

    **退化成什么样:** 因为上游异常的属性叫 `field`(单数)就发
    `{"field": ...}`。前端要写两套解析,而漏掉的那一套表现是错误提示
    变成 `undefined` —— `main.py` 的 `http_error_handler` 顶部记的正是它。
    """
    ours = {f.name for f in dataclasses.fields(FieldProblem)}
    handler = _func(MAIN, "validation_handler")
    theirs = {
        node.value
        for parent in ast.walk(handler)
        if isinstance(parent, ast.Dict)
        for node in parent.keys
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert ours <= theirs, f"我们发的是 {sorted(ours)},422 处理器发的是 {sorted(theirs)}"


def test_the_expansion_error_field_is_handed_over_not_dropped():
    """`_translate()` 必须把 `field` 传下去。

    **退化成什么样:** 上一版的原样 —— 注释写着「**保留 `field`**」,
    代码里 `ValidationError(error.message, code=..., http_status=422)`,
    字段位置在这一行蒸发。硬规则 5:注释和代码对不上,过期的是代码这一侧。
    """
    names = _identifiers(_func(SPU_SERVICE, "_translate"))
    assert "fields" in names, "_translate 没有传 fields"
    assert "field" in names, "_translate 没有读 SkuPlanError.field"


# ================================================================ F4 事务边界


def test_the_archiving_route_commits_because_nothing_else_will():
    """路由**必须**提交。这是 `test_..._does_not_commit_the_session` 的另一半。

    **退化成什么样:** 有人照着"事务边界由 `get_session` 收口"那句错话把
    路由里的 `session.commit()` 删掉。建档从此什么都不落库,**而测试不会红**
    —— conftest 的 session 夹具跑在外层事务里
    (`join_transaction_mode="create_savepoint"`),同一个 session 内看不出
    提交与否。只有真库手测才会发现,而这台机器上没有真库。
    """
    fn = _func(SPU_API, "create_spu")
    receivers = {
        node.func.value.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "commit"
        and isinstance(node.func.value, ast.Name)
    }
    assert "session" in receivers, "POST /spus 的路由不提交 —— 那就没有人提交了"


def test_the_session_dependency_still_does_not_commit():
    """`get_session` 不提交。这是上面那条注释所依据的事实。

    **退化成什么样:** 有人为了"保险"把请求级自动 commit 加回去。§7.8 第一条
    禁止项,危害在读路径:任何一处误写都会被提交,哪怕那个端点从头到尾
    没打算写库。
    """
    fn = _func(DB_SESSION, "get_session")
    committers = {
        node.func.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "commit" not in committers, "get_session 又开始提交了(§7.8 第一条禁止项)"


def test_no_module_still_blames_the_session_dependency_for_the_boundary():
    """那句错话不许留在树里。

    **退化成什么样:** 代码改对了、注释还在。下一个人照着注释删掉路由的
    提交 —— 这条守卫存在的全部理由,就是那句话曾经**同时**出现在服务层
    文档和一条守卫的 docstring 里,两份都错,而错的那份看起来像权威。
    """
    wrong = "事务边界由 `api/deps.get_session` 收口"
    # 这份文件自己必须引用那句话(上面的 docstring 里),否则读守卫的人
    # 不知道它在防什么。跳过自己,不是给自己开后门
    myself = pathlib.Path(__file__).resolve()
    for path in BACKEND_ROOT.rglob("*.py"):
        if "__pycache__" in str(path) or path.resolve() == myself:
            continue
        assert wrong not in path.read_text(encoding="utf-8"), (
            f"{path.relative_to(BACKEND_ROOT)} 还写着「{wrong}」,而那个依赖不提交"
        )


# ================================================================ F5 预取


def test_the_spu_list_eager_loads_its_colour_variants():
    """列表查询预取颜色。

    **退化成什么样:** 去掉 `selectinload`。`SpuOut` 带 `color_variants`,
    序列化每一行补一次查询,`page_size` 上限 200 就是 200 次 —— 而这个端点
    不在 `test_workbench_query_budget.py` 那条棘轮的覆盖范围里(它盯的是
    `api.workbench_batch`),没有任何门禁会叫。
    """
    names = _identifiers(_func(SPU_SERVICE, "list_spus"))
    assert "selectinload" in names, "list_spus 没有预取 color_variants"
    assert "color_variants" in names


def test_the_sibling_query_eager_loads_the_colour_variant():
    """同门兄弟查询预取颜色变体。

    **退化成什么样:** `_rows()` 要读颜色变体的名字,不预取的话九行 SPU
    一次查询变十次。这条路径同时被图片集校验和属性完整度消费,每次批准
    都会走一遍。
    """
    names = _identifiers(_func(VARIANTS, "_siblings"))
    assert "selectinload" in names, "_siblings 没有预取 color_variant"
