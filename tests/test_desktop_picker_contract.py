# -*- coding: utf-8 -*-
"""「选模型」面板的两个真实缺陷（2026-10-28 主人在界面上当场发现）。

## 为什么有这个文件

我上一轮只跑了 `tsc --noEmit` + `vite build` 就说「改好了」。这两个 bug 都正好
落在它们的盲区里：类型检查只看形状、打包只看能否构建，**都不看你点一下会怎样**。

主人原话：「打开下拉框后点击空白处取消不了，你有在浏览器测试过吗？当前模型名字
还是与背景颜色一样得白色，不易看清」。

## 钉两条（都能红）

1. **点空白关不掉** —— 设置弹窗容器上有 `onMouseDown={(e) => e.stopPropagation()}`
   （防止点面板内部时把整个设置关掉）。挂在**冒泡阶段**的 document 监听器永远
   收不到事件，于是 picker 关不掉。必须用**捕获阶段**（第三个参数 `true`），
   捕获先于冒泡，`stopPropagation` 挡不住它。
2. **当前模型名是白字** —— `.dsl-modelpick` 写了 `var(--text, #e6e6e6)` 和
   `var(--bg-input, rgba(255,255,255,0.04))`，但这套主题**没有** `--text` /
   `--bg-input` 这些变量（真实变量在 `styles/tokens.css`：`--label-primary` /
   `--bg-layer-2` / `--border-l2`）。于是永远落到浅色回退值 —— 浅色主题下就是
   白底浅字。

## 修完的验收

`vite.probe.config.ts` + chrome-devtools（浏览器里真点）：
打开面板 → 点面板外 → 关；点面板内 → 不关；Esc → 关；按钮文字 `rgb(15,17,21)`。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
SRC = ROOT / "desktop" / "src"


def _read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


def test_picker_listens_on_capture_phase():
    """冒泡阶段会被设置面板的 stopPropagation 挡掉 —— 必须用捕获阶段。"""
    source = _read("components/ModelPicker.tsx")
    assert "addEventListener('mousedown', onDown, true)" in source, (
        "ModelPicker 的 mousedown 监听不在捕获阶段 —— 设置弹窗里点空白关不掉")
    # 移除时的第三个参数必须一致，否则监听器根本不会摘掉（内存里越积越多）
    assert "removeEventListener('mousedown', onDown, true)" in source, (
        "移除监听器时没带 capture 参数 —— 按添加时的参数不一致，摘不掉")


def test_settings_panel_really_stops_bubbling():
    """这条是上面那条的**前提事实**：如果哪天 stopPropagation 删了，捕获仍然安全。

    反过来，如果这里红了，说明前提变了 —— 那时要么更新注释，要么确认 picker 还关得掉。
    """
    source = _read("pages/Settings.tsx")
    assert "onMouseDown={(e) => e.stopPropagation()}" in source, (
        "设置面板不再阻止冒泡了 —— 请复核 ModelPicker 的捕获监听是否还必要（以及注释是否过期）")


def _css_block(path: Path, selector: str) -> str:
    text = path.read_text(encoding="utf-8")
    # 先剥注释：注释里会写「以前用的是 `var(--text, …)`」这类历史说明，
    # 不剥掉就会被当成真的声明去找茬（第一次跑这条闸门就是这么红的）
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", text)
    assert match, f"{path.name} 里找不到 {selector} 规则"
    return match.group(1)


def test_modelpick_uses_theme_tokens_that_exist():
    """`.dsl-modelpick` 的颜色必须来自 tokens.css 里真实定义的变量。

    「有个 fallback 就安全」是错觉：fallback 只在变量**没定义**时生效，而它
    恰恰就是为了「变量名打错了」这种情况准备的遮羞布——打错字不报错，只是颜色
    悄悄变成回退值（这次就是白字）。
    """
    tokens = (SRC / "styles" / "tokens.css").read_text(encoding="utf-8")
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", tokens))
    assert "--label-primary" in defined and "--bg-layer-2" in defined

    block = _css_block(SRC / "components" / "SettingsFields.css", ".dsl-modelpick")
    used = re.findall(r"var\((--[a-z0-9-]+)", block)
    assert used, "`.dsl-modelpick` 一个主题变量都没用 —— 主题切换时会脱色"
    unknown = [name for name in used if name not in defined]
    assert not unknown, (
        f"`.dsl-modelpick` 用了 tokens.css 里不存在的变量 {unknown} —— "
        "它们会静默落到 fallback（这次就是白字）")
    assert "--label-primary" in used, "文字颜色应该来自 --label-primary"


def test_modelpick_has_no_bare_fallbacks():
    """再钉一层：那四个「看起来像本项目变量」的错名一个都不许回来。

    它们是从 DSH 的 CSS 里看来的（`--text` / `--bg-input` / `--border` /
    `--border-strong`），本项目的变量表里没有这些名字。
    """
    block = _css_block(SRC / "components" / "SettingsFields.css", ".dsl-modelpick")
    for bad in ("var(--text", "var(--bg-input", "var(--border,", "var(--border-strong"):
        assert bad not in block, f"`.dsl-modelpick` 又用了不存在的变量 {bad}"
