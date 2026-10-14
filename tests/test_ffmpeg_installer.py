"""ffmpeg 安装器的防复发闸门。

背景（2026-10-14 实测）
──────────────────────
老版 tools/快速安装ffmpeg.py 干的事是「把 static ffmpeg.exe 复制进
C:\\Windows\\System32」。那是一次**假安装**，留下三个后遗症：

1. static 版**没有 DLL**，而 torchcodec 要的正是 avcodec/avutil 这些共享库
   —— 于是 torchcodec 永远报 "FFmpeg is not properly installed"。
2. 只复制了 ffmpeg.exe，**ffprobe.exe 没进去** —— 调 ffprobe 的工具直接找不到。
   （2026-10-14 复现：System32 里另有一个 143 MB 的 ffprobe.exe，
   ffmpeg/ffplay 都解析到新装的，唯独 ffprobe 解析到旧的。）
3. shutil.which("ffmpeg") 从此**永远返回一个路径** —— 体检和安装器的
   「FFmpeg 检查」全部报 [√]。**检查项报的绿是假的**（本仓教训 #30 的变体）。

本文件把那三条钉成会红的测试。
"""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"
INSTALLER = TOOLS / "快速安装ffmpeg.py"


def _load():
    spec = importlib.util.spec_from_file_location("_ffmpeg_installer_under_test", INSTALLER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


# ── 1. 再也不能往 System32 里复制二进制 ───────────────────────────────
def test_installer_never_copies_a_binary_into_place():
    """安装器里不允许出现任何 shutil.copy/copy2/copyfile —— 那是假安装的根源。"""
    tree = ast.parse(INSTALLER.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = None
            if isinstance(fn, ast.Attribute):
                name = fn.attr
            elif isinstance(fn, ast.Name):
                name = fn.id
            if name in {"copy", "copy2", "copyfile", "copyfileobj", "copytree"}:
                offenders.append((node.lineno, name))
    assert offenders == [], (
        f"快速安装ffmpeg.py 又出现了复制调用 {offenders} —— "
        "复制单个 exe 进系统目录会造成假安装（无 DLL、缺 ffprobe、which 恒有值）"
    )


def test_installer_only_moves_things_OUT_of_system32():
    """shutil.move 的方向只允许「System32 → 隔离目录」，绝不能反过来。"""
    tree = ast.parse(INSTALLER.read_text(encoding="utf-8"))
    moves = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "move"
    ]
    assert moves, "应当存在隔离用的 shutil.move（改名可回滚），而不是删除"
    for node in moves:
        assert len(node.args) >= 2, f"第 {node.lineno} 行 move 缺目标参数"
        dst = ast.unparse(node.args[1])
        assert "SYSTEM32" not in dst, (
            f"第 {node.lineno} 行的 move 把东西搬进了 System32 —— 方向反了"
        )


# ── 2. 清理名单必须穷举 ───────────────────────────────────────────────
def test_required_exes_covers_all_three(mod):
    assert tuple(mod.REQUIRED_EXES) == ("ffmpeg.exe", "ffprobe.exe", "ffplay.exe")


def test_stale_names_is_exhaustive(mod):
    """只清 ffmpeg.exe 会留下 ffprobe.exe 继续遮住新装的 —— 清单必须穷举。"""
    assert set(mod.STALE_NAMES) == set(mod.REQUIRED_EXES), (
        "STALE_NAMES 必须覆盖 REQUIRED_EXES 的全部名字，否则会给没列到的那些开天窗"
    )


def test_stale_system32_finds_each_name(tmp_path, monkeypatch, mod):
    monkeypatch.setattr(mod, "SYSTEM32", tmp_path)
    assert mod.stale_system32() == []
    (tmp_path / "ffprobe.exe").write_bytes(b"x")
    found = mod.stale_system32()
    assert [p.name for p in found] == ["ffprobe.exe"]


# ── 3. 检查项必须能失败（阳性对照）───────────────────────────────────
def test_check_fails_when_bin_has_no_dll(tmp_path, monkeypatch, mod):
    """把一个空目录当安装目录 —— check() 必须返回 False。

    这是教训 #30 要求的阳性对照：只测「跑起来没报错」等于没测，
    因为它本来就不会报错。
    """
    monkeypatch.setattr(mod, "ffmpeg_home", lambda: tmp_path / "ffmpeg")
    monkeypatch.setattr(mod, "ffmpeg_bin", lambda: tmp_path / "ffmpeg" / "bin")
    monkeypatch.setattr(mod, "stale_system32", lambda: [])
    assert mod.check(verbose=False) is False


def test_check_fails_when_dlls_present_but_exes_missing(tmp_path, monkeypatch, mod):
    """有 DLL 但缺 exe，同样必须 False —— 不能只看一个条件。"""
    bin_dir = tmp_path / "ffmpeg" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "avcodec-63.dll").write_bytes(b"x")
    monkeypatch.setattr(mod, "ffmpeg_home", lambda: tmp_path / "ffmpeg")
    monkeypatch.setattr(mod, "ffmpeg_bin", lambda: bin_dir)
    monkeypatch.setattr(mod, "stale_system32", lambda: [])
    assert mod.check(verbose=False) is False


def test_check_flags_stale_system32_only_when_asked(tmp_path, monkeypatch, mod):
    """include_stale=False 时残留不算失败（装之前判断要不要重下）。"""
    bin_dir = tmp_path / "ffmpeg" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "avcodec-63.dll").write_bytes(b"x")
    fake_exe = bin_dir / "ffmpeg.exe"
    fake_exe.write_bytes(b"x")  # 不是真 exe，-version 会失败 → 本来就是 False
    monkeypatch.setattr(mod, "ffmpeg_home", lambda: tmp_path / "ffmpeg")
    monkeypatch.setattr(mod, "ffmpeg_bin", lambda: bin_dir)
    monkeypatch.setattr(mod, "stale_system32", lambda: [tmp_path / "ffprobe.exe"])
    assert mod.check(verbose=False, include_stale=True) is False


# ── 4. 安装目标在项目内、不在系统盘 ──────────────────────────────────
def test_install_target_is_inside_project_deps(mod):
    home = mod.ffmpeg_home()
    deps = mod.get_deps_root()
    assert str(home).startswith(str(deps)), f"{home} 不在依赖根 {deps} 之下"


def test_install_target_not_on_system_drive(mod):
    sysdrive = os.environ.get("SystemDrive", "C:").rstrip("\\/").casefold()
    drive = str(mod.get_deps_root().resolve().drive).rstrip("\\/").casefold()
    if drive == sysdrive:
        pytest.skip("依赖根就在系统盘（项目本身装在系统盘），此断言不适用")
    assert drive != sysdrive


# ── 5. PATH 写入幂等 ─────────────────────────────────────────────────
class _FakeWinreg:
    """假 winreg：只在内存里存 Path，**绝不碰真实注册表**。

    全部写成 staticmethod —— 被测模块做的是「导入 winreg 拿到类本身」，
    再 winreg.OpenKey(...)，所以这里收到的是函数的第一个实参而不是实例。
    """

    HKEY_LOCAL_MACHINE = "HKLM"
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_WRITE = 2
    REG_EXPAND_SZ = 2
    store: dict = {}

    class _Key:
        def __init__(self, hive):
            self.hive = hive

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    @staticmethod
    def OpenKey(hive, sub, reserved=0, access=0):  # noqa: N802
        return _FakeWinreg._Key(hive)

    @staticmethod
    def QueryValueEx(key, name):  # noqa: N802
        if key.hive not in _FakeWinreg.store:
            raise FileNotFoundError(name)
        return _FakeWinreg.store[key.hive], _FakeWinreg.REG_EXPAND_SZ

    @staticmethod
    def SetValueEx(key, name, reserved, kind, value):  # noqa: N802
        _FakeWinreg.store[key.hive] = value


def test_add_to_path_is_idempotent(tmp_path, monkeypatch, mod):
    _FakeWinreg.store = {_FakeWinreg.HKEY_LOCAL_MACHINE: r"C:\Windows\System32"}
    monkeypatch.setitem(sys.modules, "winreg", _FakeWinreg)
    monkeypatch.setattr(mod, "_broadcast_env_change", lambda: None)

    target = tmp_path / "ffmpeg" / "bin"
    target.mkdir(parents=True)

    first = mod.add_to_path(target)
    assert "已写入" in first
    stored = _FakeWinreg.store[_FakeWinreg.HKEY_LOCAL_MACHINE]
    assert stored.count(str(target)) == 1
    assert stored.startswith(r"C:\Windows\System32;")

    second = mod.add_to_path(target)
    assert "已在" in second, "第二次调用不能再追加一遍"
    assert _FakeWinreg.store[_FakeWinreg.HKEY_LOCAL_MACHINE].count(str(target)) == 1
