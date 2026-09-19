@echo off
chcp 65001 >nul 2>&1
title TangTang Console
cd /d "%~dp0"

set "PY="
if exist "%LocalAppData%\Programs\Python\Python310\python.exe" set "PY=%LocalAppData%\Programs\Python\Python310\python.exe"
if not defined PY if exist "%ProgramFiles%\Python310\python.exe" set "PY=%ProgramFiles%\Python310\python.exe"
if not defined PY if exist "C:\Python310\python.exe" set "PY=C:\Python310\python.exe"
if not defined PY (for /f "delims=" %%i in ('where python 2^>nul') do (set "PY=%%i" & goto :found))
if not defined PY (for /f "delims=" %%i in ('where py 2^>nul') do (set "PY=%%i" & goto :found))
:found
if not defined PY (
    echo.
    echo [ERROR] Python 3.10 not found.
    echo Install Python 3.10 from https://www.python.org/downloads/release/python-31011/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

echo Using: %PY%
echo Checking PySide6...
"%PY%" -c "import PySide6" 2>nul
if %errorlevel% neq 0 (
    echo PySide6 not installed. Installing...
    "%PY%" -m pip install pyside6 -q
    if %errorlevel% neq 0 (
        echo Failed to install PySide6. Run manually: "%PY%" -m pip install pyside6
        pause
        exit /b 1
    )
)

echo Starting TangTang Console...
"%PY%" "%~dp0start.py" console
pause
