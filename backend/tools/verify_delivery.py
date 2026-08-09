#!/usr/bin/env python3
"""交付卫生与门禁接线自检。

## 它从哪来

原来是 `backend/tests/pure/test_delivery_hygiene.py` 的 9 条测试。
方案 v4.1 第 8.2 节把它移出纯测试套件：

    移到 tools/verify_delivery.py
    由 make verify-delivery 和 CI 执行
    不再作为纯业务测试

理由是它测的根本不是业务逻辑，而是**交付动作有没有做对**：密钥有没有进包、
`.gitignore` 全不全、门禁脚本有没有人调用。这些东西和 `test_scoring.py`
放在同一个套件里，会让「业务回归」这个词失去意义——重构一次评分规则，
红的可能是一条关于 `.dockerignore` 的断言。

搬出来之后它的执行时机反而更硬：CI 里它是独立一步，失败就阻断合并。

## 用法

    python3 tools/verify_delivery.py          # 或 make verify-delivery

零三方依赖，只读文件。任何机器上都能跑。
"""
from __future__ import annotations

import ast
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND = PROJECT_ROOT / "backend"
FRONTEND = PROJECT_ROOT / "frontend"

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


class Failure(Exception):
    """一条检查没过。消息要写清楚**该怎么办**，不只是哪里错了。"""


# ================================================================ 交付卫生

#: 扫描时跳过的目录。提成常量不只是为了行宽——这份名单会被下面两条检查
#: 共用,写成两份字面量的下场是加了 `playwright-report` 却只加在一处,
#: 于是另一条检查在有报告产物的机器上莫名其妙变红。
_SKIPPED_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        ".venv",
        "venv",
        ".git",
        "dist",
        "__pycache__",
        "test-results",
        "playwright-report",
    }
)


def check_no_secret_material_in_the_working_tree() -> None:
    """主密钥、`.env`、私钥不许出现在仓库树里。

    钉的是那次事故的第一现场：`.secrets/.settings.key` 连着两个交付包出去，
    第二个包和主密钥毫无关系。`.gitignore` 与 `tools/pack.sh` 是两道拦截，
    但拦截只对「新产生的文件」有效——已经躺在树里的那份要有人指出来。

    `.env.example` 是模板，里面全是占位符，它必须在。
    """
    offenders: list[str] = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(PROJECT_ROOT)
        parts = set(rel.parts)
        if parts & _SKIPPED_DIRS:
            continue
        if ".secrets" in parts:
            offenders.append(str(rel))
        elif path.suffix in {".key", ".pem"}:
            offenders.append(str(rel))
        elif path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
            offenders.append(str(rel))

    if offenders:
        raise Failure(
            "仓库树里有凭据文件。这不是「打包时排除」能解决的问题——"
            "它已经在树里了，先轮换再删：\n  " + "\n  ".join(sorted(offenders))
        )


def check_no_build_artifacts_in_the_working_tree() -> None:
    """构建产物不进仓库。

    和上一条同源（都是缺 `.gitignore`），但后果只是噪音，所以单独一条——
    混在一起的话，某天有人为了让「密钥」那条变绿而顺手放宽整条规则。
    """
    offenders = [
        str(p.relative_to(PROJECT_ROOT))
        for p in PROJECT_ROOT.rglob("*.tsbuildinfo")
        if "node_modules" not in p.parts
    ]
    if offenders:
        raise Failure("构建产物进了仓库：\n  " + "\n  ".join(sorted(offenders)))


def check_gitignore_covers_every_class_of_secret() -> None:
    """`.gitignore` 存在，并且覆盖每一类东西。

    只断言「文件存在」是不够的：一个内容为空的 `.gitignore` 也能让那条检查
    变绿，而它挡不住任何东西。
    """
    path = PROJECT_ROOT / ".gitignore"
    if not path.exists():
        raise Failure("仓库根没有 .gitignore —— 那次密钥进包就是这么发生的")
    body = path.read_text(encoding="utf-8")

    required = {
        ".secrets/": "主密钥目录",
        ".env": "环境变量（含 Provider Key）",
        "*.key": "任何私钥",
        "storage/": "真实素材与产出，会被挂成 /files 对外托管",
        "node_modules/": "前端依赖",
        ".venv/": "仓库内 Python 虚拟环境",
        "*.tsbuildinfo": "TS 增量构建产物",
    }
    missing = [f"{pat}（{why}）" for pat, why in required.items() if pat not in body]
    if missing:
        raise Failure(".gitignore 少了这几类：\n  " + "\n  ".join(missing))


def check_the_pack_script_excludes_and_then_verifies() -> None:
    """打包脚本必须**先排除、再回头验一遍**。

    只做排除的话，规则写错了（少个星号、路径前缀不对）没有任何征兆：
    包照样生成、照样发出去，而所有人都以为规则在工作。
    """
    script = PROJECT_ROOT / "tools" / "pack.sh"
    if not script.exists():
        raise Failure("没有打包脚本 —— 手打的 zip 命令没有记忆，那次事故会再发生一次")
    body = script.read_text(encoding="utf-8")

    if ".secrets" not in body:
        raise Failure("打包脚本没有排除 .secrets/")
    if "unzip" not in body:
        raise Failure("打包脚本只排除、没有回头检查产物。排除规则写错时不会有任何征兆")
    if not re.search(r"rm -f .*OUT", body):
        raise Failure(
            "检查发现禁品时必须删包并退出非零 —— "
            "一个「有警告但仍然生成了」的包会被发出去"
        )

    windows_script = PROJECT_ROOT / "tools" / "pack.ps1"
    if not windows_script.exists():
        raise Failure("缺少 Windows 打包脚本 tools/pack.ps1")
    windows_body = windows_script.read_text(encoding="utf-8")
    required_markers = (
        "$ForbiddenDirs",
        ".secrets",
        "$Required",
        "ZipFile]::OpenRead",
        "Test-ForbiddenArchivePath",
        "[System.IO.File]::Delete($Out)",
    )
    missing = [marker for marker in required_markers if marker not in windows_body]
    if missing:
        raise Failure(
            "Windows 打包脚本没有完整实现排除、成包复验与失败删包："
            + ", ".join(missing)
        )

    # 两套入口若各自维护禁品清单，迟早会出现“Linux 安全、Windows 漏一类”的分叉。
    # 这里在交付门禁中逐项比较，让新增规则必须同时落到两个脚本。
    def shell_array(name: str) -> list[str]:
        match = re.search(rf"{re.escape(name)}=\((.*?)\n\)", body, re.DOTALL)
        if not match:
            raise Failure(f"tools/pack.sh 缺少 {name} 数组")
        return shlex.split(match.group(1), comments=True, posix=True)

    def powershell_array(name: str) -> list[str]:
        match = re.search(
            rf"\${re.escape(name)}\s*=\s*@\((.*?)\)",
            windows_body,
            re.DOTALL,
        )
        if not match:
            raise Failure(f"tools/pack.ps1 缺少 ${name} 数组")
        return re.findall(r"'([^']*)'", match.group(1))

    paired_arrays = {
        "FORBIDDEN_DIRS": "ForbiddenDirs",
        "CONTENT_ONLY_DIRS": "ContentOnlyDirs",
        "FORBIDDEN_FILES": "ForbiddenFiles",
        "ENV_EXAMPLES": "EnvExamples",
        "REQUIRED": "Required",
    }
    for shell_name, windows_name in paired_arrays.items():
        linux_values = shell_array(shell_name)
        windows_values = powershell_array(windows_name)
        if linux_values != windows_values:
            raise Failure(
                f"Linux/Windows 打包规则分叉：{shell_name} != ${windows_name}"
            )


def check_backend_dockerignore_excludes_the_key_directory() -> None:
    """构建上下文里带着主密钥，等于每一层镜像都留一份副本。

    删掉源文件删不掉镜像层里的那份——所以这条和 `.gitignore` 那条不能互相替代。
    """
    body = (BACKEND / ".dockerignore").read_text(encoding="utf-8")
    for pattern in (".secrets/", ".env"):
        if pattern not in body:
            raise Failure(f"backend/.dockerignore 没排除 {pattern}")


# ================================================================ 门禁接线


def _makefile() -> str:
    return (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")


def _package_json() -> dict:
    return json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))


def check_every_frontend_gate_script_is_invoked() -> None:
    """声明了门禁脚本，就必须有地方调用它。

    在这条之前的状态是：`typecheck` / `lint` / `build` 三条写在 `package.json`
    里，而全仓没有任何地方调用——于是「两个 tsc 一秒就能报的错活到了打包」，
    而且这件事从 A12 就被记录过，到 a20 还在。

    **现在有 CI 了，所以两处都要检查，不是二选一。**
    只看 CI 会让本地 `make check` 悄悄退化，只看 Makefile 会让 CI 悄悄退化。
    """
    scripts = _package_json().get("scripts", {})
    makefile = _makefile()
    ci = _ci_text()

    #: `dev` / `preview` 是给人手动跑的；`test:watch` 是 `test` 的交互版本，
    #: `e2e` 由 CI 单独一个 job 跑（要装浏览器）。其余都是门禁。
    not_a_gate = {"dev", "preview", "test:watch", "e2e"}
    missing = [
        name
        for name in scripts
        if name not in not_a_gate and f"npm run {name}" not in makefile
    ]
    if missing:
        raise Failure(
            "这些前端脚本没有任何 make 目标会调用它们，等于不存在：\n  "
            + "\n  ".join(missing)
            + "\n（如果某条确实只该手动跑，把它加进 not_a_gate 并写清理由）"
        )

    if "npm run e2e" not in ci and "playwright test" not in ci:
        raise Failure("CI 里没有任何一步会跑 Playwright —— 骨架接了但没人调用")


def _ci_text() -> str:
    """把 CI 配置全文读出来。没有 CI 就是硬失败——这是 Phase 0 的第一件事。"""
    workflows = sorted((PROJECT_ROOT / ".github" / "workflows").glob("*.yml"))
    if not workflows:
        raise Failure(
            "全仓找不到 CI 配置（.github/workflows/*.yml）。\n"
            "方案 v4.1 把「建立最小 CI」列为 P0 的第一项，排在删测试之前：\n"
            "没有执行者的门禁不是门禁，是备忘录。"
        )
    return "\n".join(p.read_text(encoding="utf-8") for p in workflows)


def check_ci_runs_every_gate() -> None:
    """CI 必须真的跑全那 13 条，缺一条就是「清单写在文档里」。

    方案 4.1 节 G-1 列了 11 项，A45-batch14-14 加了第 12 项，
    A45-batch14-24 的列写入点审计是第 13 项。
    这里逐条在 CI 配置里找它的执行痕迹。
    找的是命令而不是 step 名字：step 名字可以随便写，命令不能。

    ## 第 13 条为什么补在这里(A45-batch15)

    `CHECKS` 上那个标签在 14-24 就写成了「13 条」,而这个字典一直是 12 条 ——
    标签、守卫(`test_the_audit_is_registered_as_a_delivery_gate`)、变异锚点
    (`mutate_batch14_24.MUTATIONS[6]`)三处都按「审计已挂 CI」写好了,
    **唯独 CI 步骤本身和这一条 required 没落**。表现是纯测试 1 红 + 锚点
    1 条对不上,而 `verify_delivery` 自己是绿的 —— 因为它数的是这个字典,
    而这个字典没有那一条。

    注意它与 `check_every_column_can_say_who_writes_it` 不重复:那一条**执行**
    审计(判退出码),这一条判**CI 里有没有独立步骤**。只有前者时,审计的
    失败会混在 `verify_delivery` 的一堆输出里,CI 上看不出是哪道门禁红的。
    """
    ci = _ci_text()
    required = {
        "ruff check": "Ruff",
        "run_pure_tests.py": "后端纯测试",
        "pytest": "非 DB / PostgreSQL pytest",
        "alembic": "migration 升降级",
        "npm ci": "前端依赖可复现安装",
        "npm run typecheck": "前端类型检查",
        "npm run lint": "前端 lint",
        "npm run test": "Vitest",
        "npm run build": "前端构建",
        "audit_source_guards.py": "守卫窗口体检（反向断言不许吃切窄的源码）",
        "audit_column_writers.py": "列写入点审计（每一列都答得出谁写它）",
        "verify_delivery.py": "交付卫生（本文件）",
        "docker build": "生产镜像构建",
    }
    missing = [f"{cmd}（{why}）" for cmd, why in required.items() if cmd not in ci]
    if missing:
        raise Failure("CI 没有覆盖这些门禁：\n  " + "\n  ".join(missing))


def check_ci_backs_pytest_with_redis() -> None:
    """跑 `pytest` 的那个 job 必须同时起 PostgreSQL **和** Redis。

    上一条只检查「`pytest` 这条命令在不在 CI 里」。命令在、库也在，
    但 Redis 不在的时候，那一条照样绿 —— 而 `pytest` 会红，
    **红在一批完全不指向 Redis 的地方**。

    A42 实测过这个形状：有库无 Redis 是 24 条失败，全部是生成链路的任务
    静默停在 `CREATED`。根因是 `task_always_eager=True` 只跳过投递，
    Celery 仍要构造 result backend，缺 Redis 时 `.delay()` 抛
    `AttributeError: 'NoneType' object has no attribute 'Redis'`，
    而它被 `_deliver` 吞进 outbox。那 24 条里没有一条提到 Redis。

    所以这条门禁盯的不是「有没有装 Redis」，是**「pytest 需要的东西有没有
    全部被声明出来」**。少一样，红灯就会指向错误的地方 ——
    而门禁存在的意义正是替人省掉那一步考古。

    `tests/conftest.py` 顶部另有一条同题守卫（collect 阶段点名拦住）。
    两处不重复：这边看的是 CI 配置写没写，那边看的是运行时连不连得上。
    """
    ci = _ci_text()
    if "pytest" not in ci:
        return  # 上一条已经会因此失败，这里不重复报
    has_pg = "postgres:" in ci and "POSTGRES_DB" in ci
    has_redis = "redis:" in ci and "REDIS_URL" in ci
    if not (has_pg and has_redis):
        lacking = []
        if not has_pg:
            lacking.append("PostgreSQL 服务（postgres: 镜像 + POSTGRES_* 环境变量）")
        if not has_redis:
            lacking.append("Redis 服务（redis: 镜像 + REDIS_URL 环境变量）")
        raise Failure(
            "CI 跑 pytest 但没有声明它需要的服务：\n  "
            + "\n  ".join(lacking)
            + "\n缺 Redis 时 pytest 会红在生成链路上，而报错完全不指向 Redis"
            "（A42 实测 24 条）。"
        )


def check_ci_satisfies_the_test_database_contract() -> None:
    """跑 `pytest` 的 job 必须满足 `tests/conftest.py` 的测试库契约。

    ## 这条门禁补的是哪一格(2026-08-09 评审 F-01)

    上一条只问「PostgreSQL 服务声明了没有」，而 conftest 要的**不是**主库：

        TEST_DB_URL = os.getenv("TEST_DATABASE_URL") or _derive_test_url()
        # 推导 = 把 database 段换成 `<主库名>_test`

    再加一道 `_assert_safe_to_wipe()`：库名必须以 `_test` 结尾，**且**必须显式
    设 `ALLOW_DESTRUCTIVE_TEST_DB`（夹具会 `DROP SCHEMA public CASCADE`）。

    而 GitHub Actions 自己会设 `CI=true`，于是 conftest 的
    `_database_required()` 为真 —— 连不上**不是 skip，是 collect 阶段
    `raise RuntimeError`**。少任何一样，这个 job 都不是"少跑几条"，
    是**整个 job 起不来**。

    这就是它为什么值得单独一条：`postgres:` 和 `POSTGRES_DB` 都在、
    上一条门禁全绿，而 `backend` job 从来没有绿过 —— 连带
    `all-green` 也没有，而分支保护挂的正是 all-green。
    一条永远红的门禁最后会被人摘掉，而摘掉它之前没有任何东西会说
    「你摘的是唯一验证这次改动的那批用例」。

    ## 为什么问三件事而不是一件

        建库        `pg_database` / `CREATE DATABASE` —— 夹具建 schema，建不了库
        指向        `TEST_DATABASE_URL`（或能推导出 `_test` 的主库名）
        销毁授权    `ALLOW_DESTRUCTIVE_TEST_DB`

    三件缺任何一件，失败信息都完全不一样，而且都不指向真正的原因。
    """
    ci = _ci_text()
    if "pytest" not in ci:
        return  # 上一条已经会因此失败，这里不重复报

    lacking: list[str] = []
    if "ALLOW_DESTRUCTIVE_TEST_DB" not in ci:
        lacking.append(
            "ALLOW_DESTRUCTIVE_TEST_DB —— conftest._assert_safe_to_wipe() 要求"
            "显式确认这个库可以被 DROP SCHEMA，没有它夹具直接拒绝执行"
        )
    if "TEST_DATABASE_URL" not in ci:
        lacking.append(
            "TEST_DATABASE_URL —— 不写的话 conftest 从 DATABASE_URL 推导出"
            "`<主库名>_test`，而那个库 CI 里没人建"
        )
    # 建库这一步：接受任何一种写法（psql / createdb / initdb 脚本），
    # 只要求 CI 里真的出现「建一个 _test 库」这件事。
    creates_test_db = "CREATE DATABASE" in ci.upper() or "createdb" in ci
    if not creates_test_db:
        lacking.append(
            "建专用测试库的步骤 —— 夹具走 alembic 建的是 schema，"
            "**库本身**得先存在（postgres 服务只按 POSTGRES_DB 建了主库）"
        )
    if lacking:
        raise Failure(
            "CI 跑 pytest 但不满足 tests/conftest.py 的测试库契约：\n  "
            + "\n  ".join(lacking)
            + "\n少任何一样，这个 job 都不是少跑几条 —— GitHub Actions 会设 "
            "CI=true，conftest 因此在 collect 阶段 raise，整个 job 起不来。"
        )


def check_the_offline_gate_is_honest_about_its_coverage() -> None:
    """`make check` 要么覆盖全部门禁，要么说清楚自己没覆盖什么。

    危险在于一条跑绿的 `make check` 会给人「都过了」的印象，
    而类型层可能一次都没跑过。方案 4.1 节 G-0 的退路写得很明确：
    **把 fe-check 并入 make check，让一条命令覆盖全部门禁**。
    """
    makefile = _makefile()
    # 按行首锚定，不能用 `split("check:")` —— 那会先命中 `fe-check:`，
    # 于是读到的是隔壁目标的正文
    match = re.search(r"^check:.*?(?=\n\S|\Z)", makefile, re.M | re.S)
    if not match:
        raise Failure("没有 make check 目标")
    if "fe-check" not in match.group(0):
        raise Failure("make check 没有包含 fe-check —— 它跑绿会被当成「全都过了」")


def check_the_frontend_test_runner_is_actually_wired() -> None:
    """Vitest 接线的四件事，缺一件那批用例就还是死的。

    这条在 a20 之前是「今天不生效、装上之后自动生效」的形态——因为当时
    离线环境装不上 vitest。Phase 0 已经装上了，所以它现在是硬检查。
    """
    pkg = _package_json()
    dev = pkg.get("devDependencies", {})
    if "vitest" not in dev:
        raise Failure(
            "frontend/tests/ 下有用例但没装 vitest。这正是方案 0.3 第 2 条说的状态：\n"
            "「目录里躺着 29 条用例，任何人打开这个目录都会认为前端有测试覆盖，\n"
            "  但它们从写下那天起一次也没执行过。」"
        )

    scripts = pkg.get("scripts", {})
    if "test" not in scripts:
        raise Failure("装了 vitest 却没有 `test` 脚本")
    if "vitest" not in scripts["test"]:
        raise Failure(f"`test` 脚本没在跑 vitest：{scripts['test']!r}")
    if "npm run test" not in _makefile():
        raise Failure("有 test 脚本却没有 make 目标调用它")

    # 用例当初放在 src/ 外面，是为了不让 typecheck 因为找不到 describe / expect 而红。
    # 装上 vitest 之后这个理由就没了——一批不被 tsc 看的测试会慢慢和源码类型脱节
    tsconfig = (FRONTEND / "tsconfig.json").read_text(encoding="utf-8")
    if "tests" not in tsconfig:
        raise Failure(
            "tsconfig 的 include 还没把 frontend/tests 纳进来 —— "
            "那批用例仍然不过类型检查"
        )

    if not list((FRONTEND / "tests").rglob("*.test.ts")) and not list(
        (FRONTEND / "tests").rglob("*.test.tsx")
    ):
        raise Failure("接了 vitest 但一条用例都没有")


def check_no_skipped_frontend_tests() -> None:
    """方案 10.1 节第 10 项：Vitest 通过且 **0 skip**。

    `tests/setup.ts` 里有一条 afterAll 会在运行时拦截 skip/todo。
    这里再从源码上拦一次，理由是两者的时机不同：运行期那条要等测试跑完，
    而这条在 CI 的第一步就能报出来，且不需要 node_modules。
    """
    offenders: list[str] = []
    for path in sorted((FRONTEND / "tests").rglob("*.test.ts*")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            if re.search(r"\b(it|test|describe)\.(skip|todo)\b", stripped):
                offenders.append(f"{path.relative_to(FRONTEND)}:{lineno} {stripped[:60]}")
    if offenders:
        raise Failure(
            "前端用例里有 skip/todo。方案 10.1 节要求 0 skip，"
            "「不接受 skip / todo 状态进入人工测试」：\n  " + "\n  ".join(offenders)
        )


# ================================================================ 入口

def check_the_migration_chain_has_a_single_head() -> None:
    """迁移必须串成一条链，且只有一个 head。

    ## 这条是被一次返工换来的

    A24 那轮新迁移最初编成 `0021`，而仓库里**已经有**一个 `0021_...` ——
    两个同号 revision，`alembic upgrade` 直接报重复。发现它是在打完包之后。

    根因不是「手滑」，是查看时用了 `ls | head -20` 而目录里正好 21 个文件，
    第 21 个被截掉了。所以教训不是「别用 head」，而是**新增迁移前要按
    revision 链验一次唯一 head，而不是数文件名** —— 而那件事只要没有门禁守着，
    下一次仍然会靠人记得。

    放在这个脚本里而不是 pytest 里：它零三方依赖、只读文件、秒级完成，
    和「密钥有没有进包」是同一类「交付动作有没有做对」的检查。
    """
    versions = BACKEND / "migrations" / "versions"
    files = sorted(p for p in versions.glob("*.py") if p.name != "__init__.py")
    if not files:
        raise Failure(f"{versions} 下一个迁移文件都没有")

    revisions: dict[str, str] = {}
    down: dict[str, str | None] = {}
    for path in files:
        text = path.read_text(encoding="utf-8")
        rev = re.search(r"^revision(?::\s*str)?\s*=\s*[\"']([^\"']+)[\"']", text, re.M)
        prev = re.search(
            r"^down_revision(?::\s*[^=]+)?\s*=\s*(?:[\"']([^\"']+)[\"']|None)", text, re.M
        )
        if not rev:
            raise Failure(f"{path.name} 里找不到 revision 声明")
        if rev.group(1) in revisions:
            raise Failure(
                f"revision {rev.group(1)} 重复：{revisions[rev.group(1)]} 与 {path.name}。"
                "alembic upgrade 会直接报错。改掉其中一个的 revision 与 down_revision。"
            )
        revisions[rev.group(1)] = path.name
        down[rev.group(1)] = prev.group(1) if prev and prev.group(1) else None

    missing = {d for d in down.values() if d is not None and d not in revisions}
    if missing:
        raise Failure(f"这些 down_revision 指向不存在的迁移：{', '.join(sorted(missing))}")

    referenced = {d for d in down.values() if d is not None}
    heads = sorted(set(revisions) - referenced)
    if len(heads) != 1:
        raise Failure(
            f"迁移链有 {len(heads)} 个 head：{', '.join(heads)}。"
            "两个 head 意味着有人在同一个父节点上并行加了迁移；"
            "把其中一条的 down_revision 改成指向另一条。"
        )

    roots = [r for r, d in down.items() if d is None]
    if len(roots) != 1:
        raise Failure(f"迁移链有 {len(roots)} 个起点：{', '.join(sorted(roots))}")

#: 这些模块里的公开函数**必须真的被别处调用**。
#:
#: 它们都是「算好了要写进库、要发出去」的那一类:算术全对、测试全绿,
#: 而没有人调用时功能整条不通,且**没有任何征兆**。
#:
#: 这条门禁是被两次同样的事换来的:
#:
#:   describe_extractors() 硬编码 configured: true   前端老实展示了一个假状态
#:   record_cost()          从落地起就零调用           /spend 页恒为 0,而钱照花,
#:                                                   同时「去配价目表」的提示永远亮着
#:
#: 两次的共同点是:纯测试覆盖了**算术**,没有覆盖**接线**。而接线正是
#: 一个零依赖脚本能秒判的东西。
WIRED_MODULES: dict[str, tuple[str, ...]] = {
    # A45-batch14-7。这条门禁对本模块尤其要紧:`evidence_rules` 的判定
    # 在纯层被穷举验过,**验得干不干净和它有没有被调用完全无关** ——
    # 不接线的话套件照样 31/31 全绿,而 `_persist_candidates` 每跑完一个
    # 生成任务仍在往识别输入里塞 AI 图,每张一次真实付费调用。
    #
    # `derive_evidence_class` / `is_extraction_input` **不在这张表里**:
    # 它们今天的唯一调用点是同一个文件里的 `asset_is_extraction_input`,
    # 而这条门禁刻意排除模块内部互调(那不叫接线)。它们由
    # `tests/pure/test_a45_batch14_7_evidence_class.py` 用 AST 单独钉着 ——
    # 钉的是「过滤条件必须是那个适配器的裸调用、不许取反」,
    # 那是变异 P2 撞出来的洞:取反之后识别会只跑在 AI 图上,而前面几条
    # 守卫全部看不见。
    "app/media/evidence_rules.py": ("asset_is_extraction_input",),
    # A45-batch14-10。同一条道理:§6.2 的门禁判定在纯层被穷举验过,
    # **验得干不干净和它有没有被调用完全无关** —— 不接线的话套件照样全绿,
    # 而 `_material_facts` 仍然用那句「有 role 就算数」判定完整度,
    # M3 的角色识别一落地,每一张 AI 候选图都会满足「至少一张正面图」。
    #
    # 只登记 **SPU 作用域那一半**用得到的四个函数。
    #
    # **A45-batch14-21 修了这段的一句陈旧理由。** 原文写的是
    # 「归属外键(`color_variant_id`)今天不存在」,并引用一条名叫
    # `test_the_variant_scope_cannot_be_wired_yet_and_here_is_exactly_why`
    # 的守卫。两句都不成立了:那一列在**迁移 0037** 就落了,守卫也按它
    # 自己的约定翻转并改了名(现名
    # `test_the_variant_scope_is_wired_now_that_the_attribution_column_exists`),
    # 而 `variant_gate_roles` 从 14-15 起就在 `workbench/_material_facts`
    # 里被调着。§3.33:**一条过期的理由比没有理由更糟** —— 下一个人照着
    # "列不存在"去查,会发现列就在那儿。
    #
    # 今天不登记的只剩 `variant_confirmable_roles` 一个,而理由换成了真的
    # 那一条:它没有调用点。颜色作用域的"还差哪几个角色可以补"要等按颜色
    # 上传的界面(阶段 2 剩余项),没有那个界面,算出来的清单没有地方显示。
    # A45-batch14-20(阶段 4)。同一条道理,而这三处的失效方式各不相同:
    #
    # `generation_plan` 不接线的话,方案表建了、界面能配,而 `create_task`
    # 仍然按接口入参出图 —— 运营"确认方案"之后什么都没变,且没有任何地方会红。
    #
    # `fact_consistency` 不接线的话,九项检查在纯层验得干干净净,而评分器
    # 从不返回那一段(Schema 里没有它),于是事实不一致的图照常自动通过。
    #
    # `image_set_rules.coverage` 不接线的话,§6.5 的门禁只是一份规格:
    # 红色 SKU 会继续挂着黑色主图,而 `validate` 说"校验通过"。
    #
    # `plan_problems` / `budget_blocks` / `serialize_angles` 等**不在表里**:
    # 它们的调用点在同一个模块内,而这条门禁刻意排除模块内互调。
    "app/workflows/generation_plan.py": (
        "normalize_angles",
        "fingerprint_of",
        "resolve_plan",
        "image_set_stale_scopes",
        "budget_verdict",
    ),
    "app/evaluators/fact_consistency.py": (
        "evaluate",
        "problem_dicts",
        "uncertain_notes",
        "enabled_checks",
    ),
    "app/media/sample_completeness.py": (
        "gate_roles",
        "confirmable_roles",
        "missing_role_groups",
        "group_label",
    ),
    # A45-batch14-11。同一条道理,而这两个模块的失效方式格外安静:
    #
    # `queue_policy` 不接线的话,`_attribute_facts` 仍然直接按状态取桶 ——
    # 今天结果一样,而真实抽取器接上、校准为空那天,每一个 CANDIDATE 字段
    # 都会重新涌进确认队列(§11 要禁的正是这件事),套件照样全绿。
    #
    # `provenance_conflict` 不接线的话,`ingest()` 仍然按去重键找,
    # 于是把 A 色的 AI 图当样品传给 B 色的 SKU 会新建一条 `PRODUCT_EVIDENCE`
    # —— 直接进识别输入,每张一次真实付费调用(AC-22 的字面场景)。
    #
    # `carries_generation_provenance` / `claims_product_evidence` /
    # `enters_confirm_queue` **不在这张表里**:它们今天的调用点都在同一个
    # 文件里,而这条门禁刻意排除模块内互调。它们由
    # `tests/pure/test_a45_batch14_11_queue_and_provenance.py` 穷举钉着。
    # A45-batch14-12 / 14-20。
    #
    # `terminal_status_for` 的接线点在 14-20 之前是 `models/attribute.py` 的
    # `status` 派生属性,五列落库之后挪到了写入方(`attributes/service.py` 的
    # `run_extraction`)—— 派生属性已删,理由见那个文件里的整段注释。
    # `run_is_authoritative` 的接线点仍是 `api/attributes.py` 决定要不要
    # 合并证据那一行。
    #
    # 幂等那四个是 **A45-batch14-20 新登记**的。它们从 14-12 起就写好、
    # 穷举验过,却整整八批没有登记 —— 每一批的理由都是同一句:§4.6 的五个列
    # 一个都不存在,没有列就没有地方存键、也没有行可以查重。迁移 0040 把列
    # 落下去之后这条借口消失,于是登记。
    #
    # 不登记的话套件照样全绿,而 `run_extraction` 会继续每次双击都建一个新
    # run、打一轮真实付费调用 —— 「双击不产生第二个 run」这条验收静静地不成立。
    "app/attributes/run_state.py": (
        "terminal_status_for",
        "run_is_authoritative",
        # A45-batch14-13。接线点是 `run_extraction` 的逐图成绩归集。
        # 今天它一定给出空清单(归属外键不存在),接的是**形状** ——
        # 那一列落库那天不接线的表现是「判定写好了没人调」,
        # 而最省事的补法是就近抓一个 `variant_hint` 来分组。
        "outcome_by_scope",
        # A45-batch14-20。四个一起登记,少登记一个都不会有征兆:
        #
        #   canonical_scope        不调它 -> `requested_scope` 是空的,
        #                          而键少了作用域那一维:共享事实与按颜色
        #                          识别会算出同一个键
        #   idempotency_key        不调它 -> 键列全 NULL,唯一索引形同虚设
        #   reuse_verdict          不调它 -> 查到了占键的行也照样再跑一次
        #   unique_index_predicate 不调它 -> 索引谓词变成手打的字面量,
        #                          与 KEY_OCCUPYING_STATUSES 漂移
        #
        # 最后一个的接线点在 `models/attribute.py` 的 `__table_args__`
        # (ORM 那一侧);迁移 0040 里是它的**冻结孪生**,由
        # `test_the_migration_predicate_is_the_twin_of_the_pure_one` 盯着。
        "canonical_scope",
        "idempotency_key",
        "reuse_verdict",
        "unique_index_predicate",
    ),
    # A45-batch14-20。指纹模块同样从 14-9 起就验过、一直接不上线 ——
    # 那时的理由是「算得出来却没有地方存」:`facts_stale` 要比的是
    # 「这条事实当初是按哪组素材算出来的」,而 `input_fingerprint` 不存在,
    # 接上去只会每次现算一个和空值比,`facts_stale(stored=None)` 恒为 True,
    # **全库事实一次性集体过期**。迁移 0040 给了它落点。
    #
    # `views` 一起登记:少了它,`_element()` 的每一项都 getattr 到 None,
    # 算出来的是一个所有素材都长得一样的指纹 —— 不报错、不抛异常,
    # 只是换了图也不 stale。那是本模块最安静的坏法。
    #
    # A45-batch14-21:`facts_stale` / `changed_scopes` 这一层也接上了。
    # 上一版这两个不在表里,理由是「要等属性值行也带上指纹列」——
    # 迁移 0042 落了 `product_attribute_values.input_fingerprint`,借口消失。
    #
    #   facts_stale     唯一调用点 `attributes/service.stale_fields`。不接线
    #                   的话系统回答不了「这条商品事实还成不成立」,而 AC-21
    #                   不是会答错,是没有任何地方在算
    #   changed_scopes  调用点在 `media/service` 的隔离 / 放行 / 改角色。
    #                   不接线的话「为什么这堆事实突然要重新确认」这个问题
    #                   在审计里没有答案 —— 派生比较证明得了它们过期了,
    #                   证明不了是哪一次动作造成的
    "app/attributes/scope_fingerprint.py": (
        "fingerprints",
        "views",
        "facts_stale",
        "changed_scopes",
    ),
    "app/attributes/queue_policy.py": ("bucket_fields",),
    "app/media/provenance_conflict.py": ("verdict", "may_fill_role"),
    "app/services/spend.py": ("record_cost", "summary"),
    "app/core/pricing.py": ("evaluate_budget", "resolve_unit_price", "parse_price_book"),
    "app/core/http_headers.py": ("download_headers",),
    "app/core/clock.py": ("utc_now",),
    # 任务 18。`poll_one` 与 `inventory` 在任务 15/16 就落地了,但**只有
    # 命令行调用**——对运营来说等于不存在。`build_view` / `describe_enqueue`
    # 是本轮新写的判定,不接线的话前端只能自己拼状态,而硬规则 4 禁止那样做。
    #
    # 注意这条门禁只查函数不查字段:`reused_terminal` 这类"算好了没人读"的
    # 字段它抓不到,由 `tests/pure/test_publish_view.py` 用 AST 单独钉着。
    "app/workflows/publish_view.py": ("build_view", "describe_enqueue"),
    "app/services/poll_service.py": ("poll_one",),
    "app/services/cleanup_service.py": ("inventory",),
    "app/services/publish_service.py": ("enqueue", "safe_preview"),
    # 任务 17 / 18。这两个是本条门禁最典型的适用对象:它们**只在出事时
    # 才走到** —— worker 猝死、消息丢了。不接线的话本机跑不出任何差别,
    # 测试也全绿,而"批次卡住谁来救"这件事整条不存在。
    #
    # `claim_items` **不在这张表里**:它的唯一调用点是同一个文件里的
    # `run_batch`,而这条门禁刻意排除模块内部互调(那不叫接线)。
    # 它由 `tests/pure/test_batch_lease.py` 用 AST 钉着 ——
    # 钉的是"`run_batch` 必须经过它"和"它必须带 skip_locked",
    # 因为改回无锁 SELECT 之后租约会变成一个没人写的列,
    # 而回收器会把正在跑的条目当成残骸。
    "app/workbench/batch_service.py": (
        "reap_expired_leases",
        "redispatch_stalled_batches",
    ),
    # A44。这几个是本条门禁最典型的适用对象:变体身份改完之后,`variants.py`
    # 里每一个新函数都可能"写好了没人调",而那种状态下测试全绿、口径照旧 ——
    # 分配不接线的话 `variant_key` 全是 NULL,`variant_id_for()` 静静回落到
    # A44 之前的表达式,改名照样丢属性,唯一的差别是多了一列没人填的字段。
    #
    # `variant_id_for` / `required_variant_ids` **不在表里**:它们本来就有
    # 一堆调用点,列进来只是噪音。这张表要盯的是新写的那几个。
    "app/listings/variants.py": (
        "assign_variant_key",
        "variant_directory",
        "drift_report",
    ),
    # 翻译层不接线的话写入侧就回到自由文本,而 `unknown_tags` 只在批准时
    # 才看得见 —— 那时人已经不记得自己打了什么
    #
    # A45-batch13-2:`label_of` 换成 `label_for_row`。前者从"界面该显示什么"
    # 的入口降级成了内部原语 —— 身份换成 UUID 之后回落要三级,而它只有两级,
    # 现在唯一的调用点是同一个文件里的 `label_for_row`。按这张表的规矩
    # (模块内部互调不叫接线,同 `claim_items`),点名的必须是新的那个入口。
    #
    # 不接线的表现:建档刚做完、属性识别还没跑那一刻 `primary_color` 是空的,
    # 图片绑定界面上三个变体会显示成三串 UUID —— 而所有测试照常全绿。
    "app/listings/variant_key.py": ("resolve_ref", "label_for_row"),
    # 命名空间不接线 = 跨 SPU 串档原样存在,而它在单 SPU 的测试里
    # 永远复现不出来
    # A45-batch14-18 / 任务 9。**这条门禁最该管的一批** —— 本批修的缺陷
    # 本身就是它的形状:FASHN 从 `x-fashn-credits-used` 解析出来的真实额度
    # 被抄在候选图上,然后全仓再没有第二处读它。算术全对、测试全绿,
    # 而台账记的一直是 `max(provider_count, 1)`,少记 2~5 倍且方向恒定是少记。
    #
    # 不接线的表现和本批之前**一模一样**:预算横幅一路绿着,账单是它的
    # 好几倍,唯一的差别是多了一列没人填的 `units_source`。
    #
    # `ProviderUsage` / `UnitsSource` 不在表里:它们是数据结构不是判定,
    # 而这张表要盯的是"写好了没人调"的那一类。
    "app/providers/base.py": ("settle_billable_units", "usage_from_candidates"),
    # A45-batch14-19 / PRD 阶段 2。§5.1 的「唯一取数入口」。
    #
    # 不接线的表现和本批之前**一模一样**:判定还在、AI 图也还是进不来,
    # 唯一的差别是多了一个没人调的函数 —— 而 §5.1 防的不是今天的代码,
    # 是明天那个照着 `usable_assets()` 抄一行的调用点。
    #
    # `usable_asset_count` 也登记:它存在的唯一理由是让识别路径拿不到
    # 未过滤的**行**。没人调的话 `run_extraction` 迟早会退回
    # `len(usable_assets(...))`,那正是它要挡的那一步。
    "app/media/service.py": ("evidence_assets_for", "usable_asset_count"),
    # A45-batch20(阶段 5 批次 5-2)。5-1 落判定层时 `app/` 下零调用,
    # 由 `test_a45_batch19_...` 的两条欠账守卫记着;接线之后由这里接管。
    #
    # `active_colors` / `primary_of` **不在这张表里**:前者今天的调用点在
    # 判定层自己的两个 builder 里(模块内互调,这条门禁刻意排除),后者的
    # 消费者是 5-5 的导出预览 —— 还没有人读那一列的单行主图。
    #
    # `ready_problems` 单独登记而不是只登记 `map_problems`:合取入口正是
    # 它存在的全部理由。只登记被它包住的那一个的话,有人把服务层改成
    # 直接调 `map_problems`(少了 §6.5 那一半)照样能过这条门禁,
    # 而表现是一个红色一张自有图都没有的 SPU 顺利 READY。
    "app/workbench/upstream_snapshot.py": (
        "build_upstream_versions",
        "build_color_sku_image_map",
        "diff_upstream",
        "ready_problems",
    ),
    "app/workbench/upstream_collect.py": ("collect",),
    # 阶段 6。**这四个模块是本条门禁最典型的适用对象** —— 它们全都是
    # "判定层先落地、接线排在下一批"的形状,而 6-1 交付 `color_flow` 时
    # 零调用点这件事整整挂了三批(6-1 -> 6-3)。那次有人明说了,所以不是账;
    # 没说的那些才是。登记之后这件事由门禁记着,不靠交付说明记着。
    #
    # 各自不接线的表现:
    #
    #   color_flow      向导上的颜色维退回一个空表,而 §6.7 的范围口径
    #                   还在别处生效 —— 界面说"没有颜色拦着",门禁说不能导出
    #   color_rollup    同上,只是断在取数这一跳
    #   wizard          AC-05 退回成"前端置灰":界面上按钮是灰的,
    #                   而手搓一个 POST 照样能越过前置把草稿建出来
    #   cost_rollup     费用预估恒为空,向导上那一格永远显示"未预估",
    #                   而它和"这次不花钱"在界面上长得一样
    #   cost_estimate   同上,断在判定这一跳
    #   impact          AC-17 的"修改之前"没有任何提示,而 `stale_matrix`
    #                   照常把草稿打成 STALE —— 运营事后才知道
    #
    # 只登记有**跨模块**调用点的那些(这条门禁刻意排除模块内互调):
    # `wizard.build` / `serialize` 的调用点在 `api/workbench.py`,但两个名字
    # 太常见,字面重名会让门禁假绿 —— 它们由
    # `tests/pure/test_a45_batch29_wizard.py` 的接线三跳单独钉着。
    "app/workbench/color_flow.py": ("evaluate_colors", "colors_needing"),
    "app/workbench/color_rollup.py": ("views_and_rollup", "serialize_rollup"),
    "app/workbench/wizard.py": ("check_action",),
    "app/workbench/cost_rollup.py": ("build_estimate",),
    "app/workflows/cost_estimate.py": ("estimate",),
    "app/workbench/impact.py": ("preview", "describe_sources"),
}


def check_every_wired_module_is_actually_called() -> None:
    """`WIRED_MODULES` 里点名的函数必须在 `app/` 下有**非自身**的调用点。

    只找字面量 `名字(`,不做真的调用图分析 —— 这条门禁的价值在于零依赖、
    秒级、且能挡住"整个模块没人用"这一类。它挡不住"调用点在死代码里",
    那种要靠覆盖率,不该由这个脚本承担。
    """
    root = PROJECT_ROOT / "backend" / "app"
    if not root.is_dir():
        root = PROJECT_ROOT / "app"
    if not root.is_dir():
        raise Failure("找不到 app/ 目录")

    # 用 AST 而不是字面量搜索。字面量会把**注释和字符串**也算成调用点 ——
    # 实测把 `spend.record_cost(...)` 注释掉之后,字面量版本照样判定"已接线",
    # 也就是说它抓不到它唯一要抓的那件事。
    called: dict[str, set[str]] = {}
    for path in root.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else None
            )
            if name:
                # 记**相对路径**,不是文件名。A45-batch14-19 撞到的:
                # 按文件名去重时,`app/media/service.py` 的函数被
                # `app/attributes/service.py` 调用会被当成"模块内部互调"而
                # 判定未接线 —— 而 `service.py` 是这个仓库里最常见的文件名
                # (media / attributes / publish / poll / cleanup / dispatch …)。
                #
                # 也就是说这条门禁在**最容易发生接线遗漏的那一批模块上**
                # 恰好是瞎的,而它的失败方向是假红:被它拦下来的人最省事的
                # 做法是把条目从 WIRED_MODULES 里删掉,于是门禁静静失效。
                called.setdefault(name, set()).add(
                    path.relative_to(root).as_posix()
                )

    missing: list[str] = []
    for module, names in WIRED_MODULES.items():
        # `app/media/service.py` -> `media/service.py`(`called` 的键相对 app/)
        owner = module.split("/", 1)[1] if module.startswith("app/") else module
        for name in names:
            # 排除定义它的那个文件自己 —— 模块内部互相调用不算"接线"
            if not (called.get(name, set()) - {owner}):
                missing.append(f"{module}:{name}")

    if missing:
        raise Failure(
            "这些函数写好了但没有任何调用点："
            + "、".join(missing)
            + "。测试会全绿而功能整条不通 —— 接上它，或者从 WIRED_MODULES 里删掉。"
        )


#: 欠账守卫的自述标记。docstring 里出现这句话 = 这条守卫记的是一笔欠账。
#:
#: 用一句中文而不是装饰器 / 命名前缀:纯层用例刻意不依赖 pytest,没有
#: 装饰器可用;而命名前缀会诱导人靠改名绕过。标记写在文档里,和它解释的
#: 那件事在同一处 —— 而**下面这条门禁让"写了标记"这件事有代价**。
DEBT_GUARD_MARKER = "记的是欠账"

#: **只在 docstring 的第一段里找那句标记。**
#:
#: 第一版扫全文,当场被自己咬了两次:先是 14-10 那条**已还清**的守卫
#: (叙述里留着"原来记的是欠账"),后是本批**盯着这条门禁自己**的两条
#: 元守卫——它们在解释这套机制时必须引用那句标记,于是被判成欠账。
#:
#: 第二次那个方向值得记一句:**一条守卫要讲清它守的是什么,就得说出那件
#: 事的名字;而按全文匹配的判据会把"说出名字"当成"就是那件事"。**
#: 这是 §3.26「docstring 是源码的一部分」的又一个方向——前几次是文档把
#: 断言喂成平凡真,这次是文档把判据喂成假阳。
#:
#: 收进第一段是有依据的,不是为了绕开:仓库里现存欠账守卫的声明**全都**
#: 写在第一行,形如「**这条守卫记的是欠账,不是成绩。还款日:阶段 N。**」。
#: 也就是说这条规矩本来就存在,只是原来没写下来——**声明写在开头,
#: 解释可以出现在任何地方。**
DEBT_DECLARATION_PARAGRAPHS = 1

#: 已还清的标记。这个仓库的习惯是把欠账守卫**翻转**成正向守卫、并在文档里
#: 留一段"原来记的是欠账,某批把它收了" —— 那段叙述很有价值,是这里最好读的
#: 一类注释。
#:
#: 但它会让上面那个标记在**过去时**里命中(第一版当场误伤了 14-10 那条)。
#: 所以还清的那一刻要显式说一句 —— **三种状态各有一句话,不靠时态去猜。**
#: §3.26 那条:按字符串在源码里找东西,找到的从来不保证是你以为的那个位置;
#: 解法不是把正则写得更巧,是让每一种状态都有自己的一句话。
DEBT_REPAID_MARKER = "欠账已还"

#: 欠账守卫的命名式。名字里有这些的必须带标记 —— 否则"不写标记"就是
#: 一条绕过整条门禁的捷径,而绕过不会有任何征兆。
DEBT_GUARD_NAME_HINTS = (
    "_and_this_is_the_ledger",
    "cannot_be_wired_yet",
    "_still_owed_",
    "_not_reachable_yet",
    "_have_no_writer_yet",
)

#: 还款日。**只有一种写法:交付阶段。**
#:
#: 第一版这里还有一种「还款日:条件式」,意思是"某件事落地那天守卫自己会红,
#: 判据长在自己身上,不用外部盯"。写完之后回头核了一遍仓库里现存欠账,
#: **没有一条用得上它** —— 每一条都自称"接线那天这条会红",而每一条同时
#: 都有一个真实的阶段死线。
#:
#: 留着一个永远不被走的分支,这条门禁的覆盖就有一格是假的(与"别把明知
#: 抓不住的变异列进脚本"同一条规矩)。删掉。真遇到没有死线的欠账时,
#: 诚实的做法也是**挑一个阶段**:"没有死线"正是欠账被忘掉的方式,
#: 而这条门禁存在的全部理由就是那件事。
_DUE_STAGE = re.compile(r"还款日\s*[:：]\s*阶段\s*([0-9]+)")
_DUE_ANY = re.compile(r"还款日\s*[:：]")

#: 当前交付阶段。**从 `docs/STATUS.md` 的机器可读标记读,不写在这里。**
#: 写在这个脚本里的话,它会和 STATUS 里那张阶段清点表分叉,而分叉的表现是
#: 门禁按一个没人维护的数字判到期。
_CURRENT_STAGE = re.compile(r"<!--\s*DELIVERY_STAGE\s*[:：]\s*([0-9]+)\s*-->")


def _declared_stage() -> int:
    status = PROJECT_ROOT / "docs" / "STATUS.md"
    if not status.exists():
        raise Failure(f"{status} 不存在 —— 还款日门禁没有当前阶段可比")
    match = _CURRENT_STAGE.search(status.read_text(encoding="utf-8"))
    if not match:
        raise Failure(
            "docs/STATUS.md 里找不到 `<!-- DELIVERY_STAGE: N -->` 标记。"
            "少了它，「欠账到期没还」这件事又回到没有任何机制会响的状态。"
            "把它写在文件顶部那一格里，和阶段清点表放在一起。"
        )
    return int(match.group(1))


_CODE_STAGE = re.compile(r"<!--\s*CODE_STAGE\s*[:：]\s*([0-9]+)\s*-->")


def check_stage_markers_are_consistent() -> None:
    """两个阶段标记必须都在，且结算不许跑到落码前面（2026-08-09 评审 F-04）。

    ## 为什么要两个标记

    `DELIVERY_STAGE` 原来自称"本仓当前落码到第几阶段"，而它实际是**欠账结算
    闸**：往上加一，所有还款日 ≤ 新值的欠账守卫当场变红。于是阶段 5、6 都
    落码之后，这个数字仍然停在 4 —— 停在能让门禁保持绿的那个位置。

    一个自称 A 而实际是 B 的标记，比没有标记更糟：读它的人会以为阶段 5、6
    没有开工，然后去重做一遍已经写好的东西（本仓为这个形状付过三次账，
    根 `CLAUDE.md` 里 5/6、7/9 那两段点名的就是它）。

    所以拆成两个：

        DELIVERY_STAGE  欠账结算阶段 —— 闸，还款日 ≤ 它的欠账必须已还清
        CODE_STAGE      已落码阶段   —— 如实描述，不参与任何闸

    ## 这条门禁只钉一个方向

    `CODE_STAGE >= DELIVERY_STAGE`。反过来意味着"结算跑到了落码前面"，
    而那只有一种成因：有人为了让某条门禁变绿而调高了闸。

    **不钉相等**：两者的差就是欠着的账，差本身是合法状态，也正是它要
    暴露的东西。今天是 6 − 4 = 2。
    """
    status = PROJECT_ROOT / "docs" / "STATUS.md"
    if not status.exists():
        raise Failure(f"{status} 不存在 —— 阶段标记无从读起")
    text = status.read_text(encoding="utf-8")
    code_match = _CODE_STAGE.search(text)
    if not code_match:
        raise Failure(
            "docs/STATUS.md 里找不到 `<!-- CODE_STAGE: N -->` 标记。\n"
            "它是「代码实际推进到 PRD §13 第几阶段」的唯一机器可读来源。\n"
            "少了它，`DELIVERY_STAGE` 又会被当成进度报告读 —— 而它是一条"
            "欠账结算闸，两者今天并不相等。"
        )
    code_stage = int(code_match.group(1))
    settle_stage = _declared_stage()
    if code_stage < settle_stage:
        raise Failure(
            f"CODE_STAGE({code_stage}) 小于 DELIVERY_STAGE({settle_stage}) —— "
            "欠账结算跑到了落码前面。\n"
            "这只有一种成因：为了让某条门禁变绿而调高了结算阶段。\n"
            "该做的是还上欠账或重新认领还款日，不是调标记。"
        )


def check_no_debt_guard_is_past_its_due_stage() -> None:
    """欠账守卫必须声明还款日，而且不许过期。

    ## 这条门禁是被一笔"到期没还"的欠账换来的

    §3.34 立的规矩是「欠账要么写成会变红的守卫,要么就不要写」,而那条规矩
    有一个当时没看见的缺口:**守卫钉的是"没接线"这个事实,不钉"什么时候
    该接"。**

    `facts_stale` 那条欠账的还款日写在 docstring 里 —— 「要等属性值行也带上
    指纹列,**那是阶段 4 的事**」。阶段 4 落码了,这件事没做,而守卫**没有
    变红**:它断言的是"这两个不许被登记成接线",而"没登记"这件事仍然为真。

    一般化:**判据落在别人身上的守卫,守不住自己。**

    ## 还款日的语义:阶段 N 之前必须还清

    `还款日:阶段 N` 读作「交付阶段推进到 N 之前要还清」,所以
    `N <= 当前阶段` 就是逾期。选这个语义而不是"阶段 N 期间还",是因为前者
    能被一个数字判定,而后者要判"这个阶段做完了没有" —— 那是一句没有人
    能机械回答的话。

    拿本批那笔欠账验一遍:它的还款日写的是"阶段 4",而当前阶段正是 4 ——
    **这条门禁要是早一批存在,它会当场红。**

    ## 自己会红,不等于到期会红

    仓库里的欠账守卫都写着"接线那天这条会红",而那是真的:它们断言的是
    "还没人接",接了就红。但**没人接的时候它们永远是绿的** —— 那正是
    `facts_stale` 那笔欠账躺过一整个阶段的方式。自翻转是还款的**确认**,
    不是还款的**催促**,两者都要有。

    ## 三种状态,三句话,不靠时态去猜

        欠着      docstring 有 `记的是欠账` + `还款日:阶段 N`
        已还清    docstring 有 `欠账已还`(翻转成正向守卫时补上这一句)
        不相干    两句都没有

    第二句是必须的:把还清的欠账守卫翻转成正向守卫、并留一段"原来记的是
    欠账,某批把它收了"的叙述,是这个仓库的习惯 —— 而那段叙述会让第一个
    标记在过去时里命中(第一版当场误伤了 14-10 那条)。

    ## 反向也钉:不写标记不能当成绕过

    名字里带欠账命名式(`..._and_this_is_the_ledger` 等)却没有标记的,
    一律红。少了这一条,整条门禁最省事的绕法就是把那句话删掉。

    ## 为什么当前阶段读 STATUS 而不是写在这里

    写在脚本里的常量没有人会记得改,而 STATUS 顶部那张阶段清点表是每批都要
    动的东西。标记就放在它旁边,读的是同一个人刚写下的判断。
    读不到标记时**红**,不是静静跳过 —— 静静跳过等于这条门禁自己也变成
    一笔没有还款日的欠账。
    """
    stage = _declared_stage()
    root = PROJECT_ROOT / "backend" / "tests" / "pure"
    if not root.is_dir():
        raise Failure(f"找不到 {root} —— 这条门禁失去了对象")

    undeclared: list[str] = []
    overdue: list[str] = []
    unmarked: list[str] = []
    seen_debt = 0

    for path in sorted(root.rglob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef) and node.name.startswith("test_")):
                continue
            doc = ast.get_docstring(node) or ""
            # 声明只认开头那一段,见 DEBT_DECLARATION_PARAGRAPHS 上面那段
            head = "\n\n".join(doc.split("\n\n")[:DEBT_DECLARATION_PARAGRAPHS])
            where = f"{path.name}::{node.name}"
            if DEBT_REPAID_MARKER in head:
                continue
            if DEBT_GUARD_MARKER not in head:
                if any(hint in node.name for hint in DEBT_GUARD_NAME_HINTS):
                    unmarked.append(where)
                continue
            seen_debt += 1
            due = _DUE_STAGE.search(doc)
            if not due:
                undeclared.append(
                    where
                    + ("（写了还款日但不是「阶段 N」）" if _DUE_ANY.search(doc) else "")
                )
                continue
            if int(due.group(1)) <= stage:
                overdue.append(f"{where}（还款日 阶段 {due.group(1)}）")

    problems: list[str] = []
    if unmarked:
        problems.append(
            "这些守卫的名字说它记的是欠账，而 docstring 里没有那句标记："
            + "、".join(unmarked)
            + f"。补一句「{DEBT_GUARD_MARKER}」和「还款日：阶段 N」；"
            f"已经还清的话补「{DEBT_REPAID_MARKER}」。不写标记不是绕过的办法。"
        )
    if undeclared:
        problems.append(
            "这些欠账守卫没有声明还款日："
            + "、".join(undeclared)
            + "。写「还款日：阶段 N」，读作「阶段 N 之前必须还清」。"
            "没有还款日的欠账有两种下场——被忘掉，或者在还清之后继续吓唬人。"
        )
    if overdue:
        problems.append(
            f"当前交付阶段是 {stage}，而这些欠账的还款日已经到了："
            + "、".join(overdue)
            + "。这正是 facts_stale 那笔欠账的形状——守卫钉的是「没接线」这个事实，"
            "不钉「什么时候该接」，于是到期不还没有任何东西会响。"
            "现在要么还上它，要么把还款日往后改并写清为什么改。"
        )
    if problems:
        raise Failure("；".join(problems))

    # 反向：一条欠账都找不到时**红**。今天仓库里确实有四条，归零只可能是两种
    # 情况——真的全还清了（那时该连这条门禁一起重新想），或者标记被人顺手删了。
    # 后者才是常见的那一种，而它的表现是这条门禁静静地永远绿
    if not seen_debt:
        raise Failure(
            "一条带标记的欠账守卫都没找到。要么欠账真的全还清了"
            "（那时该重新想这条门禁还盯不盯得住东西），"
            f"要么「{DEBT_GUARD_MARKER}」这句标记被删掉了——后者的表现是本条永远绿。"
        )


def check_every_migration_and_db_test_is_tracked_by_git() -> None:
    """交付所需源码、迁移、测试与工具**必须已经被 Git 跟踪**。

    ## 这条是被一次"本机全绿、clean checkout 起不来"换来的

    A45-batch17-2 那轮新增了 `0046_stage1_identity_owner_uuid.py` 与
    `0047_publish_outbox_lease_token.py`,以及对应的并发测试。**三个文件都没有
    `git add`。** 而已经提交的代码里:

        attributes/validation.py    要求 owner 是 UUID 变体(0046 建的)
        models/publishing.py        读 publish_outbox.lease_token(0047 建的)

    于是本机 `alembic heads` 说 0047、`verify_delivery` 16/16、纯测试全绿,
    而从 main 做一次 clean checkout 得到的最后一条迁移是 **0045** ——
    部署之后发布查询会去访问一个不存在的列(UndefinedColumn),存量
    VARIANT 属性的颜色事实会读不出来。

    ## 为什么既有门禁全部看不见它

    `check_the_migration_chain_has_a_single_head()` 和 `alembic heads` 扫的都是
    **当前文件树**。文件就在那里,链也是通的 —— 它们没有任何理由报错。
    这一整类问题("工作树里有,版本库里没有")只有问 Git 才答得出来,
    而在这次之前没有任何一条门禁问过 Git。

    ## 为什么不是 git 仓库时直接失败

    这个脚本的定位是"**交付动作**有没有做对"(见模块文档),而交付冻结
    发生在仓库里。从 tarball 解出来的目录跑不了这一条,也确实不该在那里
    宣布"交付卫生检查通过" —— 那正是上一次事故里那份 16/16 的作用。

    ## 范围为什么覆盖生产源码、测试和交付工具

    只盯迁移与 `*_db.py` 仍然会漏掉同一种事故:已跟踪的 `App.tsx` 可以导入
    一个未跟踪页面,当前工作树的 typecheck 全绿,clean checkout 却直接缺模块;
    `verify_delivery` 也可以读取一个未跟踪的 `tools/pack.ps1` 并宣布打包门禁通过。

    这里仍然不扫描整个工作树,只扫描会进入运行、测试或交付动作的高信号目录,
    并按源码后缀过滤。个人笔记和临时数据不会因此让门禁变红。
    """
    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:  # pragma: no cover - 取决于机器
        raise Failure(
            "找不到 git 命令,这条门禁无法执行。它守的是「工作树里有、版本库里没有」,"
            "只有 Git 答得出来 —— 交付冻结必须在装了 git 的仓库里做。"
        ) from exc
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise Failure(
            f"{PROJECT_ROOT} 不是一个 Git 工作树,这条门禁无法执行。\n"
            "从 tarball 解出来的目录跑不了它,也不该在那里宣布交付卫生通过 —— "
            "上一次事故里本机那份 16/16 起的正是这个作用。"
        )

    watched_roots: tuple[tuple[Path, frozenset[str]], ...] = (
        (BACKEND / "app", frozenset({".py"})),
        (BACKEND / "migrations" / "versions", frozenset({".py"})),
        (BACKEND / "tests", frozenset({".py"})),
        (BACKEND / "tools", frozenset({".py"})),
        (FRONTEND / "src", frozenset({".ts", ".tsx", ".css"})),
        (FRONTEND / "tests", frozenset({".ts", ".tsx"})),
        (FRONTEND / "tools", frozenset({".js", ".mjs", ".cjs"})),
        (PROJECT_ROOT / "tools", frozenset({".py", ".sh", ".ps1"})),
    )
    git_paths = [str(root.relative_to(PROJECT_ROOT)) for root, _ in watched_roots]
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", *git_paths],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:  # pragma: no cover - 取决于机器
        raise Failure(f"git ls-files 执行失败:{listed.stderr.strip()}")
    tracked = {
        (PROJECT_ROOT / name).resolve()
        for name in listed.stdout.split("\0")
        if name.strip()
    }

    watched = sorted(
        p
        for root, suffixes in watched_roots
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in suffixes
    )

    missing = sorted(
        str(p.relative_to(PROJECT_ROOT)) for p in watched if p.resolve() not in tracked
    )
    if missing:
        raise Failure(
            "这些文件在工作树里、但 Git 没有跟踪:\n  "
            + "\n  ".join(missing)
            + "\n\n从 main 做 clean checkout 的人拿不到它们。结果可能是模块缺失、"
            "schema 比代码旧一截、测试证据缺席,或者打包安全入口消失。\n"
            "把生产代码、迁移、测试与交付工具作为**同一个提交**交付:"
            "git add 上面这些文件。"
        )


def check_the_decision_log_has_no_duplicate_section_numbers() -> None:
    """`docs/DECISIONS.md` 里同一个 §编号不许出现两次。

    ## 这条是被一次并线合并换来的

    A45-batch14-20 有两条并行线,基线相同、互不知情,各自往决策日志末尾追了
    一节 **§3.34**。合并之后两节都在,编号相同,内容无关。

    这件事的坏法和迁移撞号是**同一类**(见上面那条门禁),但后果不同:
    迁移撞号会当场炸,决策撞号**永远不会炸** —— 它只是让"§3.34 说了什么"
    从此有两个都说得通的答案。而这份文档的用法恰恰是被别处**按编号引用**的
    (代码注释、其他决策、评审回复里到处是「见 §3.x」),引用点不会跟着变,
    读的人也不会知道自己读岔了。

    只查编号唯一,不查编号连续:允许中间空号(有节被删掉过),
    也允许一次合并里补两节。

    零三方依赖、只读一个文件,和「迁移链单一 head」放在一起。
    """
    doc = PROJECT_ROOT / "docs" / "DECISIONS.md"
    if not doc.exists():
        raise Failure(f"{doc} 不存在 —— 决策日志是这个仓库的引用目标,不能没有")
    seen: dict[str, int] = {}
    for lineno, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
        match = re.match(r"^##\s+§([0-9]+(?:\.[0-9]+)*)", line)
        if not match:
            continue
        number = match.group(1)
        if number in seen:
            raise Failure(
                f"§{number} 出现了两次(第 {seen[number]} 行与第 {lineno} 行)。"
                "决策撞号不会报错,它只是让「§" + number + " 说了什么」"
                "从此有两个答案,而全仓到处按编号引用它。"
                "给后写的那一节改个号,并回头看一眼有没有人已经引用过。"
            )
        seen[number] = lineno
    if not seen:
        raise Failure("docs/DECISIONS.md 里一节都没有 —— 这条门禁失去了对象")


def check_every_column_can_say_who_writes_it() -> None:
    """每一列都要回答「谁写它」——判定在 `tools/audit_column_writers.py`。

    ## 为什么这条门禁要存在

    §3.38 把「一列落库了、被判定读取,而没有任何写入路径」点成了一个类。
    这个仓库为它付过两次代价:`media_assets.color_variant_id` 躲了五批,
    §4.8 新去重键躲了六批。**两次都是人逐条对着 PRD 核才核出来的。**

    第三次不会有人这么核 —— 一条只在有人手工复核时才会发现的规矩,
    等于没有规矩(§3.34 立那条时说的就是这句话)。

    ## 判定不写在这里

    与「欠账守卫都在还款日之内」同一个理由:这条要遍历 `app/` 全部源码、
    建每个模型的列表与写入点索引,几百行。写进本文件会让 `verify_delivery`
    变成一个既是门禁又是分析器的东西,而它的定位是**串起所有门禁**。

    审计脚本自己可以单独跑(`make audit-columns`),输出带分类与原因;
    这里只判它的退出码。
    """
    script = PROJECT_ROOT / "backend" / "tools" / "audit_column_writers.py"
    if not script.exists():
        raise Failure(f"找不到 {script} —— 这条门禁失去了判定")
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=script.parent.parent,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise Failure(
            "有列答不出「谁写它」。逐条见下,处理方式是补写入路径、"
            "或者进 `audit_column_writers.LEDGER` 并写清原因与还款日:\n"
            + (proc.stdout or proc.stderr).strip()
        )


CHECKS = [
    ("仓库树里没有凭据文件", check_no_secret_material_in_the_working_tree),
    ("仓库树里没有构建产物", check_no_build_artifacts_in_the_working_tree),
    (".gitignore 覆盖每一类密钥", check_gitignore_covers_every_class_of_secret),
    ("打包脚本先排除再复验", check_the_pack_script_excludes_and_then_verifies),
    ("backend/.dockerignore 排除密钥目录", check_backend_dockerignore_excludes_the_key_directory),
    ("每条前端门禁脚本都有人调用", check_every_frontend_gate_script_is_invoked),
    ("CI 覆盖全部 14 条门禁", check_ci_runs_every_gate),
    ("CI 的 pytest 有 PostgreSQL + Redis", check_ci_backs_pytest_with_redis),
    (
        "CI 满足 conftest 的测试库契约",
        check_ci_satisfies_the_test_database_contract,
    ),
    ("make check 覆盖前端门禁", check_the_offline_gate_is_honest_about_its_coverage),
    ("Vitest 四件事都接上了", check_the_frontend_test_runner_is_actually_wired),
    ("前端用例 0 skip", check_no_skipped_frontend_tests),
    ("迁移链单一 head", check_the_migration_chain_has_a_single_head),
    ("交付源码、迁移与测试都已被 Git 跟踪", check_every_migration_and_db_test_is_tracked_by_git),
    ("决策日志编号不重复", check_the_decision_log_has_no_duplicate_section_numbers),
    ("阶段标记:结算不跑到落码前面", check_stage_markers_are_consistent),
    ("欠账守卫都在还款日之内", check_no_debt_guard_is_past_its_due_stage),
    ("落地的模块真的被接线了", check_every_wired_module_is_actually_called),
    ("每一列都答得出谁写它", check_every_column_can_say_who_writes_it),
]


def main() -> int:
    failures: list[tuple[str, str]] = []
    for label, fn in CHECKS:
        try:
            fn()
        except Failure as exc:
            failures.append((label, str(exc)))
            print(f"  {RED}FAIL{RESET} {label}")
        except Exception as exc:  # noqa: BLE001 - 工具自己出错也要说清楚是哪一条
            failures.append((label, f"检查本身抛了异常：{exc!r}"))
            print(f"  {RED}ERROR{RESET} {label}")
        else:
            print(f"  {GREEN}OK{RESET}   {label}")

    print()
    for label, detail in failures:
        print(f"{RED}{label}{RESET}\n  {detail}\n")

    total = len(CHECKS)
    color = GREEN if not failures else RED
    print(f"{color}{total - len(failures)}/{total} 项通过{RESET}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
