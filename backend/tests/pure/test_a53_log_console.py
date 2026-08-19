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
import json
from pathlib import Path

from app.core.log_events import (
    ACCESS_EVENT,
    DOMAINS,
    EVENTS,
    LOGGER_DOMAIN_FALLBACK,
    ROUTINE_GROUPS,
    domain_for_logger,
    folds_away,
    level_at_least,
    normalize_level,
    parse_ts,
    resolve_domain,
    seq_sort_key,
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
        module = "app." + path.relative_to(APP).with_suffix("").as_posix().replace("/", ".")
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
    # **吃去注释再比位置。**(a55 修)
    #
    # 上一版用 `ast.get_source_segment`,而那个函数返回的是**带注释的源码**。
    # 于是 a55 在函数开头加了一段注释、里面提到了 `_attach_ring_handler`,
    # 这条守卫当场变红 —— 而代码本身的挂载顺序一个字没动。
    #
    # 这正是第三条守卫(查看器不持消息字面量)顶上写着的那个教训,原话是
    # 「正向断言会命中 docstring 里的同一串字」。同一个坑,换了个文件复发。
    # 判据应该是"代码做了什么",不是"源文件里这两个词谁先出现"。
    body = ast.unparse(setup)
    assert "StreamHandler" in body, "stdout handler 不见了 —— 归档面被换掉了"
    assert "_attach_ring_handler" in body, "环形 handler 没有挂上"
    assert body.index("StreamHandler") < body.index("_attach_ring_handler"), (
        "环形必须是**第二个** handler:stdout 先挂,它挂不上也不影响归档"
    )


# ============================================================ a54 修复的守卫
#
# 下面这一组钉的是 a53 落地时**没有被任何东西守住**的七件事。它们的共同形状:
# 每一条都不会报错、不会变红,只会让控制台在某个具体时刻少说一句真话。


def test_the_worker_installs_the_json_logging_too():
    """**worker 必须自己装一次日志。**

    a53 只在 `app/main.py` 顶层调过 `setup_logging()`,而 worker 的入口是
    `celery -A app.tasks.celery_app.celery_app worker` —— 它不 import
    `app.main`,全仓也没有任何模块 import 它。于是环形里一条 worker 日志
    都没有,而"为什么是 Redis 而不是进程内环形"的全部理由就是那 59 个
    住在 worker 里的调用点。

    症状是沉默的:页面上 `gen` / `batch` / `publish` 三个域基本空着,
    看起来像"这段时间没跑任务"。连 a53 交接给出的自检顺序(先看 `held`
    涨不涨)都发现不了 —— API 进程自己在写,那个数一直在涨。
    """
    source = (BACKEND / "app/tasks/celery_app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    hooked: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        calls = {
            n.func.id
            for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        if "setup_logging" not in calls:
            continue
        for deco in node.decorator_list:
            target = deco.func if isinstance(deco, ast.Call) else deco
            if isinstance(target, ast.Attribute):
                hooked.add(target.value.id if isinstance(target.value, ast.Name) else target.attr)
    assert "worker_process_init" in hooked, (
        "worker 子进程没有装 JSON 日志 —— 环形里不会有任何 tasks 域的日志,"
        "而页面不会说少了一个进程,它只会看起来像「这段时间没跑任务」"
    )
    assert "beat_init" in hooked, "beat 进程同理:它产出的节拍与投递日志也该进环形"


def _access_record(fields: dict):
    """造一条访问日志的 LogRecord。三条过滤器测试共用。"""
    import logging as std_logging

    one = std_logging.LogRecord("app.main", 20, "x", 1, "http request completed", None, None)
    one.extra_fields = fields
    return one


def test_watching_the_console_does_not_flush_the_console():
    """**看日志的动作不许冲刷诊断窗口。**(a54 的那一半,`all` 模式下仍然成立)

    a53 没有这条过滤:中间件对每个请求写一条访问日志,包括 `/api/ops/logs`
    自己,而跟随模式是 3 秒一拍。1200 条/小时、cap 5000 —— 四小时后环形里
    全是"某人在看运行日志",而 `held/cap` 显示 5000/5000,看起来非常健康。
    """
    from app.core.log_ring import AccessNoiseFilter

    keep = AccessNoiseFilter(mode="all")
    self_traffic = {"event": ACCESS_EVENT, "path": "/api/ops/logs", "status": 200}
    assert not keep.filter(_access_record(self_traffic)), "控制台自己的 2xx 访问日志不该进环形"
    assert keep.filter(_access_record({**self_traffic, "status": 500})), (
        "`/api/ops/logs` 自己 500 了是要看见的 —— 那不是噪音,是这一页坏了"
    )
    assert keep.filter(_access_record({**self_traffic, "path": "/api/generation-tasks"})), (
        "`all` 模式下别的端点的访问日志照进 —— 那一档被挡掉的只有自指部分"
    )
    assert keep.filter(_access_record({"event": "gen.round_evaluated", "path": "/api/ops/logs"})), (
        "只认访问日志那一个事件码,不许按路径一刀切"
    )


# ============================================================ a55 修复的守卫
#
# a54 的守卫全绿、门禁全绿,而下面这一组钉的每一件都**没有被它们看见**。
# 共同形状还是那个:不报错、不变红,只在某个具体时刻让控制台少说一句真话。
# 与 a54 那一组的区别是,这一轮里有一条是**守卫自己**看走了眼(见
# `test_the_stream_really_stops_serialising_the_whole_window`)。


def test_the_access_filter_defaults_to_keeping_the_window_usable():
    """**默认档只收出错的访问日志。**(a55)

    a54 只挡了 `/api/ops/` 前缀,而前端有 17 处 `refetchInterval`:打开一个
    任务详情页 ≈ 90 请求/分钟 = **5400 条/小时**,cap 是 5000。一个开着任务
    详情页的标签,一小时内把整个诊断窗口洗一遍 —— 而排障的人恰恰会开着
    任务详情页。

    **修了自指那一半比不修更危险**:这些行标了 routine,折进计数条;
    `held/cap` 显示 5000/5000,于是没人再怀疑它。
    """
    from app.core.log_ring import AccessNoiseFilter

    keep = AccessNoiseFilter(mode="errors")
    for status in (200, 204, 302):
        assert not keep.filter(
            _access_record(
                {"event": ACCESS_EVENT, "path": "/api/generation-tasks", "status": status}
            )
        ), f"{status} 的访问日志在默认档不该占用诊断窗口 —— 它照常进 stdout"
    for status in (401, 404, 500, 503):
        assert keep.filter(
            _access_record(
                {"event": ACCESS_EVENT, "path": "/api/generation-tasks", "status": status}
            )
        ), f"{status} 是诊断信息,不是噪音 —— 两个档位下都必须放行"
    assert keep.filter(_access_record({"event": "gen.round_evaluated"})), (
        "非访问日志一律放行 —— 这个过滤器只认那一个事件码"
    )


def test_an_unknown_access_mode_fails_at_startup():
    """拼错的模式名在**启动期**就炸,不留到运行期。

    按默认处理的话表现是"访问日志少了一半而没人知道为什么";按 `all` 处理
    的话表现是"窗口又开始被冲刷了"。**两种静默失败都指向同一个结论 ——
    这个值不许猜。**

    ## 为什么在这里只做源码断言

    `tests/pure/` 跑在只有标准库的运行器上,`app.core.config` 依赖
    pydantic-settings,import 不进来 —— 顶层 import 会让整个文件在导入期炸掉,
    而运行器只把它记成一条失败(`run_pure_tests.py` 顶部写明的那类坑)。
    真的构造一次 `Settings` 的用例在 `tests/test_a55_log_console.py`。
    """
    source = (BACKEND / "app/core/config.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    validators = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_known_access_mode"
    ]
    assert validators, "OPS_LOG_RING_ACCESS 没有启动期校验 —— 拼错会静默走默认档"
    body = ast.unparse(validators[0])
    assert "raise ValueError" in body, "校验函数不抛 —— 那它只是个格式化器"
    assert "errors" in body and "all" in body, "两个合法取值必须在校验里列出来"


def test_every_log_line_says_which_process_wrote_it():
    """**每条日志答得出「哪个进程写的」。**(a55)

    API / worker / beat / script 全部 LPUSH 进同一个 `ops:log_ring`,而在此
    之前没有任何顶层字段区分它们。于是运行日志页上「`gen` 域为什么是空的」
    **答不出来** —— 是没跑任务,还是 worker 挂了?这两件事的下一步完全相反。

    a54 修的是「worker 的日志一条都没进过环形」;修完之后的新问题是
    「进来了但认不出是谁」,而排障的第一个问题恰恰是「哪个进程」。
    """
    import logging as std_logging

    from app.core import logging as logging_mod

    # 直接改模块级的那个名字,不调 `setup_logging` —— 它会 `root.handlers.clear()`
    # 并挂一个新的 stdout handler,而纯运行器正靠 stdout 捕获输出。
    was = logging_mod._service_name  # noqa: SLF001
    try:
        logging_mod._service_name = "worker"  # noqa: SLF001
        record = std_logging.LogRecord("app.tasks.batch_tasks", 20, "x", 1, "hi", None, None)
        record.extra_fields = {"event": "batch.async_finished"}
        payload = logging_mod.JsonFormatter().build_payload(record)
    finally:
        logging_mod._service_name = was  # noqa: SLF001
    assert payload["service"] == "worker", "顶层没有 service —— 三种进程在窗口里分不开"
    assert isinstance(payload["pid"], int), "pid 要是个能读的整数,不是 seq 里那段十六进制"


def test_every_entry_point_names_itself():
    """四个入口都必须报上名字,不许默认。

    默认值是 `api`,而一个**没报名字的 worker 会自称 api** —— 那比没有这个
    字段更糟:它不是"不知道",是一句错话,而且看起来完全正常。
    """
    expected = {
        "app/main.py": "api",
        "app/tasks/celery_app.py": "worker",
        "app/scripts/requeue_stranded.py": "script",
    }
    for path, service in expected.items():
        source = (BACKEND / path).read_text(encoding="utf-8")
        assert f'service="{service}"' in source, f"{path} 没有把自己报成 {service}"


# 下面四件事的守卫住在 `tests/test_a55_log_console.py`,不在这里:
#
#     跟随模式不再全窗序列化   数 `json.dumps` 的调用次数
#     按时间排序 / ts 解析失败  真的调一次 `list_logs`
#     截断显形                  同上
#     时间窗坏值 400            同上
#
# 理由是这个目录的硬约束:纯运行器只有标准库,而 `app/api/ops_logs.py` import
# fastapi。顶层 import 会让整个文件在导入期炸掉,运行器只记一条失败 ——
# 那正是 `run_pure_tests.py` 顶部写着的那个坑(9 个用例一条都没跑过,
# 而套件显示"只差一条")。

def test_the_viewer_reads_the_encoding_the_writer_wrote():
    """CLI 读日志文件的编码必须和写入端一致。(a55)

    `core/logging.py` 有一整段注释在讲为什么必须强制 UTF-8(中文 Windows 上
    GBK 会让整条记录消失)。而 `watch_logs.py` 上一版用的是
    `locale.getpreferredencoding(False)` —— **写的时候强制 UTF-8,读的时候
    用系统代码页**。中文 Windows 下所有中文字段变成乱码,而 JSON 结构是纯
    ASCII,`json.loads` 照样成功,**不报任何错**。

    反向断言(吃去注释再查),理由同第三条守卫:正向断言会命中本文件与被测
    文件注释里的同一串字。
    """
    source = (BACKEND / "tools/watch_logs.py").read_text(encoding="utf-8")
    code = ast.unparse(ast.parse(source))
    assert "getpreferredencoding" not in code, (
        "查看器又按本机代码页读日志了 —— 写入端强制 UTF-8,两边对不上时"
        "表现是安静的乱码,不是报错"
    )
    assert "utf-8" in code.lower(), "读取编码没有钉死"


def test_the_level_filter_means_at_least_not_exactly():
    """选 WARNING 的人要找的是问题,而 ERROR 是更严重的问题。

    a53 的 API 是精确匹配,于是筛 WARNING 会把 ERROR 挡掉 —— §1.4 那个病
    换了个地方复发。CLI 那边一直是"及以上",两个入口两种意思。
    """
    assert level_at_least("ERROR", "WARNING"), "选 WARNING 必须看得见 ERROR"
    assert level_at_least("CRITICAL", "WARNING")
    assert not level_at_least("INFO", "WARNING")
    assert level_at_least("INFO", None), "不筛就是全都要"

    api = (BACKEND / "app/api/ops_logs.py").read_text(encoding="utf-8")
    cli = (BACKEND / "tools/watch_logs.py").read_text(encoding="utf-8")
    for name, body in (("API", api), ("CLI", cli)):
        assert "level_at_least" in body, f"{name} 没有走共用的级别判定,两边会再次分叉"
        assert "_LEVEL_ORDER" not in body, f"{name} 自己存了一张级别表 —— 那就是第二份真相"


def test_critical_is_never_folded_either():
    """`routine` 对 ERROR **和 CRITICAL** 都不生效。

    a53 的 API 写的是 `level != "ERROR"`,CLI 写的是 `< 40` —— 同一条
    CRITICAL 的例行事件,页面折起来、终端展开着。而"什么时候可以藏一条日志"
    是业务规则,规则有两份等于没有。
    """
    routine = next(key for key, one in EVENTS.items() if one.routine)
    assert folds_away(routine, "INFO")
    assert not folds_away(routine, "ERROR")
    assert not folds_away(routine, "CRITICAL"), "CRITICAL 被折进计数条了"
    assert not folds_away("gen.round_evaluated", "INFO"), "非例行事件永远不折"


def test_the_byte_budget_truncation_is_visible_too():
    """`_fit` 砍掉的字段也要进 `truncated`。

    a53 的顺序是 `truncated_paths()` 在前、`_fit()` 在后,于是 256KB 那一档
    砍掉的东西一条都没进那张表 —— 而它恰恰砍得最狠。设计 §6.2 第三条边界
    写的是**截断必须显形**,界面照着 `truncated` 画,表是空的就等于没截。
    """
    from app.llm import payload_store

    payload = {"body": {"system": "长" * 400_000}, "truncated": []}
    fitted = payload_store._fit(payload)  # noqa: SLF001 - 守卫要打到这一层

    assert "body.system" in fitted["truncated"], (
        f"按字节预算砍过的字段没有列进 truncated:{fitted['truncated']}"
    )
    assert len(json.dumps(fitted, ensure_ascii=False).encode("utf-8")) <= 262_144


class _FakeSettings:
    """纯测试环境里没有 pydantic,`payload_store._settings()` 会返回 None,

    于是 `capture_enabled()` 恒假、写入路径**一步都走不到**。用它顶上去,
    守卫才真的打在被守的那段代码上而不是在早退分支上空转。
    """

    REDIS_URL = "redis://localhost:6379/0"
    OPS_LLM_PAYLOAD_CAPTURE = True
    OPS_LLM_PAYLOAD_TTL_SECONDS = 86_400
    OPS_LLM_PAYLOAD_MAX_BYTES = 262_144
    OPS_PAYLOAD_STRING_CHARS = 40_000


def _with_fake_settings(store):
    original = store._settings  # noqa: SLF001
    store._settings = lambda: _FakeSettings()  # noqa: SLF001
    return original


def test_a_path_without_a_call_id_writes_nothing():
    """`"-"` 不是 id,是「这条路径没有 id」。

    `evaluators/vision.py` 的连接自检直接调 `_send_once`,绕过
    `_send_with_retries`,于是 contextvar 还是默认值。a53 只挡空串,
    结果每点一次「测试连接」就往 `ops:llm:-` 覆盖写一次,还顺手续 TTL。
    """
    from app.llm import payload_store

    assert not payload_store.has_call_id("-")
    assert not payload_store.has_call_id("")
    assert not payload_store.has_call_id(None)
    assert payload_store.has_call_id("c41f8a09d2e3")

    written: list[str] = []

    class Recording:
        def pipeline(self, *_args, **_kwargs):
            return self

        def hset(self, key, *_args, **_kwargs):
            written.append(key)
            return self

        def expire(self, *_args):
            return self

        def execute(self):
            return None

    from app.core import log_ring

    original = _with_fake_settings(payload_store)
    log_ring._HOLDER._client = Recording()  # noqa: SLF001
    log_ring._HOLDER._blocked_until = 0.0  # noqa: SLF001
    payload_store.capture_attempt(
        "-",
        attempt=0,
        http_status=200,
        duration_ms=1,
        content_type="application/json",
        upstream_request_id=None,
        body={"ok": True},
    )
    # 同一个假客户端下,**真的有 id** 时必须写得出来 —— 否则上面那条断言
    # 可能只是因为整条路径根本没通。
    payload_store.capture_attempt(
        "c41f8a09d2e3",
        attempt=1,
        http_status=200,
        duration_ms=1,
        content_type="application/json",
        upstream_request_id=None,
        body={"ok": True},
    )
    payload_store._settings = original  # noqa: SLF001
    log_ring.reset_client_for_tests()
    assert written == ["ops:llm:c41f8a09d2e3"], f"写出的键不对:{written}"


def test_the_sidecar_keeps_its_own_books():
    """旁挂库写失败也要记账。

    a53 的 `except` 直接 `return`:不记数、不进冷却期。于是
    `/api/ops/llm/{id}` 报 404 的时候,分不清"过期了"还是"从来没写进去过",
    而这两者的下一步完全相反。环形那边的口径是「不赌,但也不瞎」。
    """
    from app.core import log_ring
    from app.llm import payload_store

    class Exploding:
        def pipeline(self, *_args, **_kwargs):
            raise RuntimeError("redis is down")

    before = payload_store.STATS.snapshot()["dropped_since_boot"]
    original = _with_fake_settings(payload_store)
    log_ring._HOLDER._client = Exploding()  # noqa: SLF001
    log_ring._HOLDER._blocked_until = 0.0  # noqa: SLF001
    payload_store.capture_attempt(
        "c41f8a09d2e3",
        attempt=1,
        http_status=200,
        duration_ms=7,
        content_type="application/json",
        upstream_request_id=None,
        body={"ok": True},
    )
    payload_store._settings = original  # noqa: SLF001
    log_ring.reset_client_for_tests()

    after = payload_store.STATS.snapshot()["dropped_since_boot"]
    assert after == before + 1, "旁挂库掉了一条却没有记账"

    meta = (BACKEND / "app/api/ops_logs.py").read_text(encoding="utf-8")
    assert "payload_store.STATS" in meta, "这个数没有出口就等于没有"


def test_a_disabled_ring_never_looks_like_an_empty_period():
    """`OPS_LOG_RING_ENABLED=false` 时必须说出来。

    a53 没有这条分支:handler 没挂,但 `/logs` 照样去读 Redis,读到空,
    界面画出"这个筛选组合下没有日志" —— 而那句话的意思是"这段时间没发生",
    正是这一页反复申明不许说的那一句。
    """
    source = (BACKEND / "app/api/ops_logs.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    body = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "list_logs"
    )
    guard = ast.unparse(body)
    assert "OPS_LOG_RING_ENABLED" in guard, "关掉环形之后这个端点没有任何分支"
    assert "ring_disabled" in guard, "关掉之后要给界面一个能说出口的理由"


# `test_the_stream_is_filtered_before_it_is_shaped` 删除于 a55。
#
# 它断言的是 `list_logs` 里 `_matches` 的调用**行号**小于 `_shape` 的,而
# 那个形状从 a54 起就一直成立 —— 与此同时,调用方的循环里还留着一行无条件
# 的 `json.dumps`,对全窗 5000 条各做一遍。**守卫一直是绿的,开销一次没降。**
#
# 换掉它的是 `test_the_stream_really_stops_serialising_the_whole_window`:
# 数 dumps 的调用次数,而不是数行号。这段注释留着,是因为把它改回 AST 断言
# 看起来会更"快、更纯"—— 而那正是它上一次失效的样子。


def test_the_domain_counts_survive_picking_a_domain():
    """点进一个域之后,其余域的计数不许全变成 0。

    a53 是前端按已经筛过的那一屏算的,于是点进 `gen` 之后其余十四格全是 0 ——
    恰好在最需要"别处还有没有事"的时候把这个信息拿掉。
    """
    source = (BACKEND / "app/api/ops_logs.py").read_text(encoding="utf-8")
    assert "domain_counts" in source, "服务端没有给出域计数,前端只能按当前这一屏猜"
    tree = ast.parse(source)
    body = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "list_logs"
    )
    text = ast.unparse(body)
    assert "domain=None" in text, "域计数吃了 domain 筛选 —— 那它只会数出选中的那一个"


# ============================================================ a56:五处复盘守卫


def test_parse_ts_always_returns_aware_or_none():
    """**解析成功的时间恒带时区。** naive 混进 aware 的比较是 TypeError。

    a55 的兜底路径(`fromisoformat`)对无时区输入原样返回 naive,而
    `list_logs` 的排序与 `_matches` 的时间窗拿它和主格式的 aware 比 ——
    `?since=2026-08-18T09:30:00` 让 `/ops/logs` 整个 500;第三方 handler
    往环形写一条无时区 ts,整页挂掉。**恰恰是"容一手第三方写法"的那条
    路径在让调用方崩溃。** 无时区按本机时区解释:写入端 `formatTime`
    用的就是本机墙上钟。
    """
    for value in (
        "2026-08-18T17:30:00+0800",
        "2026-08-18T09:30:00Z",
        "2026-08-18T09:30:00",          # 手打 since 最常见的形状
        "2026-08-18 09:30:00",
        "2026-08-18T09:30:00.123456",   # 第三方 handler 的毫秒写法
    ):
        parsed = parse_ts(value)
        assert parsed is not None, f"{value!r} 应当可解析"
        assert parsed.tzinfo is not None, (
            f"{value!r} 解析出了 naive datetime —— 它和 aware 一比就是 TypeError,"
            "而比较发生在 list_logs 的排序与时间窗里"
        )
    assert parse_ts("not a time") is None
    assert parse_ts(None) is None
    # 有它才有资格说"混排不炸":aware 与(本机时区解释后的)aware 可比。
    early = parse_ts("2026-08-18T00:00:00+0800")
    late = parse_ts("2026-08-18T23:59:59")
    assert early is not None and late is not None
    assert (early < late) or (early > late) or (early == late)


def test_a_mistyped_level_filter_is_rejected_not_silently_widened():
    """筛选级别打错要**当场说出来**,不是静默变成"不筛"。

    `level_rank` 对未知名字按 INFO 兜底 —— 那是给**日志行自己的级别**的
    容错(第三方 logger 会写自定义级别名)。筛选条件走同一条兜底,
    `level=WARN` 会静默拉回整窗 5000 条,而打错的人以为自己看的是
    WARNING 以上。CLI 对 `--domain`/`--event` 早就写明这个口径,
    级别是唯一漏掉的那个。
    """
    assert normalize_level(None) is None
    assert normalize_level("   ") is None
    assert normalize_level("warning") == "WARNING"
    assert normalize_level("Error") == "ERROR"
    for bad in ("WARN", "ERR", "FATAL", "VERBOSE"):
        try:
            normalize_level(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} 被静默接受了 —— 它会变成 rank 20,等于不筛")

    # 两个入口都必须接这一份判定,不许各自 `.upper()` 完就交给 level_rank。
    api = (BACKEND / "app/api/ops_logs.py").read_text(encoding="utf-8")
    assert "normalize_level(level)" in api, "API 的 level 参数没走 normalize_level"
    assert 'wanted_level = (level or "").upper()' not in api, (
        "API 又在自己 .upper() 了 —— 打错的级别会被 level_rank 按 INFO 兜底"
    )
    cli = (BACKEND / "tools/watch_logs.py").read_text(encoding="utf-8")
    assert "normalize_level(args.level)" in cli, "CLI 的 --level 没走 normalize_level"


def test_seq_ties_break_numerically_not_lexically():
    """同一秒内的先后按**计数的数值**,不按字符串。

    ts 只有秒级精度(`_TS_FORMAT` 无毫秒),同秒顺序全靠 seq;而字符串序里
    `"1a2b-10" < "1a2b-7"`,同一进程一秒内发第 7~10 条时,链路视角的展示
    顺序会倒置 —— 横幅上正写着「按时间顺读」。
    """
    assert seq_sort_key("1a2b-7") < seq_sort_key("1a2b-10"), "还是字符串序"
    assert sorted(["1a2b-10", "1a2b-2", "1a2b-7"], key=seq_sort_key) == [
        "1a2b-2", "1a2b-7", "1a2b-10",
    ]
    # 坏值不炸,排同秒最前 —— 与 parse_ts 的"不猜、不炸"同一个姿势。
    assert seq_sort_key(None) == ("", -1)
    assert seq_sort_key("no-dash-here")[1] == -1 or seq_sort_key("no-dash-here")[0]
    api = (BACKEND / "app/api/ops_logs.py").read_text(encoding="utf-8")
    assert "seq_sort_key(row.get" in api, "list_logs 的 tie-break 没接 seq_sort_key"
    assert 'str(row.get("seq") or "")' not in api, (
        "tie-break 又回到字符串序了 —— 同一进程同一秒内 10 会排在 7 前面"
    )


def test_held_is_the_redis_length_not_the_parsed_count():
    """`ring.held` 是 Redis 里真实的 LLEN,不是解析成功的条数。

    `read_ring` 的文档承诺坏行"会体现在 held 与实际条数的差里" —— a55 把
    `/logs` 的 held 填成 `len(rows)`,那个差恒为 0,承诺成了空话;而
    `/logs/meta` 给的又是真实 LLEN:同名字段,两个端点,两种含义。
    行为断言在 `tests/test_a55_log_console.py`(要 fastapi);这里钉住源码
    形状,防止那行 hint 被写回来。
    """
    api = (BACKEND / "app/api/ops_logs.py").read_text(encoding="utf-8")
    assert "held_hint=len(rows)" not in api, (
        "/logs 又在拿解析条数冒充 held —— 坏行的差从此恒为 0,"
        "而 /logs/meta 给的是真实 LLEN,同名字段两种含义"
    )
    # 关掉环形那个分支的 held_hint=0 是对的:不碰 Redis,0 附带 ring_disabled。
    assert "held_hint=0" in api, "ring_disabled 分支的 held=0 被顺手删了"


def test_the_cli_never_truncates_a_traceback():
    """CLI 的 `exc` 全文打印,不进 160 字符的 compact 预算。

    一条 Traceback 被 `_compact` 截成
    `"Traceback (most recent call last):\n  File …(2489 chars)"` 之后,
    真正报错的最后一行恰恰在被截掉的那一截里 —— 而 exc 是排障最要紧的字段。
    顺带:`llm_call_id` 表头已经打过,字段区不许再打第二遍。
    """
    cli = (BACKEND / "tools/watch_logs.py").read_text(encoding="utf-8")
    assert 'fields.pop("exc"' in cli, "exc 还混在 compact 循环里 —— 堆栈会被截断"
    assert 'fields.pop("llm_call_id"' in cli, "llm_call_id 会在表头与字段区打两遍"
