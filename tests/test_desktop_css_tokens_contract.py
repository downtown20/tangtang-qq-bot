# -*- coding: utf-8 -*-
"""前端 CSS 变量契约：**引用的每个变量都必须有定义**（2026-10-28）。

## 为什么有这个闸门

用户立的规矩：「不得引用不存在的变量名（变量名写错不会报错，只会静默回退到错误颜色）」。

这不是理论风险，2026-10-27/28 连踩两次：

1. `.dsl-modelpick` 写了 `var(--text, #e6e6e6)` —— 这套主题没有 `--text`，于是永远
   落到浅色回退值，浅色主题下白底白字（主人当场抓到「当前模型名字还是与背景颜色
   一样得白色」）。
2. `--bg-layer-4` 被 24 处引用、**一次都没定义过**：进度条槽、小标签底的背景整条
   声明失效（`background: var(--bg-layer-4)` 没有 fallback → 无效声明 → 透明）。
   同批还有 `--warning` / `--warning-soft` / `--success-soft` / `--fs-10` /
   `--surface-2` / `--bg-layer-1` / `--line` / `--bg-elevated` /
   `--sidebar-inline-padding`，一共 51 处悬空引用、36 处无 fallback。

## 判据

- 「定义」= 任意 `.css` 里的 `--x:`（组件内部的作用域变量也算——`--fg-ok` 这类
  定义在同一个文件的选择器里，不是 bug）
- 「引用」= 任意 `.css` 里的 `var(--x)` / `var(--x, fallback)`
- 两边一减，剩下的就是悬空引用

JS 里用 `style.setProperty('--x', ...)` 动态注入的变量要进 `DYNAMIC` 白名单，
否则会误报。
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "desktop" / "src"

#: 由 JS 在运行时 setProperty 注入的变量（CSS 里找不到定义是正常的）
DYNAMIC: frozenset[str] = frozenset()


def _strip_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def _css_files() -> list[Path]:
    return sorted(SRC.rglob("*.css"))


def _defined_vars() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for css in _css_files():
        text = _strip_comments(css.read_text(encoding="utf-8"))
        for m in re.finditer(r"(--[a-z0-9-]+)\s*:", text):
            found.setdefault(m.group(1), set()).add(str(css.relative_to(SRC)))
    return found


def _referenced_vars() -> list[tuple[str, int, str, bool]]:
    """[(文件, 行号, 变量名, 有没有 fallback)]"""
    rows = []
    for css in _css_files():
        text = _strip_comments(css.read_text(encoding="utf-8"))
        for m in re.finditer(r"var\((--[a-z0-9-]+)(\s*,)?", text):
            line = text.count("\n", 0, m.start()) + 1
            rows.append((str(css.relative_to(SRC)), line, m.group(1), bool(m.group(2))))
    return rows


def test_every_referenced_css_variable_is_defined():
    """核心断言：不许有悬空引用（有 fallback 也不行）。"""
    defined = _defined_vars()
    dangling = [
        (f, line, name, fb)
        for f, line, name, fb in _referenced_vars()
        if name not in defined and name not in DYNAMIC
    ]
    if not dangling:
        return
    # 按「无 fallback」优先排，方便定位真正静默失效的那些
    dangling.sort(key=lambda r: r[3])
    detail = "\n".join(
        f"  {'(无 fallback，整条声明失效)' if not fb else '(有 fallback，颜色会静默错)'}"
        f"  {f}:{line}  {name}"
        for f, line, name, fb in dangling
    )
    raise AssertionError(
        f"{len(dangling)} 处引用了不存在的 CSS 变量：\n{detail}\n"
        "修法：在 desktop/src/styles/tokens.css 里补定义（浅色 + 深色两套），"
        "或改用已有的语义 token。变量名打错不会报错，只会静默回退。"
    )


def test_theme_tokens_cover_both_themes():
    """`tokens.css` 必须有深色主题块，且两套都定义了核心语义 token。

    常见翻车：浅色写了、深色漏了 → 切深色时那处回退成浅色值（或反过来）。
    """
    tokens = (SRC / "styles" / "tokens.css").read_text(encoding="utf-8")
    assert ':root[data-theme="dark"]' in tokens, "tokens.css 没有深色主题块"

    light, _, dark = tokens.partition(':root[data-theme="dark"]')
    core = ("--bg-layer-2", "--bg-layer-4", "--label-primary", "--border-l2",
            "--line", "--warning", "--success-soft", "--warning-soft")
    missing_light = [n for n in core if not re.search(rf"{re.escape(n)}\s*:", light)]
    assert not missing_light, f"浅色主题缺 {missing_light}"
    # 深色块允许靠别名自动跟随，所以只要求「要么有定义、要么在浅色里是 var() 别名」
    for name in core:
        has_own = re.search(rf"{re.escape(name)}\s*:", dark)
        is_alias = re.search(rf"{re.escape(name)}\s*:\s*var\(", tokens)
        assert has_own or is_alias, f"{name} 在深色主题下既没覆盖、也不是别名"


def test_no_bare_unknown_fallbacks_in_model_picker():
    """`ModelPicker.css` 是上一轮踩坑的地方，单独钉一层（保护性重复）。"""
    text = _strip_comments((SRC / "components" / "ModelPicker.css").read_text(encoding="utf-8"))
    defined = _defined_vars()
    bad = sorted({
        m.group(1) for m in re.finditer(r"var\((--[a-z0-9-]+)", text)
        if m.group(1) not in defined
    })
    assert not bad, f"ModelPicker.css 引用了未定义的变量：{bad}"
