"""安装时勾了什么——**机读记录**，让日志能按「你装的这个包」说话。

## 为什么必须落盘

安装器 `tools/安装糖糖.py` 把功能勾选翻译成 config.yaml 的开关
（`voice.enabled` / `llm.vision.enabled` …），但**开关只说明「要不要用」，
不说明「装没装」**。这两件事在那之前没有任何地方记着。

后果就是 2026-09-20 主人反馈的那一幕：他在安装器里只勾了 1/4/6 + 运维
（没有识图、没有语音），启动后却看到一串「某某模型未找到」的警告，
**分不清缺的是识图/语音的模型，还是 1/4/6 里的模型**。
对机器来说「没装」和「装坏了」长得一模一样——都是目录不存在。

现在安装器在接线之后写一份 `installed_features.json`，运行期所有
「某某没就位」的提示先问这里，才能说出人话：

    装了这个功能 → 「装的时候没下成，重跑 安装糖糖.bat 可续传」
    没装这个功能 → 「安装时没勾这一项；想用就重跑并勾上」← 这种情况**根本不该报警**
    读不到清单   → 老安装/开发机，退回原来的话术，不假装知道

## 读不到时怎么办（很重要）

`choices()` 返回 **None 表示「不知道」**，不是「没装」。调用方必须分开处理：
不知道就说不知道。把「不知道」当成「没装」会静默吞掉真实的故障——
而静默吞掉正是这个模块要解决的那类问题。

闸门：`tests/test_installed_manifest.py` 钉住安装器真的会写、且 FEATURE_NAMES
与安装器的 FEATURES 逐项同名（两边错位会让提示指向错误的功能）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from .paths import PROJECT_ROOT

logger = logging.getLogger("糖糖.Installed")

MANIFEST_NAME = "installed_features.json"
MANIFEST_VERSION = 1

# ⚠ 顺序必须与 tools/安装糖糖.py 的 FEATURES 逐项对应——
# 那上面的编号（「1./2./3.」）就是用户勾选时看到的东西，错位会把提示指到别的功能。
# 由 tests/test_installed_manifest.py 机械断言。
FEATURE_NAMES: list[str] = [
    "聊天+高质量记忆",      # 0 不勾则整个装不了，实际必选
    "发语音+听懂语音消息",   # 1
    "本地识图",             # 2
    "唱歌",                 # 3
    "读文档",               # 4
    # 2026-11-14：原 5「图形控制台」随 PySide6 下线（用户拍板），此处不留位。
    "测试工具",             # 5
    "运维工具",             # 6
]

# 具名常量——调用方写 `installed.FEAT_MEMORY` 而不是裸 `0`。
# 裸下标在跨文件处最容易错位，而错位会把提示指到**别的**功能上（比不提示更坏）。
FEAT_MEMORY = 0
FEAT_VOICE = 1
FEAT_VISION = 2
FEAT_SING = 3
FEAT_DOC = 4
# [!] 2026-11-14：`FEAT_CONSOLE = 5`（图形控制台）随 PySide6 一起下线，删掉。
#   原「测试工具」6 → 5、原「运维工具」7 → 6，与工具/安装糖糖.py 的 FEATURES 对齐。
#   0-4 不受影响；后移的两个都是"非模型"功能，不会让 log_missing 误报模型缺失。
FEAT_TESTS = 5
FEAT_OPS = 6

# 列表编号（1 起）——给用户看的，与控制台/安装器里的「第 N 项」一致
def feature_number(index: int) -> int:
    return index + 1


def feature_label(index: int) -> str:
    if 0 <= index < len(FEATURE_NAMES):
        return f"{feature_number(index)}. {FEATURE_NAMES[index]}"
    return f"第 {feature_number(index)} 项"


def manifest_path(root: Optional[Path] = None) -> Path:
    return (root or PROJECT_ROOT) / MANIFEST_NAME


# 缓存键 = (清单路径, mtime)。**路径必须在键里**——只按 mtime 缓存的话，
# 两个不同目录的清单只要 mtime 相同（测试里用 tmp_path 极易撞上，
# 或者同一秒内写下两份）就会互相串味：`load(rootB)` 拿到 `rootA` 的结果。
# 生产里只有 PROJECT_ROOT 一条路径，所以这个 bug 只有在测试里才现形——
# 而测试里现形的形态是"假绿"（断言通过但它验的不是自己想验的那份文件）。
_cache: dict = {"key": None, "data": None}


def load(root: Optional[Path] = None, *, refresh: bool = False) -> Optional[dict]:
    """读安装清单。**返回 None 表示「不知道」**，不是「什么都没装」。"""
    path = manifest_path(root)
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        return None
    if not refresh and _cache["key"] == key and _cache["data"] is not None:
        return _cache["data"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.debug("安装清单读不出来（按「不知道」处理）: %s", e)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("choices"), list):
        return None
    data["choices"] = [c for c in data["choices"] if isinstance(c, int)]
    _cache["key"], _cache["data"] = key, data
    return data


def choices(root: Optional[Path] = None) -> Optional[list[int]]:
    m = load(root)
    return None if m is None else list(m["choices"])


def has(index: int, root: Optional[Path] = None) -> Optional[bool]:
    """这项功能装了吗。**None = 不知道**（老安装 / 开发机 / 清单被删）。"""
    ch = choices(root)
    return None if ch is None else (index in ch)


def installed_summary(root: Optional[Path] = None) -> str:
    """一行话说明这台机器装了什么——给「连接自检」和体检用。"""
    ch = choices(root)
    if ch is None:
        return "读不到安装清单（老安装或开发机）——下面的提示按「不确定」处理"
    if not ch:
        return "安装清单里一项都没有"
    return "安装时勾了：" + "、".join(feature_label(i) for i in sorted(ch))


def missing_note(index: int, what: str, *, optional: bool = False,
                 root: Optional[Path] = None) -> str:
    """「{what} 没就位」该怎么说——**按安装清单分三种说法**。

    这是本模块的主要用途。返回的是**一整句可以直接进日志的话**，
    调用方只管前面加 level、后面不加尾巴。
    """
    state = has(index, root)
    label = feature_label(index)
    tail_optional = "（它是可选项，缺了只降级，不影响聊天）" if optional else ""
    if state is True:
        return (f"{what} 没就位——它属于「{label}」，是你**装过**的功能，"
                f"多半是当时那份没下成{tail_optional}。"
                f"重跑 安装糖糖.bat 勾上同一项可续传（已下好的不会重来）。")
    if state is False:
        return (f"{what} 没就位——安装时**没有勾**「{label}」，这是正常的"
                f"{tail_optional}。要用它：重跑 安装糖糖.bat 勾上这一项。")
    return (f"{what} 没就位——读不到本次安装的功能清单，无法判断是你没装它、"
            f"还是装了没下成{tail_optional}。"
            f"它属于「{label}」；不想要这个功能可以不理它。")


def should_warn(index: int, root: Optional[Path] = None) -> bool:
    """这项功能「没就位」值不值得进日志。

    没勾过的功能**一律不报警**——那正是主人这次反馈里那堆噪音的来源：
    一台没装语音的机器不该每次启动都提醒它语音模型不在。
    读不到清单时返回 True（宁可多说一句，也不静默吞掉真实故障）。
    """
    return has(index, root) is not False


def log_missing(logger, index: int, what: str, *, optional: bool = False,
                root: Optional[Path] = None) -> bool:
    """统一的「{what} 没就位」日志出口。返回是否真的打了日志。

    **等级由「清单状态」决定，不由调用方决定**——这是本函数存在的理由：

        清单说装过   → WARNING（你花了流量装它，它不在，你有权知道）
        清单说没勾   → 一个字都不打（INFO 也不打：没人会因为「你没装语音」
                        这四个字被帮助到，只会被又一次打扰）
        清单读不到   → INFO（老安装/开发机。说清楚「不确定」，但不摆出
                        故障的架势——它很可能本来就是正常的）

    这样调用方只要写 `log_missing(logger, FEAT_VOICE, "语音识别模型")`，
    不用自己判断该 warning 还是 info，也就不会两家写法不一致。
    """
    if not should_warn(index, root):
        return False
    state = has(index, root)
    text = missing_note(index, what, optional=optional, root=root)
    if state is True:
        logger.warning("⚠ %s", text)
    else:
        logger.info("ℹ %s", text)
    return True
