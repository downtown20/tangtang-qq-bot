#!python3.10
"""检查 SnowLuma 是否有新版本（2026-08-16 QQ 9.9.33 注入事故后加的监控）。

现场：QQ 热更新到 9.9.33 → SnowLuma 1.14.8 OIDB 全挂（connection changed）、
重装旧 QQ/退多端/重登全无效——账号会话被风控标记，需时间冷却；
SnowLuma 1.14.8 之后的新版本很可能适配 9.9.33。GitHub API 本机可达。

用法：python tools/检查SnowLuma更新.py
输出：有新版本时打印提醒 + 返回码 1；无新版本静默（返回码 0）。
可挂到控制台开机检查（运维/糖糖控制台_qt.py _auto_unpack_memory 附近调用）。
"""
import json
import subprocess
import sys
from pathlib import Path

# 强制 UTF-8 输出（与 同步记忆.py / 体检.py / 安装糖糖.py 同一守卫）。
# [!] 2026-09-20 补：本文件此前**是唯一没有这道守卫的脚本**，而它要打印的
#   「🔔 SnowLuma 有新版本！」在管道/重定向下（GBK）直接抛 UnicodeEncodeError，
#   实测 rc=1、stdout 空、stderr 一堆 traceback——**有新版反而比没新版更糟**。
for _s in (sys.stdout, sys.stderr):
    if _s and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

BASE = Path(__file__).resolve().parent.parent
SNOWLUMA_DIR = BASE / "SnowLuma"
API = "https://api.github.com/repos/SnowLuma/SnowLuma/releases/latest"


def _local_pkg() -> Path | None:
    """找出本机装在 SnowLuma/ 下的那个 package.json——**取版本号最高的那个**。

    ⚠ 2026-09-20 修：原先这里写死了 `SnowLuma-v1.14.9-win-x64`。而安装器与 README
    让用户解压**任意** `SnowLuma-vX.Y.Z-win-x64`（check_prerequisites 用 glob 找），
    开发机上还并排放着 1.11.4 / 1.12.0 / 1.14.8 / 1.14.9 四个目录。
    于是：用户装的版本名对不上 → `_local_version()` 返回 "" → main() 在
    `if not local or not latest: return 0` 早退 → **更新检查永久静默，而且不报错**。
    一个"跑了但从没生效"的检查器——本项目在发布脱敏上正是被同一形态坑过
    （见 CLAUDE.md 反模式 #30），只是这次没有 4000 个用户替我们发现它。
    """
    cands = []
    for pkg in SNOWLUMA_DIR.glob("*/package.json"):
        ver = ""
        try:
            ver = json.loads(pkg.read_text(encoding="utf-8")).get("version", "") or ""
        except Exception:
            continue
        if ver:
            cands.append((tuple(int(x) for x in (ver.split(".") + ["0", "0", "0"])[:3]), pkg))
    return max(cands, default=(None, None), key=lambda t: t[0])[1]


def _local_version() -> str:
    pkg = _local_pkg()
    if pkg is None:
        return ""
    try:
        return json.loads(pkg.read_text(encoding="utf-8")).get("version", "") or ""
    except Exception:
        return ""


def _latest_version() -> str:
    """GitHub 上最新版本号；取不到返回 ""。

    ⚠ 2026-09-20 修——**这里曾经让整个检查永久静默，而且不报错。**

    原写法是 `subprocess.run(..., text=True)`，没指定编码。Windows 中文版下
    `text=True` 用 **locale 编码（cp936/GBK）**去解码子进程的 stdout，而 GitHub
    API 返回的是 UTF-8 JSON，响应里带任何非 ASCII 字节（release 说明里的 emoji、
    作者名……）→ `UnicodeDecodeError` → **`out.stdout` 是 None** →
    `json.loads(None)` 抛 `TypeError` → 被裸 `except` 吞掉 → 返回 "" →
    `main()` 在 `if not local or not latest: return 0` 早退。

    症状：本机 SnowLuma 1.14.9、线上最新 v1.14.17，**落后 8 个版本，一条提示都没有**。
    检查器"跑了、退出码 0、什么都不说"——和"没有新版本"长得一模一样。
    这正是本项目记在反模式 #30 里的形态（检查器写了但从没生效），
    只不过那次是正则写错，这次是编码写错。

    同一个坑在本项目其它地方也踩过：`tools/发布前检查.py:197` 的子进程调用
    显式写了 `encoding="utf-8", errors="replace"` 并附了注释——这里当时漏了。
    """
    try:
        out = subprocess.run(
            ["curl", "-s", "--max-time", "20", API],
            capture_output=True, text=True, timeout=25,
            encoding="utf-8", errors="replace",
        )
        return json.loads(out.stdout or "").get("tag_name", "").lstrip("v")
    except Exception:
        return ""


def _ver_tuple(v: str):
    try:
        return tuple(int(x) for x in v.split(".")[:3])
    except Exception:
        return (0, 0, 0)


def main() -> int:
    local = _local_version()
    latest = _latest_version()
    # 分不清"没有新版本"和"查不到"就会安于现状——但控制台是**静默调用**它的
    # （`_check_snowluma_update` 只在 stdout 非空时弹窗），所以这里仍不能瞎报。
    # 折中：查不到时往 stderr 说一句人话。控制台不读 stderr，用户手动跑能看到。
    if not local:
        print("[i] 没找到本机的 SnowLuma（SnowLuma/<版本目录>/package.json）——跳过更新检查",
              file=sys.stderr)
        return 0
    if not latest:
        print("[i] 查不到线上最新版本（网络不通/被墙/GitHub API 限流）——本次不判断",
              file=sys.stderr)
        return 0
    if _ver_tuple(latest) > _ver_tuple(local):
        print(f"🔔 SnowLuma 有新版本！本机 {local} → 最新 {latest}")
        print(f"   下载页: https://github.com/SnowLuma/SnowLuma/releases/latest")
        print(f"   （2026-08-16 QQ 9.9.33 注入事故背景：新版可能修复 OIDB connection changed）")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
