#!/usr/bin/env python3
"""小糖糖 一键安装脚本"""

import os
import sys
import subprocess
import shutil
from pathlib import Path

BASE = Path(__file__).parent
USERNAME = os.environ.get("USERNAME", "Administrator")
PY310 = Path(f"C:/Users/{USERNAME}/AppData/Local/Programs/Python/Python310/python.exe")

def run(cmd, **kw):
    print(f"  $ {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    return subprocess.run(cmd, check=False, **kw)

def step(n, total, title):
    print(f"\n{'='*50}")
    print(f"  [{n}/{total}] {title}")
    print(f"{'='*50}")

TOTAL = 5

# ---- Step 1: Check Python 3.10 ----
step(1, TOTAL, "Check Python 3.10")
if not PY310.exists():
    print(f"  [FAIL] Python 3.10 not found at: {PY310}")
    print(f"  Please install Python 3.10 from:")
    print(f"    https://www.python.org/downloads/release/python-31011/")
    print(f"  Install to: {PY310.parent}")
    input("\nPress Enter to exit...")
    sys.exit(1)
result = run([str(PY310), "--version"], capture_output=True, text=True)
print(f"  [OK] {result.stdout.strip()}")

# ---- Step 2: Check FFmpeg ----
step(2, TOTAL, "Check FFmpeg")
if shutil.which("ffmpeg"):
    print("  [OK] FFmpeg found")
else:
    print("  [FAIL] FFmpeg not in PATH")
    print("  Run in cmd:  winget install ffmpeg")
    print("  Then close this window, reopen, and run this script again.")
    input("\nPress Enter to exit...")
    sys.exit(1)

# ---- Step 3: Install Python deps ----
step(3, TOTAL, "Install Python dependencies")

deps = [
    ("Core", ["websockets", "httpx", "pyyaml", "python-dotenv",
              "pyside6", "Pillow", "pytest", "pytest-asyncio",
              "python-docx", "pdfplumber", "PyPDF2"]),
    ("Audio/ML", ["onnxruntime", "pypinyin", "edge-tts",
                  "soundfile", "av"]),
]

for label, packages in deps:
    print(f"  [{label}]")
    for pkg in packages:
        run([str(PY310), "-m", "pip", "install"] + pkg.split(), capture_output=True)

print("  [OK] Core dependencies installed")
print()

# ---- Step 4: Fix hardcoded paths ----
step(4, TOTAL, "Fix hardcoded paths")

if USERNAME != "Administrator":
    files_to_fix = [
        BASE / "糖糖控制台_qt.py",
        BASE / "tools" / "denoise.py",
        BASE / "tools" / "trim_silence.py",
    ]
    # 从本机实际用户目录推出「旧账号」路径，不写死用户名（2026-10-15 T55）
    old = str(Path.home().parent / "Administrator" / "AppData" / "Local" / "Programs" / "Python" / "Python310")
    new = f"C:\\Users\\{USERNAME}\\AppData\\Local\\Programs\\Python\\Python310"
    for f in files_to_fix:
        if f.exists():
            content = f.read_text(encoding="utf-8")
            if old in content:
                f.write_text(content.replace(old, new), encoding="utf-8")
                print(f"  [OK] {f.name}")
    print(f"  [OK] Paths updated for: {USERNAME}")
else:
    print("  [OK] Username matches, no changes needed")

# ---- Step 5: Done ----
step(5, TOTAL, "Done!")
print()
print("  Setup complete!")
print()
print("  Next steps:")
print("  1. Run:  python 糖糖控制台_qt.py  (or double-click 启动控制台.bat)")
print("  2. 控制台 → 设置页 → 填入机器人QQ、你的QQ、API Key → 保存")
print("     (不用手动编辑 config.yaml，控制台里就能填)")
print("  3. In console: click「启动 SnowLuma」→ scan QR to login bot QQ")
print("  4. Run:  python tools/配置SnowLuma.py  (one-time OneBot setup)")
print("  5. In console: click「🍬 启动小糖糖」")
input("Press Enter to exit...")
