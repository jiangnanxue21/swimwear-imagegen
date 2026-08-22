"""配置契约:.env.example 必须覆盖 Settings 的全部字段,且不得内联真实密钥。

用 ast 静态解析 config.py,避免依赖 pydantic-settings。
"""
from __future__ import annotations

import ast

from app.core.dotenv import parse_dotenv
from app.core.settings_schema import FIELDS, SETTING_GROUPS
from tests.pure._helpers import BACKEND_ROOT, PROJECT_ROOT

CONFIG_PY = BACKEND_ROOT / "app" / "core" / "config.py"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
PYPROJECT = BACKEND_ROOT / "pyproject.toml"

#: 这些字段有安全默认值,允许不出现在 .env.example
OPTIONAL_IN_EXAMPLE = {"APP_NAME"}


def _settings_fields() -> dict[str, str]:
    tree = ast.parse(CONFIG_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            return {
                stmt.target.id: ast.unparse(stmt.annotation)
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
                and stmt.target.id.isupper()
            }
    raise AssertionError("未找到 Settings 类")


def test_every_setting_is_documented_in_env_example():
    """.env.example 必须同时覆盖两类配置,缺哪一类要分开报。

    (原先设置页那一类在 test_settings_schema.py 里单独测一遍。两处比对的是
    同一个文件、只是字段集合略有差异,失败信息互相重复 —— 合并到这里,
    改成分别报告,一次就能看清缺的是纯后端配置还是设置页可改项。)

    文件本身不存在时,下面的读取会直接失败,不需要单独的存在性用例。
    """
    documented = set(parse_dotenv(ENV_EXAMPLE.read_text(encoding="utf-8")))

    missing_backend = sorted(set(_settings_fields()) - documented - OPTIONAL_IN_EXAMPLE)
    editable = {f.key for g in SETTING_GROUPS for f in g.fields}
    missing_editable = sorted(editable - documented)

    problems = []
    if missing_backend:
        problems.append(f"后端配置缺失: {missing_backend}")
    if missing_editable:
        problems.append(f"设置页可改项缺失: {missing_editable}")
    assert not problems, ".env.example 覆盖不全 -> " + "; ".join(problems)


def test_env_example_has_no_unknown_keys():
    fields = set(_settings_fields())
    documented = set(parse_dotenv(ENV_EXAMPLE.read_text(encoding="utf-8")))
    unknown = sorted(documented - fields)
    assert not unknown, f".env.example 含未知字段(拼写错误?): {unknown}"


#: 名字里带这几段、而 `settings_schema` 又没声明过的字段,按密钥对待。
#:
#: **分段匹配,不是子串匹配。** 子串匹配会把 ``VISION_MODEL_MAX_OUTPUT_TOKENS=1800``
#: 判成泄露的密钥 —— 那是一个 token **数量**。
SECRET_NAME_SEGMENTS = {"KEY", "SECRET", "TOKEN"}


def _is_secret(key: str) -> bool:
    """这一项算不算密钥。**声明优先于猜名字。**

    `settings_schema` 已经为每个设置页字段答过这个问题(`Field.secret`),
    而那份声明是加密落库、回传打码、日志脱敏三处共用的同一个判据。
    这里复用它,不另起一套 —— 两套判据漂移之后,"哪些值算密钥"在
    安全断言和真实脱敏之间会有两个答案。

    名字启发式退化成**兜底**:只在字段没被声明过时才用(纯后端配置
    不进设置页,`FIELDS` 里没有它们,例如 `SETTINGS_SECRET_KEY`、
    `ADMIN_TOKEN`、`S3_SECRET_ACCESS_KEY`)。

    修的是这条:``EXTRACTOR_MODEL_SEND_IDEMPOTENCY_KEY=false`` 是一个
    **布尔开关**,只因为字段名末段是 `KEY` 就被判成"内联了真实密钥"。
    一条会规律性误报的安全断言,最后一定会被人加豁免、再被人整个注释掉 ——
    所以这里把判据修准,而不是给它开一个白名单。
    """
    spec = FIELDS.get(key)
    if spec is not None:
        return spec.secret
    return bool(SECRET_NAME_SEGMENTS & set(key.upper().split("_")))


def test_secret_fields_are_empty_in_example():
    """密钥项在 .env.example 里必须留空。"""
    env = parse_dotenv(ENV_EXAMPLE.read_text(encoding="utf-8"))
    leaked = {k: v for k, v in env.items() if _is_secret(k) and v}
    assert not leaked, f".env.example 不得含真实密钥: {sorted(leaked)}"


def test_the_secret_verdict_is_not_just_a_name_guess():
    """反向断言:声明为非密钥的项,不许因为名字被判成密钥。

    没有这一条的话,把 `_is_secret` 退回纯名字匹配不会有任何地方变红 ——
    而那正是刚修掉的那个假红。同时钉住正方向:声明为密钥的那些仍然算。
    """
    assert _is_secret("EXTRACTOR_MODEL_SEND_IDEMPOTENCY_KEY") is False, (
        "布尔开关又被名字判成密钥了"
    )
    assert _is_secret("EXTRACTOR_MODEL_API_KEY") is True
    # 没进设置页的纯后端密钥仍然靠名字兜底
    assert _is_secret("ADMIN_TOKEN") is True


def test_all_provider_keys_default_to_empty_so_app_starts_unconfigured():
    """需求第十七章:没有 Key 时系统必须能启动 —— 因此默认值必须是空串。"""
    tree = ast.parse(CONFIG_PY.read_text(encoding="utf-8"))
    defaults: dict[str, object] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                    and stmt.value
                ):
                    try:
                        defaults[stmt.target.id] = ast.literal_eval(stmt.value)
                    except ValueError:
                        # 同 `test_a45_batch18_lease_budget.py`:读不出字面量的字段跳过,
                        # 而下面点名的那几个都是字面量 —— 真读不出来会在取用时 KeyError,
                        # 不会静默用一个错的默认值。
                        pass
    for key in ("FASHN_API_KEY", "FAL_API_KEY", "COMFYUI_BASE_URL", "VISION_MODEL_API_KEY"):
        assert defaults.get(key) == "", f"{key} 默认值必须为空字符串"


# ---------------------------------------------------------------- Celery result backend 钉 RESP2
#
# ## 这几条守卫为什么存在
#
# redis-py 8.x 把默认 RESP 协议从 2 改成 3。Celery 的 result
# backend 由 redis-py 建连,会走 RESP3 协商,而项目连的远端 Redis(或前置代理)在
# `HELLO 3 AUTH` 阶段直接断连 —— 任务做完后结果写不回去。`config.py` 的
# `result_backend` property 用 `_pin_resp2()` 把协议钉回 RESP2 来绕开那段交互。
#
# ## 为什么只钉 result_backend,不钉 broker
#
# broker 走 Kombu 自己的 Redis transport。真实 worker 已证明 redis-py 8.1 的默认
# RESP3 可以完整执行任务；把 result backend 的 RESP2 修补扩散过去，会让当前服务端
# 在第一次握手就返回 `server:RedisError`。所以 `broker_url` 必须原样返回。
#
# ## 为什么用 AST/exec,而不是 `Settings(...)`
#
# 这个文件跑在两条路上:`make test-pure`(只有标准库,`app.core.config` 因为
# 依赖 pydantic-settings 而 import 不进来)和真实 pytest(CI、容器内)。在顶层
# `from app.core.config import Settings` 会让整个文件在纯运行器导入期炸掉,
# 运行器只把它记成一条失败 —— 那是本仓库踩过、并在 `run_pure_tests.py` 顶部写
# 明的那类坑(`import pytest` 的同构)。
#
# `_pin_resp2` 本身只用 `urllib.parse`(标准库),所以能从源码里抽出来直接跑,
# 不经过 Settings。逻辑钉住了,property 调它由下面一条 AST 断言钉着。


def _load_pin_resp2():
    """从 config.py 源码里抽出 `_pin_resp2`,返回可直接调用的函数对象。

    `_pin_resp2` 只依赖标准库 urllib.parse,exec 出来即等价于运行时的那份。
    那四个名字(urlsplit / parse_qsl / urlencode / urlunsplit)在 config.py 里是
    **模块级** import,不在函数体内,所以这里要喂进 exec 的命名空间。
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    source = CONFIG_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    func_node = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_pin_resp2"
    )
    func_src = ast.get_source_segment(source, func_node)
    assert func_src is not None, "抽不出 _pin_resp2 的源码"
    namespace: dict = {
        "urlsplit": urlsplit,
        "parse_qsl": parse_qsl,
        "urlencode": urlencode,
        "urlunsplit": urlunsplit,
    }
    exec(func_src, namespace)  # noqa: S102 - 测试用,源码来自本仓库
    return namespace["_pin_resp2"]


def test_pin_resp2_adds_protocol_to_plain_url():
    pin = _load_pin_resp2()
    assert pin("redis://localhost:6379/0") == "redis://localhost:6379/0?protocol=2"


def test_pin_resp2_preserves_password_in_url():
    """`.env` 里最常见的带密码形状。纯字符串拼接会拼坏 host 段,解析式注入不会。"""
    pin = _load_pin_resp2()
    assert pin("redis://:secret@redis:6379/1") == "redis://:secret@redis:6379/1?protocol=2"


def test_pin_resp2_does_not_overwrite_explicit_protocol():
    """用户显式写了 `?protocol=3` 时不覆盖 —— 那是有人刻意选了另一档。

    没有这条,有人想切回 RESP3 做对照实验会被悄悄改回去,而表现是「改了没反应」。
    """
    pin = _load_pin_resp2()
    out = pin("redis://localhost:6379/0?protocol=3")
    assert "protocol=3" in out
    assert "protocol=2" not in out


def test_pin_resp2_appends_to_existing_query_params():
    """URL 已有别的 query 参数时,protocol 要追加而不是覆盖整段 query。"""
    pin = _load_pin_resp2()
    out = pin("redis://localhost:6379/0?ssl=true")
    assert "ssl=true" in out
    assert "protocol=2" in out


def test_pin_resp2_leaves_non_redis_backends_unchanged():
    """显式配置数据库等其他 result backend 时不能塞进 Redis 专属参数。"""
    pin = _load_pin_resp2()
    url = "db+postgresql://db.example/imagegen?sslmode=require"
    assert pin(url) == url


def test_redis_dependency_matches_the_runtime_major_version():
    """项目明确支持运行环境的 redis-py 8.1，不靠开发机偶然解析版本。"""
    source = PYPROJECT.read_text(encoding="utf-8")
    assert '"redis>=8.1,<9"' in source


def test_only_result_backend_pins_resp2_not_broker():
    """result_backend 必须调 `_pin_resp2`,broker_url 必须不调。

    两条断言合在一起才封闭:

    - **result_backend 调它**:有人重构时把 property 改回裸
      `return self.REDIS_URL`,行为测试一条都不会红 —— `_pin_resp2` 还在、还能跑、
      只是没人调它了。这条挡那个形状。少了它,result backend 在 redis-py 8.x
      (RESP3 默认)下写不回任务结果 —— broker 能连、任务能跑、但结果落不回去,
      排查方向永远不会落到这里。

    - **broker_url 不调它**:真实 worker 已证明 redis-py 8.1 默认 RESP3 可以跑完
      任务；强制 RESP2 会在当前服务端首次握手直接失败。两边不是对称问题。
    """
    source = CONFIG_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    settings_cls = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "Settings"
    )
    pins: dict[str, bool] = {}
    for stmt in settings_cls.body:
        is_target = (
            isinstance(stmt, ast.FunctionDef)
            and stmt.name in {"broker_url", "result_backend"}
        )
        if not is_target:
            continue
        body_src = ast.get_source_segment(source, stmt) or ""
        pins[stmt.name] = "_pin_resp2(" in body_src
    assert pins.get("result_backend") is True, (
        "result_backend property 没有调用 _pin_resp2 —— "
        "redis-py 8.x 默认 RESP3,远端 Redis 在 HELLO 3 阶段断连,结果写不回去。"
    )
    assert pins.get("broker_url") is False, (
        "broker_url property 调了 _pin_resp2 —— 当前 broker 使用 redis-py 8.1 的 "
        "RESP3 可以跑完任务，强制 RESP2 会在首次握手返回 server:RedisError。"
    )


def test_worker_cancels_late_ack_tasks_when_broker_connection_is_lost():
    """断线后 broker 会重投未 ack 消息，旧任务必须停下以免并行重复付费。"""
    celery_source = (
        BACKEND_ROOT / "app" / "tasks" / "celery_app.py"
    ).read_text(encoding="utf-8")
    assert "worker_cancel_long_running_tasks_on_connection_loss=True" in celery_source


def test_celery_keeps_native_broker_protocol_and_enables_recovery_options():
    celery_source = (
        BACKEND_ROOT / "app" / "tasks" / "celery_app.py"
    ).read_text(encoding="utf-8")
    assert "broker_transport=" not in celery_source
    for expected in (
        '"health_check_interval": 15',
        '"socket_keepalive": True',
        '"retry_on_timeout": True',
        "broker_connection_retry_on_startup=True",
        "broker_connection_retry=True",
    ):
        assert expected in celery_source
