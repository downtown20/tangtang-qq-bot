@echo off
chcp 65001 >nul 2>&1
cd /d %~dp0

set PY=
if exist "%LocalAppData%\Programs\Python\Python310\python.exe" set PY=%LocalAppData%\Programs\Python\Python310\python.exe
if not defined PY if exist "%ProgramFiles%\Python310\python.exe" set PY=%ProgramFiles%\Python310\python.exe
if not defined PY if exist "C:\Python310\python.exe" set PY=C:\Python310\python.exe
if not defined PY (for /f "delims=" %%i in ('where python 2^>nul') do (set PY=%%i & goto :found_install))
if not defined PY (for /f "delims=" %%i in ('where py 2^>nul') do (set PY=%%i & goto :found_install))
:found_install
if not defined PY (
    echo Python not found!
    echo Please install Python 3.10 from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation
    pause
    exit /b 1
)

echo Using: %PY%
"%PY%" 一键安装.py
pause