# -*- coding: utf-8 -*-
"""离线打包桌面端（绿色版，不需要 electron-builder / 不需要联网）。

产物：desktop/release/小糖糖桌面端/
    ├── 小糖糖桌面端.exe      ← 双击即用
    ├── *.dll / *.pak / locales/
    └── resources/app/        ← 我们的 dist + dist-electron

为什么手工组装：electron-builder 需要从 npm 下载 app-builder-bin（本机实测超时）。
Electron 运行时本来就是「一个目录 + exe」，把应用放进 resources/app/ 即可，
不依赖任何安装器。

前置：先跑 `cd desktop && pnpm build`（或在脚本里自动跑）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DESKTOP = Path(r"D:\qq-小糖糖\desktop")
ELECTRON_DIST = DESKTOP / "node_modules" / "electron" / "dist"
APP_NAME = "小糖糖桌面端"
OUT = DESKTOP / "release" / APP_NAME
DIST = DESKTOP / "dist"
DIST_ELECTRON = DESKTOP / "dist-electron"


def ensure_build():
    """每次打包都强制重建——否则会拿上一轮的 dist 打包出「旧界面新 exe」。"""
    print("构建前端（vite build）…")
    r = subprocess.run(
        ["node", "node_modules/vite/bin/vite.js", "build"],
        cwd=str(DESKTOP), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    print(r.stdout[-800:])
    if r.returncode != 0:
        print(r.stderr[-1500:])
        raise SystemExit("构建失败")
    if not (DIST / "index.html").exists():
        raise SystemExit("构建后缺少 dist/index.html")
    # 构建后必须存在 preload.cjs / main.js
    for name in ("main.js", "preload.cjs"):
        if not (DIST_ELECTRON / name).exists():
            raise SystemExit(f"缺少 dist-electron/{name}")


def main():
    ensure_build()
    if not ELECTRON_DIST.exists():
        raise SystemExit(f"Electron 运行时缺失: {ELECTRON_DIST}")

    if OUT.exists():
        print("清理旧产物:", OUT)
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    print("复制 Electron 运行时（约 200MB）…")
    for item in ELECTRON_DIST.iterdir():
        target = OUT / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)

    # 应用放到 resources/app/
    app_dir = OUT / "resources" / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    print("复制应用构建产物…")
    shutil.copytree(DIST, app_dir / "dist")
    shutil.copytree(DIST_ELECTRON, app_dir / "dist-electron")

    # Electron 需要一个 package.json 才知道入口
    pkg = {
        "name": "tangtang-desktop",
        "version": "0.1.0",
        "description": "小糖糖桌面端",
        "main": "dist-electron/main.js",
    }
    (app_dir / "package.json").write_text(
        json.dumps(pkg, ensure_ascii=False, indent=2), encoding="utf-8")

    # Electron 自带的默认应用在 resources/default_app.asar，保留无妨；
    # 但 resources/app 存在时它不会被使用。

    # exe 改名
    exe_src = OUT / "electron.exe"
    exe_dst = OUT / f"{APP_NAME}.exe"
    if exe_src.exists():
        exe_src.rename(exe_dst)
        print("exe:", exe_dst.name)
    else:
        raise SystemExit("electron.exe 不存在，运行时复制不完整")

    # 快捷方式必须带 --no-sandbox。
    # 原因：Electron 的 Chromium 沙箱在某些环境（本机实测：DSH 进程树下）初始化失败，
    # 裸启动 exe 会以 STATUS_BREAKPOINT(0x80000003) 静默秒退、连 main.js 都不执行。
    # vite-plugin-electron 的 dev 模式默认就传 --no-sandbox，所以开发时看不出来。
    make_shortcut(exe_dst, OUT)

    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file()) / 1024 / 1024
    print(f"\n✅ 打包完成: {OUT}")
    print(f"   体积: {size:.0f} MB")
    print(f"   双击运行: {OUT / (APP_NAME + '.lnk')}  ← 打开这个")
    print(f"   （{APP_NAME}.exe 本身也可用，但需带 --no-sandbox 参数）")
    print("\n提示：exe 依赖系统里的 python（项目环境已有）。")


def make_shortcut(exe: Path, workdir: Path):
    """用 PowerShell 的 WScript.Shell 建 .lnk（不依赖 pywin32）。"""
    lnk = workdir / f"{APP_NAME}.lnk"
    icon = Path(r"D:\qq-小糖糖\糖.ico")
    icon_spec = str(icon) if icon.exists() else f"{exe},0"
    ps = (
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
        f"$s.TargetPath = '{exe}';"
        f"$s.Arguments = '--no-sandbox';"
        f"$s.WorkingDirectory = '{workdir}';"
        f"$s.IconLocation = '{icon_spec}';"
        f"$s.Description = '{APP_NAME}';"
        f"$s.Save()"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=60,
        )
        if lnk.exists():
            print("快捷方式:", lnk.name)
        else:
            print("⚠ 快捷方式创建失败（exe 仍可手动带 --no-sandbox 启动）:", r.stderr[:200])
    except Exception as e:
        print("⚠ 快捷方式创建异常:", e)


if __name__ == "__main__":
    main()
