"""维护 `运维/启动运维控制台.bat` — 智能查找 Python（2026-10-26 重写）

历史：本脚本原先生成项目根目录的 `一键安装.bat` + `启动控制台.bat`。
2026-10-26 Qt 控制台移入 `运维/`（根目录不再放它，避免新人把它当启动入口）：

- `启动控制台.bat` 不再属于根目录，改生成 `运维/启动运维控制台.bat`；
  内容经 `start.py console` 转发（bat 是纯 ASCII，中文文件名交给 Python——
  与 2026-09-05 发布编码事故的修法一致）。
- `一键安装.bat` 的目标 `一键安装.py` 早已不存在（现为 `tools/安装糖糖.py`），
  继续生成只会得到一个坏 bat，故移除该段。

⚠ 本脚本只写这一个文件，不碰用户的 `安装糖糖.bat` / `启动桌面端.bat`。
"""
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根

console_bat = "\r\n".join([
    "@echo off",
    "chcp 65001 >nul 2>&1",
    "title TangTang Ops Console",
    "REM 2026-10-26: moved from project root into YUNWEI/ (no longer a root entry point)",
    'cd /d "%~dp0.."',
    "",
    "set PY=",
    'if exist "%LocalAppData%\\Programs\\Python\\Python310\\python.exe" set PY=%LocalAppData%\\Programs\\Python\\Python310\\python.exe',
    'if not defined PY if exist "%ProgramFiles%\\Python310\\python.exe" set PY=%ProgramFiles%\\Python310\\python.exe',
    'if not defined PY if exist "C:\\Python310\\python.exe" set PY=C:\\Python310\\python.exe',
    'if not defined PY (for /f "delims=" %%i in (' + "'where python 2^>nul') do (set PY=%%i & goto :found))",
    'if not defined PY (for /f "delims=" %%i in (' + "'where py 2^>nul') do (set PY=%%i & goto :found))",
    ":found",
    "if not defined PY (",
    "    echo Python not found!",
    "    echo Please install Python 3.10 from https://www.python.org/downloads/",
    '    echo Make sure to check "Add Python to PATH" during installation',
    "    pause",
    "    exit /b 1",
    ")",
    "",
    "echo Using: %PY%",
    "echo.",
    "echo Checking PySide6...",
    '"%PY%" -c "import PySide6" 2>nul',
    "if %errorlevel% neq 0 (",
    "    echo PySide6 not installed. Installing...",
    '    "%PY%" -m pip install pyside6 -q',
    "    if %errorlevel% neq 0 (",
    '        echo Failed to install PySide6. Run manually: "%PY%" -m pip install pyside6',
    "        pause",
    "        exit /b 1",
    "    )",
    ")",
    "",
    "echo Starting TangTang Ops Console...",
    '"%PY%" "%~dp0..\\start.py" console',
    "pause",
])

ops_dir = os.path.join(BASE, "运维")
os.makedirs(ops_dir, exist_ok=True)
path = os.path.join(ops_dir, "启动运维控制台.bat")
with open(path, "wb") as f:
    f.write(console_bat.encode("utf-8"))
print(f"运维/启动运维控制台.bat: written ({len(console_bat)} bytes)")

with open(path, "rb") as f:
    d = f.read()
has_crlf = b"\r\n" in d
has_var = b"%LocalAppData%" in d
print(f"运维/启动运维控制台.bat: CRLF={has_crlf}, has_LocalAppData={has_var}")
