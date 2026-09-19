"""控制台输出不得使用「字体不可移植」的字符（2026-09-19）。

## 为什么会有这条闸门

主人报告：跑安装器时终端里看不到 ✅ 和 ⬜。排查结论**不是编码问题**，是字体问题——
Python 把码位正确送到了控制台（实测 stdout.encoding = utf-8，码位逐一核对无误），
是 conhost 找不到字形可画。

取证（本机 Windows 10 19045，像素级截图回读，不是推测）：

  1. `HKCU\\Console\\FaceName = __DefaultTTFont__`，且未装 Windows Terminal
     （`wt.exe` 不存在）→ 走的是传统 conhost，不是 DirectWrite 那条路径
  2. 字体链接链 `FontLink\\SystemLink` 每一行**都以 `SEGUISYM.TTF, Segoe UI Symbol`
     结尾，没有任何一条挂到 Segoe UI Emoji**
  3. 逐字形查表：U+2705 ✅ / U+2B1C ⬜ / U+274C ❌ / U+2139 ℹ 在
     Consolas、新宋体、微软雅黑里**全部没有**，只存在于 Segoe UI Emoji
  4. 真开一个控制台窗口渲染并回读像素：这四个字符显示成**一模一样的空心方框 □**

Windows 11 默认终端是 Windows Terminal（DirectWrite 完整字体回退），所以那边看得到——
这正是主人「win11 应该可以看到」的猜测成立的原因。但 Win10 用户量还很大，
安装器不能要求用户先去改字体。

## 口径

**状态信息不能用 emoji 承载**，一律用 bracket 家族（每个字符都在
Consolas / 新宋体 / Segoe UI Symbol / 微软雅黑 四者中全部存在，已逐一查表 + 像素验证）：

    [√] 完成/已启用   [ ] 未勾选/缺失   [×] 失败/未找到   [!] 警告
    [i] 提示          [~] 进行中        [>] 跳过          [·] 条目
    [↓] 下载          -> 指向           ━ ─ 分隔线（U+2500 段，安全）

⚠ `糖糖控制台_qt.py` 不在此列：它是 PySide6 图形界面，Qt 走系统字体栈的完整回退，
emoji 正常显示。**本闸门只管控制台（stdout）路径**——不要拿它去要求 GUI 去 emoji。
"""

import re
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent

# ── 用户跑得到的控制台入口（新增此类脚本请登记到这里）──
# 与 tests/test_integration_contract.py 的 LLM_SEND_FUNCS 表同一套路：
# 清单是契约，不是自动发现——自动发现会让新脚本悄悄漏网。
CONSOLE_ENTRY = {
    "tools/安装糖糖.py": "安装器主程序（用户双击 安装糖糖.bat 跑的就是它）",
    "tools/同步记忆.py": "安装器菜单 2/3：记忆快照打包与解包",
    "tools/体检.py": "安装器菜单 7：换机功能对比",
    "start.py": "bat 的实际入口，console / install 两条路径都经它",
}

# ── 实测在 Win10 conhost 下渲染成空框的码位区间 ──
BLOCKED_RANGES = [
    (0x20E3, 0x20E3),   # COMBINING ENCLOSING KEYCAP（1️⃣ 的粘合符）
    (0x2139, 0x2139),   # ℹ INFORMATION SOURCE
    (0x2190, 0x21FF),   # 箭头：只放行 U+2190/2192/2193（已查表全字体覆盖）
    (0x2300, 0x23FF),   # ⌛⏳⏭ 等
    (0x2600, 0x27BF),   # ☀⚠✅❌★✂✎ 等杂项符号 + 装饰符号
    (0x2B00, 0x2BFF),   # ⬜⬇⭐ 等
    (0xFE0E, 0xFE0F),   # 变体选择符（emoji 呈现）——渲染成额外的空框
    (0x1F000, 0x1FAFF), # 📦🍬🩺 等 emoji 区
]

# 明确放行：已逐字体查表确认全覆盖，且像素验证过
ALLOWED = {0x2190, 0x2192, 0x2193, 0x2717, 0x2713, 0x2500, 0x2501}


def _blocked_in(text: str) -> dict[str, list[int]]:
    """返回 {字符: [行号...]}，只收用户可见的控制台输出里的违禁字符。"""
    hits: dict[str, list[int]] = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        for ch in line:
            cp = ord(ch)
            if cp in ALLOWED:
                continue
            if any(a <= cp <= b for a, b in BLOCKED_RANGES):
                hits.setdefault(ch, []).append(lineno)
    return hits


# ═══════════════════════════════════════════════════════
# 1. 控制台入口不得出现字体不可移植的字符
# ═══════════════════════════════════════════════════════

@pytest.mark.parametrize("rel", sorted(CONSOLE_ENTRY))
def test_console_scripts_use_portable_glyphs(rel: str):
    p = BASE / rel
    if not p.is_file():
        pytest.skip(f"{rel} 不存在（清单过期？）")
    hits = _blocked_in(p.read_text(encoding="utf-8"))
    if not hits:
        return
    detail = "\n".join(
        f"  {ch!r} U+{ord(ch):04X} 行 {ls}" for ch, ls in hits.items())
    pytest.fail(
        f"{rel}（{CONSOLE_ENTRY[rel]}）里有 Win10 控制台画不出的字符：\n{detail}\n"
        f"  改用 bracket 家族：[√] [ ] [×] [!] [i] [~] [>] [·] [↓] ->")


# ═══════════════════════════════════════════════════════
# 2. stdout 守卫必须在
# ═══════════════════════════════════════════════════════

@pytest.mark.parametrize("rel", sorted(CONSOLE_ENTRY))
def test_console_scripts_guard_stdout_encoding(rel: str):
    """重定向/管道下 Python 退回 GBK，print 非 GBK 字符直接抛 UnicodeEncodeError。

    同步记忆.py 真出过这个事故：退出码 1 但快照已生成 →「显示失败实际成功」。
    """
    p = BASE / rel
    if not p.is_file():
        pytest.skip(f"{rel} 不存在")
    text = p.read_text(encoding="utf-8")
    assert "reconfigure" in text and "utf-8" in text, (
        f"{rel} 缺 stdout 编码守卫。照抄这段（其他脚本已有）：\n"
        "    for _s in (sys.stdout, sys.stderr):\n"
        "        if _s and hasattr(_s, 'reconfigure'):\n"
        "            try: _s.reconfigure(encoding='utf-8', errors='replace')\n"
        "            except Exception: pass")


# ═══════════════════════════════════════════════════════
# 3. 闸门自检——确认检测逻辑真的抓得住
# ═══════════════════════════════════════════════════════

def test_detector_actually_catches_the_emoji():
    """防「闸门写了但从来没生效」——用当初报障的那两个字符做阳性对照。"""
    sample = "  ✅ 聊天 + 记忆\n  ⬜ 语音（未勾选）\n  ⚠️ 警告\n  1️⃣ 第一节\n"
    hits = _blocked_in(sample)
    # 只有组合符与 emoji 该拦；键帽里的 '1' 是普通 ASCII 数字，放行是对的
    assert set(hits) == {"✅", "⬜", "⚠", "️", "⃣"}, \
        f"检测逻辑漏掉了已知的坏字符，实际命中：{sorted(hits)}"

    # 阴性对照：替换后的 bracket 家族必须全部放行
    ok = "  [√] 聊天 + 记忆\n  [ ] 语音（未勾选）\n  [!] 警告\n  [1] 第一节\n  ━━━ 分隔 ━━━\n"
    assert not _blocked_in(ok), f"修正后的写法仍被误报：{sorted(_blocked_in(ok))}"


def test_console_entry_list_is_not_stale():
    """清单里的文件都得存在——防止改名后闸门静默跳过（skip 不等于通过）。"""
    missing = [r for r in CONSOLE_ENTRY if not (BASE / r).is_file()]
    assert not missing, f"CONSOLE_ENTRY 登记了不存在的文件：{missing}"
