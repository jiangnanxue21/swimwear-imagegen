"""A45-batch14-15:素材归属外键落库,四批欠账收其二。

这一批加的是一**列**(`media_assets.color_variant_id`,迁移 0037)加一条
**接线**。判定一行都没新增 —— 14-9 / 14-10 / 14-13 三批早就把判定验完了,
它们缺的一直是这一列。

## 五条守卫,五种「不响」的坏法

变异先跑出 2/7,五条漏网的都在这里补上。它们的共同点是**改完之后本机
一个断言都不会动**:

    列改成 NOT NULL      迁移在任何有存量数据的库上升不上去(而本机没有库)
    分桶的结果没人接      算好了没人读
    新字段给宽松默认      漏填的表现是悄悄放行(硬规则 4 第二次事故的形状)
    适配器读 variant_hint 模型猜出来的值决定人工确认过的事实何时过期
    适配器读错列名        `content_hash` 恒为 None —— 换了文件指纹不变
"""
from __future__ import annotations

import ast
import re

from app.attributes import scope_fingerprint as fp
from app.workbench.flow import MaterialFacts
from tests.pure._helpers import BACKEND_ROOT, window

APP = BACKEND_ROOT / "app"
MIGRATIONS = BACKEND_ROOT / "migrations" / "versions"


def _src(rel: str) -> str:
    return (APP / rel).read_text(encoding="utf-8")


class _Row:
    """真素材行的最小替身:列名用**模型上的那一套**(id / sha256)。"""

    def __init__(self, **kw):
        self.id = kw.get("id", "row-1")
        self.sha256 = kw.get("sha256", "hash-1")
        self.source = kw.get("source", "MANUAL_UPLOAD")
        self.role = kw.get("role", "PRODUCT_FRONT")
        self.status = kw.get("status", "READY")
        # §6.2 门禁只认人定的角色 —— 替身不补这一项的话,
        # 它一条都过不了闸,而那会让下面那条一致性守卫平凡通过
        self.role_source = kw.get("role_source", "HUMAN")
        self.color_variant_id = kw.get("color_variant_id")
        self.variant_hint = kw.get("variant_hint")


# ================================================================ 一、列本身


def test_the_attribution_column_stays_nullable():
    """**可空,而且不给默认值。**

    存量素材的归属是「未确认」—— 那和「通用」不是一回事,也和「属于某个
    颜色」不是一回事。NOT NULL 的后果分两层:迁移在任何有存量数据的库上
    直接升不上去(而这台机器没有库,所以本机测不出来);就算升上去了,
    每一张历史图都会被迫认领一个颜色。
    """
    model = _src("models/media_asset.py")
    block = window(model, "class MediaAsset", "class MediaDerivative")
    # `    )` 是**分隔符**不是地标(块里有很多个),所以不能拿它喂 `window()` ——
    # 按行扫到这条声明自己的收尾。三种范围三种工具,§3.30 第三节
    lines = block.splitlines()
    begin = next(i for i, ln in enumerate(lines) if "color_variant_id: Mapped" in ln)
    stop = next(i for i in range(begin + 1, len(lines)) if lines[i].rstrip() == "    )")
    decl = "\n".join(lines[begin : stop + 1])
    assert "nullable=True" in decl, "归属外键不再可空 —— 存量素材没有归属可填"
    assert "Mapped[UUID | None]" in decl, "类型标注说它不可空"
    assert "default" not in decl, (
        "归属外键有了默认值 —— 那等于替存量素材宣布它们属于某个颜色"
    )


def test_the_migration_is_additive_and_reversible():
    """这条迁移必须是纯增列:不动既有行,回退拿得回去。

    它能先走、四批欠账能先收,靠的就是「不需要回填、不加约束」——
    §4.8 另外那几样(溯源列、evidence_class 的 CHECK、新去重键)各自要一次
    回填或一条约束,所以刻意不在这一版里。
    """
    text = (MIGRATIONS / "0037_media_asset_color_variant.py").read_text(encoding="utf-8")
    assert 'revision: str = "0037"' in text and 'down_revision: Union[str, None] = "0036"' in text
    up = window(text, "def upgrade()", "def downgrade()")
    assert "add_column" in up and "nullable=True" in up
    for forbidden in ("UPDATE ", "execute(", "alter_column"):
        assert forbidden not in up, f"这条迁移动了既有数据/既有列:{forbidden}"
    down = window(text, "def downgrade()", what="downgrade")
    assert "drop_column" in down, "回退拿不回去"


# ================================================================ 二、接线


def test_the_bucketed_result_is_actually_handed_to_material_facts():
    """算好了得有人接。

    变异 W1 把关键字改名(`variant_gate_roles_unused=`),而**调用还在** ——
    只断言「那个函数被调用过」的守卫对它完全隐形。所以这里钉的是
    `MaterialFacts(...)` 的**关键字**,不是函数调用。
    """
    tree = ast.parse(_src("workbench/service.py"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_material_facts"
    )
    calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "MaterialFacts"
    ]
    assert calls, "找不到 MaterialFacts(...) 的构造"
    keywords = {kw.arg for kw in calls[0].keywords}
    assert "variant_gate_roles" in keywords, (
        "按颜色分好的桶没有交给 MaterialFacts —— 算好了没人读"
    )
    assert "gate_roles" in keywords, "SPU 那一半也丢了"


def test_the_new_field_has_no_lenient_default():
    """新字段的默认值必须是**空**,不是「退回 gate_roles」。

    宽松默认的漏填表现是悄悄放行:每个颜色都拿到全 SPU 的角色集合,
    于是「B 色一张正面图都没有」被判成合格。硬规则 4 第二次事故的形状,
    14-10 的 C2 已经演过一次。
    """
    assert MaterialFacts().variant_gate_roles == {}, (
        "颜色门禁角色有了宽松默认值 —— 漏填的表现是每个颜色都合格"
    )
    facts = MaterialFacts(gate_roles=frozenset({"PRODUCT_FRONT"}))
    assert facts.variant_gate_roles == {}, "没传颜色桶时退回了 gate_roles"


# ================================================================ 三、适配器


def test_the_fingerprint_adapter_translates_the_column_names():
    """§5.3 的元素清单要 `asset_id` / `content_hash`,模型上的列名是
    `id` / `sha256`。翻译放在判定那一侧,不放调用点。

    读错列名不会报错:`getattr` 拿不到就是 None,而 `content_hash` 恒为 None
    的后果是**换了文件指纹也不变** —— 一份该过期的事实带着「指纹没变」
    的证明进了导出。
    """
    view = fp.AssetView(_Row(id="a1", sha256="deadbeef"))
    assert view.asset_id == "a1"
    assert view.content_hash == "deadbeef", "内容哈希没翻译过来 —— 换文件指纹不变"
    # 真的进了指纹:两张只有内容不同的图必须算出不同的指纹
    one = fp.shared_fingerprint(fp.views([_Row(sha256="aaa")]))
    two = fp.shared_fingerprint(fp.views([_Row(sha256="bbb")]))
    assert one != two, "内容哈希没有进指纹"


def test_the_adapter_reads_the_attribution_column_not_the_hint():
    """**这是三批各自拒绝过一次的那件事。**

    指纹决定「一份已确认的事实什么时候过期」。读 `variant_hint` 等于让
    模型猜出来的值去让人工确认过的事实过期 —— 方向是反的,而且不报错。
    """
    view = fp.AssetView(_Row(color_variant_id=None, variant_hint="RED"))
    assert view.color_variant_id is None, "归属为空时拿 hint 顶上了"
    assert fp.AssetView(_Row(color_variant_id="cv-1")).color_variant_id == "cv-1"

    source = _src("attributes/scope_fingerprint.py")
    body = window(source, "class AssetView", "def views(", what="适配器本体")
    assert "variant_hint" not in body.split('"""')[-1], "适配器读了 variant_hint"


def test_the_adapter_survives_a_row_that_has_none_of_the_columns():
    """列名一个都对不上的行也要答得出来 —— `getattr` 而不是属性访问。

    与 `evidence_rules.asset_is_extraction_input` 同一处理:属性访问会在
    列落库前当场炸,写死 None 会在列落库之后继续瞎。
    """

    class _Bare:
        pass

    view = fp.AssetView(_Bare())
    assert view.asset_id is None and view.color_variant_id is None


def test_the_two_scopes_still_mean_the_same_thing_everywhere():
    """接线之后再钉一次:同一批素材,指纹与完整度门禁切出同一组颜色。

    分叉的表现极隐蔽 —— 一个模块说「这次覆盖了 A 和 B」,另一个说
    「只有 A 的指纹变了」,而没有任何地方会把两句话放在一起比较。
    """
    from app.media import sample_completeness as sc

    rows = [_Row(color_variant_id="A"), _Row(color_variant_id="B"), _Row()]
    from_fp = {k for k in fp.fingerprints(fp.views(rows)) if k}
    from_gate = {
        v for v in ("A", "B", "C") if sc.variant_gate_roles(rows, v)
    }
    assert from_fp == {"A", "B"} == from_gate, (
        f"指纹切出 {sorted(from_fp)},门禁切出 {sorted(from_gate)}"
    )


def test_the_migration_chain_has_exactly_one_head():
    """链上必须只有一个 head。**不点名是哪一条。**

    并发的几条线各自加一条 0037 时,合树的人无法在本地解决 ——
    §3.29 第二节把迁移链定成**单写者资源**,本批是那条规矩之后第一次
    真的动它,所以在这里留一条断言:下一个动链的人会先看见它。

    **原来这条写的是 `heads == {"0037"}`** —— 点了一个每加一条迁移就会变的
    值。下一批(14-16)加 0038 时它当场变红,而红的原因是「有人正常地加了
    一条迁移」。CLAUDE.md 里「条数不写在这里」那条规矩说的是同一件事:
    **规矩要写成不变式(只有一个 head),不是写成当时那个值。**
    好在这次是红不是静默过期 —— 但它仍然是噪音。

    ## A45-batch14-21:这条守卫十二批以来一直是瞎的

    正则原来写的是 `^revision: str = "..."`,**只认带类型标注那一种写法**。
    而迁移 0041 写的是 `revision = "0041"`(裸赋值),于是它从来没进过这张
    链表。

    后果不是当时会红,是当时**碰巧绿**:0041 是 head,不出现在任何
    `down_revision` 里,它不在 keys 也就不在 heads,`len(heads) == 1` 照样
    成立。守卫说"链上只有一个 head",而它看的是一条缺了一节的链。

    照出它的是 0042 —— 挂在 0041 下面,断点第一次落在链**中间**,heads
    变成 `{0040, 0042}`。红的信息对,红的理由错:不是有人建了两个 head,
    是它一直数不全。

    **修法不是顺手放宽正则。** 放宽之后它照样证明不了自己看全了 ——
    下一种写法(`revision: Final = ...`、多行赋值)会让同一件事再来一次,
    而且同样表现为"碰巧绿"。所以这次加的是一条**覆盖率断言**:解析出的
    revision 条数必须等于 `versions/` 下的迁移文件数。少看见一个文件当场红。

    正则本身与 `tools/verify_delivery.check_the_migration_chain_has_a_single_head`
    对齐 —— 那一条一直是对的,两处同一条不变式两个实现,瞎了的是这一个。
    真要收成一份的话该收进被两边 import 的地方,而纯层不许 import
    `tools/`;所以这里的做法是**让它自己证明自己看全了**,而不是指望
    两份实现永远同步。
    """
    files = sorted(p for p in MIGRATIONS.glob("*.py") if p.name != "__init__.py")
    assert files, "versions/ 下一个迁移文件都没有 —— 这条守卫失去了对象"

    revisions: dict[str, str | None] = {}
    for path in files:
        text = path.read_text(encoding="utf-8")
        rev = re.search(r"^revision(?::\s*[^=]+)?\s*=\s*[\"']([^\"']+)[\"']", text, re.M)
        down = re.search(
            r"^down_revision(?::\s*[^=]+)?\s*=\s*(?:[\"']([^\"']+)[\"']|None)", text, re.M
        )
        assert rev, f"{path.name} 里找不到 revision 声明 —— 它不会进链表,而那不报错"
        revisions[rev.group(1)] = down.group(1) if down and down.group(1) else None

    # **覆盖率断言。** 少了它,一种没被正则认出来的写法会让那个文件静静
    # 缺席,而缺席的表现取决于它在链上的位置:在末端就碰巧绿,在中间才红。
    # 一条守卫的正确性不该取决于被它漏掉的东西恰好在哪
    assert len(revisions) == len(files), (
        f"解析出 {len(revisions)} 条 revision,而 versions/ 下有 {len(files)} 个迁移文件"
        " —— 有文件没被认出来(重号会覆盖,新写法会漏掉)。"
        "这条守卫在看一条缺了节的链,而缺节时它可能碰巧是绿的"
    )

    referenced = {d for d in revisions.values() if d}
    heads = sorted(set(revisions) - referenced)
    assert len(heads) == 1, f"迁移链有 {len(heads)} 个 head:{heads}"
    assert "0037" in revisions, "本批那条 revision 不在链上了"
