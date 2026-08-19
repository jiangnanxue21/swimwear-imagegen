"""两个测试入口都必须在 Settings 单例构造前安装 Provider 基线。"""
from __future__ import annotations

from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]


def _assert_installed_before_settings(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    call = source.index("install_provider_test_baseline()")
    settings_import = source.find("\nfrom app.core.config import settings")
    if settings_import >= 0:
        assert call < settings_import
    else:
        # 零依赖运行器不直接导入 Settings,但基线必须在加载任何用例之前。
        assert call < source.index("def load_module")


def test_pytest_installs_the_provider_baseline_before_settings():
    _assert_installed_before_settings(BACKEND / "tests" / "conftest.py")


def test_zero_dependency_runner_installs_the_same_baseline_before_imports():
    _assert_installed_before_settings(BACKEND / "tools" / "run_pure_tests.py")
