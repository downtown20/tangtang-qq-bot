#!/usr/bin/env python3
"""小糖糖 安装/维护工具（统一入口）

用法：
    python tools/安装糖糖.py                  # 菜单选择
    python tools/安装糖糖.py 安装              # 安装依赖（功能选择 + 环境/目录检测 + 配置调整）
    python tools/安装糖糖.py 核心              # 直接安装一个依赖组（组: 核心 记忆 语音 识图 文档 控制台 测试 运维）
    python tools/安装糖糖.py 打包 [--if-newer]  # 导出记忆快照（跑完糖糖后、换机前）
    python tools/安装糖糖.py 解包 [机器名]      # 应用记忆快照（启动糖糖前）
    python tools/安装糖糖.py 检查              # 环境 + 快照状态
    python tools/安装糖糖.py 体检              # 换机功能对比
    python tools/安装糖糖.py --dry-run ...     # 预览，不实际执行
    python tools/安装糖糖.py 安装 --deps-root <你选的目录>   # 指定安装位置（不弹对话框）
    python tools/安装糖糖.py 安装 --yes        # 非交互：不弹目录选择框，用已配置的位置
    python tools/安装糖糖.py 安装 --torch-variant xpu   # 人工指定 torch 档位（默认 auto：探测本机设备）

双击入口：项目根目录 安装糖糖.bat（同一程序的壳）；也可直接双击本文件
说明：重量级依赖全部懒加载+降级链——不装的组只会让对应功能不可用，不会崩。
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

# 强制 UTF-8 输出（与 同步记忆.py / 体检.py 同一守卫）：
# stdout 被重定向或走管道时 Python 退回 GBK，print 里的符号抛 UnicodeEncodeError
# （同步记忆.py 真出过这个事故：退出码 1 但快照已生成 →「显示失败实际成功」）。
#
# 本文件此前是**被顺带保护**的：模块加载时会 exec 同步记忆.py，而那个模块顶层的
# 守卫改的正是同一个 sys.stdout 对象。那是巧合不是设计——导入顺序一变、或那个模块
# 改成懒加载，保护就无声消失。这里显式兜住，不再依赖别人的副作用。
for _s in (sys.stdout, sys.stderr):
    if _s and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

BASE = Path(__file__).resolve().parent.parent

# ── 加载同步记忆模块（打包/解包/检查——单一实现，避免两份逻辑漂移）──
_spec = importlib.util.spec_from_file_location(
    "sync_memory", Path(__file__).resolve().parent / "同步记忆.py")
_sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sync)

# ── 加载依赖位置（「依赖装到哪儿」的唯一事实源）──
# [!] 严禁在本文件里重新实现一遍位置解析：两份规则必然漂移（本仓已因「同一规则的
#   两份拷贝」踩坑四次）。这里只 import 它，并用 _deps_target() 作为落盘位置的唯一入口。
_spec_deps = importlib.util.spec_from_file_location(
    "deps_location", Path(__file__).resolve().parent / "依赖位置.py")
依赖位置 = importlib.util.module_from_spec(_spec_deps)
_spec_deps.loader.exec_module(依赖位置)

# ── 加载 agent/paths.py（机器相关事实的唯一来源）──
# MSVC 探测也走这里：安装器与体检必须用**同一个**判据，否则迟早出现
# 「安装器说找到了、体检说没有」。按路径加载而不是往 sys.path 塞项目根——
# 后者会把整个 agent 包导入进来，而这个脚本要在依赖装齐之前就能跑。
_spec_paths = importlib.util.spec_from_file_location(
    "agent_paths", BASE / "agent" / "paths.py")
paths = importlib.util.module_from_spec(_spec_paths)
_spec_paths.loader.exec_module(paths)

# ── 加载 agent/devices.py（「这台机器有什么加速器」的唯一事实源）──
# 同上面两条理由：按路径加载，不往 sys.path 塞项目根——这个脚本要在依赖装齐之前
# 就能跑，而 import agent 包会立刻拉起 torch / transformers。
# [!] 它的检测**全部依赖 torch 能否导入**：新机器上 torch 还没装，has_cuda() / has_xpu()
#   一律返回 False，best_device() 会一口咬定 "cpu"。所以「装哪一档」不能只听它——
#   硬件存在性由 nvidia-smi / WMI 独立探测（见 detect_torch_variant），
#   devices 只在 torch 已装时提供第二判据与人类可读的名字。
_spec_devices = importlib.util.spec_from_file_location(
    "agent_devices", BASE / "agent" / "devices.py")
devices = importlib.util.module_from_spec(_spec_devices)
_spec_devices.loader.exec_module(devices)


# ═══════════════════════════════════════════════════════════
# torch 档位 ——「什么设备装什么」的单一事实源（T54-A，2026-10-14）
#
# [!] 这里修的是「没有自动化落地」：安装器原来在两处把 torch 写死成
#   extra=[(["torch"], {"index-url": ".../whl/cpu"})]，于是**新装一台有独显的机器，
#   拿到的是一份没有 GPU 加速的糖糖**——100 字 198.2 秒（有卡时 5.4 秒），长语音
#   要等三分钟。用户手册 §0.5 早就写着「安装程序会自己探测本机设备并选对应那一档」，
#   代码没做 → 文档与代码分家。现在：探测本机设备 → 自动选档 → 装对应 wheel；
#   人工用 --torch-variant cuda|xpu|cpu|auto 覆盖。
#
# 三条判据（都可判定，不是口号；与用户手册 §0.5 的表一一对应）：
#   ① nvidia-smi 能列出显卡，且算力 ≥ sm_70        → cuda（cu130 轮子）
#   ② 否则 WMI 显示适配器里有 Intel Arc / Iris Xe  → xpu（xpu 轮子，beta）
#   ③ 否则（含 AMD 核显/独显——PyTorch 在 Windows 上无官方 ROCm）→ cpu
# ═══════════════════════════════════════════════════════════

# 低于这个算力的 NVIDIA 卡不装 cu 版：cu130 的轮子里没有那些老架构的 kernel，
# 装了也不会加速，只会白占几个 G（用户手册 §0.5 写的就是 sm_70 这条线）。
MIN_COMPUTE_CAP = 70

TORCH_VARIANTS = {
    "cuda": {
        "label": "CUDA（NVIDIA 独显，sm_70 及以上）",
        "needs": "NVIDIA 独显 + 驱动（nvidia-smi 能列出显卡）",
        "pkgs": ["torch==2.13.0+cu130"],
        # --no-deps 不能去：cu130 轮子会顺手把 numpy 拖到 2.x，而「核心」组把 numpy
        # 钉在 <1.24（见 GROUPS「核心」里的注释、requirements.txt）。
        "kwargs": {"index-url": "https://download.pytorch.org/whl/cu130", "no-deps": True},
        #   [取证 2026-10-14] --no-deps 不会造出「装完不能用」：把 fsspec / networkx /
        #   sympy 全 block 掉再 import torch，仍然成功、CUDA 可用、matmul 正常
        #   （torch 把这三个都做成了懒加载）；filelock / typing-extensions / jinja2
        #   由「核心」「记忆」两组带入。
        # [!] torchaudio 在 cp310-win 上没有 2.13 的稳定轮子；台式机实测可用的是这个
        #   dev 轮子（requirements.txt:51-54）。dev 轮子随时可能被上游删掉，所以它只算
        #   **软增补**：失败仅警告不中断——torch 本体已可用，缺 torchaudio 只影响
        #   GPT-SoVITS / 降噪这两条 import torchaudio 的路径。
        "soft_pkgs": ["torchaudio==2.11.0.dev20260727+cu130"],
        "soft_kwargs": {"index-url": "https://download.pytorch.org/whl/cu130", "no-deps": True},
        "cost": "语音 100 字 5.4 秒（RTX 5070 实测）",
        "verdict": "推荐——语音 / 本地识图 / 记忆精排全部可用",
    },
    "xpu": {
        "label": "XPU（Intel 核显 / Arc）",
        "needs": "Intel Arc / Iris Xe 核显（AMD 的核显不算，见下一档）",
        # [!] 2026-11-14：torchaudio 原是 `pkgs` 里的第二项，于是**任何**要 torch 的组
        #   （包括「记忆」，BGE 只要 torch）都会把它装进来。用户报告"只勾第 1 项却连
        #   第 2 项（语音）的库一起装了"。torchaudio 只有 GPT-SoVITS 用
        #   （gpt-sovits/api_v2.py:122 顶层 import torchaudio），改成 soft——由
        #   install_groups 按「是否勾了语音」决定装不装。
        "pkgs": ["torch==2.9.1+xpu"],
        "soft_pkgs": ["torchaudio==2.9.1+xpu"],
        "soft_kwargs": {"index-url": "https://download.pytorch.org/whl/xpu", "no-deps": True},
        "kwargs": {"index-url": "https://download.pytorch.org/whl/xpu", "no-deps": True},
        "cost": "比 CPU 快得多；装成后自动走 XPU，否则回落 CPU",
        "verdict": "可用（beta）——功能与 CUDA 档相同",
    },
    "cpu": {
        "label": "CPU（无可用加速器）",
        "needs": "任何机器都能装；AMD 核显/独显也走这一档",
        # 同上：torchaudio 走 soft，只随「语音」组装。
        "pkgs": ["torch"],
        "soft_pkgs": ["torchaudio"],
        "soft_kwargs": {"index-url": "https://download.pytorch.org/whl/cpu"},
        "kwargs": {"index-url": "https://download.pytorch.org/whl/cpu"},
        "cost": "语音 100 字 198.2 秒（长语音要等三分钟）",
        "verdict": "能用但很慢——识图建议走云端、语音长句要等",
    },
}
DEFAULT_TORCH_VARIANT = "auto"


def _run_capture(cmd: list[str], timeout: int = 25) -> str | None:
    """跑一条探测命令；失败 / 超时一律返回 None——探测不许把安装流程打断。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return None
    return (p.stdout or "") if p.returncode == 0 else None


def probe_nvidia() -> tuple[bool, str, int | None]:
    """硬件判据①：nvidia-smi 能列出显卡吗，算力是多少。

    → (有没有可用 NVIDIA 卡, 显卡名 / 原因, 算力×10+次版本 或 None)

    **这条不依赖 torch**——这正是它存在的理由：新机器上 torch 还没装，
    devices.py 那条路必然报 CPU，只有它能判出「这台机器有卡」。
    """
    exe = shutil.which("nvidia-smi")
    if not exe:
        cand = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
        exe = str(cand) if cand.exists() else None
    if not exe:
        return False, "nvidia-smi 不在（未装 NVIDIA 驱动）", None
    out = _run_capture([exe, "--query-gpu=name,compute_cap", "--format=csv,noheader"])
    if out is None:
        return False, "nvidia-smi 执行失败（驱动异常？）", None
    for line in out.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if not parts or not parts[0]:
            continue
        cap = None
        if len(parts) > 1:
            try:
                major, minor = parts[1].split(".")[:2]
                cap = int(major) * 10 + int(minor)
            except Exception:
                cap = None
        return True, parts[0], cap
    return False, "nvidia-smi 没列出任何显卡", None


# 虚拟显示适配器 / 远程桌面软件造的假显卡：WMI 里会混进来，必须排除，
# 否则一个带 Intel 字样的虚拟适配器就能骗到 XPU 档。
# （本机 WMI 实测就有 AskLinkIddDriver / OrayIddDriver / Todesk Virtual Display 三个。）
_VIRTUAL_DISPLAY_HINTS = ("idd", "virtual", "todesk", "oray", "parsec", "spacedesk",
                          "basic display", "remote", "mirror")


def probe_intel_arc() -> tuple[bool, str]:
    """硬件判据②：WMI 里有没有 Intel 核显（Arc / Iris Xe）。

    → (能不能装 xpu 档, 适配器名 / 为什么不能)

    Intel 的老核显（UHD 之类）不在 torch-xpu 的支持列表里，判成「不能」并写明原因——
    让它静默落到 CPU 才是坑（用户手册 §0.5 把 AMD 与老 Intel 核显都归到 CPU 档）。
    """
    out = _run_capture(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        "Get-CimInstance Win32_VideoController | "
                        "Select-Object -ExpandProperty Name"])
    if out is None:
        return False, "WMI 查询失败（拿不到显示适配器列表）"
    intel_seen = ""
    for raw in out.splitlines():
        name = raw.strip()
        low = name.lower()
        if not name or any(h in low for h in _VIRTUAL_DISPLAY_HINTS):
            continue
        if "intel" in low and ("arc" in low or "iris" in low):
            return True, name
        if "intel" in low and not intel_seen:
            intel_seen = name
    if intel_seen:
        return False, f"{intel_seen}（xpu 档只支持 Arc / Iris Xe，老核显不支持）"
    return False, "没有 Intel Arc / Iris 核显"


def torch_reported_device() -> str | None:
    """torch 已装时它自己的判定（第二判据）；没装 / 导不进来 → None。

    单独一个函数是为了留一个可替换的接缝：测试要能在「本机有卡」的机器上模拟
    「本机没卡」，否则这条路永远只跑真实硬件那一支。
    """
    try:
        if devices._torch() is None:
            return None
        return devices.best_device()
    except Exception:
        return None


def _variant_result(variant: str, evidence: list[str]) -> dict:
    v = TORCH_VARIANTS[variant]
    return {"variant": variant, "label": v["label"], "cost": v["cost"],
            "verdict": v["verdict"], "pkgs": list(v["pkgs"]),
            "kwargs": dict(v["kwargs"]), "evidence": list(evidence)}


def detect_torch_variant(force: str | None = None) -> dict:
    """探测本机设备 → 选出 torch 档位。force: None / "auto" 自动，或 cuda/xpu/cpu。"""
    evidence: list[str] = []
    torch_dev = torch_reported_device()
    if torch_dev is None:
        evidence.append("torch 尚未安装（或导不进来）——只能靠硬件探测判档")
    else:
        evidence.append(f"torch 已装，它自己报 best_device={torch_dev}")

    if force and force != DEFAULT_TORCH_VARIANT:
        if force not in TORCH_VARIANTS:
            evidence.append(f"[!] --torch-variant {force} 不认识"
                            f"（可选 {'/'.join(TORCH_VARIANTS)}），按自动探测继续")
        else:
            evidence.append(f"人工指定档位：{force}")
            return _variant_result(force, evidence)

    has_nv, nv_name, cap = probe_nvidia()
    if has_nv:
        evidence.append(f"nvidia-smi → {nv_name}"
                        + (f"（算力 sm_{cap}）" if cap else "（算力未知）"))
        if cap is None or cap >= MIN_COMPUTE_CAP:
            return _variant_result("cuda", evidence)
        evidence.append(f"[!] 算力 sm_{cap} < sm_{MIN_COMPUTE_CAP}："
                        "cu130 轮子没有它的 kernel，按 CPU 装")
    elif torch_dev == "cuda":
        evidence.append("nvidia-smi 没判出来，但 torch 报告 CUDA 可用——按 CUDA 装")
        return _variant_result("cuda", evidence)

    has_arc, arc_name = probe_intel_arc()
    if has_arc:
        evidence.append(f"WMI 显示适配器 → {arc_name}")
        return _variant_result("xpu", evidence)
    if torch_dev == "xpu":
        evidence.append("WMI 没判出 Intel 核显，但 torch 报告 XPU 可用——按 XPU 装")
        return _variant_result("xpu", evidence)
    evidence.append(f"Intel 核显探测：{arc_name}")
    return _variant_result("cpu", evidence)


def torch_variant_name(force: str | None = None) -> str:
    """把 auto / None 解析成一个具体档位名。"""
    if force in TORCH_VARIANTS:
        return force
    return detect_torch_variant(force)["variant"]


def torch_pip_spec(variant: str) -> tuple[list[str], dict]:
    """档位 → (pip 包清单, pip 参数)。给 _plan 用。"""
    v = TORCH_VARIANTS[variant]
    return list(v["pkgs"]), dict(v["kwargs"])


def torch_soft_spec(variant: str) -> tuple[list[str], dict] | None:
    """档位的「软增补」→ 目前是 torchaudio（三档都有）。

    [!] 2026-11-14：原来只有 CUDA 档声明 soft_pkgs，xpu / cpu 两档把 torchaudio
    写进了 `pkgs`（主清单）——于是"只勾第 1 项"的用户也会装上语音侧的库。
    现在三档统一：torchaudio 一律 soft，装不装由 install_groups 按
    `GROUPS[g].get("audio")` 决定。"""
    v = TORCH_VARIANTS[variant]
    if not v.get("soft_pkgs"):
        return None
    return list(v["soft_pkgs"]), dict(v.get("soft_kwargs") or v["kwargs"])


def print_torch_rules(chosen: str) -> None:
    """「什么设备适合装什么」——印成可判定、可选、能覆盖的档位表，不是口号。"""
    print("\n[·] torch 档位表（本机判定见上；--torch-variant 可人工覆盖）：")
    for dev in devices.DEVICE_LADDER:
        v = TORCH_VARIANTS.get(dev)
        if not v:
            continue
        prefix = "[本机]" if dev == chosen else "      "
        print(f"    {prefix} {v['label']}")
        print(f"           适用设备：{v['needs']}")
        print(f"           实测代价：{v['cost']}｜{v['verdict']}")


def _is_link_dir(path: Path) -> bool:
    """目录联接/符号链接判定（Python 3.10 的 Path 还没有 is_junction）。"""
    try:  # 0xA0000003 = IO_REPARSE_TAG_MOUNT_POINT（联接），0xA000000C = SYMLINK
        return os.lstat(path).st_reparse_tag in (0xA0000003, 0xA000000C)
    except (AttributeError, OSError):
        return False


def _make_junction(link: Path, target: Path) -> bool:
    """在 link 处建目录联接指向 target（Windows 原生，不需要管理员）。"""
    if os.name != "nt":
        return False
    link.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=True)
    # [!] 不要 capture_output + text：mklink 的中文提示按 OEM 代码页输出，
    #   与 Python 的 GBK 解码器对不上，解码在子线程里炸（PytestUnhandledThreadException）。
    proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.returncode == 0 and link.exists()


def _deps_target(rel: str, link_back: bool = False) -> Path:
    """本文件里**唯一**的落盘位置解析入口——不要在别处手拼 `BASE / "…"`。

    为什么老位置还要照顾：运行期读取点全是硬编码的项目内路径
    （`agent/embeddings.py:23-25`、`agent/reranker.py:26-27`、
    `agent/mood_tracker.py:23-24`、`agent/asr.py:30`、`agent/service_manager.py:23`），
    装到别处它就找不到。对齐办法用 `tools/依赖迁移.py` 对 site-packages 的同一手法——
    目录联接，对进程完全透明，`agent/` 一个字都不用改。

    · 依赖根 == 项目根        → 就地写 `BASE/<rel>`（项目在 D 盘时的老行为，零变化）
    · `BASE/<rel>` 是真实目录 → 原地用它：**绝不自动搬动已有数据**
    · 其余                    → `<依赖根>/<rel>`；link_back=True 时把 `BASE/<rel>`
                                联接到新位置，运行期照旧读得到
    """
    root = 依赖位置.ensure_deps_root()
    legacy = BASE / rel if rel else BASE
    if root.resolve() == BASE.resolve():
        return legacy
    if legacy.exists() and not _is_link_dir(legacy):
        return legacy
    target = root / rel
    if not link_back:
        return target
    if _is_link_dir(legacy) or _make_junction(legacy, target):
        return target
    print(f"  [!] 目录联接没建成：{legacy} → {target}")
    print("      退回项目内安装——否则糖糖启动后读不到它。")
    return legacy


def _deps_path(rel: str, link_back: bool = False) -> Path:
    """依赖根下某个「相对项目根」的落盘路径。按**顶层目录**对齐：
    `models/` `gpt-sovits/` `asr_models/` 各只需一次目录联接，运行期看到的是完整目录。

    `link_back` 只在真正要写盘的路径上给 True——检测/预览不该有副作用。
    """
    top, _, sub = rel.partition("/")
    p = _deps_target(top, link_back=link_back)
    return p / sub if sub else p


def _display(path: Path) -> str:
    """给用户看的路径：项目内显示相对路径，项目外显示绝对路径。"""
    try:
        return str(path.relative_to(BASE))
    except ValueError:
        return str(path)


def _confirm_deps_root(deps_root: str | None, assume_yes: bool) -> Path:
    """开始装之前，把「装到哪儿」定下来（唯一入口）。

    · `--deps-root PATH` → 直接用并落盘（自动化场景，不弹框）
    · `--yes`            → 不弹框，用已配置的位置（默认 = 项目内 `依赖/`）
    · 交互式             → 弹系统原生目录选择框；**取消 = 继续用默认，不算失败**
    三条路都打印 describe_deps_root()；落在系统盘只警告，不中止。
    """
    if deps_root:
        p = 依赖位置.set_deps_root(deps_root)
        print(f"\n[·] 安装位置（--deps-root）：{p}")
    elif assume_yes or not (sys.stdin and sys.stdin.isatty()):
        p = 依赖位置.ensure_deps_root()
        print(f"\n[·] 安装位置：{依赖位置.describe_deps_root()}")
    else:
        print("\n━━━ 安装位置 ━━━")
        print(f"  当前：{依赖位置.describe_deps_root()}")
        print("  接下来弹系统目录选择框；点「取消」就用当前位置继续。")
        chosen = 依赖位置.choose_directory("选择安装位置（模型 / 缓存 / 依赖）",
                                          initial=依赖位置.get_deps_root())
        if chosen:
            p = 依赖位置.set_deps_root(chosen)
            print(f"  [√] 已改到：{p}（已写入 {依赖位置.CONFIG_PATH.name}）")
        else:
            p = 依赖位置.ensure_deps_root()
            print(f"  [i] 未选/已取消——继续用：{p}")
    if 依赖位置.is_on_system_drive(p):
        print(f"  [!] 警告：{p} 在系统盘 {p.drive} 上——模型 + pip 缓存能到十几 G，")
        print("      建议换到别的盘（重跑本工具，或加 --deps-root PATH 指定）。")
    if p.resolve() != 依赖位置.DEFAULT_DEPS_ROOT.resolve():
        print("  [i] pip 装的包仍在 Python 自己的 site-packages。要连带挪到依赖根下，")
        print("      跑 python tools/依赖迁移.py（它建目录联接，sys.path 一个字不变）。")
    return p

# ─────────────────────────────────────────
# 依赖组定义
# ─────────────────────────────────────────

GROUPS = {
    "核心": {
        "desc": "L0 必装——缺了无法启动",
        # [!] numpy 的上界与 requirements.txt 保持一致（>=1.23.5,<1.24），改要两边一起改。
        # 原理由（RVC 链只认 NumPy 1.x——pyworld / numba 0.56）已随翻唱制作侧下线而失效，
        # 但上界仍必须钉住，理由换成了 GPT-SoVITS 自己那条链（2026-10-15 实测）：
        #   numba 0.56.4 的元数据硬要求 numpy<1.24，而 librosa 0.10.2 依赖 numba，
        #   librosa 又被在跑的 GPT-SoVITS 推理链顶层 import。动上界要有实测证据。
        "pkgs": ["websockets>=12.0", "httpx>=0.27.0", "pyyaml>=6.0",
                 "python-dotenv>=1.0", "jinja2>=3.0", "numpy>=1.23.5,<1.24"],
    },
    "记忆": {
        "desc": "L1 高质量记忆——强烈建议（含 torch，按本机设备自动选档）",
        "pkgs": ["jieba>=0.42", "transformers==4.57.6", "modelscope>=1.20"],
        # [!] 这里原来是硬编码的 CPU 版：
        #   extra=[(["torch"], {"index-url": "https://download.pytorch.org/whl/cpu"})]
        #   于是新装一台有独显的机器拿到的是一份没有 GPU 加速的糖糖（T54-A 修的就是它）。
        #   现在只声明「本组需要 torch」，装哪一档由 detect_torch_variant() 按本机设备定。
        "torch": True,
    },
    "语音": {
        "desc": "L2 语音输入（ASR + SILK 解码）+ 输出辅助；引擎与模型由模型层自动获取",
        # [!] 2026-10-15 T55：librosa / pypinyin 原先挂在「唱歌」组，被当成翻唱制作依赖；
        #   两者其实是 **GPT-SoVITS 的硬依赖**，不是 RVC 的（逐包扫 importer 取证）：
        #     pypinyin → gpt-sovits/GPT_SoVITS/text/chinese.py、chinese2.py、tone_sandhi.py、text/g2pw/g2pw.py
        #     librosa  → gpt-sovits/api.py、GPT_SoVITS/utils.py、GPT_SoVITS/module/mel_processing.py
        #   跟着「唱歌」组一起删掉的话，新机器上 GPT-SoVITS 顶层 import 就炸、直接起不来。
        "pkgs": ["soundfile>=0.12", "edge-tts>=6.1", "sherpa-onnx>=1.10", "pilk>=0.2",
                 "matplotlib>=3.7,<4", "librosa>=0.10", "pypinyin>=0.48"],
        # [!] 2026-09-20 补：torch 原先**只挂在「记忆」组**，于是"只勾第 2 项（发语音）
        #   不勾第 1 项（聊天+记忆）"的用户会拿到一份**起不来的语音服务**——
        #   `gpt-sovits/api_v2.py:122-123` 顶层就 `import torchaudio` / `import torch`，
        #   而 6.4G 的语音模型照样下完了。实跑复现：`GPT-SoVITS 进程意外退出 (code=2)`，
        #   最后输出是带绝对路径的 traceback。
        #   这正是本项目记过的「勾了就一定能用」那条契约的反面：勾了拿不到。
        #   torch 重复出现在两个组没关系——`_plan` 会去重，且 pip 对已装的包是空操作。
        #   [T54-A] 与「记忆」组一样只声明需要，档位按本机设备选（原先是硬编码 CPU 版，
        #   笔记本上装对了、台式机上装了个没加速的）。
        "torch": True,
        # soft：失败仅警告不中断——jieba-fast 是 GPT-SoVITS 分词加速（需 MSVC 源码编译），
        # 装不上时 start_api_patched.py 自动回退纯 Python jieba，语音仍可启动
        "soft": ["jieba-fast>=0.39"],
        # [!] 2026-11-14：本组是**唯一**需要 torchaudio 的组（GPT-SoVITS 顶层 import
        #   torchaudio）。这个键存在的理由，就是别让「记忆」组（只要 torch 跑 BGE）
        #   把语音侧的库一起装进来——用户 2026-11-14 报的就是这个："只勾 1，顺带装了 2"。
        "audio": True,
    },
    "识图": {
        "desc": "L3 本地识图——需另装 Ollama+minicpm-v；不装自动用云端 qwen-vl（效果几乎一样）",
        "pkgs": ["Pillow>=10.0"],
    },
    "唱歌": {
        # 2026-10-15：原写「41 首」——那是 SongLibrary.list_songs_with_audio() 在本机的实测值，
        # 但**不是随包常量**：发布包带全套，仓库克隆只带 4 首代表作（见 .gitignore 的 songs/
        # 放行段）。静态字符串钉死数字必然在某一侧说假话（教训 #35「数字要可复现」），故去数字化。
        "desc": "L4 唱歌——播放随包自带的预录成品（播放走标准库）；本组无依赖",
        "pkgs": [],
    },
    "文档": {
        "desc": "L5 读 docx/pdf",
        "pkgs": ["python-docx>=0.8", "pdfplumber>=0.10", "PyPDF2>=3.0"],
    },
    "控制台": {
        "desc": "PySide6 图形界面——建议",
        "pkgs": ["pyside6>=6.5"],
    },
    "测试": {
        "desc": "pytest 开发测试 + pyflakes undefined 门禁",
        "pkgs": ["pytest>=8.0", "pytest-asyncio>=0.24", "pyflakes>=3.0"],
    },
    "运维": {
        "desc": "L6 runtime_observer 观察器、体检/语音质检等运维脚本（选装）",
        "pkgs": ["psutil>=5.9", "requests>=2.28"],
    },
}

# 功能 → (名称, 说明, 依赖组列表)
FEATURES = [
    ("聊天+高质量记忆", "核心——LLM、记忆、知识库、BGE 向量（必须）", ["核心", "记忆"]),
    ("发语音+听懂语音消息",
     "听懂别人的语音消息 + GPT-SoVITS 发声。米雪儿声线随包自带；"
     "公开底模约 3.7G 需自行下载（见 docs/用户手册/语音模型自行配置.md）", ["语音"]),
    ("本地识图", "本地 MiniCPM-V 识图（约 2.5G，需 Ollama）；不选则用云端 qwen-vl", ["识图"]),
    # [!] 这句原来只写「已随包自带，本项无需下载」，让用户以为勾了它就有全部唱歌能力；
    #   2026-09-19 主人在笔记本上正是被这句话误导，去点了「模型」按钮来问「是不是我这个
    #   模式没有」，所以描述要写清它到底给什么。翻唱制作侧已整体下线，这里只剩播放。
    ("唱歌", "点歌即播随包自带的预录录音成品；播放不需要任何依赖、也不需要下载任何模型", ["唱歌"]),
    ("读文档", "docx/pdf 解析", ["文档"]),
    ("图形控制台", "PySide6 界面——强烈建议", ["控制台"]),
    ("测试工具", "pytest 开发测试", ["测试"]),
    ("运维工具", "runtime_observer 观察器、体检/语音质检等脚本（选装）", ["运维"]),
]

# 功能 → 需要检查的目录：(显示名, 相对项目根的位置, 是不是"模型目录", 是不是"安装落点")
#
# 第三项决定判据：模型目录要看到 `config.json` 才算就位（modelscope 下一半会留个
# 空壳目录）；资源目录只要存在。**两个判据不能共用一个谓词**——2026-09-20 我先
# 漏了这一列，`gpt-sovits` / `asr_models` 这种没有 config.json 的正常目录
# 会被报成「尚未就位」，与 tests/test_health_diagnostics.py 抓到的体检同款错误。
# 第四项决定位置从哪儿取：**安装落点**走 _deps_path()（用户可以把依赖根换到别的盘）；
# 项目自带内容（不随依赖根走）用 False——当前没有这种条目，机制保留。
DIR_CHECKS = {
    0: [("BGE 语义向量模型", "models/BAAI/bge-small-zh-v1.5", True, True),
        ("BGE 重排序模型（可选，2.2G）",
         "models/BAAI/bge-reranker-v2-m3", True, True),
        ("StructBERT 情绪模型（可选，390M；心理陪伴场景用）",
         "models/iic/nlp_structbert_sentiment-classification_chinese-base", True, True)],
    1: [("gpt-sovits/（语音引擎 + 私有声线）", "gpt-sovits", False, True),
        ("GPT-SoVITS 公开底模（约 3.7G，需自行配置）",
         "gpt-sovits/GPT_SoVITS/pretrained_models/s1v3.ckpt", False, True),
        ("asr_models/（语音识别模型，勾了语音就已自动下好）", "asr_models", False, True)],
    # 本地识图走 Ollama（agent/vision_local.py 连 127.0.0.1:11434），
    # 不读 models/minicpm-v/——那个目录是早期路线遗留，已不再使用（2026-09-18 核实）
    2: [],
    # [!] 2026-10-15 T55：本项原被我一并划进「翻唱制作链」删除，是错的——
    #   songs/ 是**保留的播放侧**资源（sing 工具 → agent/songs.py → songs/audio/）。
    #   唱歌功能描述已改成「播放预录成品」，那就更该在勾了它之后验证目录真的在：
    #   songs/ 有 5.6G 音频，不进 git（见 .gitignore），克隆出来的仓库里是没有它的。
    3: [("曲库 songs/（唱歌预录成品）", "songs", False, True)],
    4: [],
    5: [],
    6: [],
}


# ─────────────────────────────────────────
# 依赖安装
# ─────────────────────────────────────────

def _plan(groups: list[str], variant: str | None = None) -> list[tuple[list[str], dict]]:
    """把选中的组展开成 pip 调用列表。**同一个包清单只留一次。**

    去重是必须的：torch 同时挂在「记忆」和「语音」两个组下（见 GROUPS 里的注释——
    只勾语音的用户也要能起得来引擎），而勾了 1+2 的用户会把两组都选上，
    不去重就会 `pip install torch` 跑两遍。第二遍是空操作，但多花一次联网解析时间，
    而且输出里会出现两行一模一样的命令，看着像出了什么事。

    torch 走 GROUPS[g]["torch"] 这个开关而不是写死的 extra 清单，两个理由：
    ① 档位按本机设备选（T54-A）；② 它**必须单独一条 pip 命令**——cu / xpu 档带
       --no-deps（为保住 numpy<1.24），混进 jieba/transformers 那条会把它们的
       依赖一起掐掉，那是「装完不能用」。
    """
    if variant is None and any(GROUPS[g].get("torch") for g in groups):
        variant = torch_variant_name(None)
    spec = torch_pip_spec(variant) if variant in TORCH_VARIANTS else None
    plan = []
    seen: set[tuple] = set()
    for name in groups:
        g = GROUPS[name]
        items = ([(g["pkgs"], {})] if g["pkgs"] else []) + list(g.get("extra", []))
        if g.get("torch") and spec:
            items.append(spec)
        for pkgs, kw in items:
            key = (tuple(pkgs), tuple(sorted(kw.items())))
            if key in seen:
                continue
            seen.add(key)
            plan.append((pkgs, kw))
    return plan


def _install(pkgs: list[str], kwargs: dict) -> bool:
    # pip 的 wheel/http 缓存默认堆在用户目录下的 pip 缓存——装 torch 全家桶
    # 就是好几个 G。缓存是纯落盘位置，直接跟依赖根走。
    # （包本身装哪儿由 Python 前缀决定：**不要**改用 `pip --target`——那会让 import
    #   全部失效；要挪整份 site-packages 得用 tools/依赖迁移.py 的目录联接方案。）
    cmd = [sys.executable, "-m", "pip", "install", *pkgs,
           "--cache-dir", str(_deps_target("cache/pip"))]
    for flag, val in kwargs.items():
        if val is True:
            # 裸开关（--no-deps）：值本身就是 True，拼成 "--no-deps True" pip 会拒。
            cmd.append(f"--{flag}")
            continue
        cmd.append(f"--{flag}")
        cmd.append(str(val))
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd).returncode == 0


def _render_pip_flags(kwargs: dict) -> str:
    """把 pip 参数渲染到预览里。**torch 档位的全部信息就在这些参数上**
    （index-url 决定装哪个轮子、--no-deps 决定 numpy 不被拖走），
    不打印出来，用户看到的就只是 "torch" 三个字母——等于没落地。"""
    if not kwargs:
        return ""
    parts = [f"--{k}" if v is True else f"--{k} {v}" for k, v in kwargs.items()]
    return "   (" + " ".join(parts) + ")"


def install_groups(groups: list[str], dry_run: bool = False,
                   variant: str | None = None) -> bool:
    """按组安装依赖。返回是否全部成功。soft 组（jieba-fast）失败仅警告不置失败。

    variant: torch 档位。None / "auto" → 探测本机设备自动选；cuda / xpu / cpu 人工指定。
    """
    if not groups:
        print("没有选择任何组")
        return False
    wants_torch = any(GROUPS[g].get("torch") for g in groups)
    # [!] torchaudio ≠ torch 的同义词：它是**语音侧**的库（gpt-sovits/api_v2.py 顶层
    #   import torchaudio），而「记忆」组只要 torch 跑 BGE 向量/精排。
    #   原先这里用 wants_torch 判，于是只勾第 1 项的用户也被装上语音侧的 torchaudio
    #   ——2026-11-14 用户实机报告"选择只 1 好像顺带也选择 2"，复现确认。
    wants_audio = any(GROUPS[g].get("audio") for g in groups)
    info = detect_torch_variant(variant) if wants_torch else None
    vname = info["variant"] if info else None
    plan = _plan(groups, vname)
    soft = [p for g in groups for p in GROUPS[g].get("soft", [])]
    soft_torch = torch_soft_spec(vname) if (wants_torch and wants_audio) else None
    if info:
        print(f"\n[·] torch 档位：{info['label']}")
        for line in info["evidence"]:
            print(f"       · {line}")
        print_torch_rules(vname)
    print(f"\n[·] 将安装 {len(groups)} 组、{sum(len(p) for p, _ in plan)} 个包"
          + (f" + {len(soft)} 个加速可选包" if soft else "") + "：")
    for pkgs, kw in plan:
        print(f"  - {' '.join(pkgs)}{_render_pip_flags(kw)}")
    if soft:
        print(f"  - {' '.join(soft)}（可选——失败仅警告，GPT-SoVITS 自动回退纯 jieba）")
    if soft_torch:
        print(f"  - {' '.join(soft_torch[0])}{_render_pip_flags(soft_torch[1])}"
              "（可选——只有「发语音」用得到；失败仅警告）")
    if dry_run:
        print("\n（--dry-run 预览模式，未实际安装）")
        return True
    ok = True
    for pkgs, kw in plan:
        if not _install(pkgs, kw):
            ok = False
            print(f"  [!] 安装失败: {pkgs}——请手动重试")
    for pkgs in soft:
        if not _install(pkgs, {}):
            print("  [i] jieba-fast 未装成功——如需加速请先装 VS2022 Build Tools（C++ 桌面开发）后重试；"
                  "不装也能用（语音自动回退纯 Python jieba，见 gpt-sovits/start_api_patched.py）")
    if soft_torch and not _install(*soft_torch):
        print("  [i] torchaudio 未装成功（cu 档是 dev 轮子，上游可能已删除）——"
              "GPT-SoVITS / 降噪缺 import torchaudio；torch 本体不受影响，糖糖照常启动。")
    print("\n[√] 依赖安装完成" if ok else "\n[!] 部分安装失败，请检查上方输出")
    return ok


# ─────────────────────────────────────────
# 安装向导（环境/选择/目录/配置）
# ─────────────────────────────────────────

def check_environment() -> None:
    print("━━━ 环境检测 ━━━")
    v = sys.version_info
    py_ok = v[:2] == (3, 10)
    print(f"  Python: {v.major}.{v.minor}.{v.micro}{' [√] 符合建议（3.10）' if py_ok else '（建议 3.10，其他版本可能缺依赖兼容性）'}")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        # 项目自带的**共享库版**（tools/快速安装ffmpeg.py 装到依赖根下）。
        # 系统 PATH 的改动只对之后新启动的进程生效，而安装器自己往往就是那个
        # 「还没重启」的进程 —— 不按落盘位置再找一遍，就会把「已经装好」报成
        # 「未找到」，用户去重装一遍，还是同一个结果。
        _cand = 依赖位置.get_deps_root() / "ffmpeg" / "bin" / (
            "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if _cand.exists():
            ffmpeg = str(_cand)
    if ffmpeg:
        print(f"  FFmpeg: [√] {ffmpeg}")
    else:
        print("  FFmpeg: [×] 未找到——运行 python tools/快速安装ffmpeg.py "
              "（装共享库版到项目依赖目录并挂 PATH）")
    # MSVC cl.exe —— 判据走 agent/paths.find_msvc_cl()（体检用同一份，避免两份漂移）。
    # 只有 jieba-fast 需要它编译，而且是 soft 组：装不上只降级不阻断。
    cl = paths.find_msvc_cl()
    if cl:
        print(f"  MSVC cl.exe: [√] {cl}")
    else:
        print("  MSVC cl.exe: [!] 未找到 VS2022 C++ 工具链。")
        print("     jieba-fast 装不上 → 回退纯 Python jieba"
              "（语音仍可启动；实测 450 字 0.21ms → 0.56ms，对合成总耗时无感）")
        print("     装法：VS2022 Build Tools → 勾选「使用 C++ 的桌面开发」（含 Windows SDK），重开终端再装。")
    free_gb = shutil.disk_usage(BASE).free / 1e9
    print(f"  磁盘剩余: {free_gb:.1f} GB")
    if free_gb < 10:
        print("  [!] 磁盘空间紧张（<10GB），安装语音可能失败")


def check_prerequisites() -> None:
    """必装前置——不能等装完才说。

    SnowLuma 是糖糖连 QQ 所需的协议端，属第三方项目：其 EULA 第 5.4 条禁止
    「将其并入第三方安装包」与「通过自动化脚本部署」，所以安装器不能随包发、
    也不能代下，只能提前说清楚，让用户在我们下模型的这段时间里去准备。
    """
    print("\n━━━ 必装前置 ━━━")
    installed = sorted((BASE / "SnowLuma").glob("SnowLuma-v*"))
    if installed:
        print(f"  [√] SnowLuma: {installed[-1].name}")
    else:
        print("  [×] SnowLuma（QQ 协议端）——糖糖靠它连上 QQ，本机还没有")
        print("     它不属于本项目，许可协议也不允许随包分发或由安装器代下，")
        print("     需要你手动下载一次（一次性，约 100MB）：")
        print("       1. 打开 https://github.com/SnowLuma/SnowLuma/releases")
        print("       2. 下载 Windows x64 版（形如 SnowLuma-vX.Y.Z-win-x64.zip）")
        print("          [!] 别下带 -lite 的那个包——它不带 Node.js，解压了也起不来")
        print(f"       3. 解压到：{BASE / 'SnowLuma'}")
        print("          （解压后应出现 SnowLuma-vX.Y.Z-win-x64/ 文件夹）")
        print("     也可以用任意其他 OneBot 11 反向 WebSocket 实现替代。")
        print("     [i] 现在就可以去下——下面装依赖/下模型要等一会儿，正好并行。")


def choose_features(default: list[int] | None = None) -> list[int]:
    """default：发布版本的推荐组合（回车即采用，用户可另行输入覆盖）"""
    print("\n━━━ 功能选择（可多选）━━━")
    for i, (name, desc, _) in enumerate(FEATURES, 1):
        mark = " *" if default and (i - 1) in default else ""
        print(f"    {i}. {name} —— {desc}{mark}")
    print("    0. 全部安装")
    hint = "回车=采用 * 推荐" if default else "回车=跳过"
    try:
        raw = input(f"  输入编号（逗号分隔，如 1,5,6；{hint}）> ").strip()
    except (EOFError, KeyboardInterrupt):
        return list(default or [])
    if raw in ("0", "全部"):
        return list(range(len(FEATURES)))
    if not raw:
        return list(default or [])
    try:
        sel = [int(x) for x in raw.replace("，", ",").split(",") if x.strip()]
        return [i - 1 for i in sel if 1 <= i <= len(FEATURES)]
    except ValueError:
        print("  [!] 输入无效，按跳过处理")
        return []


def _model_ready(path: Path, is_model: bool) -> bool:
    """这一项就位了吗。`is_model=True` 时要求真的看到 `config.json`。

    [!] 原先判的是**父目录**（`models/BAAI/`）存不存在。于是用户跳过了
    2.2G 的 reranker（`_ask_optional` 答 y），安装完成页照样打印
    `[√] models/BAAI/（BGE 向量模型 + bge-reranker-v2-m3）`——用户以为重排在跑，
    启动后却看到「Reranker 模型未下载」。（Code Review A-5：两句话互相打脸。）
    顺带一提，那个"父目录"判据也认不下 `bge-small-zh-v1___5` 这种 modelscope
    把点号转义后的落点，而 `_fetch_modelscope` 是认的。
    """
    if not path.exists():
        return False
    if not is_model:
        return True
    if path.is_file():
        return True
    return (path / "config.json").exists() or any(path.glob("*/config.json"))


def check_directories(choices: list[int]) -> None:
    """在模型获取之后跑——所以「缺失」意味着下载没成功，而不是要用户自己摆"""
    print("\n━━━ 目录/服务检测 ━━━")
    for i in choices:
        for name, rel, is_model, is_install in DIR_CHECKS.get(i, []):
            path = _deps_path(rel) if is_install else BASE / rel
            if _model_ready(path, is_model):
                print(f"  [√] {name}")
            else:
                print(f"  [!] {name}——尚未就位（上一步下载可能中断，重跑本工具可续传）")
    if 2 in choices:  # 识图 → 检查 Ollama
        import urllib.request
        try:
            with urllib.request.urlopen("http://127.0.0.1:11434", timeout=2):
                print("  [√] Ollama 服务（11434 端口）")
        except Exception:
            print("  [×] Ollama 未运行——本地识图不可用；云端 qwen-vl 会兜底（无需处理）")
# ─────────────────────────────────────────
# 模型获取（自动下载 + 自动落位）
# ─────────────────────────────────────────
# 2026-09-18：发布三版本（文字/识图/完整）后新增。此前模型要么手敲命令、
# 要么自己解压到指定子目录——「下载即用」就卡在这一步。本层把落位规则
# 写死在代码里，用户只需双击 安装糖糖.bat。

RELEASE_REPO = "downtown20/tangtang-qq-bot"
# 扫全部 Release 汇总附件——语音分卷可能挂在任意一个 Release 下，
# 只查 /releases/latest 会漏
RELEASE_API = f"https://api.github.com/repos/{RELEASE_REPO}/releases?per_page=50"

# 默认预勾选的功能（2026-09-18 主人拍板）：
#   0 聊天+高质量记忆 · 3 唱歌 · 5 图形控制台
# 相当于原来的「文字版」。重活（语音 6G / 识图 2.5G）默认不勾——
# 别替用户决定下几个 G，让他自己选。
DEFAULT_FEATURES = [0, 3, 5]

# 语音识别模型（听懂别人的语音消息）。
# [!] 与 agent/asr.py 的 _SENSE_VOICE_MODEL / _SENSE_VOICE_URL 必须一致——
#   由 tests/test_installer_wiring.py 的契约断言机械保证，改一处会红。
# 为什么要预取：代码里是「首次遇到语音消息才懒加载下载」，可那一刻用户正在
# 对话中被卡住等 233M 下载。安装时先拿掉，才叫「下载的功能直接能用」。
ASR_MODEL_NAME = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
ASR_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    f"{ASR_MODEL_NAME}.tar.bz2"
)

# 功能下标 → 模型清单
#   (显示名, 目标位置(相对项目根；空=特殊处理), 获取方式, 参数, 体积, 模式)
#   模式: "auto" = 直接下载（失败仅告警）
#         "ask"  = 下载前询问——体积大且缺了只降级不影响可用（如 2.2G 重排序）
MODELS = {
    0: [
        ("BGE 中文语义向量模型——记忆/知识库语义搜索的根基",
         "models/BAAI/bge-small-zh-v1.5", "modelscope",
         {"repo": "BAAI/bge-small-zh-v1.5"}, "184M", "auto"),
        ("BGE 重排序模型——混合检索精排；缺则召回质量下降，不影响聊天",
         "models/BAAI/bge-reranker-v2-m3", "modelscope",
         {"repo": "BAAI/bge-reranker-v2-m3"}, "2.2G", "ask"),
        # [!] 2026-09-20 补：这个模型 agent/mood_tracker.py 一直在用（情绪趋势 →
        #   心理陪伴的主动关心），tools/体检.py 也查它，**但从来没进过这张下载表**。
        #   结果每台新机器启动都打一行「[!] 未找到 StructBERT 模型」，而用户
        #   翻遍安装器也找不到哪儿能补——因为确实没有。这就是「日志说缺东西
        #   但那条路根本不存在」的典型。落点与 mood_tracker.MODEL_DIRS[1] 一致。
        ("StructBERT 中文情绪模型——心理陪伴场景的情绪趋势分析",
         "models/iic/nlp_structbert_sentiment-classification_chinese-base", "modelscope",
         {"repo": "iic/nlp_structbert_sentiment-classification_chinese-base"}, "390M", "ask"),
    ],
    1: [
        # 2026-10-28 主人拍板：GPT-SoVITS **不再自动下载**。
        #   · 私有部分（米雪儿声线 229M + 情绪参考音 8M）随发布包自带，见 准备发布.py 的 4b 步
        #   · 公开底模 3.7G 由用户按 docs/用户手册/语音模型自行配置.md 自行放置
        # 为什么不做自动下载：第三方仓库的版本我们锁不死，装到一半对不上会得到
        # 「看起来装好了但起不来」的语音服务——比明确要用户自己下更糟。
        ("语音识别模型——听懂别人发的语音消息",
         "asr_models", "tarbz2",
         {"url": ASR_MODEL_URL, "expect": ASR_MODEL_NAME}, "233M", "auto"),
    ],
    2: [
        ("MiniCPM-V 本地识图模型——经 Ollama 调用（不装则用云端 qwen-vl）",
         "", "ollama", {"model": "minicpm-v"}, "2.5G", "auto"),
    ],
    # 唱歌无下载项（2026-09-18）：41 首预录成品已随版本包自带，播放走标准库
    # （agent/songs.py 只 import random/logging/pathlib），不需要任何模型或额外包。
}

# 子进程执行 modelscope 下载——刚 pip 装上的包当前解释器看不见，必须新起进程
_MODELSCOPE_SNIPPET = (
    "import sys\n"
    "from modelscope import snapshot_download\n"
    "snapshot_download(sys.argv[1], cache_dir=sys.argv[2])\n"
)


def _sizeof(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.2f}GB"


def _download_file(url: str, dest: Path, label: str) -> bool:
    """HTTP 下载（断点续传）。失败返回 False，不抛异常——由调用方决定是否致命。"""
    import urllib.request
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    done = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": "tangtang-installer"})
    if done:
        req.add_header("Range", f"bytes={done}-")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if done and getattr(resp, "status", 200) != 206:
                done = 0          # 服务端不支持断点——从头来
            total = done + int(resp.headers.get("Content-Length") or 0)
            print(f"    [↓] {label}" + (f"（{_sizeof(total)}）" if total else ""))
            with open(part, "wb" if done == 0 else "ab") as f:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        print(f"\r      {done * 100 // total}%  ({_sizeof(done)}/{_sizeof(total)})",
                              end="", flush=True)
            print()
    except Exception as e:
        print(f"\n    [!] 下载中断（重跑本工具会自动续传）: {e}")
        return False
    if dest.exists():
        dest.unlink()
    part.replace(dest)
    return True


def _fetch_modelscope(repo: str, target: Path, label: str) -> bool:
    """modelscope 下载。cache_dir 的 <命名空间>/<模型名> 布局天然就是代码期望的落点，无需搬运。"""
    if (target / "config.json").exists():
        print(f"    [√] 已就绪，跳过：{label}")
        return True
    print(f"    [↓] {label}（modelscope）")
    proc = subprocess.run(
        [sys.executable, "-c", _MODELSCOPE_SNIPPET, repo, str(_deps_target("models"))],
        cwd=str(BASE))
    if proc.returncode != 0 or not (target / "config.json").exists():
        # modelscope 某些版本把点号转义成 ___，embeddings.py 两种都认
        alt = target.with_name(target.name.replace(".", "___"))
        if (alt / "config.json").exists():
            print(f"    [√] 已就绪：{_display(alt)}")
            return True
        print("    [!] 下载未完成——请检查网络后重跑；也可稍后手动补装")
        return False
    print(f"    [√] 已放置：{_display(target)}")
    return True


def _release_assets() -> dict:
    """全部 Release 的 附件名 → 下载地址（三个版本的附件合并成一张表）"""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(RELEASE_API, timeout=20) as r:
            releases = json.load(r)
    except Exception as e:
        print(f"    [!] 读取 Release 附件列表失败: {e}")
        return {}
    out = {}
    for rel in releases if isinstance(releases, list) else []:
        for a in rel.get("assets", []):
            out[a["name"]] = a["browser_download_url"]
    return out


def _extract_tar_safely(tar, target: Path) -> None:
    """安全解压：拒绝绝对路径、`..` 穿越、符号链接与设备文件。

    [!] Python 3.10 的 `extractall()` **没有 `filter` 参数**（3.12 才加），
    默认完全信任压缩包内容。模型是从 GitHub Releases 拉的 tar.bz2——
    处于中间人环境或上游 Release 被替换时，一个含 `../../启动/x.bat`
    或符号链接条目的小包就能一路写到目标目录之外。
    2026-09-19 独立安全审计把这条列为唯一带本地代码执行潜力的面。

    [!] 与 `agent/asr.py` 的同名函数**必须保持一致**——两处都是同一类压缩包，
    闸门 tests/test_release_sanitizer.py 会比对两份实现（改一处漏一处会红）。
    """
    base = Path(target).resolve()
    for member in tar.getmembers():
        if member.issym() or member.islnk() or member.isdev() or member.isfifo():
            raise ValueError(f"压缩包含非法条目（链接/设备文件）：{member.name}")
        dest = (base / member.name).resolve()
        if base != dest and base not in dest.parents:
            raise ValueError(f"压缩包含路径穿越条目：{member.name}")
    tar.extractall(target)


def _unzip_into(zip_path: Path, target: Path) -> None:
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(target)


def _fetch_release_asset(asset: str, target: Path, label: str) -> bool:
    assets = _release_assets()
    url = assets.get(asset)
    if not url:
        print(f"    [!] Release 里找不到附件 {asset}")
        return False
    tmp = _deps_target("temp_files") / asset
    if not _download_file(url, tmp, f"{label}（{asset}）"):
        return False
    print("    [~] 解压…")
    target.mkdir(parents=True, exist_ok=True)
    _unzip_into(tmp, target)
    tmp.unlink(missing_ok=True)
    print(f"    [√] 已放置：{_display(target)}")
    return True


def _fetch_release_parts(prefix: str, target: Path, label: str) -> bool:
    """分卷附件：全部下载后依次解压到同一目录（自动，无需人工排序）"""
    assets = _release_assets()
    parts = sorted(n for n in assets if n.startswith(prefix))
    if not parts:
        print(f"    [!] Release 里找不到 {prefix}* 分卷")
        return False
    print(f"    [i] 共 {len(parts)} 卷，逐卷下载解压（断点续传，中断可重跑）")
    tmpdir = _deps_target("temp_files") / "parts"
    for name in parts:
        tmp = tmpdir / name
        if not _download_file(assets[name], tmp, f"{label} {name}"):
            return False
        print("    [~] 解压…")
        target.mkdir(parents=True, exist_ok=True)
        _unzip_into(tmp, target)
        tmp.unlink(missing_ok=True)
    print(f"    [√] 已放置：{_display(target)}")
    return True


def _fetch_tarbz2(url: str, target: Path, expect: str, label: str) -> bool:
    """下 tar.bz2 并解压到 target——与 agent/asr.py 的落位规则一致。

    asr.py 解压时用 `expect`（顶层目录名）来找模型文件，所以这里也必须
    让它落到 target/<expect>/ 下。
    """
    if (target / expect).is_dir():
        print(f"    [√] 已就绪，跳过：{label}")
        return True
    tmp = _deps_target("temp_files") / f"{expect}.tar.bz2"
    if not _download_file(url, tmp, label):
        return False
    print("    [~] 解压…")
    target.mkdir(parents=True, exist_ok=True)
    try:
        import tarfile
        with tarfile.open(tmp, "r:bz2") as tar:
            _extract_tar_safely(tar, target)
    except Exception as e:
        print(f"    [!] 解压失败: {e}")
        tmp.unlink(missing_ok=True)
        return False
    tmp.unlink(missing_ok=True)
    if not (target / expect).is_dir():
        print(f"    [!] 解压完成但没找到 {expect}/——请检查压缩包结构")
        return False
    print(f"    [√] 已放置：{_display(target)}/{expect}")
    return True


def _fetch_ollama(model: str, label: str) -> bool:
    if not shutil.which("ollama"):
        print("    [!] 未检测到 Ollama——本地识图靠它跑，装不了就跳过这一步")
        print("      识图有两条路，任选一条即可用：")
        print("        · 云端：控制台把「识图方式」选云端，填阿里云千问 API Key（零下载，推荐先这么用）")
        print("        · 本地：装 Ollama（https://ollama.com/download）后重跑本工具，")
        print("                它会自动 pull 识图模型")
        return False
    print(f"    [↓] {label}（ollama pull {model}）")
    if subprocess.run(["ollama", "pull", model]).returncode != 0:
        print("    [!] Ollama 拉取失败——稍后可手动执行 ollama pull " + model)
        return False
    print("    [√] 本地识图模型就绪")
    return True


def _ask_optional(specs: list) -> set[str]:
    """下载前先问可选大模型——别让用户等完 2.2G 才后悔。

    非交互环境（无 stdin）按「不跳过」处理：宁可多下，也别静默删功能。
    """
    asks = [s for s in specs if s[5] == "ask"]
    if not asks:
        return set()
    print("\n  [!] 以下模型体积较大，但缺了只影响质量、不影响能不能用：")
    for name, _t, _k, _p, size, _m in asks:
        print(f"      · {name}  [{size}]")
    try:
        raw = input("    跳过它们以节省下载量？[y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        raw = ""
    if raw in ("y", "yes"):
        print("    → 已跳过（以后想要：重跑本工具即可补装）")
        return {s[0] for s in asks}
    print("    → 一并下载")
    return set()


def fetch_models(choices: list[int], dry_run: bool = False) -> list[str]:
    """按功能选择下载并落位模型。返回未能就位的模型名。"""
    specs = [s for i in choices for s in MODELS.get(i, [])]
    if not specs:
        return []
    print("\n━━━ 模型获取（自动下载并放到位，无需手动解压）━━━")
    skip = set() if dry_run else _ask_optional(specs)
    missing = []
    for name, target_rel, kind, params, size, _mode in specs:
        if name in skip:
            print(f"\n  · {name}  [{size}]  [>] 已跳过")
            continue
        print(f"\n  · {name}  [{size}]")
        if dry_run:
            print("    （--dry-run：未实际下载）")
            continue
        # 落盘位置从依赖根取（_deps_path 按顶层目录建一次目录联接）
        target = _deps_path(target_rel, link_back=True) if target_rel else None
        if kind == "modelscope":
            ok = _fetch_modelscope(params["repo"], target, name)
        elif kind == "release":
            ok = _fetch_release_asset(params["asset"], target, name)
        elif kind == "release_parts":
            ok = _fetch_release_parts(params["prefix"], target, name)
        elif kind == "tarbz2":
            ok = _fetch_tarbz2(params["url"], target, params["expect"], name)
        elif kind == "ollama":
            ok = _fetch_ollama(params["model"], name)
        else:
            ok = False
        if not ok:
            missing.append(name)
    return missing


def bootstrap_config(dry_run: bool) -> bool:
    """首次安装时由 config.example.yaml 生成 config.yaml。

    此前用户必须手动「复制 config.example.yaml 改名」——「下载即用」的又一处手工步骤。
    返回 True 表示本次新建了配置（后续接线无需再征求同意）。
    """
    cfg, example = BASE / "config.yaml", BASE / "config.example.yaml"
    if cfg.exists():
        return False
    if not example.is_file():
        # 教训 #30：检查写了却静默返回，等于没写——缺模板必须说出来。
        # 否则用户只会看到「装完了却起不来」，屏幕上没有任何线索指出缺的是这个文件。
        print("\n  [!] 找不到 config.example.yaml，无法自动生成 config.yaml——"
              "请从仓库或发布包中取回该模板")
        return False
    print("\n━━━ 生成配置 ━━━")
    if dry_run:
        print("  （--dry-run：未实际生成 config.yaml）")
        return True
    try:
        shutil.copy2(example, cfg)
        print("  [√] 已生成 config.yaml")
        return True
    except OSError as e:
        print(f"  [!] 自动生成失败: {e}——请手动复制 config.example.yaml 为 config.yaml")
        return False


# 功能下标 → 需要打开的配置开关（2026-09-18 接线契约）
#   voice.enabled     说（TTS，糖糖发语音条）
#   voice.asr_enabled 听（别人发的语音转文字）
#   voice.provider    决定启动不启动本地 TTS 服务（agent/handler.py: _is_full_mode）
#   llm.vision.enabled 识图总开关
#
# [!] 这张表是「勾了就一定能用」的实现。历史上安装器只会把功能**关掉**、从不会打开，
#   导致用户勾了语音、6G 模型下完、启动后糖糖一声不吭。
VOICE_FEATURE = 1
VISION_FEATURE = 2


def _config_switches(choices: list[int]) -> dict[tuple[str, ...], object]:
    """把功能勾选翻译成配置开关（含「没勾就关掉」的反向接线）。

    键是配置路径元组——`llm.vision.enabled` 三段，不能按点号 split 后两段解包。
    """
    has_voice = VOICE_FEATURE in choices
    has_vision = VISION_FEATURE in choices
    return {
        ("voice", "enabled"): has_voice,
        ("voice", "asr_enabled"): has_voice,   # 听与说同属「语音」功能
        # 没装引擎就切走 provider——否则每次启动都会去拉一个不存在的本地 TTS 服务
        ("voice", "provider"): "gpt-sovits" if has_voice else "edge-tts",
        ("llm", "vision", "enabled"): has_vision,
    }


def _cfg_node(data: dict, path: tuple[str, ...]) -> dict:
    """按路径逐层 setdefault，返回叶子所在的字典"""
    node = data
    for key in path[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    return node


def wire_config(choices: list[int], fresh: bool, dry_run: bool) -> None:
    """按功能勾选把配置开关拨到位——勾了就一定能用，没勾就干净地关掉。

    刚生成的配置直接改；已有配置先给用户看要改什么再问——不静默改别人的设置。
    """
    cfg = BASE / "config.yaml"
    if not cfg.exists():
        return
    print("\n━━━ 配置接线（勾选的功能开箱即用）━━━")
    try:
        import yaml
    except ImportError:
        print("  [!] 缺少 pyyaml，跳过接线——请自行确认 config.yaml 里的功能开关")
        return
    want = _config_switches(choices)
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    changes = []
    for path, value in want.items():
        node = _cfg_node(data, path)
        field = path[-1]
        if node.get(field) != value:
            changes.append((".".join(path), node.get(field), value))
            node[field] = value
    if not changes:
        print("  [√] 开关已与本次选择一致，无需调整")
        return
    print("  将修改：")
    for key, old, new in changes:
        print(f"    · {key}: {old} → {new}")
    if dry_run:
        print("  （--dry-run：未实际写入）")
        return
    if not fresh:
        try:
            ans = input("  是否按本次选择调整配置？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans not in ("y", "yes"):
            print("  [>] 已跳过——配置保持原样（对应功能可能仍不可用）")
            return
    bak = cfg.with_name(f"config.yaml.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(cfg, bak)
    cfg.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    print(f"  [√] 已写入（原配置备份为 {bak.name}）")


def write_manifest(choices: list[int], dry_run: bool = False) -> None:
    """把「这次勾了什么」落成机读记录（2026-09-20）。

    为什么需要：功能开关（config.yaml 的 voice.enabled 等）说的是**要不要用**，
    不是**装没装**。少了这一份记录，运行期就分不清「你没装语音」和
    「你装了语音但模型没下成」——两者在磁盘上都是「目录不存在」。
    主人这次看到的「一堆模型未找到的警告分不清属于哪个功能」，根子就在这。

    写失败**不阻断安装**（磁盘满/权限），只提示——没有这份清单，
    运行期退回「不知道」的说法，比装不上强。
    """
    import json
    from datetime import datetime as _dt
    names = [FEATURES[i][0] for i in sorted(set(choices)) if 0 <= i < len(FEATURES)]
    payload = {
        "version": 1,
        "choices": sorted(set(int(c) for c in choices)),
        "features": names,
        "written_at": _dt.now().isoformat(timespec="seconds"),
        "by": "tools/安装糖糖.py",
    }
    print("\n━━━ 记录安装清单 ━━━")
    if dry_run:
        print(f"  （--dry-run：未写入）将记下 {len(names)} 项：{'、'.join(names)}")
        return
    try:
        (BASE / "installed_features.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  [√] installed_features.json（{len(names)} 项）"
              f"——运行期据此判断「模型缺失」该不该提醒你")
    except OSError as e:
        print(f"  [!] 写入失败: {e}——不影响使用，只是运行期的提示会说不清属于哪个功能")


def _song_count() -> int:
    """曲库可识别多少首——与 agent/songs.py 的发现口径一致（两个来源），
    只数 audio/ 会漏掉 separation 完还没拷过去的那几首。

    原声要连 .mp3 一起认：开发机上是分离出来的无损 wav，
    发布包里是转码后的 mp3（同目录换后缀）。只认 wav 会在用户机器上少数 40 首。
    """
    songs = BASE / "songs"
    if not songs.is_dir():
        return 0
    audio = songs / "audio"
    stems = {p.stem for p in audio.glob("*.wav")}
    # 2026-11-14 修：上面那行原先只 glob `*.wav`，与本文档字符串（"原声要连 .mp3
    # 一起认"）和 agent/songs.py:64 的口径都不一致——发布包里 audio/ 是**转码后的 mp3**，
    # 于是用户机器上数出来 0 首，而生产端照样能播。数字对不上就是这么来的。
    stems |= {p.stem for p in audio.glob("*.mp3")}
    stems |= {p.stem.removesuffix("_FINAL")
              for p in (songs / "covers" / "separated").glob("*_FINAL.*")
              if p.suffix.lower() in (".wav", ".mp3")}
    return len(stems)


def report_ready(choices: list[int]) -> None:
    """装完告诉用户「现在能做什么、还差什么」——避免装完一脸茫然"""
    print("\n━━━ 本次装好的功能 ━━━")
    has_voice, has_vision = VOICE_FEATURE in choices, VISION_FEATURE in choices
    print(f"  [√] 聊天 + 记忆{'（含语义检索）' if 0 in choices else ''}")
    if 3 in choices:
        print(f"  [√] 唱歌（曲库 {_song_count()} 首，点歌即播）")
    if 5 in choices:
        print("  [√] 图形控制台")
    print(f"  {'[√]' if has_voice else '[ ]'} 语音：听懂语音消息 + 糖糖发语音条"
          + ("" if has_voice else "（未勾选）"))
    print(f"  {'[√]' if has_vision else '[ ]'} 识图" + ("" if has_vision else "（未勾选）"))
    todo = []
    if not any((BASE / "SnowLuma").glob("SnowLuma-v*")):
        todo.append("SnowLuma 还没装（见开头的「必装前置」）——"
                    "去 github.com/SnowLuma/SnowLuma/releases 下 Windows x64 版解压到 SnowLuma/")
    if has_vision:
        todo.append("识图：控制台选「识图方式」——云端要填千问 API Key；"
                    "本地要装 Ollama（不装会自动走云端，不阻断）")
    if has_voice:
        todo.append("语音：第一次发语音时会启动 GPT-SoVITS 服务，首次启动较慢属正常")
    todo.append("填配置：机器人的 QQ 号、你的 QQ 号、模型 API Key（控制台「设置」页）")
    print("\n  还要做的事：")
    for i, t in enumerate(todo, 1):
        print(f"    {i}. {t}")

    not_chosen = [FEATURES[i][0] for i in range(len(FEATURES))
                  if i not in choices and i not in (6, 7)]
    if not_chosen:
        print(f"\n  本次没勾：{'、'.join(not_chosen)}")
        print("  以后想要 —— 重跑 安装糖糖.bat 勾上即可，模型/依赖会自动补齐，已装的不重来。")


def cmd_install(dry_run: bool, deps_root: str | None = None,
                assume_yes: bool = False, torch_variant: str | None = None) -> int:
    check_environment()
    check_prerequisites()
    _confirm_deps_root(deps_root, assume_yes)
    choices = choose_features(DEFAULT_FEATURES)
    if not choices:
        print("  未选择任何功能，跳过")
        return 0
    groups = []
    for i in choices:
        for g in FEATURES[i][2]:
            if g not in groups:
                groups.append(g)
    print(f"\n━━━ 安装依赖（{len(groups)} 组）━━━")
    for g in groups:
        print(f"  · {g} — {GROUPS[g]['desc']}")
    if not install_groups(groups, dry_run, torch_variant):
        return 1
    # 顺序有讲究：先建配置 → 下模型 → 接线 → 报"还差什么"。
    # 接线必须在模型之后——模型没就位就打开开关，等于让用户对着报错找原因。
    fresh = bootstrap_config(dry_run)
    missing = fetch_models(choices, dry_run)
    check_directories(choices)
    wire_config(choices, fresh, dry_run)
    write_manifest(choices, dry_run)
    if missing:
        print("\n[!]  以下模型未能就位，对应功能会降级或不可用：")
        for m in missing:
            print(f"    · {m}")
        print("   重跑本工具可自动续传（已下好的不会重复下载）")
    if not dry_run:
        report_ready(choices)
    print("\n━━━ 完成 ━━━")
    print("  接下来：")
    print("    1. 双击 desktop\\release\\小糖糖桌面端\\小糖糖桌面端.exe → 在「设置」页填：机器人的 QQ 号、你的 QQ 号、模型 API Key")
    print("    2. 桌面端里启动 SnowLuma → 面板里注入并配好连接 → 启动糖糖")
    print("  （换机搬家：本工具菜单 2/3 打包/解包记忆快照）")
    return 0


# ─────────────────────────────────────────
# 打包项目（生成分享部署包）
# ─────────────────────────────────────────

# 打包时排除的目录名/文件名（任何层级）
_EXCLUDE_DIRS = {"__pycache__", ".git", "logs", "voice_cache", "generated_images",
                 "share_images", "audio", "venv",
                 # 2026-10-15 T55-F：运行时状态与日志收进专目录，整目录排除
                 "状态", "日志"}
_EXCLUDE_SUFFIXES = (".pyc", ".log", ".bak", ".previous")
_EXCLUDE_NAMES = ("memory.db", "memory.db-wal", "memory.db-shm", ".extraction_state.json")


def _collect_include_roots(choices: list[int]) -> list[Path]:
    """按功能选择计算要打包的根级项"""
    roots = ["agent", "main.py", "config.yaml", ".env",
             "role_card.md", "role_card_murasame.md", "role_card_michele.md",
             "scenarios", "knowledge", "SnowLuma", "onebot",
             "stickers", "stickers_cg", "stickers_murasame", "stickers_michele",
             "tools", "安装糖糖.bat", "运维", "requirements.txt", ".stignore"]
    opt_roots = {
        0: ["models/BAAI"],            # 聊天+记忆 → BGE 向量模型
        1: ["gpt-sovits"],             # 语音
        2: ["models/minicpm-v"],       # 识图
    }
    for i in choices:
        for r in opt_roots.get(i, []):
            if (BASE / r).exists():
                roots.append(r)
    return [BASE / r for r in roots]


def _add_path(zf: zipfile.ZipFile, src: Path):
    """把文件/目录加入 zip（跳过排除项）"""
    if src.is_file():
        if src.name in _EXCLUDE_NAMES or src.name.endswith(_EXCLUDE_SUFFIXES) \
                or ".sync-conflict-" in src.name:
            return
        zf.write(src, src.relative_to(BASE).as_posix())
        return
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(BASE)
        if any(part in _EXCLUDE_DIRS for part in rel.parts):
            continue
        if p.is_dir():
            continue
        if p.name in _EXCLUDE_NAMES or p.name.endswith(_EXCLUDE_SUFFIXES) \
                or ".sync-conflict-" in p.name:
            continue
        zf.write(p, rel.as_posix())


def cmd_package() -> int:
    print("━━━ 打包项目（生成分享部署包）━━━")
    print("  勾选目标机器需要的功能——决定带哪些可选目录（语音/识图的模型可以不带）")
    choices = choose_features()
    if not choices:
        print("  未选择功能，跳过")
        return 0

    # 数据库快照：可选携带（不带则目标机解压后自行 打包/解包）
    snap = None
    snaps = sorted((BASE / "memory_sync").glob("memory-*.db"),
                   key=lambda p: p.stat().st_mtime, reverse=True) if (BASE / "memory_sync").exists() else []
    if snaps:
        raw = input(f"\n  包含记忆快照 {snaps[0].name}（{snaps[0].stat().st_size / 1e6:.0f}MB）？[Y/n] ").strip().lower()
        if raw not in ("n", "no"):
            snap = snaps[0]
            print("  [√] 将包含记忆快照")

    roots = _collect_include_roots(choices)
    print(f"\n  [·] 收集 {len(roots)} 个根项：")
    for r in roots:
        size = sum(p.stat().st_size for p in r.rglob("*") if p.is_file()) / 1e9 if r.is_dir() \
            else r.stat().st_size / 1e9
        print(f"    · {r.name if r != BASE else BASE}（{size:.1f} GB）" if size > 0.1 else f"    · {r.name}")
    print("  （已排除: __pycache__/日志/冲突文件/运行时产物/voice_cache 等）")

    out = BASE.parent / f"小糖糖部署包-{datetime.now().strftime('%Y%m%d-%H%M')}.zip"
    print(f"\n  [~] 正在生成 {out.name}（大目录可能需要几分钟）…")
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for src in roots:
            _add_path(zf, src)
            count += 1
        if snap:
            zf.write(snap, f"memory_sync/{snap.name}")

    size_gb = out.stat().st_size / 1e9
    print(f"  [√] 部署包已生成: {out}")
    print(f"     大小 {size_gb:.1f} GB，{count} 个根项")
    print()
    print("  目标机器解压后：")
    print("    1. 运行 安装糖糖.bat → 菜单 1 安装依赖")
    if snap:
        print("    2. 菜单 3 解包记忆（应用记忆快照）")
    print("    3. 启动桌面端 → 启动 SnowLuma → 面板里配好连接 → 启动糖糖")
    print("  [!] 部署包含 .env（API 密钥），请勿外传")
    return 0


# ─────────────────────────────────────────
# 入口
# ─────────────────────────────────────────

def cmd_release() -> int:
    """发布项目——清理个人数据，生成分享文件夹（tools/准备发布.py）"""
    script = BASE / "tools" / "准备发布.py"
    if not script.exists():
        print("  [×] 未找到 tools/准备发布.py")
        return 1
    print("  [·] 准备发布包（清理个人数据，输出到 项目上级目录/小糖糖-发布/）…")
    return subprocess.run([sys.executable, str(script)], cwd=str(BASE)).returncode


def cmd_pack(if_newer: bool) -> int:
    return _sync.cmd_pack(if_newer)


def cmd_unpack(machine: str | None) -> int:
    return _sync.cmd_unpack(machine)


def cmd_check() -> int:
    check_environment()
    print()
    return _sync.cmd_check()


def cmd_health() -> int:
    """体检——换机功能对比（[×]=缺失 [!]=留意；两台机器各跑一遍逐行对照）"""
    script = BASE / "tools" / "体检.py"
    if not script.exists():
        print(f"  [×] 未找到 {script}")
        return 1
    print("  [·] 运行体检…")
    return subprocess.run([sys.executable, str(script)], cwd=str(BASE)).returncode


def main() -> int:
    raw = [a for a in sys.argv[1:] if a]
    dry_run = "--dry-run" in raw
    # 安装位置开关：--deps-root PATH（或 --deps-root=PATH）直接落盘、不弹框；
    # --yes / -y 表示非交互——自动化场景不弹目录选择框，用已配置的位置。
    deps_root = None
    assume_yes = False
    # torch 档位开关：默认 auto（探测本机设备），cuda / xpu / cpu 人工覆盖。
    # 存在的意义：探测偶尔会判错（虚拟适配器、老卡、离线机器），要留一条明路，
    # 否则用户只能"装错就忍着"。
    torch_variant = None
    args = []
    i = 0
    while i < len(raw):
        a = raw[i]
        if a == "--deps-root" and i + 1 < len(raw):
            deps_root, i = raw[i + 1], i + 2
            continue
        if a.startswith("--deps-root="):
            deps_root, i = a.split("=", 1)[1], i + 1
            continue
        if a == "--torch-variant" and i + 1 < len(raw):
            torch_variant, i = raw[i + 1], i + 2
            continue
        if a.startswith("--torch-variant="):
            torch_variant, i = a.split("=", 1)[1], i + 1
            continue
        if a in ("--yes", "-y"):
            assume_yes, i = True, i + 1
            continue
        if a != "--dry-run":
            args.append(a)
        i += 1

    if not args:
        while True:
            print("小糖糖 安装/维护工具")
            print("   第一次用 → 选 1「安装依赖」：勾需要的功能，模型会自动下载并放到正确位置")
            print("   已经装过 → 直接双击 desktop\\release\\小糖糖桌面端\\小糖糖桌面端.exe")
            print()
            print("  1. 安装依赖 — 功能选择安装（含环境/目录检测、配置调整）")
            print("  2. 打包记忆 — 手动导出快照（自动打包失败/想立即切机时用；平时自动进行）")
            print("  3. 解包记忆 — 手动应用快照（自动解包被分叉拦截/需指定机器时用；平时自动进行）")
            print("  4. 打包项目 — 生成部署 zip（给自己笔记本用，按功能瘦身）")
            print("  5. 发布项目 — 清理个人数据后生成分享文件夹（发给别人）")
            print("  6. 检查状态 — 环境 + 快照总览")
            print("  7. 体检 — 换机功能对比（[×]=缺失 [!]=留意，两台机器各跑一遍对照）")
            print("  0. 退出")
            try:
                raw = input("\n  选择 > ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if raw == "1":
                cmd_install(dry_run, deps_root, assume_yes, torch_variant)
            elif raw == "2":
                _sync.cmd_pack(True)
            elif raw == "3":
                _sync.cmd_unpack(None)
            elif raw == "4":
                cmd_package()
            elif raw == "5":
                cmd_release()
            elif raw == "6":
                cmd_check()
            elif raw == "7":
                cmd_health()
            else:
                break
            try:
                input("\n  （回车返回菜单…）")
            except (EOFError, KeyboardInterrupt):
                break
        return 0

    cmd = args[0]
    if cmd in ("安装", "install"):
        return cmd_install(dry_run, deps_root, assume_yes, torch_variant)
    if cmd in ("打包", "pack"):
        return _sync.cmd_pack("--if-newer" in args or dry_run)
    if cmd in ("解包", "unpack"):
        machine = args[1] if len(args) > 1 else None
        return _sync.cmd_unpack(machine)
    if cmd in ("项目", "package", "share"):
        return cmd_package()
    if cmd in ("发布", "release"):
        return cmd_release()
    if cmd in ("检查", "check"):
        return cmd_check()
    if cmd in ("体检", "health"):
        return cmd_health()
    # 兼容旧用法：直接指定依赖组
    return 0 if install_groups([cmd], dry_run, torch_variant) else 1


if __name__ == "__main__":
    sys.exit(main())
