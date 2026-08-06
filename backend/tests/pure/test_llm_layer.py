"""M2 验收判据(§16 修正版)。

需求文档把 M2 的验收从「vision.py ≤ 450 行」改成了四条**可静态校验**的判据。
这个文件就是那四条:

    无重复        识别层与评分层之间不存在复制粘贴的 HTTP/重试/SSRF/base64 逻辑
    依赖方向清晰   app/llm 不得 import app/evaluators、app/extractors、app/channels
    可复用        同一个 MultimodalClient 被两层使用,无子类特判
    行为不变      现有 test_vision_evaluator.py / test_vision_http.py 全绿且零修改

第四条不在这里断言 —— 它由那两个文件自己保证。这里只断言前三条。

「测试文件未被修改」和「旧模块仍再导出旧符号」两条守卫已在 M2 验收完成后
删除:前者验证的是开发过程而非系统行为,后者会逼着生产模块长期保留无意义的
转发符号。测试现在直接从符号真正所属的模块(``app.llm.transport``)导入。
"""
from __future__ import annotations

import ast
from pathlib import Path

from tests.pure._helpers import BACKEND_ROOT

APP_DIR = BACKEND_ROOT / "app"
LLM_DIR = APP_DIR / "llm"

#: `app/llm` 不得依赖的上层包。它是被它们共用的下层,反向依赖会立刻
#: 变成循环 import,而且意味着「传输层知道自己在为谁服务」——
#: 那样就没法被第二个调用方复用。
FORBIDDEN_UPSTREAM = ("app.evaluators", "app.extractors", "app.channels", "app.listings")


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


# ---------------------------------------------------------------- 依赖方向


def test_llm_never_imports_an_upper_layer():
    """这条是 import-linter 规则的零依赖版本,违反即 CI 红。"""
    problems: list[str] = []
    for path in LLM_DIR.glob("*.py"):
        for name in _imports(path):
            for forbidden in FORBIDDEN_UPSTREAM:
                if name == forbidden or name.startswith(forbidden + "."):
                    problems.append(f"{path.name} 导入了 {name}")
    assert not problems, "app/llm 反向依赖了上层 -> " + "; ".join(problems)


# ---------------------------------------------------------------- 无重复


def test_image_preparation_has_exactly_one_implementation():
    """SSRF / 逐跳重定向 / 钉 IP / base64 只能有一份实现。

    识别层(M3)要逐图调用模型,需要的图片准备逻辑和评分层完全一样。
    它自己写一遍的话,写出来的多半是 `requests.get(url).content` ——
    而 SSRF 那条正是这个项目已经修过两轮的地方。
    """
    owners = []
    for path in APP_DIR.rglob("*.py"):
        if "test" in path.name:
            continue
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        # 看的是**调用**,不是字符串出现 —— 注释里提一句不算实现
        calls = {
            getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
        }
        if "pinned_transport" in calls and "verify_image" in text:
            owners.append(str(path.relative_to(APP_DIR)))
    assert owners == ["llm/images.py"], f"图片准备逻辑出现在多处:{owners}"


def test_retry_loop_has_exactly_one_implementation():
    """退避重试只能有一份。

    识别层自己写一遍的后果很具体:没有退避、不认 Retry-After、
    把 429 当成永久失败 —— 于是识别在高峰期整批失败,
    而评分层明明已经解决过这个问题。
    """
    owners = []
    for path in APP_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "MAX_RETRY_AFTER_SECONDS" in text and "random.uniform" in text:
            owners.append(str(path.relative_to(APP_DIR)))
    assert owners == ["llm/transport.py"], f"退避逻辑出现在多处:{owners}"


def test_evaluator_keeps_no_transport_logic():
    """评分器里不该再有 HTTP 往返的实现。

    只允许转发。判据:`vision.py` 里不出现 httpx 的异常处理,
    也不出现自己建客户端的代码。
    """
    text = (APP_DIR / "evaluators" / "vision.py").read_text(encoding="utf-8")
    assert "httpx.AsyncClient(" not in text, "评分器又自己建 HTTP 客户端了"
    assert "httpx.TimeoutException" not in text, "评分器又自己处理 httpx 异常了"
    assert "asynccontextmanager" not in text, "客户端生命周期应该在传输层"


# ---------------------------------------------------------------- 可复用


def test_client_takes_a_protocol_not_a_subclass():
    """传输层用 Protocol 约束配置,不用基类。

    评分层和识别层是两套不同的环境变量,让它们继承同一个基类
    只会逼出一堆 isinstance 分支 —— 而「无子类特判」正是验收判据之一。
    """
    text = (LLM_DIR / "transport.py").read_text(encoding="utf-8")
    assert "class TransportConfig(Protocol)" in text
    # 要禁的是"按配置的类型分支",不是所有 isinstance ——
    # 解析不可信的响应 JSON 时做类型检查完全正当,而且必须做。
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "isinstance"):
            continue
        target = ast.unparse(node.args[0]) if node.args else ""
        assert "config" not in target, (
            f"传输层按配置类型分支了:isinstance({target}, ...) —— "
            "这正是「无子类特判」要禁的东西"
        )


def test_retry_policy_is_separable_from_the_round_trip():
    """重试策略与「一次往返」必须能分开。

    识别层将来要接批量接口时,需要的是另一种往返,而不是另一套退避。
    `send()` 因此接受一个可注入的 `send_once`。
    """
    tree = ast.parse((LLM_DIR / "transport.py").read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "send"
    )
    kwonly = {a.arg for a in fn.args.kwonlyargs}
    assert "send_once" in kwonly, "send() 没有把单次往返做成可注入的"
