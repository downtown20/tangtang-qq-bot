@echo off
chcp 65001 >nul 2>&1
title 图库打标（已迁到桌面端）
cd /d "%~dp0"

echo ============================================================
echo   这个 bat 的打标功能已经搬进桌面端了（2026-10-26）
echo.
echo   推荐这样做：
echo     1) 桌面端 - 设置 - 图库打标：选「打标方式」
echo          ollama = 本机（免费、离线，要先 ollama pull 模型）
echo          api    = 云端视觉 API（不装模型，但要 key）
echo        再选模型 / 端点 / 是否改名。
echo     2) 桌面端 - 图库：点右上角「打标」。
echo        跑的时候显示进度、可以停；失败会说是哪一张、为什么。
echo.
echo   这个文件仍然能跑（参数和以前一样），留作桌面端没开时的后备：
echo     不带参数 = 只打没有 persons 字段的（增量）
echo     --limit 10 = 先试 10 张      --dry = 只看计划
echo     去掉程序里的 --resume 才等于全量重打
echo ============================================================
echo.

set "PY=%~dp0venv_tagger\Scripts\python.exe"
set "MODEL=qwen3-vl:8b-instruct-q4_K_M"

if not exist "%PY%" (
    echo [ERROR] venv_tagger 环境不存在：
    echo         %PY%
    echo.
    echo         桌面端的 ollama / api 两种方式**不需要**这个 venv，
    echo         走界面就行；这个 bat 才需要它。
    pause
    exit /b 1
)

ollama list >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Ollama 没在跑。从托盘启动它，或改用桌面端的 api 方式。
    pause
    exit /b 1
)

ollama show %MODEL% >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 没有这个模型：%MODEL%
    echo         先执行:  ollama pull %MODEL%
    pause
    exit /b 1
)

echo 开始打标（约 1.5 秒一张，不要关窗口）...
echo.

"%PY%" "%~dp0stickers_cg\_tag\tag_cg_ollama.py" --model %MODEL% --resume %*
set "RC=%errorlevel%"

echo.
if not "%RC%"=="0" (
    echo [FAIL] 退出码 %RC% - 上面有原因。
    pause
    exit /b %RC%
)

echo [OK] 完成。重启糖糖后新标签才生效（图库页刷新就能看到）。
pause
exit /b 0
