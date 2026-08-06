"""设置页的契约与纯逻辑(阶段 8)。

设置页是唯一一个\"网页能改后端行为\"的地方,所以它的失败方式最阴:
配置项名字拼错一个字母,界面照常显示、保存照常成功,只是永远不生效。
下面这批测试就是冲着这类**静默失效**去的:

    表里的键        必须是 Settings 的真实字段
    下拉的选项      必须是 Provider / 评分器真的认识的取值
    密钥字段        打码后不得残留可用片段,且不许把打码串存回去
    环境变量锁      打开后必须真的挡住写入

再加上校验函数本身的纯逻辑用例 —— 它们不碰数据库,任何机器上都能跑。
"""
from __future__ import annotations

import ast

from app.core.settings_schema import (
    FIELDS,
    MASK_TAIL,
    SECRET_KEYS,
    SETTING_GROUPS,
    TYPE_BOOL,
    TYPE_INTEGER,
    TYPE_NUMBER,
    TYPE_PASSWORD,
    TYPE_SELECT,
    looks_like_mask,
    mask,
    normalize,
)
from tests.pure._helpers import BACKEND_ROOT, expect_raises

CONFIG_PY = BACKEND_ROOT / "app" / "core" / "config.py"


def _settings_fields() -> set[str]:
    tree = ast.parse(CONFIG_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            return {
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.target.id.isupper()
            }
    raise AssertionError("未找到 Settings 类")


# ---------------------------------------------------------------- 表结构


def test_every_configurable_key_is_a_real_settings_field():
    """拼错的键会表现为\"改了没反应\",而不是报错 —— 必须在这里拦住。"""
    unknown = sorted(set(FIELDS) - _settings_fields())
    assert not unknown, f"设置页声明了 Settings 里没有的配置项: {unknown}"


# .env.example 的覆盖度检查已合并进 tests/pure/test_config_contract.py,
# 那里一次性报告「后端配置缺失」和「设置页可改项缺失」两类。


def test_group_and_field_keys_are_unique():
    group_keys = [g.key for g in SETTING_GROUPS]
    assert len(group_keys) == len(set(group_keys)), "分组 key 有重复"
    field_keys = [f.key for g in SETTING_GROUPS for f in g.fields]
    assert len(field_keys) == len(set(field_keys)), "同一个配置项出现在多个分组里"


def test_infrastructure_settings_are_not_editable_from_the_web():
    """数据库地址、存储后端这类东西改错就是整站不可用,只能走部署,不进设置页。"""
    forbidden = {
        "DATABASE_URL", "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER",
        "POSTGRES_PASSWORD", "POSTGRES_DB", "REDIS_URL", "CELERY_BROKER_URL",
        "CELERY_RESULT_BACKEND", "STORAGE_BACKEND", "STORAGE_LOCAL_DIR",
        "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "SETTINGS_SECRET_KEY",
        "ADMIN_TOKEN", "SETTINGS_ENV_LOCK", "CORS_ORIGINS", "API_PREFIX",
    }
    leaked = sorted(forbidden & set(FIELDS))
    assert not leaked, f"这些配置项不该能在网页上改: {leaked}"


def test_every_secret_field_is_a_password_input():
    for key in SECRET_KEYS:
        assert FIELDS[key].type == TYPE_PASSWORD, f"{key} 是密钥,类型必须是 password"


def test_key_like_fields_are_all_marked_secret():
    """新加一个 *_API_KEY 却忘了标 secret,就会明文落库并明文回传。"""
    should_be = {k for k in FIELDS if k.endswith("_API_KEY")}
    assert should_be <= SECRET_KEYS, f"未标记为密钥: {sorted(should_be - SECRET_KEYS)}"


def test_select_fields_all_offer_options():
    for key, spec in FIELDS.items():
        if spec.type in (TYPE_SELECT, TYPE_BOOL):
            assert spec.options, f"{key} 是下拉/开关,必须给选项"


def test_number_fields_declare_a_range():
    """没有上下界的数字框迟早会被填进一个把额度烧光的值。"""
    for key, spec in FIELDS.items():
        if spec.type in (TYPE_NUMBER, TYPE_INTEGER):
            assert spec.minimum is not None and spec.maximum is not None, f"{key} 缺少取值范围"
            assert spec.minimum < spec.maximum, f"{key} 的上下界写反了"


# ---------------------------------------------------------------- 选项与后端对齐


def test_provider_options_match_the_registry():
    from app.providers.registry import PROVIDER_FACTORIES

    offered = {o.value for o in FIELDS["DEFAULT_PROVIDER"].options}
    assert offered == set(PROVIDER_FACTORIES), f"可选 Provider 与注册表不一致: {offered}"


def test_routing_options_are_real_and_exclude_the_unsupported_one():
    from app.core.enums import RoutingMode

    offered = {o.value for o in FIELDS["PROVIDER_ROUTING_MODE"].options}
    assert offered <= {m.value for m in RoutingMode}
    assert RoutingMode.FALLBACK.value not in offered, "FALLBACK 尚未实现,不该出现在界面上"


def test_tryon_model_options_match_the_fashn_model_table():
    from app.providers.fashn import TRYON_MODELS

    offered = {o.value for o in FIELDS["FASHN_TRYON_MODEL"].options}
    assert offered == set(TRYON_MODELS), f"试穿模型选项与 FASHN 模型表不一致: {offered}"


def test_fashn_quality_options_are_accepted_by_at_least_one_model():
    from app.providers.fashn import PRODUCT_TO_MODEL, TRYON_MAX, TRYON_V16

    known = set(TRYON_MAX.quality_values) | set(TRYON_V16.quality_values)
    known |= set(PRODUCT_TO_MODEL.quality_values)
    offered = {o.value for o in FIELDS["FASHN_GENERATION_MODE"].options if o.value}
    assert offered <= known, f"没有任何 FASHN 模型认识这些档位: {sorted(offered - known)}"


def test_evaluator_options_match_the_registry():
    from app.evaluators.registry import EVALUATOR_FACTORIES

    offered = {o.value for o in FIELDS["EVALUATOR_BACKEND"].options}
    assert offered == set(EVALUATOR_FACTORIES)


def test_output_format_options_are_the_ones_fashn_actually_sends():
    source = (BACKEND_ROOT / "app" / "providers" / "fashn.py").read_text(encoding="utf-8")
    for option in FIELDS["FASHN_OUTPUT_FORMAT"].options:
        assert f'"{option.value}"' in source, f"FASHN 请求里没有出现输出格式 {option.value}"


# ---------------------------------------------------------------- 打码


def test_mask_keeps_only_the_tail():
    masked = mask("fa-abcdefghijklmnop")
    assert masked.endswith("mnop")
    assert "abcdefghijkl" not in masked
    assert len(masked) - MASK_TAIL == 8


def test_short_secret_reveals_nothing_at_all():
    assert mask("abcd") == "•" * 8
    assert "abcd" not in mask("abcd")


def test_empty_secret_masks_to_empty_so_the_ui_can_say_not_configured():
    assert mask("") == ""


def test_masked_string_is_recognised_and_rejected():
    assert looks_like_mask(mask("fa-abcdefghijklmnop"))
    assert not looks_like_mask("fa-realkey")
    err = expect_raises(ValueError, normalize, "FASHN_API_KEY", mask("fa-abcdefghijklmnop"))
    assert "打码" in str(err)


# ---------------------------------------------------------------- 校验


def test_unknown_key_is_rejected():
    expect_raises(ValueError, normalize, "DATABASE_URL", "postgres://x")


def test_number_out_of_range_is_rejected_on_both_sides():
    expect_raises(ValueError, normalize, "MAX_TOTAL_ROUNDS", 0)
    expect_raises(ValueError, normalize, "MAX_TOTAL_ROUNDS", 999)
    assert normalize("MAX_TOTAL_ROUNDS", 10) == "10"


def test_number_keeps_decimals_where_they_are_meaningful():
    assert normalize("FASHN_POLL_INTERVAL_SECONDS", 2.5) == "2.5"
    assert normalize("FASHN_POLL_INTERVAL_SECONDS", 2.0) == "2"


def test_non_numeric_text_in_a_number_field_is_rejected():
    err = expect_raises(ValueError, normalize, "MAX_TOTAL_ROUNDS", "很多")
    assert "数字" in str(err)


def test_select_only_accepts_declared_values():
    assert normalize("FASHN_RESOLUTION", "2k") == "2k"
    assert normalize("FASHN_RESOLUTION", "") == ""
    expect_raises(ValueError, normalize, "FASHN_RESOLUTION", "8k")


def test_bool_accepts_the_usual_spellings():
    for truthy in (True, "true", "1", "on", "YES"):
        assert normalize("FASHN_SEND_PUBLIC_URLS", truthy) == "true"
    for falsy in (False, "false", "0", "off", ""):
        assert normalize("FASHN_SEND_PUBLIC_URLS", falsy) == "false"


def test_text_is_trimmed_because_a_pasted_key_usually_carries_a_newline():
    assert normalize("FASHN_BASE_URL", "  https://api.fashn.ai/v1\n ") == "https://api.fashn.ai/v1"


def test_overlong_value_is_rejected():
    expect_raises(ValueError, normalize, "FASHN_API_KEY", "x" * 5000)


def test_clearing_a_value_is_expressed_as_empty_string():
    """空串是\"删掉覆盖\",不是\"存一个空值\" —— 服务层据此删行。"""
    assert normalize("FASHN_API_KEY", "") == ""
    assert normalize("FASHN_API_KEY", None) == ""


# ---------------------------------------------------------------- 来源与锁


def test_env_source_reads_both_process_env_and_dotenv(tmp_path=None):
    import os
    import tempfile
    from pathlib import Path

    from app.core.env_source import env_provided_keys

    root = Path(tempfile.mkdtemp())
    (root / ".env").write_text("FASHN_API_KEY=abc\nFAL_API_KEY=\n", encoding="utf-8")
    keys = env_provided_keys(root)
    assert "FASHN_API_KEY" in keys, ".env 里给了值的键必须算作环境变量来源"
    assert "FAL_API_KEY" not in keys, "写了 KEY= 但没有值,不该让后台变只读"
    assert "PATH" in keys or not os.environ, "进程环境变量也要算进来"


def test_settings_service_refuses_to_write_locked_keys():
    """SETTINGS_ENV_LOCK 打开后必须真的挡住写入,而不只是把界面置灰。"""
    source = (BACKEND_ROOT / "app" / "services" / "settings_service.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    guard = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_assert_writable"
    )
    body = ast.unparse(guard)
    assert "is_env_provided" in body and "_env_lock" in body

    apply_fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "apply"
    )
    assert "_assert_writable" in ast.unparse(apply_fn), "写入路径没有过锁检查"


def test_secrets_are_encrypted_before_they_reach_the_database():
    """服务层必须调 encrypt;直接把明文塞给 AppSetting 是最容易犯的错。"""
    source = (BACKEND_ROOT / "app" / "services" / "settings_service.py").read_text(
        encoding="utf-8"
    )
    assert "secrets.encrypt" in source, "密钥没有加密就落库"
    assert "secrets.available()" in source, "加密不可用时必须拒绝保存,而不是退化成明文"


def test_settings_api_never_returns_a_plaintext_secret():
    """出参模型里不该有任何\"明文\"字段;value 在密钥字段上永远是打码串。"""
    source = (BACKEND_ROOT / "app" / "schemas" / "setting.py").read_text(encoding="utf-8")
    for forbidden in ("plaintext", "raw_value", "secret_value"):
        assert forbidden not in source, f"出参里出现了疑似明文字段: {forbidden}"


def test_write_endpoints_are_guarded():
    source = (BACKEND_ROOT / "app" / "api" / "settings.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for name in ("update_settings", "reset_settings"):
        fn = next(
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name
        )
        assert "require_admin" in ast.unparse(fn), f"{name} 没有守卫"


def test_admin_guard_refuses_unconfigured_non_local_environments():
    source = (BACKEND_ROOT / "app" / "api" / "deps.py").read_text(encoding="utf-8")
    assert "compare_digest" in source, "口令比较必须用常量时间比较"
    assert "LOCAL_ENVS" in source, "没有区分本机与生产环境"


def test_overlay_only_applies_to_declared_keys():
    """数据库里被塞进别的键也不该生效。"""
    source = (BACKEND_ROOT / "app" / "services" / "settings_runtime.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "override_for"
    )
    assert "FIELDS" in ast.unparse(fn), "覆盖值没有按声明表过滤"


def test_provider_setting_consults_the_overlay_first():
    """整条链路的关键一环:不接上这里,设置页改了也不生效。"""
    source = (BACKEND_ROOT / "app" / "providers" / "_config.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "provider_setting"
    )
    assert "_override" in ast.unparse(fn), "provider_setting 没有读后台覆盖值"
