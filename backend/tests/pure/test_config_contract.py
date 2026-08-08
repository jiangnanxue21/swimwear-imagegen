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
                        pass
    for key in ("FASHN_API_KEY", "FAL_API_KEY", "COMFYUI_BASE_URL", "VISION_MODEL_API_KEY"):
        assert defaults.get(key) == "", f"{key} 默认值必须为空字符串"
