"""a61 守卫:前端第一条**会执行**的测试通路,以及调色板 token 检查。

## 这一批守的是"欠账真的在缩,不是又记了一遍"

从 a54 起,「下一步第一件事就是 `make fe-check`」在交接文档里重复了六遍 ——
而每一轮都在往那条没有行为验证的路上多写几个文件,到 a60 一轮就改了六个。
**一句重复六遍的「这是环境问题」正在变成免责声明。**

a61 不是把 `fe-check` 补上(那仍然要 node_modules),是把欠账**从"全部"缩到
"渲染那一半"**:判定层从此有真的会跑的测试。所以这里钉三件事 ——

    通路在      运行器存在、进了 Makefile / CI / 门禁表(硬规则第 3 条)
    通路有对象  用例文件不为零,判定层真的搬出了 .tsx
    话说得准    文档里不许把这条的绿说成 fe-check 的绿

第三条最容易松。**一个被说大的能力比没有能力更贵** —— 它会让下一个人不去装
node_modules。
"""
from __future__ import annotations

import re
from pathlib import Path

from tests.pure._helpers import offline_gate_targets

BACKEND = Path(__file__).resolve().parents[2]
PROJECT = BACKEND.parent
FRONTEND = PROJECT / "frontend"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ============================================================ 通路在


def test_the_frontend_pure_runner_exists_and_is_dependency_free():
    """零依赖是这条通路成立的全部前提。

    它一旦 import 了 `node_modules` 里的东西,就退回成 `fe-check` 的一部分,
    而这台机器上 `fe-check` 跑不了 —— 那等于什么都没做成。
    """
    runner = FRONTEND / "tools" / "run-pure-tests.mjs"
    assert runner.is_file(), "前端纯逻辑运行器不见了"
    source = _read(runner)
    imports = re.findall(r"^import .*? from '([^']+)'", source, re.M)
    external = [m for m in imports if not m.startswith("node:") and not m.startswith(".")]
    assert not external, f"运行器引了第三方依赖:{external} —— 零依赖是它成立的前提"


def test_the_runner_is_wired_into_all_three_places():
    """硬规则第 3 条:加一条门禁 = 改三个地方。

    只写在 Makefile 里的门禁,CI 不跑;只写在文档里的门禁,谁都不跑。
    """
    makefile = _read(PROJECT / "Makefile")
    assert "fe-test-pure:" in makefile, "Makefile 里没有这个目标"
    # 依赖项集合由 `_helpers.offline_gate_targets` 现算 —— a64 把清单拆成了
    # 一项一行,读它的三处守卫从此共用一份判定(判定只有一份)
    assert "fe-test-pure" in offline_gate_targets(makefile), (
        "目标建了但没进 check-offline —— 那它只会在有人记得手敲时跑"
    )
    ci = _read(PROJECT / ".github" / "workflows" / "ci.yml")
    assert "run-pure-tests.mjs" in ci, "CI 没跑它"
    audit = _read(BACKEND / "tools" / "verify_delivery.py")
    assert '"run-pure-tests.mjs"' in audit, "门禁清单表里没登记 —— 它自己会红"


def test_the_runner_fails_loudly_when_it_finds_nothing():
    """什么都没跑的门禁,和全都过了的门禁,在 CI 上长得一模一样。

    改测试目录、改命名约定、glob 写错,三种都会让它变成一句空话而没有征兆。
    """
    source = _read(FRONTEND / "tools" / "run-pure-tests.mjs")
    assert "files.length === 0" in source
    at = source.index("files.length === 0")
    assert "process.exit(1)" in source[at : at + 600], "找不到用例时没有退非零"


def test_the_runner_lists_files_instead_of_handing_node_a_directory():
    """`node --test <目录>` 会把目录当 CJS 模块去 require,报 Cannot find module。

    实测过:`node --test tests/pure/` 输出 `# fail 1`,而那个 1 不是任何一条用例。
    """
    source = _read(FRONTEND / "tools" / "run-pure-tests.mjs")
    # a68 在 `--test` 前面插了探测出来的 runtime 参数(A67-ISSUE-003),
    # 于是原来钉死的字面量 `'--test', ...files` 不再出现。**要守的不是那串
    # 字符,是"交给 node 的是文件列表而不是目录"** —— 判据改成看 spawn 的
    # 参数里有没有 `...files`,而 `--test` 仍在。
    at = source.index("spawnSync(")
    call = source[at : at + 300]
    assert "...files" in call, "又把目录直接交给了 --test"
    assert "'--test'" in source, "--test 不见了"


# ============================================================ 通路有对象


def test_there_is_at_least_one_executable_frontend_test():
    root = FRONTEND / "tests" / "pure"
    assert root.is_dir(), "前端纯逻辑测试目录不见了"
    files = sorted(root.rglob("*.test.ts"))
    assert files, "一个用例文件都没有 —— 通路建好了而没有对象"
    for path in files:
        assert path.suffix == ".ts", (
            f"{path.name} 不是 .ts —— 剥类型不做 JSX 变换,.tsx 一行都跑不了"
        )


def test_the_diff_logic_actually_moved_out_of_the_tsx():
    """判定留在 `.tsx` 里就是跑不了 —— 搬出来才是这条通路的全部意义。"""
    pure = FRONTEND / "src" / "utils" / "textDiff.ts"
    assert pure.is_file(), "diff 判定没有搬进可执行的 .ts"
    source = _read(pure)
    assert "export function diffLines" in source
    assert "function lcsTable" in source
    tsx = _read(FRONTEND / "src" / "components" / "TextDiff.tsx")
    assert "function lcsTable" not in tsx, "判定还留了一份在 .tsx 里 —— 两份会分叉"
    assert "from '../utils/textDiff'" in tsx, "渲染层没有引用那份判定"


def test_the_executable_tests_import_with_an_explicit_extension():
    """Node 的 ESM 解析**不补扩展名**。

    `import { x } from './lib'` 在 Vite 下没问题,在 `node --test` 下是
    `ERR_MODULE_NOT_FOUND`。这一条写在这里,是因为下一个人写第二个用例文件时
    多半会照着 src 的写法省掉 `.ts`,然后花时间查一个已知答案的问题。
    """
    for path in sorted((FRONTEND / "tests" / "pure").rglob("*.test.ts")):
        source = _read(path)
        relative_imports = re.findall(r"^import .*? from '(\.[^']+)'", source, re.M)
        for spec in relative_imports:
            assert spec.endswith(".ts"), (
                f"{path.name} 里 `{spec}` 没带扩展名 —— Node ESM 不补,它会 MODULE_NOT_FOUND"
            )


# ============================================================ 调色板


def test_the_offline_check_verifies_palette_tokens():
    """`brandVars` 是 fromEntries 出来的 Record:取不存在的键得到 undefined,

    而 CSS 收到 undefined 只是不上色 —— **运行时完全静默**。a60 当轮就写出过
    `successBg` / `dangerSoft` 两个不存在的键。
    """
    source = _read(FRONTEND / "tools" / "syntax-check.mjs")
    assert "brandVars\\.([a-zA-Z]+)" in source, "离线体检没有扫 brandVars 的用法"
    assert "paletteTokens" in source
    assert "调色板 token" in source, "汇总行没有把这一格说出来"


def test_the_palette_check_refuses_to_run_blind():
    """读不出 `lightTokens` 就红,不是静静跳过。

    静静跳过等于这一格自己变成一句空话,而它长得和"全都查过了"一模一样。
    """
    source = _read(FRONTEND / "tools" / "syntax-check.mjs")
    at = source.index("paletteTokens(readFileSync(themePath")
    window = source[at : at + 500]
    assert "badTokens += 1" in window, "解析失败时没有计一笔失败"


def test_every_brand_token_in_use_really_exists():
    """守卫自己也算一遍 —— 门禁脚本和守卫各算一次,两边对不上就说明有一边错了。"""
    theme = _read(FRONTEND / "src" / "theme.ts")
    block = theme[theme.index("const lightTokens") :]
    block = block[: block.index("\n}")]
    tokens = set(re.findall(r"^ {2}([a-zA-Z]+):", block, re.M))
    assert tokens, "解析不出调色板"
    missing: dict[str, set[str]] = {}
    for path in sorted((FRONTEND / "src").rglob("*.ts*")):
        if path.name == "theme.ts":
            continue
        used = set(re.findall(r"brandVars\.([a-zA-Z]+)", _read(path)))
        stray = used - tokens
        if stray:
            missing[path.name] = stray
    assert not missing, f"用了调色板里没有的 token:{missing}"


# ============================================================ 话说得准


def test_nothing_claims_this_replaces_fe_check():
    """**一个被说大的能力比没有能力更贵** —— 它会让下一个人不去装 node_modules。

    三处都要把射程说出来:运行器自己的输出、Makefile 的注释、STATUS 的限制条目。
    """
    runner = _read(FRONTEND / "tools" / "run-pure-tests.mjs")
    assert "只覆盖判定层" in runner, "运行器的输出没有说明射程"
    makefile = _read(PROJECT / "Makefile")
    # 取紧邻这条规则**上方那整段注释**,不按固定字符数往回数 —— 固定窗口的宽窄
    # 取决于注释写多长,那是一个和被守的事情无关的量
    lines = makefile.splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith("fe-test-pure:"))
    block = []
    i = at - 1
    while i >= 0 and (lines[i].startswith("#") or not lines[i].strip()):
        block.insert(0, lines[i])
        i -= 1
    assert "node_modules" in "\n".join(block), "Makefile 里没写清它替代不了什么"
    status = _read(PROJECT / "docs" / "STATUS.md")
    # 判的是**那条限制条目在说射程**,不是"文档里提到过这个命令"。
    # 第一版找的是 `run-pure-tests`(脚本名),而 STATUS 写的是 make 目标名 ——
    # 两个名字都对,而守卫只认其中一个。这一类假红最省事的消法是删掉守卫
    assert "fe-test-pure" in status, "STATUS 没提这条新通路"
    assert "别把" in status and "的绿读成" in status, (
        "STATUS 没有把射程说出来 —— 一个被说大的能力比没有能力更贵"
    )
    assert "至今未兑现" in status, "那条 fe-check 的欠账被顺手抹掉了,而它还欠着"
