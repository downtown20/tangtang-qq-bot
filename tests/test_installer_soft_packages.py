"""`_install()` 的参数形状 + torchaudio 轮子可用性（2026-11-14）。

## 为什么有这条闸门

用户实机（全勾 `0` 那次）的日志里有一行非常刺眼：

    $ python.exe -m pip install j i e b a - f a s t > = 0 . 3 9 --cache-dir ...
    ERROR: Invalid requirement: '-'
      [i] jieba-fast 未装成功——如需加速请先装 VS2022 Build Tools（C++ 桌面开发）后重试；

**每个字符之间都被插了空格**——`_install(pkgs: list[str])` 收到的是**字符串**
`"jieba-fast>=0.39"`，而 `*pkgs` 对字符串是逐字符展开。
更糟的是失败提示把原因归给「缺 MSVC」——用户会去装 Visual Studio，白折腾一场。

第二个坑：`torchaudio==2.11.0.dev20260727+cu130` 是 dev 轮子，**上游已经删了**：

    ERROR: Could not find a version that satisfies the requirement torchaudio==2.11.0.dev20260727+cu130
    (from versions: 2.9.0+cu130, 2.9.1+cu130, 2.10.0+cu130, 2.11.0+cu130)

本机实测 `torchaudio==2.11.0+cu130` 在 torch 2.13.0+cu130 下 import 正常，故钉稳定版。
"""

from __future__ import annotations

import importlib.util
import pathlib
from types import SimpleNamespace

import pytest

BASE = pathlib.Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("installer_soft_probe",
                                               BASE / "tools" / "安装糖糖.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


@pytest.fixture()
def captured(tmp_path, monkeypatch):
    """拦下 pip 调用，返回它真正拿到的 argv。"""
    box: dict[str, list[str]] = {}

    def fake_run(cmd, **kw):  # noqa: ARG001
        box["cmd"] = list(cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(_mod, "_deps_target", lambda rel: tmp_path / rel)
    return box


def test_string_package_is_not_exploded_into_characters(captured):
    """`_install("jieba-fast>=0.39", {})` 必须把包名当**一个**参数。"""
    _mod._install("jieba-fast>=0.39", {})
    cmd = captured["cmd"]

    assert "jieba-fast>=0.39" in cmd, (
        "包名没有作为整体出现——`*pkgs` 又把字符串逐字符展开了：\n  " + " ".join(cmd))
    for ch in ("j", "i", "e", "b", "a", "-", "f", "s", "t"):
        assert ch not in cmd, f"命令行里出现了单字符参数 {ch!r}：\n  {' '.join(cmd)}"


def test_list_package_is_passed_through(captured):
    """正常形态（list）不受影响。"""
    _mod._install(["pytest>=8.0", "pyflakes>=3.0"], {})
    cmd = captured["cmd"]
    assert "pytest>=8.0" in cmd and "pyflakes>=3.0" in cmd


def test_soft_packages_are_installed_one_by_one():
    """接线断言：soft 组必须逐包调用（`_install([pkg], {})`），不是整 list 投喂。

    这条用源码断言而不是跑 `install_groups()`——后者会走 torch 探测（subprocess），
    在测试里拦 subprocess 会顺手拦掉探测用的那次调用，反而测不准。
    """
    src = (BASE / "tools" / "安装糖糖.py").read_text(encoding="utf-8")
    assert "for pkg in soft:" in src, "soft 循环被改回 `for pkgs in soft` 了（那会把 str 直传）"
    assert "_install([pkg], {})" in src, "soft 包没有按单元素 list 传参"


def test_cuda_soft_wheel_is_not_a_dev_build():
    """dev 轮子会被上游删除（实测 2026-11-14 已删）——钉的必须是索引里真有的版本。"""
    soft = _mod.TORCH_VARIANTS["cuda"].get("soft_pkgs") or []
    assert soft, "cuda 档没有 soft_pkgs 了——torchaudio 会缺"
    assert not any(".dev" in p for p in soft), (
        f"soft_pkgs 里还有 dev 轮子：{soft}。上游删过它一次（pip 报 Could not find a version）")


def test_positive_control_old_shape_is_detected():
    """阳性对照：把旧形态（字符串直传）喂给判定，必须能被认出来。"""
    def names_of(pkgs):
        return list(pkgs) if not isinstance(pkgs, str) else [c for c in pkgs]

    assert len(names_of("jieba-fast>=0.39")) > 1, "旧形态判定失效"
    assert names_of(["jieba-fast>=0.39"]) == ["jieba-fast>=0.39"]
