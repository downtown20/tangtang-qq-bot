"""依赖落盘位置的**单一事实源**。

为什么有这个模块
────────────────
项目此前把全部依赖装进 C 盘的 Python 安装目录：

* .../Programs/Python/Python310/lib/site-packages/   —— 7.5 GB
* .../Roaming/Python/Python310/site-packages/       —— 4.1 GB
  （其中 4.0 GB 是 pip 中断留下的死目录，名字以波浪号开头）

结果 C 盘只剩 4 GB。本模块把「装到哪儿」收敛成一处，
**默认永远是项目内的 依赖/ 目录，从不默认 C 盘**。

优先级（高 → 低）
────────────────
1. 环境变量 TANGTANG_DEPS_ROOT
2. 项目根目录下的 依赖位置.json（由安装器 / 控制台的目录选择对话框写入）
3. **全局配置** `%LOCALAPPDATA%/tangtang/依赖位置.json`（2026-11-14 新增）
4. 项目根目录下的 依赖/

为什么要有第 3 级
──────────────────
用户升级的方式是「解压一个新版本的包到新目录」。新目录里没有 依赖位置.json，
于是安装器把它当「第一次安装」，默认依赖根变成 `<新版本>/依赖`——而 2.6G 模型、
1.9G torch 与 pip 缓存全在上一版目录里，安装器**看不见它们**，用户看到的是
「又要重新下载」。用户 2026-11-14 实测：新包在 `...\tangtang-v1.08.07`，
依赖却指回旧的 `...\tangtang-v1.08.05\依赖`——他这次是手动在目录框里选回去的，
没选的人就得重下 4.5G。
全局配置把「依赖装哪儿」从**单个包目录**提升到**这台机器**，升级/换包自动继承。

纪律
────
**所有需要「安装 / 落盘」的环节都必须经过 get_deps_root()**，不得各自拼路径。
两份规则必然漂移——本仓已因「同一规则的两份拷贝」踩坑四次（T10/T12/T25/T42）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

__all__ = [
    "PROJECT_ROOT",
    "CONFIG_PATH",
    "GLOBAL_CONFIG_PATH",
    "DEFAULT_DEPS_ROOT",
    "get_deps_root",
    "set_deps_root",
    "describe_deps_root",
    "ensure_deps_root",
    "is_on_system_drive",
    "choose_directory",
    "subdir",
    "has_local_config",
]

# ── 项目根：本文件在 <root>/tools/ 下 ──────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 位置配置文件名（写在项目根，便于随项目一起搬走）
CONFIG_PATH = PROJECT_ROOT / "依赖位置.json"

#: 全局配置：跨版本、跨包目录共享（升级到新解压目录时仍然认得旧依赖根）
#  [!] 路径必须**每次都算**，不能只在 import 时定死：测试要能把这一份指到临时目录
#   （否则一台装过一次的机器会让「默认位置」的断言随机变红——教训 #45）。
#   `TANGTANG_GLOBAL_CONFIG` 是给测试用的覆盖开关，正常使用不必设置。
_DEFAULT_GLOBAL_CONFIG_PATH = (
    Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    / "tangtang" / "依赖位置.json"
)
#: 兼容旧引用（只读默认值；真正的读写一律走 _global_config_path()）
GLOBAL_CONFIG_PATH = _DEFAULT_GLOBAL_CONFIG_PATH


def _global_config_path() -> Path:
    env = os.environ.get("TANGTANG_GLOBAL_CONFIG", "").strip()
    return Path(env).expanduser() if env else _DEFAULT_GLOBAL_CONFIG_PATH

#: 默认依赖根——**项目内**，不是 C 盘
DEFAULT_DEPS_ROOT = PROJECT_ROOT / "依赖"

_ENV_KEY = "TANGTANG_DEPS_ROOT"


# ── 读写配置 ──────────────────────────────────────────────────────────
def _read_json_root(path: Path) -> str | None:
    """读某个配置文件里的「依赖根」。任何异常都降级为 None（不阻断安装）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("依赖根")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _read_config() -> str | None:
    """项目内 依赖位置.json 里的依赖根（不再回退全局——分开更好读）。"""
    return _read_json_root(CONFIG_PATH)


def has_local_config() -> bool:
    """本项目目录里是否已有位置配置（决定「是不是第一次安装」）。"""
    return _read_config() is not None


def sync_global() -> None:
    """把当前生效的依赖根同步到全局配置。失败静默（全局不是必要条件）。

    用途：老用户的项目内 `依赖位置.json` 是这次改动之前写的，全局那份还不存在。
    安装器跑一次就把全局补齐，**下一个版本的包目录**才继承得到。
    """
    try:
        p = get_deps_root()
        gpath = _global_config_path()
        gpath.parent.mkdir(parents=True, exist_ok=True)
        gpath.write_text(
            json.dumps({"依赖根": str(p)}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def set_deps_root(path: str | os.PathLike[str]) -> Path:
    """把依赖根写进**项目内 + 全局**两份 依赖位置.json，返回规范化后的 Path。

    写盘前会先建目录——对话框选完立刻落盘，避免「选了但没生效」。
    全局那份是升级的关键：下一个版本的包目录里没有配置，靠它继承旧依赖根
    （否则整包重下 4.5G）。全局写失败只忽略——它不是必要条件。
    """
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except OSError:
        pass
    p.mkdir(parents=True, exist_ok=True)
    payload = {
        "依赖根": str(p),
        "说明": "所有依赖/模型/缓存统一落在这里；由 tools/依赖位置.py 读写，不要手改别的键。",
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    CONFIG_PATH.write_text(text, encoding="utf-8")
    try:
        gpath = _global_config_path()
        gpath.parent.mkdir(parents=True, exist_ok=True)
        gpath.write_text(text, encoding="utf-8")
    except OSError:
        pass
    return p


def get_deps_root() -> Path:
    """当前依赖根。四级回退：环境变量 → 项目内配置 → 全局配置 → 项目内 依赖/。

    **永不返回 C 盘的系统路径**——除非项目本身就装在 C 盘。
    """
    env = os.environ.get(_ENV_KEY, "").strip()
    if env:
        return Path(env).expanduser()
    cfg = _read_config()
    if cfg:
        return Path(cfg).expanduser()
    glob = _read_json_root(_global_config_path())
    if glob:
        return Path(glob).expanduser()
    return DEFAULT_DEPS_ROOT


def describe_deps_root() -> str:
    """给用户看的一行说明：当前依赖根 + 它来自哪一级。"""
    env = os.environ.get(_ENV_KEY, "").strip()
    if env:
        return f"{get_deps_root()}（来自环境变量 {_ENV_KEY}）"
    if _read_config():
        return f"{get_deps_root()}（来自 {CONFIG_PATH.name}）"
    if _read_json_root(_global_config_path()):
        return f"{get_deps_root()}（来自全局配置 {_global_config_path()}）"
    return f"{get_deps_root()}（默认：项目内 依赖/）"


def ensure_deps_root() -> Path:
    """确保依赖根存在并返回它。"""
    root = get_deps_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def subdir(name: str) -> Path:
    """依赖根下的子目录（不自动创建）。"""
    return get_deps_root() / name


# ── 系统盘判定 ────────────────────────────────────────────────────────
def is_on_system_drive(path: str | os.PathLike[str] | None = None) -> bool:
    """该路径是否落在系统盘（通常是 C:）。

    给安装器用来做**警告**，不做硬拒绝——项目本身装在 C 盘时，
    项目内目录也就只能在 C 盘，硬拒绝会把路堵死。
    """
    sysdrive = os.environ.get("SystemDrive", "C:").rstrip("\\/").casefold()
    p = Path(path) if path is not None else get_deps_root()
    try:
        drive = p.resolve().drive.rstrip("\\/").casefold()
    except OSError:
        drive = p.drive.rstrip("\\/").casefold()
    return drive == sysdrive


# ── Windows 原生目录选择对话框 ────────────────────────────────────────
def choose_directory(
    title: str = "选择安装位置",
    initial: str | os.PathLike[str] | None = None,
) -> Path | None:
    """弹出**系统原生**的目录选择对话框，返回用户选的目录；取消返回 None。

    优先 Qt（控制台内调用时已经是 Qt 环境，样式统一）；
    否则回退到标准库 tkinter——安装器是纯控制台程序，没有 Qt 事件循环。
    """
    start = str(Path(initial) if initial is not None else get_deps_root())

    # 1) Qt：当前进程已有 QApplication 时用它
    try:
        from PySide6.QtWidgets import QApplication, QFileDialog  # type: ignore

        if QApplication.instance() is not None:
            picked = QFileDialog.getExistingDirectory(
                None, title, start, QFileDialog.Option.ShowDirsOnly
            )
            return Path(picked) if picked else None
    except Exception:
        pass

    # 2) tkinter：标准库，装 Python 就有
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        picked = filedialog.askdirectory(title=title, initialdir=start, mustexist=False)
        root.destroy()
        return Path(picked) if picked else None
    except Exception:
        return None


# ── 自检 ──────────────────────────────────────────────────────────────
def _selfcheck() -> int:
    print(f"项目根     : {PROJECT_ROOT}")
    print(f"配置文件   : {CONFIG_PATH}（{'存在' if CONFIG_PATH.exists() else '不存在'}）")
    print(f"默认依赖根 : {DEFAULT_DEPS_ROOT}")
    print(f"当前依赖根 : {describe_deps_root()}")
    print(f"在系统盘?  : {is_on_system_drive()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selfcheck())
