@echo off
chcp 65001 >nul 2>&1
title Local Qwen3-VL 8B (Ollama)
cd /d "%~dp0"

set "MODEL=qwen3-vl:8b-instruct-q4_K_M"

ollama list >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Ollama service is not running.
    echo Start Ollama from the tray / Start Menu, then run this again.
    echo.
    pause
    exit /b 1
)

ollama show %MODEL% >nul 2>&1
if errorlevel 1 (
    echo [ERROR] model %MODEL% not found.
    echo Run:  ollama pull %MODEL%
    echo.
    pause
    exit /b 1
)

echo ============================================================
echo   Local Qwen3-VL 8B  -  type /bye to exit
echo   model : %MODEL%
echo   ctx   : 16384  ^|  keep_alive: 30m
echo ============================================================
echo.

ollama run %MODEL% --verbose %*
pause
