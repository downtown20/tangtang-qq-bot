"""lucide 线性图标工厂（2026-09-06——控制台 emoji 退役工程）。

图标数据：lucide-static（ISC 许可，agent/icons/*.svg，24x24 stroke=2 线性风）。
运行时把 SVG 的 currentColor 替换为目标色再渲染 QPixmap→QIcon——
图标随主题/状态染色（导航默认文字灰、选中/强调用 accent 等），按 (name,color,size) 缓存。

用法：
    from agent.icons import icon
    btn.setIcon(icon("layout-dashboard"))              # 默认中性灰 18px
    btn.setIcon(icon("play", color=pal["accent"], size=16))
    btn.setIcon(icon("star", color="#e2a1bc", size=20))
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtSvg import QSvgRenderer

ICON_DIR = Path(__file__).resolve().parent / "icons"

_cache: dict[tuple[str, str, int], QIcon] = {}
_svg_cache: dict[str, str] = {}

_AVAILABLE: set[str] | None = None


def available() -> set[str]:
    """本目录可用图标名集合（不带 .svg）。"""
    global _AVAILABLE
    if _AVAILABLE is None:
        _AVAILABLE = {p.stem for p in ICON_DIR.glob("*.svg")}
    return _AVAILABLE


def icon(name: str, color: str = "#a1a1aa", size: int = 18) -> QIcon:
    """取染色图标（缓存）。name 缺失时回退空图标（不炸——调用方自己保证拼写）。"""
    key = (name, color, size)
    if key in _cache:
        return _cache[key]
    qicon = QIcon()
    try:
        if name not in _svg_cache:
            p = ICON_DIR / f"{name}.svg"
            if not p.is_file():
                return qicon
            _svg_cache[name] = p.read_text(encoding="utf-8")
        svg = _svg_cache[name].replace("currentColor", color)
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        r = QSvgRenderer()
        ok = r.load(svg.encode("utf-8"))
        if not ok:
            return qicon
        from PySide6.QtGui import QPainter
        p = QPainter(pm)
        r.render(p)
        p.end()
        qicon = QIcon(pm)
    except Exception:
        qicon = QIcon()
    _cache[key] = qicon
    return qicon


def icon_with_palette(name: str, pal: dict, role: str = "text_secondary",
                      size: int = 18) -> QIcon:
    """按当前主题色板取色：role = text_secondary/text_primary/text_muted/accent/..."""
    color = pal.get(role, "#a1a1aa")
    if color.startswith("rgba"):
        # rgba(...) → hex 近似（取前三个分量）
        import re
        m = re.match(r"rgba\((\d+),\s*(\d+),\s*(\d+)", color)
        if m:
            color = "#%02x%02x%02x" % tuple(int(g) for g in m.groups())
    return icon(name, color, size)


def clear_cache() -> None:
    """主题大切换后清缓存（色变重新渲染）。"""
    _cache.clear()
