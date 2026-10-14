"""构建工具链探测（MSVC cl.exe）与 jieba-fast C 扩展的单一判据。

背景（2026-10-14 主人问「这个项目需要依赖 cl.exe 吗」时查出来的）
──────────────────────────────────────────────────────────────────
**它不是 Python 运行时依赖，是安装期工具链依赖。** 全项目只有两个包需要编译器
（查 PyPI 实证）：

  · jieba-fast —— 只有 sdist，**零个 Windows 轮子** → GPT-SoVITS 分词加速

它是 soft 组：装不上只降级、不阻断主聊天。

（2026-10-15：原先的第二个例子 fairseq==0.12.2 是歌唱工作室/RVC 的依赖，随翻唱制作链
一起删除，例子一并去掉——留着会让人以为项目还需要它。）

改造前有两处独立实现这套扫描（安装器一处、没有第二处），而「换机体检」**一个字都不提
MSVC** —— 换到没装 VS 的电脑上，这件事在体检里完全不可见。本文件把判据钉到
agent/paths.py 一处，并挡住第三份拷贝。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from agent import paths as P

BASE = Path(__file__).resolve().parent.parent


def _load_by_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── 探测本身 ─────────────────────────────────────────────────────────
def test_find_msvc_cl_never_raises():
    got = P.find_msvc_cl()
    assert got is None or isinstance(got, Path)


def test_find_msvc_cl_returns_a_real_compiler_or_none():
    got = P.find_msvc_cl()
    if got is None:
        pytest.skip("本机没装 VS2022 C++ 工具链——这条断言不适用")
    assert got.name == "cl.exe"
    assert got.exists(), "返回的路径必须真的存在（不是猜出来的）"
    assert "Hostx64" in str(got), "必须是 x64 编译器，不是 x86 的"
    assert "MSVC" in str(got)


def test_has_jieba_fast_c_extension_never_raises():
    assert isinstance(P.has_jieba_fast_c_extension(), bool)


def test_c_extension_module_name_is_the_top_level_one():
    """C 扩展是**顶层模块**，不在 jieba_fast/ 包目录里——名字写死在这里防止后人猜错。"""
    assert P.JIEBA_FAST_C_MODULE == "_jieba_fast_functions_py3"


def test_c_extension_probe_matches_reality():
    """阳性对照：装了就是 True，没装就是 False——判据必须能两边都动。"""
    present = importlib.util.find_spec(P.JIEBA_FAST_C_MODULE) is not None
    assert P.has_jieba_fast_c_extension() is present


# ── 单一判据闸门 ─────────────────────────────────────────────────────
SCAN_MARK = 'VC/Tools/MSVC'


def test_only_paths_py_scans_for_msvc():
    """安装器与体检都必须走 agent/paths.find_msvc_cl()，不许各写一遍扫描。

    这是本仓第五次同族问题的防线（T10/T12/T25/T35/T42 都是「同一规则多份拷贝」）。
    """
    offenders = []
    for rel in ("tools/安装糖糖.py", "tools/体检.py"):
        src = (BASE / rel).read_text(encoding="utf-8")
        if SCAN_MARK in src:
            offenders.append(rel)
        assert "find_msvc_cl" in src, f"{rel} 没有走 agent/paths.find_msvc_cl"
    assert offenders == [], (
        f"这些文件自己又实现了一遍 MSVC 扫描 —— 两份规则必然漂移：{offenders}"
    )


def test_installer_msvc_check_never_blocks_install():
    """MSVC 缺失只能警告，绝不能中止安装（它是 soft 前置，不是硬前置）。"""
    src = (BASE / "tools" / "安装糖糖.py").read_text(encoding="utf-8")
    start = src.index("cl = paths.find_msvc_cl()")
    tail = src[start:start + 900]
    assert "sys.exit" not in tail and "return" not in tail.split("else:")[0][-200:], (
        "MSVC 检测里出现了中止路径——它必须只打印警告"
    )


def test_installer_loads_paths_by_file_not_by_sys_path():
    """按路径加载 agent/paths.py —— 安装器要在依赖装齐之前就能跑。"""
    src = (BASE / "tools" / "安装糖糖.py").read_text(encoding="utf-8")
    assert '"agent_paths", BASE / "agent" / "paths.py"' in src
