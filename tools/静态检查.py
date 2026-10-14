# -*- coding: utf-8 -*-
"""静态检查闸门 —— 文件列表由 **Python 自己展开**（2026-10-14 T54 新建）。

为什么要这个脚本（现场取证）
────────────────────────────
项目文档里那条命令是：

    python -m pyflakes agent/*.py onebot/*.py main.py 糖糖控制台_qt.py | grep -i undefined

它在 **Git Bash** 下是对的（bash 会展开 glob）。但在 **pwsh / cmd** 下 glob 不展开，
pyflakes 收到的是字面量 `agent/*.py`，直接报 Invalid argument 并**跳过那两个目录**。
2026-10-14 实测：

    不展开（pwsh）：输出 24 行，**全是 Invalid argument 报错**，undefined 命中 = 0
    显式展开      ：传入 82 个文件，输出 116 行，undefined 命中 = 0

⇒ **82 个文件里只有 2 个（main.py 与 糖糖控制台_qt.py）真的被扫过**，而结果照样是
「undefined = 0」—— 闸门看起来通过了，实际上 80 个文件从没被看过。

这与本仓教训 #30（检查器写了但从没生效）是同一族：**跑起来没报错 ≠ 真的扫了。**

所以本脚本做了两件事：
  1. 文件列表用 pathlib 展开，**不依赖任何 shell 的 glob 行为**；
  2. **把实际扫描的文件数打出来**，并且**一旦 pyflakes 因为参数报错就立刻红灯**
     —— 数量对不上必须是一眼可见的红，不能是静默缩小。

用法：
    python tools/静态检查.py            # 只看 undefined（与项目原闸门同口径）
    python tools/静态检查.py --all      # 顺带列出全部 pyflakes 告警
退出码：0=干净；1=有 undefined；2=闸门自身失效（没收到文件 / 参数被拒）
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

#: 要扫的目录（展开其中的 *.py）
TARGET_DIRS = ("agent", "onebot")
#: 要扫的根目录单文件
TARGET_FILES = ("main.py", "糖糖控制台_qt.py")


def collect() -> list[Path]:
    """按**确定的规则**收集待扫文件。任何一步失败都返回已收集的部分。"""
    files: list[Path] = []
    for d in TARGET_DIRS:
        p = BASE / d
        if p.is_dir():
            files.extend(sorted(p.glob("*.py")))
    for name in TARGET_FILES:
        p = BASE / name
        if p.exists():
            files.append(p)
    return files


def find_rejected(lines: list[str]) -> list[str]:
    """pyflakes 抱怨参数的行 —— 有任意一条就说明它没接受全部文件。

    这是本脚本存在理由的**阳性对照**：没有这道判断，「undefined = 0」会在
    只扫了 2/82 个文件时照样成立。
    """
    return [ln for ln in lines
            if "Invalid argument" in ln or "No such file" in ln]


def find_undefined(lines: list[str]) -> list[str]:
    """undefined name 告警行（项目闸门口径）。"""
    return [ln for ln in lines if "undefined" in ln.lower()]


def main(argv: list[str]) -> int:
    show_all = "--all" in argv
    files = collect()
    print(f"扫描文件数 = {len(files)}")
    if not files:
        print("!! 一个文件都没收到 —— 闸门失效（路径规则可能变了）")
        return 2

    proc = subprocess.run(
        [sys.executable, "-m", "pyflakes", *(str(f) for f in files)],
        capture_output=True, text=True, errors="replace")
    out = (proc.stdout or "") + (proc.stderr or "")
    lines = [ln for ln in out.splitlines() if ln.strip()]

    # ── 闸门自身的阳性对照 ──────────────────────────────────────────
    # pyflakes 抱怨参数 = 它没接受全部文件。这时「undefined = 0」是假的。
    rejected = find_rejected(lines)
    if rejected:
        print(f"!! pyflakes 拒绝了 {len(rejected)} 行参数 —— 闸门只扫了一部分：")
        for ln in rejected[:10]:
            print("   " + ln.strip())
        return 2

    undef = find_undefined(lines)
    if show_all:
        print(f"pyflakes 全部输出（{len(lines)} 行）：")
        for ln in lines:
            print("   " + ln.strip())

    print(f"pyflakes 输出行数 = {len(lines)}")
    print(f"undefined 命中 = {len(undef)}")
    for ln in undef:
        print("  !! " + ln.strip())
    return 1 if undef else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
