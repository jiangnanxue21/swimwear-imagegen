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
    {"node_modules", ".git", "dist", "__pycache__", "test-results", "playwright-report"}
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
        raise Failure("检查发现禁品时必须删包并退出非零 —— 一个「有警告但仍然生成了」的包会被发出去")


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
    """CI 必须真的跑全那 12 条，缺一条就是「清单写在文档里」。

    方案 4.1 节 G-1 列了 11 项，A45-batch14-14 加了第 12 项。
    这里逐条在 CI 配置里找它的执行痕迹。
    找的是命令而不是 step 名字：step 名字可以随便写，命令不能。
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
        raise Failure("tsconfig 的 include 还没把 frontend/tests 纳进来 —— 那批用例仍然不过类型检查")

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
    # 只登记 **SPU 作用域那一半**用得到的四个函数。`variant_gate_roles` /
    # `variant_confirmable_roles` 不在表里:归属外键(`color_variant_id`)
    # 今天不存在,登记了这条门禁会红,而让它变绿的最省事做法是拿
    # `variant_hint` 凑一下 —— 那是让模型猜出来的值决定运营能不能往下走,
    # 恰恰是 §6.2 收紧 role 口径要禁的那件事。欠账改由
    # `test_the_variant_scope_cannot_be_wired_yet_and_here_is_exactly_why`
    # 记账:那一列落库那天它会红,那时接线并删掉它。
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
    # `facts_stale` / `changed_scopes` **不在表里**:它们要等 §5.3 的事实侧
    # 也带上指纹列(属性值行的 `input_fingerprint`),那是阶段 4 的事。
    # 今天登记会红,而让它变绿最省事的做法是拿 run 行的指纹去比属性值 ——
    # 两者的作用域根本不同。欠账由 14-9 的
    # `test_the_stored_fingerprint_has_a_column_to_land_in` 顶着。
    "app/attributes/scope_fingerprint.py": ("fingerprints", "views"),
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


CHECKS = [
    ("仓库树里没有凭据文件", check_no_secret_material_in_the_working_tree),
    ("仓库树里没有构建产物", check_no_build_artifacts_in_the_working_tree),
    (".gitignore 覆盖每一类密钥", check_gitignore_covers_every_class_of_secret),
    ("打包脚本先排除再复验", check_the_pack_script_excludes_and_then_verifies),
    ("backend/.dockerignore 排除密钥目录", check_backend_dockerignore_excludes_the_key_directory),
    ("每条前端门禁脚本都有人调用", check_every_frontend_gate_script_is_invoked),
    ("CI 覆盖全部 12 条门禁", check_ci_runs_every_gate),
    ("CI 的 pytest 有 PostgreSQL + Redis", check_ci_backs_pytest_with_redis),
    ("make check 覆盖前端门禁", check_the_offline_gate_is_honest_about_its_coverage),
    ("Vitest 四件事都接上了", check_the_frontend_test_runner_is_actually_wired),
    ("前端用例 0 skip", check_no_skipped_frontend_tests),
    ("迁移链单一 head", check_the_migration_chain_has_a_single_head),
    ("决策日志编号不重复", check_the_decision_log_has_no_duplicate_section_numbers),
    ("落地的模块真的被接线了", check_every_wired_module_is_actually_called),
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
