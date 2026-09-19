"""
一键打包为 .exe
用法：python 打包exe.py
需要先: pip install pyinstaller
"""
import os, subprocess, sys, shutil
from pathlib import Path

BASE = Path(__file__).parent.parent  # 项目根目录
TARGET = "糖糖控制台_qt.py"

print(f"📦 正在打包 {TARGET} ...")
print("   首次打包需 3-10 分钟，请耐心等待")

# PySide6 需要大量隐式导入
HIDDEN_IMPORTS = [
    "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
    "PySide6.QtNetwork", "PySide6.QtSvg", "PySide6.QtXml",
    "shiboken6", "yaml",
]

args = [
    sys.executable, "-m", "PyInstaller",
    "--onefile",
    "--windowed",
    "--name", "小糖糖控制台",
    "--clean",
    "--noconfirm",
]

# 添加数据文件
args += ["--add-data", f"{BASE / 'config.yaml'}{os.pathsep}."]

# 添加隐式导入
for imp in HIDDEN_IMPORTS:
    args += ["--hidden-import", imp]

# 收集 PySide6 资源（图标、翻译等）
args += ["--collect-binaries", "PySide6"]
args += ["--collect-data", "PySide6"]

args.append(str(BASE / TARGET))

import os
subprocess.run(args, check=True)

# 清理临时文件（在项目根目录，不在 tools/）
for d in ["build", "__pycache__"]:
    p = BASE / d
    if p.exists():
        shutil.rmtree(p)
spec = BASE / "小糖糖控制台.spec"
if spec.exists():
    spec.unlink()

# 移动到项目根目录
exe_src = BASE / "dist" / "小糖糖控制台.exe"
exe_dst = BASE / "小糖糖控制台.exe"
if exe_src.exists():
    shutil.move(str(exe_src), str(exe_dst))
    # 清理 dist/
    dist_dir = BASE / "dist"
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    print(f"✅ 打包完成 → {exe_dst}")
else:
    print(f"❌ 未找到 exe，请检查 dist/ 目录")
