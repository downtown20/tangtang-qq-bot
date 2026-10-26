@echo off
chcp 65001 >nul
title 小糖糖 桌面端
cd /d "%~dp0desktop"

if not exist "node_modules\electron\dist\electron.exe" (
    echo [首次运行] 正在安装依赖（约 110MB，需要几分钟）...
    call pnpm install
    if errorlevel 1 (
        echo.
        echo 依赖安装失败，请检查网络后重试。
        pause
        exit /b 1
    )
)

echo 正在启动小糖糖桌面端...
echo （关闭这个窗口 = 退出桌面端）
call pnpm dev
pause
