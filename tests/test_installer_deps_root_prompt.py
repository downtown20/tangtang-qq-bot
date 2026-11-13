"""`_confirm_deps_root()` 的「别每次都弹系统目录框」契约（2026-11-14）。

## 为什么有这条闸门

用户实机：装上装下三次，每次都弹一个系统目录选择框，只能点三次「取消」。
更要紧的是**误点的后果很难看**——依赖根一改，之前下好的 2.6G 模型和 pip 缓存在
旧的依赖根下，安装器立刻「看不见」，用户看到的是「模型又要重下」。

修法：**已经配置过、且目录还在 → 默认沿用**，只留一行「回车沿用 / 直接输入新目录」；
只有**第一次**（没有 依赖位置.json）或**配置的目录不在了**才弹系统框。

这条闸门钉住三件事：
1. 已配置 + 目录在 + 回车 → **不许**调 choose_directory；
2. 已配置 + 目录在 + 输入路径 → 走 set_deps_root（要能改）；
3. 未配置 → 必须弹一次（首次选择是合理的，不能把这条路也干掉）。

`choose_directory` / `set_deps_root` / `CONFIG_PATH` 全部 monkeypatch ——
测试不会真的弹框，也不会真的改本机的 依赖位置.json。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
INSTALLER = BASE / "tools" / "安装糖糖.py"

_spec = importlib.util.spec_from_file_location("installer_deps_root_probe", INSTALLER)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_依赖位置 = _mod.依赖位置


class _Recorder:
    """记录「弹框被叫过几次 / 落盘改成了什么」。"""

    def __init__(self, chosen: str | None = None):
        self.choose_calls: list[str] = []
        self.set_calls: list[str] = []
        self._chosen = chosen

    def choose_directory(self, _title, initial=None):  # noqa: ARG002
        self.choose_calls.append(str(initial))
        return self._chosen

    def set_deps_root(self, path):
        self.set_calls.append(str(path))
        return Path(str(path))


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """把安装器里的 依赖位置 换成可观测的假件。"""
    cfg = tmp_path / "依赖位置.json"
    configured = tmp_path / "configured-deps"
    configured.mkdir()
    rec = _Recorder(chosen=str(tmp_path / "picked-by-dialog"))

    monkeypatch.setattr(_依赖位置, "CONFIG_PATH", cfg)
    # [!] 必须把**全局**配置也指走：`_confirm_deps_root()` 结尾会调 sync_global()，
    #   不隔离的话测试会往真实的 %LOCALAPPDATA%/tangtang/依赖位置.json 里写一个
    #   pytest 临时目录（2026-11-14 当场踩到，把本机配置写成了 tmp 路径）。
    monkeypatch.setattr(_依赖位置, "GLOBAL_CONFIG_PATH",
                        tmp_path / "globalappdata" / "tangtang" / "依赖位置.json")
    monkeypatch.setattr(_依赖位置, "get_deps_root", lambda: configured)
    monkeypatch.setattr(_依赖位置, "ensure_deps_root", lambda: configured)
    monkeypatch.setattr(_依赖位置, "describe_deps_root", lambda: f"{configured}（来自 依赖位置.json）")
    monkeypatch.setattr(_依赖位置, "is_on_system_drive", lambda _p=None: False)
    monkeypatch.setattr(_依赖位置, "DEFAULT_DEPS_ROOT", tmp_path / "default-deps")
    monkeypatch.setattr(_依赖位置, "choose_directory", rec.choose_directory)
    monkeypatch.setattr(_依赖位置, "set_deps_root", rec.set_deps_root)
    # 让 isatty 为 True，走到交互分支
    monkeypatch.setattr(_mod.sys.stdin, "isatty", lambda: True, raising=False)
    return rec, cfg, configured


def _answer(monkeypatch, text: str) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt="": text)


def test_already_configured_enter_reuses_without_dialog(wired, monkeypatch):
    """已配置 + 目录在 + 回车 → 不弹框（这条就是本次修的那条）。"""
    rec, cfg, configured = wired
    cfg.write_text('{"依赖根": "%s"}\n' % configured.as_posix(), encoding="utf-8")
    _answer(monkeypatch, "")

    got = _mod._confirm_deps_root(None, assume_yes=False)

    assert rec.choose_calls == [], (
        "已配置且目录还在时仍然弹了系统目录框——用户每装一次都要点一次「取消」，"
        "误点还会把依赖根改掉（模型/pip 缓存在旧根下，等于要重下）")
    assert got == configured
    assert rec.set_calls == [], "回车沿用时不该写配置"


def test_configured_but_user_types_new_path_changes_it(wired, monkeypatch):
    """已配置 + 输入新路径 → 要能改（不给改也是缺陷）。"""
    rec, cfg, configured = wired
    cfg.write_text('{"依赖根": "%s"}\n' % configured.as_posix(), encoding="utf-8")
    new_dir = configured.parent / "new-deps"
    _answer(monkeypatch, f'"{new_dir}"')

    got = _mod._confirm_deps_root(None, assume_yes=False)

    assert rec.choose_calls == []
    assert rec.set_calls == [str(new_dir)], f"输入了新目录却没落盘：{rec.set_calls}"
    assert got == Path(str(new_dir))


def test_first_run_still_opens_dialog_once(wired, monkeypatch):
    """没有配置（第一次）→ 必须弹一次框：首次选择是合理的，不能把路堵死。"""
    rec, cfg, _configured = wired
    assert not cfg.exists()
    _answer(monkeypatch, "")

    got = _mod._confirm_deps_root(None, assume_yes=False)

    assert len(rec.choose_calls) == 1, "首次安装没有弹框——用户失去选位置的机会"
    assert rec.set_calls == [rec._chosen]
    assert got == Path(rec._chosen)


def test_non_interactive_never_pops_dialog(wired, monkeypatch):
    """--yes / 管道输入 → 一律不弹框（自动化场景不能卡住）。"""
    rec, _cfg, configured = wired
    _answer(monkeypatch, "")
    got = _mod._confirm_deps_root(None, assume_yes=True)
    assert rec.choose_calls == []
    assert got == configured
