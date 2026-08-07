"""A45-batch16 的守卫:一句话说的事,和代码在做的事,必须是同一件。

本批修的四处缺陷是同一类,而那一类不是"文档过期":

    create_product 注释    「CSV 导入从此要求 SPU 先存在」—— 导入路径根本不调它
    README                 「make check 不需要网络」—— check 依赖 fe-check,要 npm ci
    audit_anchors docstring 「今天只剩 mutate_contract_tests.py 用 CASES」—— 它已退役
    四份纯测试 docstring     「真库层在 tests/test_api_*.py」—— 那些文件从来不存在

四句话的共同点是**它们都在宣告一件没发生的事**,而且宣告的方向一致:
**都说缺口是关着的**。过期的文档让人多走弯路;这种文档让人不走 ——
读到"那边已经覆盖了"的人不会再去看那边有没有东西。

所以守卫按 §3.33 的方向写:**钉两份真相之间的一致性,不钉其中任何一份的现状。**
分界线是 §3.31 那条 —— 钉现状的守卫会因为进步而变红,而那种红会训练人去改守卫。
下面每一条都能同时容纳"缺口开着"和"缺口关上",只拒绝"两份真相对不上"。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_ROOT.parent
PRODUCT_SERVICE = BACKEND_ROOT / "app" / "services" / "product_service.py"
PRD = PROJECT_ROOT / "docs" / "swimwear_sample_to_listing_prd_v3_1_1.md"
README = PROJECT_ROOT / "README.md"
MAKEFILE = PROJECT_ROOT / "Makefile"


def _function(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(hits) == 1, f"{path.name} 里 {name} 有 {len(hits)} 个定义"
    return hits[0]


def _names_in(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            out.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            out.add(sub.attr)
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.add(sub.value)
    return out


# --------------------------------------------------------------------------
# 一、CSV 导入路径:实现走不走 SPU 闸,与注释说的必须一致
# --------------------------------------------------------------------------
#
# 这一条**不判缺口开着还是关着**。缺口今天开着(import_products 直构 Product),
# 而怎么关它是一个决定不是一段代码(§3.41):可以让 SPU 缺席的行计入 errors,
# 也可以按 CSV 里的 spu 码自动建最简 SPU —— 两种对运营的可感知行为不一样。
#
# 守卫钉的是:**哪天有人做了那个决定,这个文件里那几句话得跟着改。**
# 关缺口 + 改注释 = 绿;关缺口 + 不改注释 = 红;不关缺口 + 不改注释 = 绿。

#: 判"这条路解析了 SPU"的证据。任一出现即认为闸接上了
_SPU_GATE_MARKERS = {"Spu", "spu_id", "spu_code", "create_product", "garment_block_reason"}

#: 注释里"CSV 导入要求 SPU 先存在"的说法。出现即认为文档声称闸接上了
_CLAIM_PATTERNS = (
    r"CSV\s*导入从此\s*要求\s*SPU",
    r"CSV\s*导入.{0,12}要求\s*SPU\s*先存在",
)

#: 引述并驳斥的标记。与 `tools/audit_doc_refs.py` 是同一条口径:
#: **把一句假话原样引出来再驳掉,是本批修法的一部分,不能被自己的守卫拦下。**
#: 窗口同样是封闭的 —— 标记要挨着那句引文,不能挨着这份文件
_REFUTATION_MARKERS = ("那句话是错的", "是假的", "并不成立", "原来写的是", "**那句话是错的**")

#: 窗口半径 = 0 行:驳斥必须与引文**在同一行**。
#:
#: 这个 0 是变异验证逼出来的,不是一开始就想到的。原来写 2,
#: 于是 M1(把假注释改回去)不响 —— 那段注释里本来就有一句
#: 「原来写的是……那句话是错的」,它在 ±2 行内**替另一句假话作了担保**。
#: 一句驳斥只能管它自己引的那句,管不了邻居。
_REFUTATION_RADIUS = 0


def _claims_csv_requires_spu(source: str) -> list[str]:
    """返回**没有被当场驳斥**的声称。引文旁边有驳斥标记的不算。"""
    lines = source.splitlines()
    live: list[str] = []
    for pattern in _CLAIM_PATTERNS:
        for match in re.finditer(pattern, source):
            line_no = source.count("\n", 0, match.start())
            lo = max(0, line_no - _REFUTATION_RADIUS)
            hi = min(len(lines), line_no + _REFUTATION_RADIUS + 1)
            window = "\n".join(lines[lo:hi])
            if any(marker in window for marker in _REFUTATION_MARKERS):
                continue
            live.append(pattern)
    return live


def test_the_csv_import_path_and_the_comment_about_it_tell_the_same_story():
    """`import_products` 过不过 SPU 闸,与注释里的说法必须一致。

    原文那句「CSV 导入从此要求 SPU 先存在。这是真实的行为变更」是**假的**,
    而它是这个文件里最贵的一行 —— 它告诉下一个人缺口已经关了。
    实现里 `import_products` 直接 `Product(**row)`:不解析 spu 码、
    不抄 audience、不过 C-03 闸,写进去的行 `spu_id` 是 NULL。

    退化回去最省事的路径有两条,这条守卫两条都钉:

        真关了缺口但忘了改注释  → 下一个人以为还开着,重复做一遍
        没关缺口却写着关了      → 就是本批修的那一条
    """
    source = PRODUCT_SERVICE.read_text(encoding="utf-8")
    fn = _function(PRODUCT_SERVICE, "import_products")
    reached = _names_in(fn) & _SPU_GATE_MARKERS

    claims = _claims_csv_requires_spu(source)

    if reached and not claims:
        raise AssertionError(
            f"`import_products` 现在碰得到 {sorted(reached)} —— 看起来 SPU 闸接上了,\n"
            "但这个文件里没有任何一句说明 CSV 导入的新契约。\n"
            "**关缺口和改说明是同一件事的两半**:只做前一半,\n"
            "下一个读 `create_product` 那段注释的人会以为 CSV 那条路还开着。"
        )
    if claims and not reached:
        raise AssertionError(
            "这个文件里写着「CSV 导入要求 SPU 先存在」,而 `import_products` 的函数体里\n"
            f"找不到 {sorted(_SPU_GATE_MARKERS)} 中的任何一个 —— 它仍然直构 `Product(**row)`。\n"
            "**这句话在宣告一件没发生的事**,而且宣告的方向是「缺口已关」。\n"
            "要么把闸接上,要么把这句话改成实话。"
        )


def test_the_import_docstring_names_the_gap_instead_of_implying_it_is_closed():
    """缺口开着的时候,`import_products` 的 docstring 必须把它说出来。

    上一条守的是"不许说假话",这一条守的是"不许沉默"。两者不能互相替代:
    一份只写「落库导入结果、重复执行安全」的 docstring 里没有一个字是假的,
    而它照样让人以为这条路和 `create_product` 走的是同一套校验。

    缺口关上之后这条自动失效(前半个条件不成立),不需要有人回来删它。
    """
    fn = _function(PRODUCT_SERVICE, "import_products")
    if _names_in(fn) & _SPU_GATE_MARKERS:
        return  # 闸接上了,这条守卫没有对象了

    doc = ast.get_docstring(fn) or ""
    assert "SPU" in doc, (
        "`import_products` 绕过 SPU 闸,而它的 docstring 一个字没提 —— \n"
        "读的人没有任何理由怀疑这条路和 `create_product` 不一样。"
    )
    assert any(k in doc for k in ("缺口", "绕过", "不过")), (
        "docstring 提到了 SPU,但没说清这条路**绕过**了闸。\n"
        "「导入时会处理 SPU」这种含混说法比不提更糟:它读起来像覆盖到了。"
    )


# --------------------------------------------------------------------------
# 二、PRD 的 v3.0 悬空引用清册,必须与正文里真正悬空的章节逐一相等
# --------------------------------------------------------------------------
#
# 同样不钉数量。补回 v3.0 之后逐节消化 → 表跟着缩短 → 一直是绿的;
# 新增一处"沿用 v3.0"却不记进表 → 红。

_DANGLING_RE = re.compile(r"(沿用|保留|同)\s*v3\.0|v3\.0\s*(原文|全部|十)")

#: 清册那张表的行首:`| §6.2 | ...`;§7.4 / §7.5 那行一格里写了两个号
_TABLE_ROW_RE = re.compile(r"^\|\s*(§[\d.]+(?:\s*/\s*§[\d.]+)*)\s*\|")

#: 正文里的章节标题
#: 章节标题:`#` 之后紧跟一个号。「## 6.2 步骤 2:...」里的「步骤 2」不是章节号,
#: 所以只认**章节号位置**上的数字 —— 行首,或者顿号/斜杠之后
#: (真有这种标题:「## 7.4 页面固定信息、7.5 交互约束」一行管两节)
_HEADING_RE = re.compile(r"^#{1,3}\s+\d+(?:\.\d+)?")
_SECTION_NUM_RE = re.compile(r"(?:^|[、/,])\s*(\d+(?:\.\d+)?)")


def _sections_with_dangling_refs() -> set[str]:
    """正文里真正出现悬空 v3.0 引用的章节号。清册那一节自己不算。"""
    found: set[str] = set()
    current: list[str] = []
    in_inventory = False
    for line in PRD.read_text(encoding="utf-8").splitlines():
        if line.startswith("## 前置:"):
            in_inventory = True
            continue
        if in_inventory and line.startswith("## 0."):
            in_inventory = False
        if in_inventory:
            continue
        if _HEADING_RE.match(line):
            current = _SECTION_NUM_RE.findall(line.lstrip("# ").split("v3.0")[0])
            # 标题自己就带"保留 v3.0"的,算这一节的
            if _DANGLING_RE.search(line):
                found.update(current)
            continue
        if current and _DANGLING_RE.search(line):
            found.update(current)
    return found


def _sections_listed_in_inventory() -> set[str]:
    listed: set[str] = set()
    in_inventory = False
    for line in PRD.read_text(encoding="utf-8").splitlines():
        if line.startswith("### 甲、"):
            in_inventory = True
            continue
        if in_inventory and line.startswith("### "):
            break
        if not in_inventory:
            continue
        row = _TABLE_ROW_RE.match(line)
        if row:
            for token in row.group(1).split("/"):
                listed.add(token.strip().lstrip("§"))
    return listed


def test_the_prd_inventory_of_missing_v3_0_text_matches_the_body():
    """清册列的章节 == 正文里真正悬空的章节。

    这张表是本批唯一能对 AC-01~AC-20 那笔债做的事:**原文补不回来,
    但可以让它不再是一句「§14.1 沿用 v3.0」里藏着的一行。**

    为什么钉集合而不是数量:数量会因为进步变红(§3.31),而集合不会 ——
    补回 v3.0 之后逐节消化,两边一起缩短。
    """
    body = _sections_with_dangling_refs()
    listed = _sections_listed_in_inventory()

    missing = body - listed
    stale = listed - body

    assert not missing, (
        f"正文这些章节里有悬空的 v3.0 引用,清册里没有:{sorted(missing)}\n"
        "新增「沿用 v3.0」的同时要把它记进 PRD 开头那张表 —— \n"
        "否则这笔债又变回一句藏在正文里的话。"
    )
    assert not stale, (
        f"清册列了这些章节,正文里已经查不到悬空引用:{sorted(stale)}\n"
        "如果是把原文补回来了,把对应行从表里删掉;这是好消息,不是失败。"
    )


def test_the_prd_filename_matches_the_version_it_declares():
    """文件名里的版本号,要和正文自报的版本一致。

    原来文件叫 `..._prd_v3_1.md`,内容第 3 行写着 v3.1.1。差一个小版本号,
    而这个仓库里"v3.1 说的"和"v3.1.1 说的"是两回事(§0.4 专门讲两者差别)。
    """
    declared = re.search(r"\*\*版本:\*\*\s*v([\d.]+)", PRD.read_text(encoding="utf-8"))
    assert declared, "PRD 正文里找不到 `**版本:**` 那一行"
    in_name = PRD.stem.split("_prd_v")[-1].replace("_", ".")
    assert in_name == declared.group(1), (
        f"文件名说 v{in_name},正文说 v{declared.group(1)} —— 引用这份文档的人\n"
        "会按文件名称呼它,于是同一份文档在两个名字下被讨论。"
    )


# --------------------------------------------------------------------------
# 三、README 讲的 `make check`,要和 Makefile 里的 `make check` 是同一个东西
# --------------------------------------------------------------------------


def test_readme_does_not_call_make_check_offline_when_the_makefile_says_otherwise():
    """README 不许把 `make check` 描述成不需要网络。

    原文写的是「make check # 离线全部门禁:1270+ 纯逻辑用例。不需要 node_modules,
    不需要网络」。而 Makefile 里 `check: check-offline fe-check`,`fe-check` 第一步
    就是 `npm ci`。**照着旧文案在一台没网的机器上敲 `make check`,会得到一个
    装依赖失败的红,然后以为门禁本身坏了。**

    钉法:从 Makefile 读出 `check` 的依赖,如果它依赖 `fe-check`,
    README 里 `make check` 那一行就不许出现"不需要网络"。
    Makefile 改成真离线的那天,这条自动放行。
    """
    makefile = MAKEFILE.read_text(encoding="utf-8")
    rule = re.search(r"^check:\s*(.*)$", makefile, re.MULTILINE)
    assert rule, "Makefile 里找不到 `check:` 目标"
    needs_network = "fe-check" in rule.group(1)

    line = next(
        (
            ln
            for ln in README.read_text(encoding="utf-8").splitlines()
            if ln.strip().startswith("make check ")
        ),
        None,
    )
    assert line is not None, "README 的测试段里找不到 `make check` 那一行"

    if needs_network:
        assert "不需要网络" not in line, (
            f"Makefile 里 `check: {rule.group(1).strip()}` —— 它要跑 `fe-check`(`npm ci`),\n"
            f"而 README 这一行说它不需要网络:\n    {line.strip()}\n"
            "离线跑得动的是 `check-offline`。"
        )
