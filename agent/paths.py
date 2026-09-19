"""
路径工具——所有机器相关路径的单一事实源（2026-08-15）

目标：任何电脑复制源码 + 装好依赖即可运行，不依赖固定盘符/用户名。
纯标准库实现——tools/ 脚本与控制台可直接 import，不拖累依赖检查。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# 项目根——本文件位于 agent/ 下，上一级即项目根
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def find_python310() -> Path | None:
    """定位可用的 Python 3.10 解释器（GPT-SoVITS/CosyVoice 需要 3.10）。

    按优先级：
      1. 环境变量 PYTHON310（显式指定，最高优先）
      2. 用户 LocalAppData（标准安装位置，不依赖用户名）
      3. ProgramFiles / C:\\Python310
      4. 项目内 python310\\（随源码复制，最可移植）
      5. PATH 里的 python3.10
      6. 当前解释器恰好是 3.10
    全都不存在返回 None——调用方自行回退（控制台回退 sys.executable，
    CosyVoice 跳过启动并打日志）。
    """
    candidates: list[Path] = []

    env = os.environ.get("PYTHON310")
    if env:
        candidates.append(Path(env))

    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Programs" / "Python" / "Python310" / "python.exe")
    prog = os.environ.get("PROGRAMFILES")
    if prog:
        candidates.append(Path(prog) / "Python310" / "python.exe")
    candidates.append(Path("C:/Python310/python.exe"))
    candidates.append(PROJECT_ROOT / "python310" / "python.exe")

    for c in candidates:
        if c.exists():
            return c

    which = shutil.which("python3.10")
    if which:
        p = Path(which)
        if p.exists():
            return p

    if sys.version_info[:2] == (3, 10):
        return Path(sys.executable)
    return None
