"""临时脚本：修复 bat 文件 — 智能查找 Python"""
import os

BASE = os.path.dirname(os.path.abspath(__file__))

install_bat = "\r\n".join([
    "@echo off",
    "chcp 65001 >nul 2>&1",
    "cd /d %~dp0",
    "",
    "set PY=",
    "REM 1. 检查常见 Python 安装位置",
    'if exist "%LocalAppData%\\Programs\\Python\\Python310\\python.exe" set PY=%LocalAppData%\\Programs\\Python\\Python310\\python.exe',
    'if not defined PY if exist "%ProgramFiles%\\Python310\\python.exe" set PY=%ProgramFiles%\\Python310\\python.exe',
    'if not defined PY if exist "C:\\Python310\\python.exe" set PY=C:\\Python310\\python.exe',
    "REM 2. 用 where 找 PATH 里的 python",
    'if not defined PY (for /f "delims=" %i in (' + "'where python 2^>nul') do (set PY=%%i & goto :found))",
    "REM 3. 用 py 启动器（Windows 自带）",
    'if not defined PY (for /f "delims=" %i in (' + "'where py 2^>nul') do (set PY=%%i & goto :found))",
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
    '"%PY%" 一键安装.py',
    "pause",
])

console_bat = "\r\n".join([
    "@echo off",
    "chcp 65001 >nul 2>&1",
    "title TangTang Console",
    "cd /d %~dp0",
    "",
    "set PY=",
    'if exist "%LocalAppData%\\Programs\\Python\\Python310\\python.exe" set PY=%LocalAppData%\\Programs\\Python\\Python310\\python.exe',
    'if not defined PY if exist "%ProgramFiles%\\Python310\\python.exe" set PY=%ProgramFiles%\\Python310\\python.exe',
    'if not defined PY if exist "C:\\Python310\\python.exe" set PY=C:\\Python310\\python.exe',
    'if not defined PY (for /f "delims=" %i in (' + "'where python 2^>nul') do (set PY=%i & goto :found_console))",
    'if not defined PY (for /f "delims=" %i in (' + "'where py 2^>nul') do (set PY=%i & goto :found_console))",
    ":found_console",
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
    "echo Starting TangTang Console...",
    '"%PY%" 糖糖控制台_qt.py',
    "pause",
])

for name, data in [("一键安装.bat", install_bat), ("启动控制台.bat", console_bat)]:
    path = os.path.join(BASE, name)
    with open(path, "wb") as f:
        f.write(data.encode("utf-8"))
    print(f"{name}: written ({len(data)} bytes)")

# Verify
for name in ["一键安装.bat", "启动控制台.bat"]:
    with open(os.path.join(BASE, name), "rb") as f:
        d = f.read()
        has_crlf = b"\r\n" in d
        has_var = b"%LocalAppData%" in d
        print(f"{name}: CRLF={has_crlf}, has_LocalAppData={has_var}")
