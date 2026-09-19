#!python3.10
"""检查 SnowLuma 是否有新版本（2026-08-16 QQ 9.9.33 注入事故后加的监控）。

现场：QQ 热更新到 9.9.33 → SnowLuma 1.14.8 OIDB 全挂（connection changed）、
重装旧 QQ/退多端/重登全无效——账号会话被风控标记，需时间冷却；
SnowLuma 1.14.8 之后的新版本很可能适配 9.9.33。GitHub API 本机可达。

用法：python tools/检查SnowLuma更新.py
输出：有新版本时打印提醒 + 返回码 1；无新版本静默（返回码 0）。
可挂到控制台开机检查（糖糖控制台_qt.py _auto_unpack_memory 附近调用）。
"""
import json
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
LOCAL_PKG = BASE / "SnowLuma" / "SnowLuma-v1.14.9-win-x64" / "package.json"
API = "https://api.github.com/repos/SnowLuma/SnowLuma/releases/latest"


def _local_version() -> str:
    try:
        return json.loads(LOCAL_PKG.read_text(encoding="utf-8")).get("version", "")
    except Exception:
        return ""


def _latest_version() -> str:
    try:
        out = subprocess.run(
            ["curl", "-s", "--max-time", "20", API],
            capture_output=True, text=True, timeout=25,
        )
        return json.loads(out.stdout).get("tag_name", "").lstrip("v")
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
    if not local or not latest:
        return 0
    if _ver_tuple(latest) > _ver_tuple(local):
        print(f"🔔 SnowLuma 有新版本！本机 {local} → 最新 {latest}")
        print(f"   下载页: https://github.com/SnowLuma/SnowLuma/releases/latest")
        print(f"   （2026-08-16 QQ 9.9.33 注入事故背景：新版可能修复 OIDB connection changed）")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
