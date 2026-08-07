"""A45-batch14-23:§6.5 两列的写入路径 + §4.8 去重键拆账(PRD §13 阶段 2 / 阶段 4)。

## 这一批还的是什么

14-20 记下的那笔:`listing_image_items.shared_opt_in` 与 `angle` 落库了、
`_to_view` 读它们、§6.5 的四条规则用它们判定,**而没有任何代码路径写过**。

这是"读得到、判得了、写不进"的第三次出现(前两次:14-22 的
`media_assets.color_variant_id`,以及本条自己被记账的那一刻)。三次的
共同形状值得记住:**一列新增的、被判定读取的列,如果没有人回答「谁写它」,
它的取值会恒等于默认值,而每一层单看都是对的。**

`angle` 那一半的失效尤其安静 —— 门禁不报"少了写入路径",它报的是
「缺正面图」。运营会去补图,补多少张都没用,因为新图的 `angle` 同样是 NULL。

## 顺带拆开的一笔:§4.8 去重键

逐条核阶段 1-3 时核出的第二处**任何清单里都没有的欠账**:PRD §4.8 要求
去重键改成 `UNIQUE(spu_id, COALESCE(color_variant_id,''), sha256)`,
今天仍是 `UNIQUE(product_id, sha256)`。

它躲过六批的方式与 14-22 那一列不同,更值得记一句:**它有守卫,而守卫是
绿的。** 原 `test_dedupe_key_is_product_scoped` 把「不许全局唯一」
(永久不变量)和「今天是 product 作用域」(一笔欠账)写在同一组断言里。
两者混在一起时,这条守卫钉住的是**旧键本身** —— 将来谁按 §4.8 落新键,
它会红,而让它变绿最省事的做法正好是把 PRD 那条要求撤回去。

本批把它拆成两条:不变量留在 `test_media_layer.py`,欠账搬到这里并挂上
还款日。§3.38 之外再补一条一般化:**一条守卫同时钉住不变量与现状时,
它对现状的那一半是一个伪装成成绩的欠账。**
"""
from __future__ import annotations

import ast

from tests.pure._helpers import BACKEND_ROOT

APP_DIR = BACKEND_ROOT / "app"


def test_the_two_new_item_columns_are_written_by_the_only_constructor():
    """§6.5 那两列**每一个构造点都写**。**欠账已还**,这是那条守卫的正向替身。

    上一版守卫记的是「`shared_opt_in` 与 `angle` 落库了、`_to_view` 读它们、
    §6.5 的四条规则用它们判定,而没有任何代码路径写过它们」。它的失效方式
    不是"少了个功能",是**两条规则的判定结果被钉死**:

        shared_opt_in 恒 False   每张通用图恒定命中 UNMARKED_SHARED_IMAGE
        angle 恒 NULL            配了方案的颜色永远覆盖不了必要角度

    第二条尤其安静 —— 门禁不报"少了写入路径",它报的是「缺正面图」,
    运营会去补图,补多少张都没用。

    ## 为什么断言的是"每一个"构造点,不是"存在一个"

    退化回去最省事的路径不是删掉这两个 kwarg,是**加第二个构造点**:
    某条新链路(候选入集、复制图片集、导入)自己 `ListingImageItem(...)`
    一份而漏掉这两列。那时旧构造点仍然写着,"存在一个"照样绿,
    而漏掉的那一批行在库里与从前完全一样。
    """
    src = (BACKEND_ROOT / "app" / "listings" / "image_set_service.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(src)
    constructed = [
        {kw.arg for kw in node.keywords}
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "ListingImageItem"
    ]
    assert constructed, "`ListingImageItem` 的构造点不见了 —— 这条守卫失去了对象"
    for written in constructed:
        assert "shared_opt_in" in written and "angle" in written, (
            "有一个 `ListingImageItem` 构造点没写 §6.5 那两列。"
            "漏掉的那一批行会恒定命中 UNMARKED_SHARED_IMAGE、且永远覆盖不了角度,"
            "而门禁会把它报成「缺正面图」。"
        )


def test_the_angle_on_the_way_in_is_an_enum_not_a_free_string():
    """角度入参卡在 422,不卡在"图片集再也批不过"。

    `angle` 是拿去和 `GenerationPlan.angles_json` 的键比对的。一个拼错的
    `"Front"` 不会报错,它只是**覆盖不到任何角度** —— 表现为图片集批不过,
    而错在三层之外的一个入参。所以它在入参层就必须是枚举。

    出参那一侧刻意是 `str`:库里存的是 String(24),存量值(以及将来枚举
    加成员之前写进去的值)读不回来时不该让整个详情接口 500。
    **入严出宽**,两侧不对称是刻意的。
    """
    src = (BACKEND_ROOT / "app" / "schemas" / "image_set.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    annotations: dict[str, dict[str, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        fields: dict[str, str] = {}
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                fields[stmt.target.id] = ast.unparse(stmt.annotation)
        annotations[node.name] = fields

    assert "ImageAngle" in annotations["ImageItemIn"]["angle"], (
        "入参的 `angle` 退化成了自由字符串 —— 拼错的角度会一路走到"
        "「图片集批不过」才被发现"
    )
    assert annotations["ImageItemIn"]["shared_opt_in"] == "bool", (
        "`shared_opt_in` 在库里 nullable=False;入参可空会让"
        "「没勾」和「勾了又取消」走出两个结果"
    )
    assert "ImageAngle" not in annotations["ImageSetItemOut"]["angle"], (
        "出参也收紧成枚举了 —— 存量值或枚举加成员之前写进去的值"
        "会让详情接口 500,而那不是调用方能修的"
    )


def test_the_section_4_8_dedupe_key_has_been_paid_and_this_is_the_positive_guard():
    """**这条守卫记的是成绩,不是欠账。A45-batch14-26 还清。**

    它的前身是 `test_the_section_4_8_dedupe_key_is_still_owed_and_this_is_the_ledger`,
    一条钉着旧键 `UNIQUE(product_id, sha256)` 的欠账守卫,逾期六批。
    那条守卫自己写着遗嘱:

        它红的时候不要把新键改回旧键让它变绿 —— 让它变绿最省事的做法,
        正好是把 PRD 那条要求撤回去。红了就删掉本守卫,换成正向守卫。

    本函数就是那条正向守卫。**它断言的是覆盖面,不是"有两条索引"。**

    ## 为什么断言覆盖面而不是数索引

    §4.8 那个键落成了两条局部唯一索引(理由见迁移 0044):

        uq_media_assets_spu_colour_sha256   WHERE spu_id IS NOT NULL
        uq_media_assets_product_sha256      WHERE spu_id IS NULL

    这个落法成立的**全部前提**是两个谓词互补 —— 任何一行恰好落在一条键下。
    退化回去最省事的路径不是删掉一条索引(那太显眼),是**把某一条的谓词
    改窄**,比如给兜底那条加上 `AND legacy_kind IS NULL`。那之后仍然有
    两条唯一索引、`spu_id` 仍然在、PRD 那个键仍然在,而中间会裂开一批
    **两条都不管的行** —— 它们彻底失去去重,且没有任何地方会报。

    所以断言的是:两条谓词恰好是 `spu_id IS NOT NULL` 与 `spu_id IS NULL`,
    一字不差,互为反面。
    """
    source = (APP_DIR / "models" / "media_asset.py").read_text(encoding="utf-8")

    assert 'UniqueConstraint("product_id", "sha256"' not in source, (
        "旧的表级 UNIQUE 回来了 —— §4.8 那个键被撤回去了。"
        "这条守卫的前身逐字警告过这条最省事的退路。"
    )

    tree = ast.parse(source)
    cls = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "MediaAsset"
    )
    columns = {
        stmt.target.id
        for stmt in cls.body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    }
    assert "spu_id" in columns, (
        "`media_assets.spu_id` 不见了 —— §4.8 的键要的就是它,"
        "没有这一列时那个键落不下来(0043 是 0044 的前提)。"
    )

    # 两条局部唯一索引的谓词,按 AST 取 `postgresql_where=text("...")` 的字面量
    predicates: list[str] = []
    for node in ast.walk(cls):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Index"):
            continue
        is_unique = any(
            kw.arg == "unique" and getattr(kw.value, "value", False) is True
            for kw in node.keywords
        )
        if not is_unique:
            continue
        for kw in node.keywords:
            if kw.arg != "postgresql_where":
                continue
            inner = kw.value
            if isinstance(inner, ast.Call) and inner.args:
                arg = inner.args[0]
                if isinstance(arg, ast.Constant):
                    predicates.append(arg.value.strip())

    assert sorted(predicates) == ["spu_id IS NOT NULL", "spu_id IS NULL"], (
        "两条唯一索引的谓词不再互补:实际拿到 "
        f"{sorted(predicates)}。"
        "谓词一旦不是严格互为反面,中间就裂开一批**两条键都不管的行** —— "
        "它们彻底失去去重,而约束还在、看上去一切正常。"
    )


def test_the_fallback_dedupe_key_is_owed_a_removal_once_spu_id_goes_not_null():
    """**这条记的是欠账。还款日:阶段 5。**

    兜底那条局部索引(`WHERE spu_id IS NULL`)不是历史遗留,是覆盖面的另一半 ——
    今天存量商品还有 `spu_id` 为空的行。但它**应当自净**:

      - 老建档路径从 14-26 起必带 `spu_id`(`product_service.create_product`),
        所以新行不再落进它;
      - 存量回填完成之后它覆盖零行;
      - 那时 `products.spu_id` / `media_assets.spu_id` 收 NOT NULL、
        删掉这条索引、`variant_key` 退役 —— **四件事是同一个动作**。

    ## 它红的时候该做什么

    这条守卫钉的是「`spu_id` 今天仍然可空」。有人把它收成 NOT NULL 的那天
    它会红,那是**还款的确认**:去删掉兜底索引,并把本守卫也删掉。

    **不要把 `spu_id` 改回可空让它变绿。** 与前身那条一样,让它变绿最省事的
    做法正好是把进展撤回去。
    """
    source = (APP_DIR / "models" / "media_asset.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "MediaAsset"
    )
    ann = next(
        stmt for stmt in cls.body
        if isinstance(stmt, ast.AnnAssign)
        and isinstance(stmt.target, ast.Name)
        and stmt.target.id == "spu_id"
    )
    assert "None" in ast.unparse(ann.annotation), (
        "`media_assets.spu_id` 收成 NOT NULL 了 —— 这笔欠账还清了:"
        "请删掉兜底那条局部唯一索引(WHERE spu_id IS NULL)、删掉本守卫,"
        "并确认 `variant_key` 退役与两个外键收紧在同一个动作里做完。"
    )


def test_the_angle_goes_through_the_normaliser_on_its_way_into_the_row():
    """构造点的 `angle=` 必须是 `_angle_value(...)` 的调用,不是原值。

    `create_set` 收的 items 是 `dict`,值可以来自接口层(已是枚举)也可以
    来自内部调用(可能是字符串、可能是空串)。直接塞原值时**两种形状同时
    进库**:`""` 与 `NULL` 都表示"没标注",而 `covered_angles` 是一个集合
    —— 空串会作为一个成员进去,于是"这个颜色覆盖了几个角度"多算一个,
    而那一个永远匹配不上 `angles_json` 里的任何键。

    表现是角度验收**差一个**,而库里看上去每一行都有值。
    """
    tree = ast.parse(
        (BACKEND_ROOT / "app" / "listings" / "image_set_service.py").read_text(
            encoding="utf-8"
        )
    )
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "ListingImageItem"
    ]
    assert calls, "`ListingImageItem` 的构造点不见了 —— 这条守卫失去了对象"
    for call in calls:
        angle = next((kw.value for kw in call.keywords if kw.arg == "angle"), None)
        assert angle is not None, "构造点没写 `angle`"
        assert (
            isinstance(angle, ast.Call) and getattr(angle.func, "id", "") == "_angle_value"
        ), (
            "`angle` 绕开了归一化直接塞原值 —— 空串与 NULL 会同时进库,"
            "而 covered_angles 是集合,空串会顶掉一个名额"
        )


def test_the_normaliser_collapses_the_empty_string_to_none():
    """`_angle_value("")` 必须是 `None`,不是 `""`。

    这一条与上一条是同一件事的两半:上一条保证**走**归一化,这一条保证
    归一化**做**它自称做的事。少任何一半,空串照样进库。

    断言落在源码形状上而不是调用函数,是因为 `image_set_service` 要
    sqlalchemy —— 纯层导不进来。判据取 `_angle_value` 的返回语句:
    它必须有一支把假值收成 `None`,而不是无条件返回字符串。
    """
    tree = ast.parse(
        (BACKEND_ROOT / "app" / "listings" / "image_set_service.py").read_text(
            encoding="utf-8"
        )
    )
    fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_angle_value"
        ),
        None,
    )
    assert fn is not None, "`_angle_value` 不见了 —— 归一化被删掉或改名了"
    collapsing = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.BoolOp)
        and isinstance(node.value.op, ast.Or)
        and isinstance(node.value.values[-1], ast.Constant)
        and node.value.values[-1].value is None
    ]
    assert collapsing, (
        "`_angle_value` 没有把空串收成 None —— 库里会同时存在 '' 和 NULL 两种"
        "「没标注」,而 covered_angles 只认得其中一种"
    )
