#!/usr/bin/env python3
"""文档与注释里的路径引用体检:每一条指过去的路,那头有没有东西。

## 它钉的是哪一件事

**指错路的引用不报错、不变红,只是把下一个人送进空地。** 而这个仓库里
它最贵的形态不是"链接坏了",是**一句失实的话替一段代码背书**:

    audit_anchors.py 的 docstring 说「今天只剩 mutate_contract_tests.py 用
    CASES 形状」—— 那个脚本 batch14-3 就退役了,全仓今天零个 CASES 表。
    那段分支于是被一句失实的理由养活着:读到「不认它就漏 18 条」的人会放过它。

    test_attribute_extraction.py 说「真库层的闭环在 tests/test_api_attributes.py」
    —— 那个文件从来不存在。**它宣告了一层根本没人写的覆盖。**

两条都不是排版问题。第一条让死代码活下来,第二条让缺口看起来是关着的。
`verify_delivery.py` 盯的是"门禁有没有人调用",`audit_anchors.py` 盯的是
"变异锚点还在不在" —— **没有任何一道门禁盯"这句话指的东西还在不在"**,
这份补的正是那个空档。

## 两档严重度,只有一档会拦

    ERROR   活文档 + 活代码里的引用。这些是给人指路用的,指错就是把人送错地方
    WARN    历史台账(STATUS.md / DECISIONS.md / REVIEW.md 等)里的引用

分档不是宽严之别,是**这两类文本的语义不同**。台账是追加写的历史记录:
"batch14-21 的合入说明写在 MERGE-A45-BATCH14-21-FACTS-STALE.md" 这句话
在写下的那天是真的,那份文档后来按 CLAUDE.md「过程文档不留档」的规矩删掉了,
**这句话仍然是那天的事实**。把它判成错误,只会逼人去改历史,
或者把台账整个加进白名单 —— 两条路都比现状糟。

被删的过程文档另有去处:`docs/DECISIONS.md` §3.42 的归档台账逐份记了
它们的结论。台账里的引用因此不是断头路,是指向归档条目的。

## 点名一个已经不在的文件,是允许的 —— 但标记必须挨着它

这道门禁最容易长成的坏形状,是**逼人删掉解释**。

    「这里原来指向 tests/test_api_attributes.py,那个文件从来不存在」

这句话里的路径指不到东西,而这正是它要说的事。如果门禁拦下它,
被拦的人最省事的做法是把整句删掉 —— 于是下一个人重新踩一遍同一个坑,
而门禁是绿的。**一道让人删掉真话的门禁,比没有门禁贵。**

所以带退役标记的引用放行(`_RETIRED_MARKERS`)。但窗口是封闭的:
标记必须落在**同一行或紧邻的上下一行**里。这条限制不是形式主义,
是 `audit_source_guards.py` 那条规矩的同一件事 —— 窗口开得越宽,
"文件顶部一句『本文件提到的部分脚本已退役』" 就越能把整份文件里
所有失实引用一次性豁免掉,而那正是这道门禁要抓的东西。

判据一句话:**标记要挨着它说的那个名字,不能挨着这份文件。**

## 白名单要写理由,不写就等于没判

`_ALLOW` 里每一条都带一句为什么。**没有理由的白名单会长大** ——
下一个被这道门禁拦住的人,最省事的做法是把自己那条加进去。
带理由的那一栏至少要求他把理由写出来,而写不出理由的时候人会去修代码。

用法:

    python3 tools/audit_doc_refs.py           # ERROR 非空则 EXIT=1
    python3 tools/audit_doc_refs.py --all     # 连 WARN 一起列出来
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent

#: 不进扫描的目录。`vendor/` 是第三方文档,它指的是它自己仓库里的路径
_SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "dist",
    "build",
    ".vite-cache",
    ".pytest_cache",
    ".mypy_cache",
    "vendor",
}

_SCAN_SUFFIXES = {".md", ".py", ".ts", ".tsx", ".mjs", ".js", ".yml", ".yaml"}
_SCAN_NAMES = {"Makefile"}

#: 历史台账:里面的引用降级成 WARN。理由见模块 docstring
#:
#: 名单**只放真的需要它的**。这里原来还有 AC-VERIFICATION / REVIEW-A28-TRACKING /
#: REVIEW-A44-A45-MERGED 三份 —— 是我防御性加的,没验过它们需不需要。
#: 实测三份里前两份一处 ERROR 都没有,第三份只有一处(指向已删的 BATCH8),
#: 改成过去时之后也干净了。**一条没有理由的豁免,和一条错的豁免一样会长大。**
_LEDGERS = {
    "docs/STATUS.md",
    "docs/DECISIONS.md",
    "docs/REVIEW.md",
    "HANDOVER.md",
}

#: `docs/notes/` 整个目录同属台账,理由和上面那四份**完全一样**,只是它的形状
#: 是一目录而不是一文件:每一篇 note 记的都是"写下那天的事实",而 note 存在的
#: 全部意义就是承载那些主文档不该再背的历史 —— 里面点名的脚本、批次文档、
#: 过程文件本来就有一大半已经按规矩删掉了。
#:
#: 把它判成 ERROR 只会逼人去改档案,或者把整个目录塞进 _ALLOW,而后者等于
#: 关掉这道门禁对文档目录的覆盖。降级成 WARN,和台账同档。
_LEDGER_DIRS = ("docs/notes/",)

#: 路径样式:已知顶层目录开头、以已知扩展名结尾。
#: 刻意不认裸文件名(`foo.py`)—— 那种大多是在说"一个叫 foo 的东西",不是路径
_PATH_RE = re.compile(
    r"(?<![\w./-])"
    r"((?:backend|frontend|docs|tools|comfyui|sample-data|migrations|app|src|tests|scripts|\.github)"
    r"[\w./-]*\.(?:py|ts|tsx|md|yaml|yml|json|sh|mjs|js|cfg|toml|sql|txt))"
    r"(?![\w])"
)

#: 解析基准。同一条引用在任一基准下存在就算通过 ——
#: 后端文档写 `tests/pure/test_variant_key.py`、根文档写全路径,两种都是对的
_BASES = ("", "backend", "frontend", "backend/app", "frontend/src")

#: 退役标记。带上其一(且在封闭窗口内)的引用放行 —— 它说的正是"这个已经不在了"
#: 刻意不收「见」「参考」「详见」这类指路词:那些是**要人走过去**的意思
_RETIRED_MARKERS = (
    "退役",
    "已删",
    "不存在",
    "没有存在过",
    "从来不存在",
    "树里不存在",
    "树里没有",
    "今天不在",
    "原来是",
    "原来指向",
    "原来写的是",
    "原来用",
    "并入",
    "搬进",
    "搬到",
    "移到",
    "改名成",
    "换成",
    "拆成",
    "此前不存在",
)

#: 窗口半径:标记要落在引用所在行的上下各一行之内。为什么不放宽,见模块 docstring
_MARKER_RADIUS = 1

#: 白名单:{引用 -> 为什么它指不到东西却是对的}
_ALLOW: dict[str, str] = {
    "migrations/versions/NNNN_名字.py": "占位模板,`NNNN` 与中文名都是让人替换的槽位",
    "app/thing.py": "举例用的假路径(batch14-3 守卫在演示一条规则长什么样)",
    "backend/app/thing.py": "同上",
    "comfyui/config.yaml": "运营自己从 config.example.yaml 拷出来的,`.gitignore` 第 8 行排除",
}


def _iter_files():
    for path in sorted(PROJECT_ROOT.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.suffix in _SCAN_SUFFIXES or path.name in _SCAN_NAMES:
            yield path


def _resolves(ref: str) -> bool:
    return any((PROJECT_ROOT / base / ref).exists() for base in _BASES)


def _marked_retired(lines: list[str], line_no: int) -> bool:
    """引用所在行的上下 `_MARKER_RADIUS` 行里,有没有退役标记。

    行号从 1 起。窗口刻意开得很窄 —— 宽窗口会让一句文件级的免责声明
    豁免掉整份文件,而那正是这道门禁要抓的东西。
    """
    lo = max(0, line_no - 1 - _MARKER_RADIUS)
    hi = min(len(lines), line_no + _MARKER_RADIUS)
    window = "".join(lines[lo:hi])
    return any(marker in window for marker in _RETIRED_MARKERS)


def collect() -> tuple[list[tuple[str, str, int]], list[tuple[str, str, int]]]:
    """返回 (errors, warns),每条是 (引用, 出现的文件, 行号)。"""
    errors: list[tuple[str, str, int]] = []
    warns: list[tuple[str, str, int]] = []
    for path in _iter_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        is_ledger = rel in _LEDGERS or rel.startswith(_LEDGER_DIRS)
        bucket = warns if is_ledger else errors
        lines = text.splitlines(keepends=True)
        for match in _PATH_RE.finditer(text):
            ref = match.group(1)
            if ref in _ALLOW or _resolves(ref):
                continue
            line = text.count("\n", 0, match.start()) + 1
            if _marked_retired(lines, line):
                continue
            bucket.append((ref, rel, line))
    return errors, warns


def _render(rows: list[tuple[str, str, int]], colour: str) -> None:
    by_ref: dict[str, list[str]] = {}
    for ref, rel, line in rows:
        by_ref.setdefault(ref, []).append(f"{rel}:{line}")
    for ref in sorted(by_ref):
        sites = by_ref[ref]
        print(f"  \033[{colour}m{ref}\033[0m  ({len(sites)} 处)")
        for site in sites:
            print(f"      {site}")


def main(argv: list[str]) -> int:
    show_warns = "--all" in argv
    errors, warns = collect()

    if errors:
        print("\033[31m活文档 / 活代码里指不到东西的路径引用:\033[0m")
        _render(errors, "31")
        print()

    if warns and show_warns:
        print("\033[33m历史台账里的引用(不拦,见 audit_doc_refs.py 顶部为什么):\033[0m")
        _render(warns, "33")
        print()

    n_err = len({r for r, _, _ in errors})
    n_warn = len({r for r, _, _ in warns})

    if errors:
        print(f"\033[31m{n_err} 个引用指不到东西,{len(errors)} 处\033[0m")
        print("    改法有两条:把路径改对,或者把那句话改成过去时并点名替代者。")
        print("    第三条(加进 _ALLOW)要在那一行写清楚为什么它指不到却是对的。")
        return 1

    tail = f",另有 {n_warn} 个只在历史台账里(--all 可列出)" if n_warn else ""
    print(f"\033[32m活文档与活代码里的路径引用全部指得到{tail}\033[0m")
    print("\033[2m    这只管路径存不存在 —— 一句指得到但说错内容的话,它看不出来\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
