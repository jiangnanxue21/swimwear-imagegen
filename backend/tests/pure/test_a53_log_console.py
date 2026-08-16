"""运行日志控制台的守卫(`docs/LOG-CONSOLE.md` §8)。

按仓库惯例,约定不许只写在文档里。四条,前三条 AST:

    一  事件必注册,且**双向** —— 写了没登记会红,登记了没人写也会红
    二  载荷只走脱敏函数,不许把 `request.body` / `response.json()` 直接递进旁挂库
    三  查看器不持有任何消息原文字面量
    四  结构化字段必须包在 `extra_fields` 里(这一条是本轮**捡到的缺陷**)

第四条不在设计文档里,是接线时撞出来的:14 个调用点写的是
`logger.warning(msg, extra={"key": ...})` —— 少了 `extra_fields` 那一层包裹,
而 `JsonFormatter` 只读 `record.extra_fields`。**那些字段一个都没进过日志**,
不报错、不提示,连 `batch.billed_result_unknown_refusing_paid_retry`
(拒绝一次付费重试)那条的 `key` / `action` 都是空的。
详见 `docs/DECISIONS.md` §3.80。
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.core.log_events import (
    DOMAINS,
    EVENTS,
    LOGGER_DOMAIN_FALLBACK,
    ROUTINE_GROUPS,
    domain_for_logger,
    resolve_domain,
)

BACKEND = Path(__file__).resolve().parents[2]
APP = BACKEND / "app"
LOG_METHODS = {"info", "warning", "error", "exception", "debug", "critical"}


# ============================================================ 采集面扫描


def _logger_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "logger"
        and node.func.attr in LOG_METHODS
    ]


def _extra_fields_dict(call: ast.Call) -> ast.expr | None:
    """`extra={"extra_fields": X}` 里的那个 X。没有这个形状返回 None。"""
    for keyword in call.keywords:
        if keyword.arg != "extra" or not isinstance(keyword.value, ast.Dict):
            continue
        for key, value in zip(keyword.value.keys, keyword.value.values, strict=False):
            if isinstance(key, ast.Constant) and key.value == "extra_fields":
                return value
    return None


def _event_literals(tree: ast.AST) -> list[str]:
    """源码里所有 `"event": "..."` 的取值。

    **按字典字面量找,不按调用点找。** 传输层把字段先攒进一个变量再传给
    `logger.info(...)`,按调用点找会漏掉那两条;而漏掉的表现是"这个码没人写",
    于是双向断言会反过来冤枉注册表。
    """
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=False):
            if (
                isinstance(key, ast.Constant)
                and key.value == "event"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                found.append(value.value)
    return found


def _calls_get_logger(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "get_logger"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "__name__"
        for node in ast.walk(tree)
    )


def _app_sources() -> list[tuple[Path, ast.AST]]:
    return [(p, ast.parse(p.read_text(encoding="utf-8"))) for p in sorted(APP.rglob("*.py"))]


# ============================================================ 守卫一:事件必注册


def test_every_event_written_at_a_call_site_is_registered():
    written = {e for _, tree in _app_sources() for e in _event_literals(tree)}
    unknown = sorted(written - set(EVENTS))
    assert not unknown, f"这些事件码没有在 EVENTS 里登记:{unknown}"


def test_every_registered_event_is_actually_written_somewhere():
    """反向:登记了却没人写的码,会在筛选下拉里摆出一个永远筛不出东西的选项。

    而运营会把它读成"这段时间没发生",不是"这个码是假的"。
    """
    written = {e for _, tree in _app_sources() for e in _event_literals(tree)}
    orphans = sorted(set(EVENTS) - written)
    assert not orphans, f"这些事件码登记了但没有任何调用点写它:{orphans}"


def test_every_event_key_lives_in_a_known_domain():
    strays = sorted(key for key in EVENTS if key.split(".", 1)[0] not in DOMAINS)
    assert not strays, f"这些事件码的域前缀不在 DOMAINS 里:{strays}"


def test_every_event_key_has_the_documented_shape():
    for key, event in EVENTS.items():
        assert key == event.key, f"{key} 的 key 字段和它的键不一致"
        assert key.count(".") == 1, f"{key} 不是「域.动作」两段式"
        assert key.islower(), f"{key} 必须是小写"
        assert event.label.strip(), f"{key} 没有中文标签"


def test_every_routine_group_is_a_known_one():
    """`routine` 不是一个布尔,是一个分组名 —— 折叠计数条要说得出折的是什么。"""
    for key, event in EVENTS.items():
        if event.routine_group is not None:
            assert event.routine_group in ROUTINE_GROUPS, f"{key} 的例行分组未定义"


# ============================================================ 守卫二:域推导必须覆盖全部模块


def test_every_module_with_a_logger_is_in_the_fallback_table():
    """新模块不进推导表就红 —— 「未分类」不许在查看器里出现。

    少一行不会报错,只会让那个模块的日志静静地落进兜底域,而兜底域是
    `app`(进程生命周期)—— 一条发布失败因此会被归进"进程生命周期"。
    """
    prefixes = [prefix for prefix, _ in LOGGER_DOMAIN_FALLBACK]
    missing: list[str] = []
    for path, tree in _app_sources():
        # 按 AST 找**真的调用**,不按源码里有没有这串字。后者会把
        # `log_events.py` 自己算进来 —— 它在文档字符串里引用了这个调用形状。
        if not _calls_get_logger(tree):
            continue
        module = "app." + str(path.relative_to(APP).with_suffix("")).replace("/", ".")
        module = module.removesuffix(".__init__")
        if not any(module == p or module.startswith(p + ".") for p in prefixes):
            missing.append(module)
    assert not missing, f"这些模块持有 logger 但不在 LOGGER_DOMAIN_FALLBACK 里:{missing}"


def test_the_fallback_table_only_points_at_known_domains():
    strays = sorted({d for _, d in LOGGER_DOMAIN_FALLBACK if d not in DOMAINS})
    assert not strays, f"推导表指向了不存在的域:{strays}"


def test_the_longest_prefix_wins():
    """`app.workbench.batch_service` 归 batch,而不是被 `app.workbench` 抢走。"""
    assert domain_for_logger("app.workbench.batch_service") == "batch"
    assert domain_for_logger("app.workbench.platform_service") == "publish"
    assert domain_for_logger("app.workbench.service") == "listing"


def test_a_third_party_logger_still_lands_somewhere():
    assert domain_for_logger("celery.app.trace") in DOMAINS
    assert domain_for_logger("") in DOMAINS


def test_the_event_wins_over_the_logger_prefix():
    """一个模块两个域:`app.main` 既产访问日志也产启动日志。"""
    assert resolve_domain("http.request_completed", "app.main") == "http"
    assert resolve_domain(None, "app.main") == "app"


def test_an_unregistered_event_code_does_not_invent_a_domain():
    """打错的码不该长出一个不存在的域,而界面会老实地把它画出来。"""
    assert resolve_domain("llmm.attempt_failed", "app.llm.transport") == "llm"


# ============================================================ 守卫三:载荷只走脱敏函数


def test_the_payload_store_only_ever_stores_redacted_values():
    """写进旁挂库的实参必须是脱敏函数的返回值。

    这条和现有的「摘要里不出现 base64」是同一条规矩的延长线 —— **新开一个
    去向,最容易漏的就是在新去向上把老规矩忘了**。
    """
    source = (BACKEND / "app/llm/payload_store.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    redact_body = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "redact"
    )
    calls = [
        n.func.id
        for n in ast.walk(redact_body)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    assert "safe_payload_for_log" in calls, "redact() 必须调 safe_payload_for_log"

    # 两个 capture_* 里,凡是进 payload 的原始值都必须过 `redact(...)`。
    for name in ("capture_request", "capture_attempt"):
        fn = next(
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name
        )
        segments = [
            ast.get_source_segment(source, value)
            for node in ast.walk(fn)
            if isinstance(node, ast.Dict)
            for key, value in zip(node.keys, node.values, strict=False)
            if isinstance(key, ast.Constant) and key.value in {"body", "headers", "endpoint"}
        ]
        assert segments, f"{name} 没有把请求/响应正文放进 payload?"
        for segment in segments:
            assert segment and segment.startswith("redact("), (
                f"{name} 把 {segment} 直接写进了旁挂库 —— 必须先过 redact()"
            )


def _settings_default(name: str) -> object:
    """从 `config.py` 的源码里读一个字段的默认值。

    **不 import `Settings`。** 那需要 pydantic,而这台机器上没有 pydantic 时
    整条用例会被跳过 —— 而它守的正是"归档面默认值一个字没动",
    是最不该在某些机器上静默消失的那一条(`backend/CLAUDE.md` 里那句
    「73 个 skip 恰好是全部集成测试」讲的就是这个)。
    """
    source = (BACKEND / "app/core/config.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(
        n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "Settings"
    )
    for node in cls.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and isinstance(node.value, ast.Constant)
        ):
            return node.value.value
    raise AssertionError(f"`Settings` 里没有 {name} 这个字段")


def test_the_redaction_default_for_the_archive_side_is_untouched():
    """归档面那一档的默认值一个字都不许动。

    `LLM_LOG_PAYLOADS` 的语义与默认值是另一条决策,本轮新增的是**另一个去向**,
    不是把它偷偷打开。
    """
    from app.llm.redaction import MAX_LOG_STRING_CHARS

    assert MAX_LOG_STRING_CHARS == 12_000
    assert _settings_default("LLM_LOG_PAYLOADS") is False


def test_an_image_data_url_never_reaches_the_sidecar_in_one_piece():
    """运行时那一半:构造一个带 base64 图片与明文密钥的请求,断言两样都没落进去。"""
    from app.llm.payload_store import redact

    stored = redact(
        {
            "model": "vision-prod",
            "authorization": "Bearer sk-abcdefghijklmnopqrstuvwxyz",
            "input": [{"image_url": "data:image/png;base64," + "A" * 5000}],
        }
    )
    flat = str(stored)
    assert "A" * 100 not in flat, "图片 base64 正文进了旁挂库"
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in flat, "明文密钥进了旁挂库"
    assert stored["authorization"] == "***"
    chip = stored["input"][0]["image_url"]
    assert chip["redacted"] == "inline_image"
    assert chip["base64_chars"] == 5000
    assert len(chip["sha256_16"]) == 16


def test_the_sidecar_string_budget_is_wider_than_the_archive_one():
    """系统提示词有几千字,截在 12k 会把"输出要求"那段切掉。"""
    from app.llm.redaction import MAX_LOG_STRING_CHARS

    assert _settings_default("OPS_PAYLOAD_STRING_CHARS") > MAX_LOG_STRING_CHARS


def test_the_sidecar_capture_defaults_on_and_expires():
    """默认开是这一章唯一需要点头的地方,所以把它钉住 —— 连同 TTL。

    默认关的话,值得排查的那类失败(不可复现的)永远留不下现场,
    而那正是整个旁挂库存在的理由。
    """
    assert _settings_default("OPS_LLM_PAYLOAD_CAPTURE") is True
    assert _settings_default("OPS_LLM_PAYLOAD_TTL_SECONDS") == 86_400


def test_truncation_is_visible_and_listed():
    from app.llm.payload_store import truncated_paths
    from app.llm.redaction import safe_payload_for_log

    cut = safe_payload_for_log({"system": "字" * 50}, max_string_chars=10)
    assert "truncated" in cut["system"], "截断必须显形,不能让人以为看到的是全文"
    assert truncated_paths({"request": cut}) == ["request.system"]


# ============================================================ 守卫四:字段必须真的进得了日志


def test_every_logger_call_wraps_its_fields_in_extra_fields():
    """`extra={"key": ...}` 少了 `extra_fields` 那一层 —— 字段会被静默丢弃。

    `JsonFormatter` 只读 `record.extra_fields`;别的键会被 `logging` 挂到
    record 上然后**没有任何人去看**。不报错、不提示,只是那条日志比作者
    以为的少了一半 —— 而作者是在出事时才会去读它的。
    """
    offenders: list[str] = []
    for path, tree in _app_sources():
        for call in _logger_calls(tree):
            if not any(k.arg == "extra" for k in call.keywords):
                continue
            if _extra_fields_dict(call) is None:
                offenders.append(f"{path.relative_to(BACKEND)}:{call.lineno}")
    assert not offenders, (
        "这些调用点的结构化字段进不了日志(缺 extra_fields 包裹):" + ", ".join(offenders)
    )


def test_the_formatter_really_drops_unwrapped_fields():
    """上面那条守卫的理由,用一次真的格式化钉住 —— 别人删守卫前先看这个。"""
    import logging

    from app.core.logging import JsonFormatter

    record = logging.LogRecord("app.workbench.batch_service", 30, "x", 1, "probe", None, None)
    record.key = "K1"  # 没有 extra_fields 包裹时 logging 就是这么挂上去的
    assert "K1" not in JsonFormatter().format(record)


# ============================================================ 守卫五:查看器不持有消息原文


def _strip_comments_and_docstrings(source: str) -> str:
    """吃掉注释与文档字符串之后的源码。

    **反向断言必须吃掉它们**(a51 变异验红的教训):正向断言会命中 docstring
    里的同一串字,于是一条本该拦住人的规则永远是绿的。
    """
    tree = ast.parse(source)
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                spans.append((body[0].lineno, body[0].end_lineno))
    lines = source.split("\n")
    kept = []
    for index, line in enumerate(lines, start=1):
        if any(start <= index <= end for start, end in spans):
            continue
        kept.append(line.split("#", 1)[0])
    return "\n".join(kept)


def test_the_viewer_holds_no_message_literal_from_any_call_site():
    """过滤从注册表取,不从消息原文取。

    原来那 9 条硬编码消息是这一整套设计的起点:措辞一改,查看器**安静漏事件**。
    """
    viewer = BACKEND / "tools/watch_logs.py"
    code = _strip_comments_and_docstrings(viewer.read_text(encoding="utf-8"))

    messages: set[str] = set()
    for _, tree in _app_sources():
        for call in _logger_calls(tree):
            first = call.args[0] if call.args else None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                messages.add(first.value)

    leaked = sorted(m for m in messages if len(m) > 12 and m in code)
    assert not leaked, f"查看器里出现了调用点的 message 原文:{leaked[:5]}"


def test_the_viewer_imports_the_registry():
    code = (BACKEND / "tools/watch_logs.py").read_text(encoding="utf-8")
    assert "from app.core.log_events import" in code
    # 这条断言防的是旧查看器被谁复活回来:它带着那 9 条硬编码消息,
    # 留着下一个人会照着抄回来
    # 旧查看器已删,改名成 watch_logs.py ——
    assert not (BACKEND / "tools/watch_ai_logs.py").exists(), "旧查看器又回来了"



# ============================================================ 环形缓冲:日志不许反噬业务


def test_the_ring_swallows_every_failure_and_counts_it():
    """Redis 挂了不许把一次出图调用带下水,但也**不许瞎** —— 掉了多少要能看见。"""
    import logging

    from app.core import log_ring
    from app.core.logging import JsonFormatter

    class Exploding:
        def pipeline(self, *_args, **_kwargs):
            raise RuntimeError("redis is down")

    before = log_ring.STATS.snapshot()["dropped_since_boot"]
    handler = log_ring.RingHandler(url="redis://localhost:6379/0", cap=10)
    handler.setFormatter(JsonFormatter())
    log_ring._HOLDER._client = Exploding()  # noqa: SLF001 - 守卫要打到这一层
    log_ring._HOLDER._blocked_until = 0.0  # noqa: SLF001

    record = logging.LogRecord("app.llm.transport", 20, "x", 1, "probe", None, None)
    handler.emit(record)  # 不抛就是这条守卫的全部要求

    after = log_ring.STATS.snapshot()["dropped_since_boot"]
    assert after == before + 1, "掉了一条却没有记账"
    log_ring.reset_client_for_tests()


def test_the_ring_entry_carries_a_dedupe_marker():
    import logging

    from app.core import log_ring
    from app.core.logging import JsonFormatter

    captured: list[str] = []

    class Recording:
        def pipeline(self, *_args, **_kwargs):
            return self

        def lpush(self, _key, line):
            captured.append(line)

        def ltrim(self, *_args):
            return self

        def execute(self):
            return None

    handler = log_ring.RingHandler(url="redis://localhost:6379/0", cap=10)
    handler.setFormatter(JsonFormatter())
    log_ring._HOLDER._client = Recording()  # noqa: SLF001
    log_ring._HOLDER._blocked_until = 0.0  # noqa: SLF001
    handler.emit(logging.LogRecord("app.llm.transport", 20, "x", 1, "probe", None, None))
    log_ring.reset_client_for_tests()

    import json

    row = json.loads(captured[0])
    assert row["seq"], "跟随模式靠 seq 去重,少了它刷新会重复整屏"
    assert row["domain"] == "llm"


def test_the_ring_never_becomes_the_archive():
    """环形是诊断窗口。这条钉的是"stdout 那条链路一个字节没动"。"""
    source = (BACKEND / "app/core/logging.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    setup = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "setup_logging"
    )
    body = ast.get_source_segment(source, setup) or ""
    assert "StreamHandler" in body, "stdout handler 不见了 —— 归档面被换掉了"
    assert "_attach_ring_handler" in body, "环形 handler 没有挂上"
    assert body.index("StreamHandler") < body.index("_attach_ring_handler"), (
        "环形必须是**第二个** handler:stdout 先挂,它挂不上也不影响归档"
    )
