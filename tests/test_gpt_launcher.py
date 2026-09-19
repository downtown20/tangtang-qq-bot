"""GPT-SoVITS 启动包装异常取证回归测试。"""

import importlib.util
from pathlib import Path
import sys

import pytest


_LAUNCHER = Path(__file__).resolve().parents[1] / "gpt-sovits" / "start_api_patched.py"
_SPEC = importlib.util.spec_from_file_location("gpt_sovits_launcher", _LAUNCHER)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_launcher_reports_nonzero_system_exit(capsys):
    _MODULE._report_nonzero_exception(SystemExit(1))

    stderr = capsys.readouterr().err
    assert "GPT-SoVITS launcher non-zero SystemExit: 1" in stderr
    assert "SystemExit: 1" in stderr


def test_launcher_ignores_clean_exit(capsys):
    _MODULE._report_nonzero_exception(SystemExit(0))

    assert capsys.readouterr().err == ""


def test_launcher_reports_keyboard_interrupt(capsys):
    _MODULE._report_nonzero_exception(KeyboardInterrupt())

    stderr = capsys.readouterr().err
    assert "GPT-SoVITS launcher non-zero KeyboardInterrupt: None" in stderr


def test_launcher_reports_normal_api_return_as_explicit_failure(capsys):
    """入口 API 正常返回时应给出明确诊断，而不是裸 raise。"""
    source = _LAUNCHER.read_text(encoding="utf-8")
    entry = source[source.index('if __name__ == "__main__":'):]
    entry = entry.replace("_run_api()", "_run_api_noop()")
    namespace = {
        "__name__": "__main__",
        "sys": sys,
        "_run_api_noop": lambda: None,
        "_report_nonzero_exception": lambda _exc: None,
    }

    with pytest.raises(SystemExit) as raised:
        exec(compile(entry, str(_LAUNCHER), "exec"), namespace)

    assert raised.value.code == 1
    assert "API returned unexpectedly" in capsys.readouterr().err
