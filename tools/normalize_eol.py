#!/usr/bin/env python3
"""工作树与交付包的行尾策略。**这是策略的唯一实现。**

## 为什么需要这个文件

`.gitattributes` 顶部已经写了一篇很准的诊断(三处会真的坏掉、为什么用
`eol=lf`、为什么兜底要写在最前面),`tests/pure/test_a45_batch11_fixes.py`
里也有一条全树守卫。**但它们都没有拦住已经发出去的包。**

实测 a55 那个交付包:832 个文件里 **711 个行尾不合规**(600 个纯 CRLF、
111 个混合),其中包括 `.env.example`、两个 `Dockerfile`、
`frontend/.dockerignore`、`docker-compose.yml`、`.github/workflows/ci.yml`
—— 正好是 `.gitattributes` 那篇注释点名"会真的坏掉"的那几类,一个不落。

最能说明问题的是 `.github/workflows/*.yml`:它**明明写在白名单里**,包里
那份仍然是 CRLF。当时是 LF 的只有 70 个文件,全部是被后来重写过的那些。
也就是说那条规则一次都没对这棵树生效过。

根因不在两个打包脚本 —— 它们逐字节复制工作树,做的完全正确。根因是
`.gitattributes` 是**检出期**规则,三种情况下它一个字节都不动:

    工作树比规则老     git 不回头重写已经在盘上的文件
    收包方没有检出过   从 zip 解出来的树从来没经过 git
    文件不在版本控制里 打包脚本照样把它打进去

而两个 `.env.example` 更阴,是**混合行尾**:331 行里只有 6 行带 `\\r`,
其中两行是真的变量赋值(`OPS_LOG_RING_ACCESS=errors\\r`、
`VITE_PROXY_TARGET=http://localhost:8000\\r`)。`grep` 扫一眼看不出来,
`file` 对 JSON/INI 甚至完全不报行尾。

## 为什么是一个 Python 文件,而不是 bash 与 PowerShell 各写一遍

`tools/pack.sh` 开头整整两百行都在讲同一件事:**清单分叉**。排除清单和
复验清单曾经是分开手写的两份,于是它们分叉了;`FORBIDDEN_DIRS` 曾经漏了
根层级形态,而复验侧一条都没查。`verify_delivery.py` 里现在有一条门禁
逐项比对两个打包脚本的七个数组,就是为了不再发生第三次。

行尾判定要是在 bash 和 PowerShell 里各写一遍,分叉的表现恰好是
**"Linux 打的包干净、Windows 打的包带 CRLF"** —— 正是这套规则要防的场景
本身。所以两侧都委托给这一个文件,`--check` 由打包脚本调,`--write` 由人调。

判据同样只有一份:`tests/pure/test_a45_batch11_fixes.py` 那条全树守卫
现在也从这里取跳过表与豁免表,不再自己手写一份。

## 用法

    python3 tools/normalize_eol.py --check .      # 只报告,不合规退 1
    python3 tools/normalize_eol.py --write .      # 就地改写
    python3 tools/normalize_eol.py --check /tmp/x --quiet
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

#: 不扫的目录。与 `tools/pack.sh` 的 `FORBIDDEN_DIRS` / `CONTENT_ONLY_DIRS` /
#: `IMAGE_FREE_DIRS` 同向 —— 那些目录本来就不进交付包,而 `storage/` 与
#: `data/` 是运行期数据,里面的行尾不归这套规则管。
#:
#: **不与打包脚本共用一份声明**是有意的:那边的语义是"不许进包",这边是
#: "不必检查行尾",两者今天重合、将来未必(比如某天要把 `data/` 里的
#: `README.md` 打进包)。共用会让其中一个语义悄悄跟着另一个变。
SKIP_DIRS = frozenset(
    {
        ".git", ".idea", ".vscode", "__pycache__", ".pytest_cache", ".ruff_cache",
        ".vite-cache", ".import_linter_cache", ".venv", "venv", "node_modules",
        "test-results", "playwright-report", "dist", "coverage", "storage",
        ".secrets", "data",
    }
)

#: 唯一允许 CRLF 的两类。`cmd.exe` 对 LF 结尾的批处理行为不可靠,
#: `.gitattributes` 里那两条 `eol=crlf` 与这里同向。
CRLF_ALLOWED_SUFFIXES = frozenset({".bat", ".cmd"})

#: 二进制判据。和 git 的 `text=auto` 同一个口径:头部有 NUL 就当二进制。
#: 自己判而不是问 git,是因为这套判定要在**只有 python3**的机器上跑
#: (纯测试运行器、收包方解出来的树),那里不保证有 git。
_BINARY_SNIFF_BYTES = 8000


def looks_binary(payload: bytes) -> bool:
    return b"\x00" in payload[:_BINARY_SNIFF_BYTES]


#: 文件末尾恰好一个换行。缺一个和多几个都算不合规。
#:
#: 缺:很多工具按行读,最后一行没有换行时它要么被吞掉、要么和下一份内容黏在一起;
#: `git diff` 也会为此打印 `\ No newline at end of file`,让一次内容改动的 diff
#: 多出一行噪音。多:每一次追加都会在 diff 里带出前一个空行,而那一行不属于本次改动。
#:
#: 判据取自 deepseek-harness 的仓库约定(`AGENTS.md` 的 Conventions 一节),
#: 那边由 `git diff --cached --check` 在 pre-commit 上把关;本仓没有 hook,
#: 所以挂在这个已经被打包脚本与门禁调用的工具上 —— 行尾这件事只有一个家。
_TRAILING_NEWLINE = "trailing-newline"


def has_bad_trailing_newline(payload: bytes) -> bool:
    """末尾不是恰好一个 ``\n``。空文件豁免:它没有\"最后一行\"。"""
    if not payload or looks_binary(payload):
        return False
    return not payload.endswith(b"\n") or payload.endswith(b"\n\n")


def fix_trailing_newline(payload: bytes) -> bytes:
    """末尾收成恰好一个换行。只动末尾,前面一个字节不碰。"""
    return payload.rstrip(b"\n") + b"\n"


def classify(payload: bytes) -> str:
    """一份字节的行尾形态:``lf`` / ``crlf`` / ``mixed`` / ``cr`` / ``binary``。

    ``cr`` 是**孤立 CR**(有 `\\r` 但不成对)。老 Mac 行尾早就绝迹了,但
    一次坏掉的字符串替换能造出来 —— 而它比 CRLF 更难发现:`grep -c $'\\r$'`
    数不到,`file` 也不报。
    """
    if looks_binary(payload):
        return "binary"
    crlf = payload.count(b"\r\n")
    lone_cr = payload.count(b"\r") - crlf
    lone_lf = payload.count(b"\n") - crlf
    if lone_cr:
        return "cr"
    if not crlf:
        return "lf"
    return "crlf" if lone_lf == 0 else "mixed"


def normalize_bytes(payload: bytes) -> bytes:
    """CRLF 与孤立 CR 一律折成 LF。**顺序不能反** —— 先换 `\\r\\n` 再换 `\\r`,
    否则 `\\r\\n` 会先被拆成 `\\n\\n`,凭空多出一行空行。"""
    return payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def iter_text_files(root: Path):
    """扫描范围内的候选文件。跳过表、符号链接与 `.bat`/`.cmd` 在这里一次性处理。"""
    root = root.resolve()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative_parts = path.relative_to(root).parts
        if SKIP_DIRS & set(relative_parts) or any(
            part.endswith(".egg-info") for part in relative_parts
        ):
            continue
        # 与打包黑名单同向:真实环境文件绝不进交付物,也不能让本机凭据文件的
        # 行尾形态决定纯测试颜色；`.env.example` 是模板，仍须接受检查。
        if path.name == ".env" or (
            path.name.startswith(".env.") and path.name != ".env.example"
        ):
            continue
        if path.suffix.lower() in CRLF_ALLOWED_SUFFIXES:
            continue
        yield path


def offenders(root: Path) -> list[tuple[str, str]]:
    """不合规的文件,``[(相对路径, 形态), ...]``,按路径排序。

    读不出来的文件(权限、竞态删除)**当成不合规报出来**,不静默跳过 ——
    一个"扫不到所以通过"的门禁比没有门禁更糟,这是 `pack.sh` 开头那句话。
    """
    found: list[tuple[str, str]] = []
    root = root.resolve()
    for path in iter_text_files(root):
        try:
            payload = path.read_bytes()
        except OSError as exc:  # noqa: PERF203 - 每个文件独立报告
            found.append((str(path.relative_to(root)).replace("\\", "/"), f"unreadable:{type(exc).__name__}"))
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        kind = classify(payload)
        if kind in ("crlf", "mixed", "cr"):
            found.append((rel, kind))
        # 分开报,不合并成一条:两种不合规的修法相同(都由 --write 收掉),
        # 但**读的人要知道是哪一种** —— 一份 CRLF 的 shell 脚本会在 Linux 上
        # 直接跑不起来,而末尾少一个换行只是 diff 噪音。合并之后这两件事
        # 在报告里长得一样。
        elif has_bad_trailing_newline(payload):
            found.append((rel, _TRAILING_NEWLINE))
    return found


def rewrite(root: Path) -> list[str]:
    """就地归一化,返回被改过的相对路径。已经合规的文件一个字节都不碰。"""
    changed: list[str] = []
    root = root.resolve()
    for path in iter_text_files(root):
        payload = path.read_bytes()
        fixed = payload
        if classify(payload) in ("crlf", "mixed", "cr"):
            fixed = normalize_bytes(fixed)
        if has_bad_trailing_newline(fixed):
            fixed = fix_trailing_newline(fixed)
        if fixed == payload:
            continue
        path.write_bytes(fixed)
        changed.append(str(path.relative_to(root)).replace("\\", "/"))
    return changed


_FIX_HINT = (
    "修法:`python3 tools/normalize_eol.py --write .`,然后确认 .gitattributes\n"
    "第一条仍是 `* text=auto eol=lf`(白名单只盖得住它点名的那几个文件)。\n"
    "已经在版本控制里的树还要 `git add --renormalize .` 让索引跟上。"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="行尾归一化(工作树与交付包共用一份策略)")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", metavar="ROOT", help="只报告,不合规退出 1")
    mode.add_argument("--write", metavar="ROOT", help="就地把 CRLF/CR 折成 LF")
    parser.add_argument("--quiet", action="store_true", help="只在失败时输出")
    parser.add_argument(
        "--max-report", type=int, default=40, help="最多列出多少个不合规文件(默认 40)"
    )
    args = parser.parse_args(argv)

    root = Path(args.check or args.write)
    if not root.is_dir():
        print(f"!! 不是一个目录:{root}", file=sys.stderr)
        return 2

    if args.write:
        changed = rewrite(root)
        print(f"==> 行尾归一化:改写 {len(changed)} 个文件")
        for name in changed[: args.max_report]:
            print(f"     {name}")
        if len(changed) > args.max_report:
            print(f"     …… 另有 {len(changed) - args.max_report} 个")
        return 0

    bad = offenders(root)
    if not bad:
        if not args.quiet:
            print("==> 行尾检查通过:没有 CRLF / 混合 / 孤立 CR")
        return 0
    print(f"!! {len(bad)} 个文件行尾不合规(检查根:{root}):", file=sys.stderr)
    for name, kind in bad[: args.max_report]:
        print(f"     [{kind:>10}] {name}", file=sys.stderr)
    if len(bad) > args.max_report:
        print(f"     …… 另有 {len(bad) - args.max_report} 个", file=sys.stderr)
    print(file=sys.stderr)
    print(_FIX_HINT, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
