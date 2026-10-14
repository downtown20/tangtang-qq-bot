"""ffmpeg 路径兜底的闸门。

为什么有这个兜底
────────────────
把目录写进系统 PATH 只对**之后新启动**的进程生效。而 bot 常常是由一个早就
启动好的控制台进程拉起来的 —— 控制台的环境块还是旧的，bot 继承下来就仍旧
找不到 ffmpeg，语音识别（silk/amr → wav 转码）会莫名其妙地失效，日志里只留
一句「ffmpeg 未安装」。

2026-10-14 就是这样踩的：修好了 ffmpeg 安装（换成共享库版、挂上系统 PATH），
但因为旧的控制台进程还活着，bot 仍然解析不到。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from agent import paths as P


def _plant_ffmpeg(deps_root: Path) -> Path:
    bin_dir = deps_root / "ffmpeg" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("ffmpeg.exe", "ffprobe.exe", "ffplay.exe"):
        (bin_dir / name).write_bytes(b"fake")
    return bin_dir


# ── 定位 ─────────────────────────────────────────────────────────────
def test_find_ffmpeg_dir_honours_env_override(tmp_path, monkeypatch):
    deps = tmp_path / "deps"
    bin_dir = _plant_ffmpeg(deps)
    monkeypatch.setattr(P, "PROJECT_ROOT", tmp_path / "nowhere")
    monkeypatch.setenv("TANGTANG_DEPS_ROOT", str(deps))
    assert P.find_ffmpeg_dir() == bin_dir


def test_find_ffmpeg_dir_reads_the_same_config_file(tmp_path, monkeypatch):
    """依赖位置.json 是 tools/依赖位置.py 与 agent 层共用的同一份配置。"""
    deps = tmp_path / "deps"
    bin_dir = _plant_ffmpeg(deps)
    root = tmp_path / "proj"
    root.mkdir()
    (root / "依赖位置.json").write_text(
        '{"依赖根": ' + repr(str(deps)).replace("'", '"') + "}", encoding="utf-8"
    )
    monkeypatch.setattr(P, "PROJECT_ROOT", root)
    monkeypatch.delenv("TANGTANG_DEPS_ROOT", raising=False)
    assert P.find_ffmpeg_dir() == bin_dir


def test_find_ffmpeg_dir_defaults_to_project_deps(tmp_path, monkeypatch):
    bin_dir = _plant_ffmpeg(tmp_path / "依赖")
    monkeypatch.setattr(P, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("TANGTANG_DEPS_ROOT", raising=False)
    assert P.find_ffmpeg_dir() == bin_dir


def test_find_ffmpeg_dir_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PROJECT_ROOT", tmp_path / "empty")
    monkeypatch.delenv("TANGTANG_DEPS_ROOT", raising=False)
    assert P.find_ffmpeg_dir() is None


def test_find_ffmpeg_dir_survives_broken_config(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "依赖位置.json").write_text("{ 这不是 json", encoding="utf-8")
    monkeypatch.setattr(P, "PROJECT_ROOT", root)
    monkeypatch.delenv("TANGTANG_DEPS_ROOT", raising=False)
    assert P.find_ffmpeg_dir() is None  # 不抛异常，降级为「没找到」


# ── 挂进 PATH ────────────────────────────────────────────────────────
def test_ensure_makes_shutil_which_find_it(tmp_path, monkeypatch):
    """核心断言：即使进程 PATH 里没有 ffmpeg，挂完之后 which 必须找得到。"""
    deps = tmp_path / "deps"
    bin_dir = _plant_ffmpeg(deps)
    monkeypatch.setattr(P, "PROJECT_ROOT", tmp_path / "nowhere")
    monkeypatch.setenv("TANGTANG_DEPS_ROOT", str(deps))
    monkeypatch.setenv("PATH", str(tmp_path / "bare"))

    assert shutil.which("ffmpeg") is None, "前置条件：一开始不该找得到"
    got = P.ensure_ffmpeg_on_path()
    assert got == str(bin_dir / "ffmpeg.exe")
    # Windows 的 which 会按 PATHEXT 里的大小写去拼（ffmpeg.EXE），而文件系统
    # 不区分大小写 —— 所以这里按 normcase 比，不按字面比。
    assert os.path.normcase(shutil.which("ffmpeg") or "") == os.path.normcase(str(bin_dir / "ffmpeg.exe"))


def test_ensure_is_idempotent(tmp_path, monkeypatch):
    deps = tmp_path / "deps"
    bin_dir = _plant_ffmpeg(deps)
    monkeypatch.setattr(P, "PROJECT_ROOT", tmp_path / "nowhere")
    monkeypatch.setenv("TANGTANG_DEPS_ROOT", str(deps))
    monkeypatch.setenv("PATH", str(tmp_path / "bare"))

    P.ensure_ffmpeg_on_path()
    P.ensure_ffmpeg_on_path()
    P.ensure_ffmpeg_on_path()
    entries = [e for e in os.environ["PATH"].split(os.pathsep) if e]
    assert entries.count(str(bin_dir)) == 1, "重复调用不得把同一条目塞三遍"


def test_ensure_keeps_existing_path_entries(tmp_path, monkeypatch):
    deps = tmp_path / "deps"
    bin_dir = _plant_ffmpeg(deps)
    monkeypatch.setattr(P, "PROJECT_ROOT", tmp_path / "nowhere")
    monkeypatch.setenv("TANGTANG_DEPS_ROOT", str(deps))
    keep = str(tmp_path / "keepme")
    monkeypatch.setenv("PATH", keep)
    P.ensure_ffmpeg_on_path()
    entries = os.environ["PATH"].split(os.pathsep)
    assert keep in entries
    assert entries.index(str(bin_dir)) < entries.index(keep), "新目录应当排在前面"


def test_ensure_degrades_to_which_when_nothing_planted(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PROJECT_ROOT", tmp_path / "empty")
    monkeypatch.delenv("TANGTANG_DEPS_ROOT", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "bare"))
    assert P.ensure_ffmpeg_on_path() is None


# ── 接线闸门 ─────────────────────────────────────────────────────────
def test_main_still_wires_the_ffmpeg_fallback():
    """main.py 必须在启动早期调用 ensure_ffmpeg_on_path()，否则兜底等于不存在。"""
    src = (P.PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
    assert "ensure_ffmpeg_on_path" in src, (
        "main.py 里的 ffmpeg 兜底调用被删了 —— 删掉之后在新的控制台进程启动前，"
        "bot 会解析不到项目自带的 ffmpeg，语音识别会静默失效"
    )
    assert "from agent.paths import ensure_ffmpeg_on_path" in src
