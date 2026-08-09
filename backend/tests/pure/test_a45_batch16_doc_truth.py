"""A45-batch16 的守卫:一句话说的事,和代码在做的事,必须是同一件。

本批修的四处缺陷是同一类,而那一类不是"文档过期":

    create_product 注释    CSV 导入契约曾与实现相反；现在还要防旧断言残留
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


def _code_names_in(node: ast.AST) -> set[str]:
    """只读可执行 AST 的名字；docstring/字符串不能冒充接线证据。"""
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            out.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            out.add(sub.attr)
        elif isinstance(sub, ast.keyword) and sub.arg:
            out.add(sub.arg)
    return out


# --------------------------------------------------------------------------
# 一、CSV 导入路径:实现接闸后,正向契约与反向旧断言必须一起收口
# --------------------------------------------------------------------------
#
# 旧守卫只问「文件里有没有一句新契约」,所以新 docstring 让它变绿以后,
# 同文件另一段仍可继续说相反的话。更隐蔽的是 `_names_in()` 把 docstring 的字符串
# 常量也算成实现标记,一段写着 `spu_id` 的说明就能冒充真正的接线。
#
# 现在分三层:可执行 AST 证明闸在；`import_products` 自己的 docstring 说清正向
# 契约；整个文件不得再残留反向断言。三层缺一不可。

_SPU_GATE_MARKERS = {
    "_resolve_import_identity",
    "garment_block_reason",
    "spu_id",
    "color_variant_id",
    "audience",
}

_STALE_REVERSE_PATTERNS = (
    r"老建档路径\(`create_product`\s*/\s*CSV\s*导入\).{0,100}(?:还不写|不写).{0,30}spu_id",
    r"import_products.{0,100}直接\s*`?Product",
    r"CSV\s*导入.{0,60}(?:不过|绕过).{0,30}(?:SPU|create_product)",
    r"CSV\s*导入.{0,120}(?:仍然|今天还).{0,80}spu_id\s*(?:=|落)?\s*NULL",
)


def _import_gate_is_wired() -> bool:
    nodes = (
        _function(PRODUCT_SERVICE, "import_products"),
        _function(PRODUCT_SERVICE, "_resolve_import_identity"),
    )
    names: set[str] = set()
    for node in nodes:
        names.update(_code_names_in(node))
    return _SPU_GATE_MARKERS <= names


def test_the_csv_import_path_and_the_comment_about_it_tell_the_same_story():
    """接线、正向契约与全文件旧断言必须说同一个答案。"""
    source = PRODUCT_SERVICE.read_text(encoding="utf-8")
    fn = _function(PRODUCT_SERVICE, "import_products")
    doc = ast.get_docstring(fn) or ""
    assert _import_gate_is_wired(), "CSV 导入的 SPU / 颜色身份闸从可执行代码里消失了"
    for phrase in ("要求 SPU 先存在", "variant_code", "单颜色", "errors"):
        assert phrase in doc, f"`import_products` 的正向契约漏了 {phrase!r}"

    stale = [
        pattern
        for pattern in _STALE_REVERSE_PATTERNS
        if re.search(pattern, source, flags=re.S)
    ]
    assert not stale, (
        "CSV 身份闸已经接上,但 product_service.py 仍残留反向断言:\n"
        + "\n".join(stale)
    )


def test_the_import_gate_detector_ignores_docstrings_and_string_literals():
    """说明文字不能替可执行代码满足接线标记。"""
    fake = ast.parse(
        'def import_products():\n'
        '    """_resolve_import_identity spu_id color_variant_id audience"""\n'
        '    note = "garment_block_reason"\n'
    ).body[0]
    assert isinstance(fake, ast.FunctionDef)
    assert not (_code_names_in(fake) & _SPU_GATE_MARKERS)


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
