"""配置契约:.env.example 必须覆盖 Settings 的全部字段,且不得内联真实密钥。

用 ast 静态解析 config.py,避免依赖 pydantic-settings。
"""
from __future__ import annotations

import ast

from app.core.dotenv import parse_dotenv
from app.core.settings_schema import SETTING_GROUPS
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


def test_secret_fields_are_empty_in_example():
    """按下划线分段匹配,不是子串匹配。

    子串匹配会把 ``VISION_MODEL_MAX_OUTPUT_TOKENS=1800`` 判成泄露的密钥 ——
    那是一个 token **数量**。一条会规律性误报的安全断言,最后一定会被人加豁免、
    再被人整个注释掉,所以宁可现在把判据写准。
    """
    env = parse_dotenv(ENV_EXAMPLE.read_text(encoding="utf-8"))
    # 与原判据保持同样的覆盖面,只把匹配方式从子串换成分段 ——
    # 这次改动的目的是消除误报,不是顺手扩大扫描范围
    indicators = {"KEY", "SECRET", "TOKEN"}
    leaked = {
        k: v
        for k, v in env.items()
        if indicators & set(k.upper().split("_")) and v
    }
    assert not leaked, f".env.example 不得含真实密钥: {sorted(leaked)}"


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
