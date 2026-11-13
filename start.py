#!/usr/bin/env python3
"""启动桥（2026-09-05 发布编码事故修复）

.bat 是纯 ASCII 内容，无法书写中文文件名（cmd 按 GBK 解析 bat，
任何 UTF-8 变体都会乱码）。经本桥调用中文名入口：
    （2026-11-14：`console` 入口随 PySide6 控制台一起下线——日常请用桌面端）
    python start.py install   → 运行一键安装器（tools/安装糖糖.py）
    python start.py desktop   → 启动桌面端（desktop/release/小糖糖桌面端/小糖糖桌面端.exe）

中文路径由 python（UTF-16 控制台输出）处理，无编码问题。

为什么 desktop 要走桥（2026-11-14）：README 一直教用户**双击 exe**，而
desktop/README.md 明写 Windows 上 `--no-sandbox` 不能省。裸双击不带这个参数，
在部分机器上 Electron 起不来（用户看到的是"点了没反应"）。bat 又写不了中文路径，
所以参数和路径都放在这里，bat 只负责调它。
"""
import runpy
import subprocess
import sys
from pathlib import Path

# 强制 UTF-8 输出（见 tools/安装糖糖.py 同款守卫）。
# install 那条路径本就被目标脚本间接保护着；这里放在桥这一层，是为了让
# console（图形控制台，自身没有守卫）走重定向时同样安全，两条路径一起覆盖。
for _s in (sys.stdout, sys.stderr):
    if _s and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

BASE = Path(__file__).resolve().parent
TARGETS = {
    "install": BASE / "tools" / "安装糖糖.py",
}
# 桌面端绿色版（打包发布版本.py 的 _add_desktop 把它塞进主包的同一个位置）
DESKTOP_EXE = BASE / "desktop" / "release" / "小糖糖桌面端" / "小糖糖桌面端.exe"


def launch_desktop() -> int:
    if not DESKTOP_EXE.is_file():
        print(f"[错误] 没找到桌面端：{DESKTOP_EXE}")
        print("   完整发布包才带它；从源码跑请按 desktop/README.md 构建，")
        print("   或先用 python start.py install 装一遍。")
        return 1
    subprocess.Popen([str(DESKTOP_EXE), "--no-sandbox"], cwd=str(DESKTOP_EXE.parent))
    print(f"✅ 已启动桌面端：{DESKTOP_EXE}")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    key = argv[0] if argv else "console"
    if key == "desktop":
        return launch_desktop()
    if key not in TARGETS:
        print("用法: python start.py console|install|desktop（可选参数透传给目标）")
        return 2
    target = TARGETS[key]
    if not target.is_file():
        print(f"[错误] 找不到目标文件: {target}")
        return 1
    # 透传参数：让目标脚本认为自己是直接运行的
    sys.argv = [str(target)] + argv[1:]
    runpy.run_path(str(target), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
