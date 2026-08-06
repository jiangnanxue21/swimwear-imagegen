"""A45-batch14-3 走读修复的守卫。

本批三条:

    F5  解析失败时原文整条丢掉 —— 运营只看得到一个类型名
    F6  变异脚本的锚点/派发目标会过期,而失效的变异安静地什么都没验
    F7  `mutate_contract_tests.py` 的 18 条全部失效,却报告"全部被抓住"

F6 的守卫盯的是 `tools/audit_anchors.py` 本身。**审计工具也要被审** ——
一份"扫不出任何问题"的审计和一条恒绿的守卫是同一类东西,而它比守卫更危险:
它给出的是"全都对得上"这种结论,读的人会据此不再检查。

所以下面每一条 `test_the_audit_reports_*` 都先造一份**故意坏掉的**变异脚本
(临时目录里的 fixture),再断言审计报出来。先造变异、再写断言 ——
反过来做就是 batch13-3 / M11 的来路。

fixture 同时覆盖 `MUTATIONS` 与 `CASES` 两种形状:`CASES` 在本批之后
仓库里已经没有生产用例了(F7 删掉了唯一一份),没有 fixture 的话,
审计里那一半解析代码就变成谁也没走过的死代码 —— batch13 的 M9、
batch13-3 的 M11 记的都是这个教训。
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
PROJECT = BACKEND.parent

sys.path.insert(0, str(BACKEND))

from app.extractors.schema import (  # noqa: E402
    LOG_EXCERPT_LIMIT,
    ExtractionParseError,
    redact_for_log,
)
from app.extractors.vision import VisionAttributeExtractor  # noqa: E402

# ================================================================ F5 原文摘要


def test_the_excerpt_strips_query_strings_because_that_is_where_credentials_live():
    """预签名地址的查询串就是凭据,而它是**我们**送进提示词的。

    `EXTRACTOR_MODEL_SEND_PUBLIC_URLS=true` 时,`_prepare_image` 会把公网地址
    发给模型;模型完全可能在 `evidence_text` 里把它抄回来。原文进日志之前
    必须先过这一道。
    """
    out = redact_for_log(
        '{"fields": [{"evidence_text": "见 https://s3.example.com/a.jpg'
        '?X-Amz-Signature=DEADBEEF&X-Amz-Expires=900"}]}'
    )
    assert "DEADBEEF" not in out, "签名进了日志"
    assert "X-Amz" not in out, "查询串没抹干净"
    assert "https://s3.example.com/a.jpg" in out, "把地址整个抹掉就查不出是哪张图了"


def test_a_url_without_a_query_string_is_left_alone():
    """没有查询串的地址原样保留 —— 抹掉它只是让日志更难读,不增加任何安全性。"""
    out = redact_for_log("看 https://cdn.example.com/plain.jpg 这张")
    assert out == "看 https://cdn.example.com/plain.jpg 这张"


def test_no_query_string_survives_at_any_position():
    """地址出现在原文多靠后的位置,查询串都不许活下来。

    钉的是"只抹开头一段"那类实现。**不认任何具体窗口大小** ——
    只认「无论原文多长,签名都不出现」。

    (这条原来叫 `test_redaction_happens_before_truncation`,想钉的是
    "先抹后截"这个顺序。P1 那条变异验出那不是一条安全边界:半截地址
    照样被正则匹配、照样被削掉查询串。说明见 `redact_for_log` 的文档字符串。)
    """
    for multiple in (2, 4, 10, 50):
        padding = "x" * (LOG_EXCERPT_LIMIT * multiple)
        out = redact_for_log(padding + " https://s3.example.com/a.jpg?sig=SECRETVALUE")
        assert "SECRETVALUE" not in out, (
            f"签名在 {multiple}× 上限处泄漏 —— 实现先截了一个窗口再抹,"
            f"窗口外的查询串没过脱敏。**不认任何具体窗口大小**,只认"
            f"「无论原文多长,签名都不出现」"
        )


def test_the_excerpt_is_bounded_and_says_it_was_cut():
    long_text = "y" * (LOG_EXCERPT_LIMIT + 500)
    out = redact_for_log(long_text)
    assert len(out) < len(long_text), "没截断 —— 一条日志能把采集管道撑爆"
    assert "截断" in out, "截断了却不说,读的人会以为模型只回了这么多"
    assert str(len(long_text)) in out, "要留下原文有多长,否则判断不了是不是被 length 截断的"


def test_the_parse_failure_carries_the_raw_text_up_to_the_service():
    """原文只在 `extract()` 里有作用域,不挂到异常上,服务层就永远拿不到。"""
    broken = '{"fields": [ 这不是 JSON'
    try:
        VisionAttributeExtractor._parse_or_explain(object(), broken)
    except ExtractionParseError as exc:
        assert "raw_excerpt" in exc.detail, (
            "解析失败没带上原文摘要 —— 服务层那条 catch-all 只剩一个类型名可记"
        )
        assert "raw_length" in exc.detail
        assert exc.detail["raw_length"] == len(broken)
    else:
        raise AssertionError("坏 JSON 居然解析成功了")


def test_a_healthy_payload_is_not_touched_by_the_explainer():
    """包装层只在失败时做事。成功路径上多包一层,回归的形状是"解析对了但结果变了"。"""
    good = '{"fields": [{"name": "neckline", "value": "V 领", "confidence": 0.9}]}'
    parsed = VisionAttributeExtractor._parse_or_explain(object(), good)
    assert [f.name for f in parsed.fields] == ["neckline"]


def test_the_service_logs_the_parse_detail_and_still_hides_provider_errors():
    """服务层要**区分**两种失败,而不是一起记或者一起不记。

    读源码不 import:`app/attributes/service.py` 要 sqlalchemy,这台机器上没有。
    """
    src = (BACKEND / "app" / "attributes" / "service.py").read_text(encoding="utf-8")
    block = src[src.index("extraction failed for one image") - 1500 :]
    block = block[: block.index("extraction failed for one image") + 600]

    assert re.search(r"^\s*if isinstance\(exc, ExtractionParseError\):", block, re.M), (
        "没有按异常类型分支 —— 要么全记(会把请求地址里的凭据写进日志),"
        "要么全不记(坏 JSON 查不出模型说了什么)"
    )
    assert '"parse_error"' in block, "解析失败没记下解析器说了什么"
    assert "exc.detail" in block, "没把 detail(坏在哪 + 原文摘要)带进日志"
    assert '"error": type(exc).__name__' in block, (
        "provider 错误那条退化了 —— 它必须**只**记类型名"
    )


# ================================================================ F6 审计工具自身

AUDIT = BACKEND / "tools" / "audit_anchors.py"

#: fixture 里那份"正常"的变异脚本。两种形状各一份,都指向 fixture 自己的源码文件,
#: 这样审计跑起来不依赖真实仓库的任何一行
_GOOD_MUTATIONS = '''\
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    ("X1", "把闸拿掉", "app/thing.py", "    return guarded(value)", "    return value"),
]
SUITE_FILTER = "{suite}"
'''

_GOOD_CASES = '''\
import importlib
CASES = [
    ("把闸拿掉", "backend/app/thing.py",
     ("    return guarded(value)", "    return value"),
     "test_the_gate_is_there"),
]
m = importlib.import_module("tests.pure.{module}")
'''


def _fixture(tmp: Path, *, script: str, source: str = "    return guarded(value)") -> Path:
    """搭一棵最小的项目树,把变异脚本放进去,返回项目根。"""
    root = tmp / "project"
    (root / "backend" / "tools").mkdir(parents=True)
    (root / "backend" / "app").mkdir(parents=True)
    (root / "backend" / "tests" / "pure").mkdir(parents=True)
    (root / "backend" / "app" / "thing.py").write_text(
        f"def f(value):\n{source}\n", encoding="utf-8"
    )
    (root / "backend" / "tools" / "mutate_fixture.py").write_text(script, encoding="utf-8")
    return root


def _sibling_modules(script: Path) -> list[Path]:
    """`script` 从**同目录**里 import 的模块。

    审计工具现在不止一个文件:它和 `run_pure_tests.py` 共用
    `tools/suite_filter.py`(那份判定只能有一处,见该模块顶部)。
    只复制 `audit_anchors.py` 的话,fixture 树里的它一 import 就 ImportError,
    而这 10 条用例会**一起变红并指向各自的断言** —— 看起来像十个独立问题。

    所以这里按 import 语句算依赖,而不是维护一张手写清单:
    下一次再拆出一个兄弟模块,这个 fixture 不需要跟着改。
    """
    tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
    return [p for name in sorted(names) if (p := script.parent / f"{name}.py").is_file()]


def _run_audit(root: Path) -> tuple[int, str]:
    """在 fixture 树上跑审计。

    审计用 `parents[2]` 定位项目根,所以把它复制进 fixture 的 tools/ 里跑,
    它看到的就是 fixture 那棵树。
    """
    tools = root / "backend" / "tools"
    for extra in _sibling_modules(AUDIT):
        (tools / extra.name).write_text(extra.read_text(encoding="utf-8"), encoding="utf-8")
    target = tools / "audit_anchors.py"
    target.write_text(AUDIT.read_text(encoding="utf-8"), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "tools/audit_anchors.py"],
        cwd=root / "backend",
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.returncode, re.sub(r"\x1b\[[0-9;]*m", "", proc.stdout + proc.stderr)


def _pure_suite(root: Path, name: str) -> None:
    (root / "backend" / "tests" / "pure" / f"test_{name}.py").write_text(
        "def test_the_gate_is_there():\n    pass\n", encoding="utf-8"
    )


def test_the_audit_passes_a_healthy_script_in_both_shapes():
    """**先钉住它会绿。**

    一条永远红的检查和一条永远绿的检查一样没用 —— 后者装作在保护,
    前者会被人加进忽略名单。下面每一条"报得出来"都以这条为对照。
    """
    for shape, script in (
        ("MUTATIONS", _GOOD_MUTATIONS.format(suite="fixturesuite")),
        ("CASES", _GOOD_CASES.format(module="test_fixturesuite")),
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = _fixture(Path(tmp), script=script)
            _pure_suite(root, "fixturesuite")
            code, out = _run_audit(root)
            assert code == 0, f"{shape} 形状的健康脚本被误报:\n{out}"
            assert "1/1" in out, f"{shape} 形状一条锚点都没读到:\n{out}"


def test_the_audit_reports_an_expired_anchor():
    """最常发生的一件事:有人重构了被变异的那段源码。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = _fixture(
            Path(tmp),
            script=_GOOD_MUTATIONS.format(suite="fixturesuite"),
            source="    return guarded(value, strict=True)",
        )
        _pure_suite(root, "fixturesuite")
        code, out = _run_audit(root)
        assert code != 0 and "锚点过期" in out, out


def test_the_audit_reports_an_ambiguous_anchor():
    """注释里也含那串字 —— batch13-3 / M2 的形状。

    三份脚本里只有 `mutate_batch14_2.py` 自己拦这种;另外两份是
    `old not in text` 和 `assert old in original`,**零次会被发现,两次不会**,
    然后 `replace(old, new, 1)` 悄悄改掉第一处,而那一处可能是注释。
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = _fixture(
            Path(tmp),
            script=_GOOD_MUTATIONS.format(suite="fixturesuite"),
            source="    #     return guarded(value)\n    return guarded(value)",
        )
        _pure_suite(root, "fixturesuite")
        code, out = _run_audit(root)
        assert code != 0 and "锚点不唯一" in out, out


def test_the_audit_reports_a_dispatch_target_that_no_longer_exists():
    """F7 的形状:测试被改名/删掉,而 `getattr` 抛的 AttributeError
    会被脚本按"非零 = 响了"读成**被抓住**。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = _fixture(Path(tmp), script=_GOOD_CASES.format(module="test_fixturesuite"))
        (root / "backend" / "tests" / "pure" / "test_fixturesuite.py").write_text(
            "def test_something_else():\n    pass\n", encoding="utf-8"
        )
        code, out = _run_audit(root)
        assert code != 0 and "派发目标不存在" in out, out


def test_the_audit_reports_every_problem_on_a_row_not_just_the_first():
    """一行上可以同时有多个问题,**一次报完**。

    只报第一条的话,修完派发名字再跑一次才发现锚点也过期了 ——
    一次能说完的事分成两轮说。真实仓库里的 `CASES[9]` 正是这个形状:
    派发目标不存在 **且** 锚点过期。
    """
    with tempfile.TemporaryDirectory() as tmp:
        # 锚点对不上(源码改了)+ 派发目标不存在(测试改名了)
        root = _fixture(
            Path(tmp),
            script=_GOOD_CASES.format(module="test_fixturesuite"),
            source="    return guarded(value, strict=True)",
        )
        (root / "backend" / "tests" / "pure" / "test_fixturesuite.py").write_text(
            "def test_something_else():\n    pass\n", encoding="utf-8"
        )
        code, out = _run_audit(root)
        assert code != 0
        assert "派发目标不存在" in out and "锚点过期" in out, (
            "同一行上的两个问题只报了一个:\n" + out
        )


def test_the_audit_reports_a_suite_filter_that_matches_nothing():
    """失锚的镜像:套件里一条用例都没有 -> 平凡通过 -> **每条变异都报 GREEN**,
    而报告会指向"守卫不咬人",让人去改守卫。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = _fixture(Path(tmp), script=_GOOD_MUTATIONS.format(suite="nothing_matches_this"))
        _pure_suite(root, "fixturesuite")
        code, out = _run_audit(root)
        assert code != 0 and "SUITE_FILTER" in out, out


def test_the_audit_reports_a_suite_filter_that_matches_more_than_one_suite():
    """挑中多个套件时的病不是"慢",是**归因**(batch14-4 §五之二)。

    `"a45_batch14"` 这类子串会连带挑上 `_2` `_3` `_4`。于是这份脚本的一条变异
    可以因为**别的套件**里的守卫而变红,而报告记在自己头上 —— "我这条守卫
    咬人"是假的,删掉真正咬人的那条守卫时变异仍然红着,没有人会发现。

    与 F7 同一种病:红是真的,红的原因不是。慢只是它顺带的表现。
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = _fixture(Path(tmp), script=_GOOD_MUTATIONS.format(suite="fixturesuite"))
        _pure_suite(root, "fixturesuite")
        _pure_suite(root, "fixturesuite_2")  # 同一个子串同时挑中两个
        code, out = _run_audit(root)
        assert code != 0, f"两个套件都被挑中却算通过了:\n{out}"
        assert "挑中了 2 个套件" in out, out
        # 报告要直接给出改法,否则读的人得自己去翻规则
        assert "test_fixturesuite.py" in out, f"没告诉人改成什么:\n{out}"


def test_the_audit_accepts_an_exact_suite_filter_even_when_a_prefix_would_be_ambiguous():
    """精确文件名是出路 —— 这条钉住上一条不是"多个文件就一律报错"。

    没有这条对照,把 `select()` 写成"永远只返回零或一个"也能让上一条变绿。
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = _fixture(
            Path(tmp), script=_GOOD_MUTATIONS.format(suite="test_fixturesuite.py")
        )
        _pure_suite(root, "fixturesuite")
        _pure_suite(root, "fixturesuite_2")
        code, out = _run_audit(root)
        assert code == 0, f"精确文件名被误报:\n{out}"


def test_the_audit_reports_an_unknown_table_shape():
    """第三种形状出现时要**大声**,不能当成"这份脚本没问题"。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = _fixture(
            Path(tmp),
            script=_GOOD_MUTATIONS.format(suite="fixturesuite").replace("MUTATIONS", "MUTANTS"),
        )
        _pure_suite(root, "fixturesuite")
        code, out = _run_audit(root)
        assert code != 0 and "没有找到任何已知形状" in out, out


def test_the_audit_reports_a_no_op_mutation():
    with tempfile.TemporaryDirectory() as tmp:
        root = _fixture(
            Path(tmp),
            script=_GOOD_MUTATIONS.format(suite="fixturesuite").replace(
                '"    return value"', '"    return guarded(value)"'
            ),
        )
        _pure_suite(root, "fixturesuite")
        code, out = _run_audit(root)
        assert code != 0 and "改了个寂寞" in out, out


def test_the_audit_reports_an_anchor_it_cannot_read_statically():
    """f-string 锚点求不出值。**当作失败,不当作跳过** ——
    读不出来的行就是没有被审计的行。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = _fixture(
            Path(tmp),
            script=_GOOD_MUTATIONS.format(suite="fixturesuite").replace(
                '"    return guarded(value)"', 'f"    return guarded(value){\'\'}"'
            ),
        )
        _pure_suite(root, "fixturesuite")
        code, out = _run_audit(root)
        assert code != 0 and "读不出来" in out, out


def test_the_audit_refuses_to_pass_when_it_finds_nothing_to_audit():
    """审计对象为空**不是通过**。一条命中都没有的不变式是绿着装样子。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "project"
        (root / "backend" / "tools").mkdir(parents=True)
        code, out = _run_audit(root)
        assert code != 0 and "一份变异脚本都没找到" in out, out


def test_the_audit_never_imports_the_scripts_it_audits():
    """**这一条是这份工具存在的前提。**

    被审的脚本里有一份(历史上是 `mutate_contract_tests.py`)把变异循环写在
    模块顶层,没有 `__main__` 保护 —— import 它就是当场改写真实工作树、
    起十几个子进程。审计一旦退回 import,它自己就成了事故。
    """
    src = AUDIT.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            assert name not in ("import_module", "exec", "eval", "__import__"), (
                f"审计里出现了 `{name}` —— 它必须只解析,不执行被审的脚本"
            )
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names]
            assert "importlib" not in names, "审计不许 import importlib"
