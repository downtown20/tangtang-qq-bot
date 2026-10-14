"""算力探测 —— 「这台机器能拿什么来算」的单一事实源（2026-10-14 T52）。

为什么要收成一处
────────────────
改造前，项目里有 **5 处各自判断** torch.cuda.is_available()（其中两处属于已停用的
语音克隆链，已随链路删除）：

    agent/reranker.py:117            选设备
    tools/denoise.py:52              选设备
    gpt-sovits/start_api_patched.py  改 tts_infer.yaml 的 device

本仓已经**四次**踩「同一条规则的多份拷贝各自演化」（T10 两个发布扫描器 / T12 两处
可用性判据 / T25 clean 的两份字符类 / T35 括号舞台说明 / T42 识图判据）。而现在要
多认一类设备 —— **Intel Arc 核显**（torch.xpu），散着改就是第五次。

两台机器的现实（2026-10-14 实测）
────────────────────────────────
· 台式机 <台式机>：RTX 5070 / sm_120 → CUDA。GPT-SoVITS 100 字 **5.4 秒**。
· 笔记本 <笔记本>：Intel Arc 130T 核显，**没有 CUDA** → 全部落到 CPU，
  GPT-SoVITS 50 字实测 **80.9 秒**（2 字也要 42.8 秒）—— 而 voice.py 的超时是 60 秒，
  所以长语音在笔记本上是**必失败**。装上 torch+xpu 后 torch.xpu.is_available()
  为真，本模块会自动把它排到 CPU 前面。

设备阶梯：**cuda → xpu → cpu**。
xpu 目前是 beta 质量（Intel 自家对 Arrow Lake-H 的 Windows 支持即标 beta），
所以本模块的定位是「**有就用，没有就安静退到下一档**」——不主动安装、不影响没有
Intel 显卡的机器、任何探测失败都不抛异常。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("糖糖.Devices")

#: 设备优先级。改这里就改了全局——不要在别处再写一遍这三档。
DEVICE_LADDER = ("cuda", "xpu", "cpu")

#: 各档位对外的中文名（日志/体检/控制台共用，避免三处各写一个说法）
DEVICE_LABELS = {
    "cuda": "NVIDIA CUDA",
    "xpu": "Intel XPU",
    "cpu": "CPU",
}


def _torch():
    """拿到 torch 模块；没装/导不进返回 None（**绝不抛**）。"""
    try:
        import torch
        return torch
    except Exception:
        return None


def has_cuda() -> bool:
    """NVIDIA 卡是否可用。"""
    torch = _torch()
    if torch is None:
        return False
    try:
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def has_xpu() -> bool:
    """Intel 显卡（Arc / 核显）是否可用。

    torch.xpu 在没有 XPU 支持的 torch 构建里**根本不存在**，在装了但没驱动的
    机器上 is_available() 返回 False —— 两种情况都得安静地当作「没有」。
    """
    torch = _torch()
    if torch is None:
        return False
    xpu = getattr(torch, "xpu", None)
    if xpu is None:
        return False
    try:
        return bool(xpu.is_available())
    except Exception:
        return False


def best_device() -> str:
    """本机该用的设备：cuda / xpu / cpu。**永不抛异常。**"""
    if has_cuda():
        return "cuda"
    if has_xpu():
        return "xpu"
    return "cpu"


def is_accelerated() -> bool:
    """是否跑在加速卡上（cuda 或 xpu）。CPU 上返回 False。"""
    return best_device() != "cpu"


def accelerator_name() -> str:
    """加速卡的型号名；CPU 上返回空串。

    这是**人看的**描述（日志、体检、控制台启动横幅），不参与任何判定。
    """
    torch = _torch()
    if torch is None:
        return ""
    try:
        dev = best_device()
        if dev == "cuda":
            return str(torch.cuda.get_device_name(0))
        if dev == "xpu":
            return str(torch.xpu.get_device_name(0))
    except Exception:
        pass
    return ""


def label() -> str:
    """一行短标签，例：CUDA / NVIDIA GeForce RTX 5070、CPU。"""
    dev = best_device()
    base = DEVICE_LABELS.get(dev, dev)
    name = accelerator_name()
    return f"{base} / {name}" if name else base


def summary() -> str:
    """多行诊断摘要（体检 / 启动日志用）。每一项都能独立失败，不连坐。"""
    torch = _torch()
    if torch is None:
        return "torch 未安装 —— 无法探测算力"
    lines = [
        f"torch: {getattr(torch, '__version__', '?')}",
        f"选中的设备: {label()}",
        f"CUDA 可用: {has_cuda()}",
        f"XPU 可用: {has_xpu()}",
    ]
    if has_cuda():
        try:
            lines.append(f"  CUDA 设备数: {torch.cuda.device_count()}")
            lines.append(f"  算力架构: {torch.cuda.get_arch_list()}")
        except Exception:
            pass
    return "\n".join(lines)
