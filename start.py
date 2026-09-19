#!/usr/bin/env python3
"""启动桥（2026-09-05 发布编码事故修复）

.bat 是纯 ASCII 内容，无法书写中文文件名（cmd 按 GBK 解析 bat，
任何 UTF-8 变体都会乱码）。经本桥调用中文名入口：
    python start.py console   → 启动图形控制台（糖糖控制台_qt.py）
    python start.py install   → 运行一键安装器（tools/安装糖糖.py）

中文路径由 python（UTF-16 控制台输出）处理，无编码问题。
"""
import runpy
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
    "console": BASE / "糖糖控制台_qt.py",
    "install": BASE / "tools" / "安装糖糖.py",
}


def main() -> int:
    argv = sys.argv[1:]
    key = argv[0] if argv else "console"
    if key not in TARGETS:
        print(f"用法: python start.py console|install（可选参数透传给目标）")
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
