@echo off
chcp 65001 >nul
title 小糖糖 桌面端
cd /d "%~dp0"

rem ① 优先：随包的绿色版（双击即用，不需要 Node / pnpm）
if exist "desktop\release\小糖糖桌面端\小糖糖桌面端.exe" (
    echo 正在启动小糖糖桌面端...
    start "" "desktop\release\小糖糖桌面端\小糖糖桌面端.exe"
    exit /b 0
)

rem ② 次选：已经装好依赖的开发构建（源码方式，需要 Node 20+ / pnpm 9+）
if exist "desktop\node_modules\electron\dist\electron.exe" (
    echo 正在以开发模式启动小糖糖桌面端...
    echo （关闭这个窗口 = 退出桌面端）
    cd /d "%~dp0desktop"
    call pnpm dev
    exit /b 0
)

rem ③ 两样都没有：把缺什么说清楚，不要静默失败
echo.
echo 桌面端还没准备好：缺的是「已构建好的桌面端」，不是 Python 或依赖。
echo.
echo 二选一 ——
echo   1^) 下载发布页的桌面端压缩包（tangtang-desktop-*.zip），解压到本目录，
echo      确认存在 desktop\release\小糖糖桌面端\小糖糖桌面端.exe，再双击本文件。
echo   2^) 有 Node.js 20+ 和 pnpm 9+ 时，在本目录执行：
echo        cd desktop
echo        pnpm install
echo        pnpm dev
echo.
pause
exit /b 1
