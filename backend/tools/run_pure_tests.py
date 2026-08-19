#!/usr/bin/env python3
"""零依赖测试运行器。

背景:tests/pure/ 下的用例刻意不依赖 pytest / fastapi / sqlalchemy,
既可以用真实 pytest 运行(CI、容器内),也可以在只有 python3 的机器上直接跑。
本脚本负责后者:发现 test_* 函数、执行、汇总断言失败。

用法:python3 tools/run_pure_tests.py [模块名过滤]

过滤器有两种写法(判定在 `tools/suite_filter.py`,那里是唯一定义):

    batch14                      子串,顺手在终端敲的那种
    test_a45_batch14_fixes.py    精确文件名,变异脚本用这种

变异脚本必须用精确那种:子串会连带跑上别的套件,于是一条变异可以因为
**别人的**守卫而变红,而报告记在自己头上。见 `suite_filter.py` 顶部。

## 启动自检(v4.1 Phase 0 / 8.2)

`tests/pure/test_pure_suite_is_self_contained.py` 原来用两个测试函数守着
「纯测试不许依赖 pytest」这条规矩。方案 8.2 节的处理是「合并成运行器启动时
一次检查,无需作为多个测试函数存在」——因为它守的不是业务,是**这个运行器
自己能不能工作**。

这个位置比测试函数好在两点:

    一、时机对。它在任何用例被执行之前跑完,而不是碰巧排到第 39 个文件时才跑。
    二、失败信息对。放在测试里时,`import pytest` 会让那个文件在**导入期**炸掉,
        运行器把它记成一条普通失败——于是 `test_list_sorting.py` 带着
        `import pytest` 躺了很久,套件显示「1167/1168 通过」,看起来只差一条,
        实际上那个文件里 9 个用例一个都没跑过。漏掉的覆盖不会自己叫。
"""
from __future__ import annotations

import ast
import importlib.util
import io
import os
import sys
import tempfile
import traceback
from contextlib import redirect_stdout
from pathlib import Path

from test_environment import install_provider_test_baseline

# ## 这几行必须在任何用例被导入之前跑(A42)
#
# `app.core.secrets._material()` 找不到 `SETTINGS_SECRET_KEY` 时,会走
# `_read_or_create_key_file()` —— 那个函数**在仓库根的 `.secrets/` 下真的生成
# 一个主密钥文件**。`tests/conftest.py` 顶部为此设了固定测试密钥 + 临时目录
# (方案 12.1 节任务 4「修复密钥测试副作用」),**但那只保护 pytest 这条路**。
#
# 本运行器是另一条路,而且是文档里最常被执行的那一条:`docs/STATUS.md` 第五节
# 写着「`make test-pure` —— 只要有 python3 就能跑」。在裸 shell 里跑一次,
# 仓库树里就会多出 `.secrets/.settings.key`,而且**测试全绿、没有任何提示**。
# 硬规则 1 的三道拦截里,`.gitignore` 和 `pack.sh` 都在打包环节,
# 只有 `verify_delivery.py` 会叫一声 —— 而它跑在交付前,不是跑在这里。
#
# 「主密钥连着两个交付包出去」那次事故,第一现场就是这个文件。
# 任务 4 标了 ✅,但它只修了一半:两个运行器,补了一个。
_SECRETS_TMPDIR = tempfile.mkdtemp(prefix="swimwear-pure-secrets-")
os.environ.setdefault("SETTINGS_SECRET_KEY", "pure-runner-fixed-key-do-not-use-in-production")
os.environ.setdefault("SETTINGS_KEY_DIR", _SECRETS_TMPDIR)
# 与 pytest 入口共用一份 Provider 基线,且必须发生在任何 app 模块导入之前。
install_provider_test_baseline()

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PURE_DIR = BACKEND_ROOT / "tests" / "pure"

# 「过滤器挑中哪些文件」的判定在 `tools/suite_filter.py`,不在这里。
# `audit_anchors.py` 要用同一份判定去核对变异脚本的 SUITE_FILTER ——
# 两处各写一份的表现是审计说"挑得中"而运行器挑不中,套件平凡通过,
# **每一条变异都报 GREEN**。见那个模块顶部。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from suite_filter import describe, select_paths  # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"puretest_{path.stem}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


#: 允许出现 `pytest` 字样的文件。`_helpers.py` 的文档字符串里要解释
#: 「这是轻量版 pytest.raises」——说明文字不是依赖。
#: 只放文件名,且必须写理由;这张表变长就说明门禁在被绕过。
_DOC_ONLY = {"_helpers.py"}

#: 允许直接写 `os.environ[...] = ...` 的文件。`_helpers.py` 里的
#: `temporary_secret_key` / `temporary_config` 就是干这件事的那两个封装,
#: 它们自己必须落到 os.environ 上。同样只放文件名、必须写理由。
_ENV_WRITERS = {"_helpers.py"}


def self_check() -> list[str]:
    """跑用例之前先确认这个运行器的前提还成立。

    用 AST 而不是正则:正则会命中注释和文档字符串——本文件和
    `test_list_sorting.py` 的注释里都写着 `@pytest.mark.parametrize` 这几个字。
    一条会被自己的说明文字触发的门禁,最后一定会被加白名单绕过,然后就没人管了。

    ## 第二条:不许裸写 `os.environ[...] = ...`

    `providers/_config.provider_setting` 的取值顺序是「设置页覆盖 →
    `app.core.config.settings` → `os.environ`」,而最后一条**只在
    `app.core.config` import 不进来时才走得到**。于是「设环境变量 →
    断言取值变了」这种用例的颜色由跑它的那台机器装没装 pydantic 决定:

        本运行器(零依赖)      → 退到 os.environ    → 绿
        pytest / CI backend   → 读 import 期的单例  → 红

    `test_the_budget_tracks_configuration_instead_of_freezing_at_import`
    正是这么红的,而它在 `gates` job 里一直是绿的 —— 一条自己会分裂成
    两种结论的用例,比没有这条用例更糟。封装在
    `_helpers.temporary_config` 里,两种环境同一个结论。
    """
    problems: list[str] = []
    for path in sorted(p for p in PURE_DIR.glob("*.py") if p.name != "__init__.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "pytest":
                        problems.append(f"{path.name}:{node.lineno} import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] == "pytest":
                    problems.append(
                        f"{path.name}:{node.lineno} from {node.module} import ..."
                    )
            elif (
                path.name not in _DOC_ONLY
                and isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "pytest"
            ):
                problems.append(f"{path.name}:{node.lineno} pytest.{node.attr}")
            elif path.name not in _ENV_WRITERS and _writes_environ(node):
                problems.append(f"{path.name}:{node.lineno} os.environ[...] = ...")
    return problems


def _writes_environ(node: ast.AST) -> bool:
    """这个节点是不是在往 `os.environ` 里写?

    覆盖 `os.environ[k] = v`、`os.environ.update(...)`、`os.environ.setdefault(...)`;
    读(`os.environ.get` / `os.environ[k]` 取值)一概不管 —— 读没有这个毛病。
    """

    def _is_environ(value: ast.AST) -> bool:
        return (
            isinstance(value, ast.Attribute)
            and value.attr == "environ"
            and isinstance(value.value, ast.Name)
            and value.value.id == "os"
        )

    targets: list[ast.AST] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, ast.AugAssign):
        targets = [node.target]
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]
    elif isinstance(node, ast.Delete):
        targets = list(node.targets)
    for target in targets:
        if isinstance(target, ast.Subscript) and _is_environ(target.value):
            return True

    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"update", "setdefault", "pop", "clear"}
        and _is_environ(node.func.value)
    )


def main() -> int:
    sys.path.insert(0, str(BACKEND_ROOT))
    name_filter = sys.argv[1] if len(sys.argv) > 1 else ""

    problems = self_check()
    if problems:
        print(f"{RED}纯测试套件自检失败{RESET}")
        print(
            "一、tests/pure 下不得依赖 pytest —— 本运行器直接调用 test_* 函数,\n"
            "   不认 fixture / parametrize / raises。参数化请改成表 + for 循环,\n"
            "   断言异常请用 _helpers.expect_raises。\n"
            "二、不得裸写 os.environ —— 那条取值路径只在没有 pydantic 时才通,\n"
            "   同一条用例在 CI 的两个 job 里会得出相反结论。\n"
            "   改配置请用 _helpers.temporary_config,借密钥请用 temporary_secret_key。"
        )
        for line in problems:
            print(f"  {line}")
        return 1

    files = select_paths(PURE_DIR, name_filter)
    if not files:
        print(f"{YELLOW}未发现测试文件{RESET}（过滤器:{describe(name_filter)}）")
        return 1

    passed: list[str] = []
    failed: list[tuple[str, str]] = []
    #: 因为**第三方依赖装不上**而没法执行的用例 (label, 缺的模块名)。
    #:
    #: A45 batch12-5 收尾:`tests/pure` 里有两条用例的函数体 import 了
    #: 需要 sqlalchemy / pydantic 的应用模块。在装好依赖的环境(评审机、CI)
    #: 它们照常执行、照常能红;在装不上 pip 包的离线容器里,它们死在
    #: import 那一行 —— 这是**环境事实**,不是代码问题,连续三轮评审报告
    #: 都要为同样的两条失败写一段解释。
    #:
    #: 更实际的代价:它让 `make check-offline` 在 test-pure 这一步就中止,
    #: 排在后面的零依赖检查全部没机会跑 —— 和门禁停在 ruff 缺失是同一个病。
    #:
    #: 所以缺依赖单独成一类:大声记 SKIP、单独计数、**不算通过**。
    #: 判定必须收得很窄 —— 只认 import 阶段的 ModuleNotFoundError,且缺的
    #: 必须是 app.*/tests.* 之外的第三方顶层模块。app 自己的模块找不到是
    #: 真 bug(verify_imports 守的正是这个),照旧红。
    skipped: list[tuple[str, str]] = []

    def _missing_third_party(exc: Exception) -> str | None:
        if not isinstance(exc, ModuleNotFoundError):
            return None
        root = (exc.name or "").partition(".")[0]
        if not root or root in {"app", "tests", "tools"}:
            return None
        return root

    for path in files:
        try:
            module = load_module(path)
        except Exception as exc:
            missing = _missing_third_party(exc)
            if missing:
                skipped.append((f"{path.name}::<import>", missing))
                print(f"{YELLOW}SKIP (缺 {missing}){RESET} {path.name}")
                continue
            failed.append((f"{path.name}::<import>", traceback.format_exc()))
            print(f"{RED}IMPORT FAIL{RESET} {path.name}")
            continue

        tests = [
            (n, obj)
            for n, obj in sorted(vars(module).items())
            if n.startswith("test_") and callable(obj)
        ]
        print(f"\n{DIM}{path.name}{RESET}  ({len(tests)} tests)")
        for name, fn in tests:
            label = f"{path.name}::{name}"
            buf = io.StringIO()
            try:
                with redirect_stdout(buf):
                    fn()
            except Exception as exc:
                missing = _missing_third_party(exc)
                if missing:
                    skipped.append((label, missing))
                    print(f"  {YELLOW}SKIP{RESET} {name}(缺 {missing})")
                else:
                    failed.append((label, traceback.format_exc()))
                    print(f"  {RED}FAIL{RESET} {name}")
            else:
                passed.append(label)
                print(f"  {GREEN}PASS{RESET} {name}")

    print("\n" + "=" * 62)
    for label, tb in failed:
        print(f"\n{RED}FAILED{RESET} {label}\n{tb}")
    if skipped:
        missing_mods = sorted({m for _, m in skipped})
        print(
            f"\n{YELLOW}{len(skipped)} 条用例因缺第三方依赖被跳过"
            f"(缺:{', '.join(missing_mods)})—— 它们没有被验证。{RESET}"
        )
        for label, mod in skipped:
            print(f"  {YELLOW}SKIP{RESET} {label}(缺 {mod})")
    total = len(passed) + len(failed)
    color = GREEN if not failed else RED
    tail = f", {len(skipped)} skipped(缺依赖)" if skipped else ""
    print(f"{color}{len(passed)}/{total} passed, {len(failed)} failed{tail}{RESET}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
