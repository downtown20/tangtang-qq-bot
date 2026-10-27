"""
路径工具——所有机器相关路径的单一事实源（2026-08-15）

目标：任何电脑复制源码 + 装好依赖即可运行，不依赖固定盘符/用户名。
纯标准库实现——tools/ 脚本与控制台可直接 import，不拖累依赖检查。
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

# 项目根——本文件位于 agent/ 下，上一级即项目根
PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ── 运行时状态 / 日志（2026-10-15 T55-F 根目录规范化）──────────────────
# 为什么要有这一节：这些文件原先**散在项目根**，且各模块自己写
# `".catch_up_state.json"` 这样的**相对路径**——解析结果取决于进程的当前工作
# 目录（CWD），只有「恰好从项目根启动」时才落在对的地方。收口成绝对路径之后，
# CWD 不再是任何人的隐式前提。
#: 运行时状态目录——所有 `*.json` 状态文件的家（重启语义靠它们落盘）
STATE_DIR = PROJECT_ROOT / "状态"
#: 日志目录——`tangtang.log` 与它按天轮转出来的副本
LOG_DIR = PROJECT_ROOT / "日志"

#: 已确认存在的目录，省掉热路径上重复的 mkdir 系统调用
_ENSURED_DIRS: set[Path] = set()


def _ensure_dir(d: Path) -> Path:
    """确保目录存在并返回它（已建过的直接跳过，被外部删掉则重建）。"""
    if d not in _ENSURED_DIRS or not d.is_dir():
        d.mkdir(parents=True, exist_ok=True)
        _ENSURED_DIRS.add(d)
    return d


def state_path(name: str) -> Path:
    """根目录状态文件的唯一入口——返回 `状态/<name>`，目录不存在就建。

    为什么目录要在这里建：漏建目录 = 状态写入静默失败 = **重启清零**
    （CLAUDE.md 教训 #15 的原形——主动私聊冷却全内存态，重启 7 次骚扰同一人
    7 次）。宁可每次多一次 `is_dir()`，也不要让「目录不存在」成为一条没人
    测过的分支。

    **禁止任何模块自己拼根相对路径**——一律走这里，或 `PROJECT_ROOT`。
    """
    return _ensure_dir(STATE_DIR) / name


def log_path(name: str = "tangtang.log") -> Path:
    """日志文件的唯一入口——返回 `日志/<name>`，目录不存在就建。

    默认名 `tangtang.log`。按天轮转的副本由 TimedRotatingFileHandler 自己
    命名为 `tangtang.log.YYYY-MM-DD`，跟随主文件所在目录，无需另行处理。
    """
    return _ensure_dir(LOG_DIR) / name


# ── 路由根目录垃圾清扫（2026-10-27）────────────────────────────────────
#: 会落在**项目根**的第三方临时文件。
#: 主要嫌疑是 `pilk`（SILK→WAV 解码）：`silk_to_wav` 里
#: `pcm_path = tempfile.mktemp(suffix='.pcm')`，用它解码完**从不删除**。
#: 2026-10-27 实测根目录躺着 10 个 `tmpXXXXXXXX.pcm` 共 55MB——而 `*.pcm`
#: 被 `.gitignore` 挡着，`git status` 完全看不见，只有 `dir` 才看得见。
STALE_ROOT_GLOBS: tuple[str, ...] = ("tmp*.pcm",)


def sweep_stale_runtime_files(
    *,
    root: Path | None = None,
    max_age_seconds: float = 7200,
    globs: tuple[str, ...] = STALE_ROOT_GLOBS,
) -> list[Path]:
    """删掉第三方工具丢在项目根的临时文件，返回删除清单。

    两个安全闸：

    1. **只删够老的**（默认 2 小时）。pilk 的 pcm 是秒级中间产物，正在被它写的
       文件绝不能动；按 mtime 卡一个远大于处理耗时的阈值即可。
    2. **失败一律吞掉**。这是启动收尾工作，没有理由因为它让 bot 起不来
       （只读盘、权限异常、文件被占用都当成「这轮算了」）。

    调用方：`main.py` 启动早期。测试里可以直接传 `root=` 指向 tmp_path。
    """
    base = Path(root or PROJECT_ROOT)
    cutoff = time.time() - max_age_seconds
    removed: list[Path] = []
    for pattern in globs:
        try:
            candidates = list(base.glob(pattern))
        except OSError:
            continue
        for path in candidates:
            try:
                if not path.is_file():
                    continue
                if path.stat().st_mtime > cutoff:
                    continue
                path.unlink()
                removed.append(path)
            except OSError:
                continue
    return removed


def find_python310() -> Path | None:
    """定位可用的 Python 3.10 解释器（GPT-SoVITS 需要 3.10）。

    按优先级：
      1. 环境变量 PYTHON310（显式指定，最高优先）
      2. 用户 LocalAppData（标准安装位置，不依赖用户名）
      3. ProgramFiles / C:\\Python310
      4. 项目内 python310\\（随源码复制，最可移植）
      5. PATH 里的 python3.10
      6. 当前解释器恰好是 3.10
    全都不存在返回 None——调用方自行回退（控制台回退 sys.executable；
    需要 3.10 的脚本跳过启动并打日志）。
    """
    candidates: list[Path] = []

    env = os.environ.get("PYTHON310")
    if env:
        candidates.append(Path(env))

    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Programs" / "Python" / "Python310" / "python.exe")
    prog = os.environ.get("PROGRAMFILES")
    if prog:
        candidates.append(Path(prog) / "Python310" / "python.exe")
    candidates.append(Path("C:/Python310/python.exe"))
    candidates.append(PROJECT_ROOT / "python310" / "python.exe")

    for c in candidates:
        if c.exists():
            return c

    which = shutil.which("python3.10")
    if which:
        p = Path(which)
        if p.exists():
            return p

    if sys.version_info[:2] == (3, 10):
        return Path(sys.executable)
    return None


# ── ffmpeg ────────────────────────────────────────────────────────────
#: 项目内 ffmpeg 相对依赖根的路径（tools/快速安装ffmpeg.py 装到这儿）
_FFMPEG_REL = Path("ffmpeg") / "bin"


def find_ffmpeg_dir() -> Path | None:
    """项目自带的 ffmpeg bin 目录；找不到返回 None。

    位置策略与 tools/依赖位置.py **同一份配置**（环境变量 TANGTANG_DEPS_ROOT →
    项目根下的 依赖位置.json → 项目内 依赖/）。这里不 import tools/
    （agent 层不依赖 tools 层），只读同一个键，保持同一条规则。
    """
    roots: list[Path] = []
    env = os.environ.get("TANGTANG_DEPS_ROOT", "").strip()
    if env:
        roots.append(Path(env).expanduser())
    try:
        import json

        cfg = PROJECT_ROOT / "依赖位置.json"
        if cfg.exists():
            raw = json.loads(cfg.read_text(encoding="utf-8")).get("依赖根")
            if isinstance(raw, str) and raw.strip():
                roots.append(Path(raw.strip()).expanduser())
    except Exception:
        pass
    roots.append(PROJECT_ROOT / "依赖")

    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    for root in roots:
        d = root / _FFMPEG_REL
        if (d / exe).exists():
            return d
    return None


def ensure_ffmpeg_on_path() -> str | None:
    """让**本进程及其子进程**认得项目自带的 ffmpeg，返回最终可用的 ffmpeg 路径。

    为什么需要这个兜底：把目录写进系统 PATH 只对**之后新启动**的进程生效，而
    bot 往往由一个早就启动好的控制台进程拉起——控制台的环境块还是旧的，bot
    继承下来就仍旧找不到 ffmpeg，语音识别会莫名其妙地失效。改注册表要等整条
    进程树重启，而项目自带的那份就在项目目录里，没道理让「语音能不能听懂」
    取决于用户有没有重启过控制台。

    顺带把该目录加进 DLL 搜索路径——torchcodec 要的是 avcodec 这些**共享库**，
    光有一个 exe 不够。
    """
    d = find_ffmpeg_dir()
    if d is not None:
        sd = str(d)
        cur = os.environ.get("PATH", "")
        if os.path.normcase(sd) not in os.path.normcase(cur):
            os.environ["PATH"] = sd + os.pathsep + cur
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(sd)
            except OSError:
                pass
        exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
        if (d / exe).exists():
            return str(d / exe)
    return shutil.which("ffmpeg")


# ── 构建工具链（**安装期**前置，不是运行时依赖）────────────────────────
#: VS2022 的安装根（Community / BuildTools / Professional / Enterprise 都在其下）
_MSVC_ROOTS = (
    "C:/Program Files/Microsoft Visual Studio/2022",
    "C:/Program Files (x86)/Microsoft Visual Studio/2022",
)


def find_msvc_cl() -> "Path | None":
    """找 VS2022 的 x64 编译器 cl.exe；没有返回 None。

    这**不是 Python 运行时依赖，是安装期工具链依赖**。全项目只有两个包需要它编译
    （2026-10-14 查 PyPI 实证）：

      · jieba-fast —— 只有 sdist，**零个 Windows 轮子** → GPT-SoVITS 分词加速包
      · fairseq==0.12.2 —— 有 bdist_wheel 但**没有 win_amd64** → 歌唱工作室的 RVC/HuBERT

    （pyworld 有 3 个 Windows 轮子、numba 有 7 个；jieba / librosa / edge-tts / torchfcpe
    是纯 Python —— 都不需要编译器。）

    为什么按固定路径扫而不是 shutil.which：**普通终端里 cl.exe 不进 PATH**，只有
    「VS 开发者命令提示符」才会设置那一整套环境变量，which 永远找不到它。
    """
    for root in _MSVC_ROOTS:
        r = Path(root)
        if not r.exists():
            continue
        hits = sorted(r.glob("*/VC/Tools/MSVC/*/bin/Hostx64/x64/cl.exe"))
        if hits:
            return hits[0]
    return None


#: jieba-fast 真正干活的 C 扩展（**顶层模块**，不在 jieba_fast/ 包目录里）
JIEBA_FAST_C_MODULE = "_jieba_fast_functions_py3"


def has_jieba_fast_c_extension() -> bool:
    """jieba-fast 的 C 扩展是否**真的装上了**。

    「import jieba_fast 成功」和「它在加速」是**两件事**：jieba_fast 的包体是纯 Python
    外壳，真正快的是顶层模块 _jieba_fast_functions_py3（.pyd）。只判前者，会把
    「只有外壳」误报成「装好了」——本仓教训 #30 / #36 的同一个形状：检查要看真东西。
    """
    import importlib.util

    try:
        return importlib.util.find_spec(JIEBA_FAST_C_MODULE) is not None
    except Exception:
        return False
