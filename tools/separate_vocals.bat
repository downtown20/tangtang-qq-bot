@echo off
cd /d "%~dp0\.."
if not exist "venv_demucs\Scripts\python.exe" (
    echo venv_demucs not found
    pause
    exit /b 1
)

if not "%~1"=="" (
    venv_demucs\Scripts\python tools\separate_vocals.py "%~1" "%~2" "%~3" "%~4" "%~5" "%~6"
    pause
    exit /b
)

set /p INPUT="Path: "
if "%INPUT%"=="" ( pause & exit /b )
venv_demucs\Scripts\python tools\separate_vocals.py "%INPUT%"
pause
