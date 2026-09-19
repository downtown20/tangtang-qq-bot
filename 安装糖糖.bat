@echo off
chcp 65001 >nul 2>&1
title TangTang Installer
cd /d "%~dp0"

set "PY="
if exist "%LocalAppData%\Programs\Python\Python310\python.exe" set "PY=%LocalAppData%\Programs\Python\Python310\python.exe"
if not defined PY if exist "%ProgramFiles%\Python310\python.exe" set "PY=%ProgramFiles%\Python310\python.exe"
if not defined PY if exist "C:\Python310\python.exe" set "PY=C:\Python310\python.exe"
if not defined PY if exist "%~dp0python310\python.exe" set "PY=%~dp0python310\python.exe"
if not defined PY for /f "delims=" %%i in ('where python 2^>nul ^| findstr /v /i WindowsApps') do if not defined PY set "PY=%%i"
if not defined PY for /f "delims=" %%i in ('where py 2^>nul') do if not defined PY set "PY=%%i"

if not defined PY (
    echo.
    echo [ERROR] Python 3.10 not found.
    echo Install Python 3.10 from https://www.python.org/downloads/release/python-31011/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

echo Using interpreter: %PY%
"%PY%" "%~dp0start.py" install %*
echo.
pause
