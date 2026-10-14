"""Qt 动画小工具集（2026-09-05 UI 质感 A 档批 2）。

铁律（调研结论，勿违——见 docs/开发规划/控制台UI质感升级方案_20260905.md）：
- Qt QSS 没有时间概念：transition/animation/box-shadow/渐变均无效（静默忽略）。
  一切平滑过渡走 Qt Animation Framework（本模块）。
- QGraphicsOpacityEffect / QGraphicsDropShadowEffect 是离屏 CPU 整块重绘：
  用后即删（setGraphicsEffect(None)）、同屏同时 ≤2-3 个、不挂列表项。
- 动画对象必须持引用防 GC——统一挂到控件属性 _ui_animations。
- 动画期间避免逐帧重排版（几何动画固定内容高度）。
- 缓动：位移/滑入 OutCubic/OutQuad；淡入 InOutQuad；禁用 Bounce/Elastic。
"""
from __future__ import annotations

import re

from PySide6.QtCore import QEasingCurve, QObject, Property, QPropertyAnimation, QVariantAnimation, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsOpacityEffect, QStackedWidget


def _hold(widget, animation):
    """动画挂到控件属性防 GC；finished 自动释放。返回动画本身。"""
    running = getattr(widget, "_ui_animations", None)
    if running is None:
        running = []
        setattr(widget, "_ui_animations", running)
    running.append(animation)

    def release() -> None:
        if animation in running:
            running.remove(animation)
    animation.finished.connect(release)
    return animation


def fade_in(widget, ms: int = 200, easing=QEasingCurve.Type.InOutQuad):
    """淡入（QGraphicsOpacityEffect 0→1）。finished 后立即清理 effect（铁律）。
    整页级 fade 一次性可用；同屏多个交错 fade 时长叠加控制 effect 数 ≤2。"""
    effect = QGraphicsOpacityEffect(widget)
    effect.setOpacity(0.0)
    widget.setGraphicsEffect(effect)
    animation = QPropertyAnimation(effect, b"opacity", widget)
    animation.setDuration(ms)
    animation.setStartValue(0.0)
    animation.setEndValue(1.0)
    animation.setEasingCurve(easing)
    animation.finished.connect(lambda: widget.setGraphicsEffect(None))
    _hold(widget, animation)
    animation.start()
    return animation


def animate_prop(widget, prop: bytes, start, end, ms: int = 200,
                 easing=QEasingCurve.Type.OutCubic):
    """具名属性动画薄封装（持引用防 GC）。"""
    animation = QPropertyAnimation(widget, prop, widget)
    animation.setDuration(ms)
    animation.setStartValue(start)
    animation.setEndValue(end)
    animation.setEasingCurve(easing)
    _hold(widget, animation)
    animation.start()
    return animation


def _css_color(color: QColor, prop: str) -> str:
    """QColor → 单属性内联 css 片段（半透明走 rgba，255 走 hex）。"""
    if color.alpha() < 255:
        return (f"{prop}: rgba({color.red()}, {color.green()}, {color.blue()}, "
                f"{color.alpha() / 255:.2f});")
    return f"{prop}: #{color.name()};"


def _parse_css_color(style_sheet: str, prop: str) -> QColor | None:
    """从内联 styleSheet 解析当前 prop 色（rgba()/hex），供动画起点取现色。"""
    m = re.search(rf"{re.escape(prop)}\s*:\s*rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([\d.]+)\s*\)",
                  style_sheet)
    if m:
        r, g, b, a = m.groups()
        return QColor(int(r), int(g), int(b), round(float(a) * 255))
    m = re.search(rf"{re.escape(prop)}\s*:\s*#([0-9a-fA-F]{{6}})", style_sheet)
    if m:
        return QColor(f"#{m.group(1)}")
    return None


def animate_color(widget, start: QColor, end: QColor, ms: int = 160,
                  prop: str = "color", base_style: str = ""):
    """颜色插值动画（QVariantAnimation QColor 含 alpha 插值）。
    逐帧写单属性内联 styleSheet——只用于低频 hover 与少量状态 label。
    base_style：控件原有内联样式前缀（自动保留拼回，防抹掉字号等）。"""
    animation = QVariantAnimation(widget)
    animation.setDuration(ms)
    animation.setStartValue(start)
    animation.setEndValue(end)
    animation.setEasingCurve(QEasingCurve.Type.InOutQuad)

    def _apply(value: QColor) -> None:
        widget.setStyleSheet(base_style + _css_color(value, prop))
    animation.valueChanged.connect(_apply)
    animation.finished.connect(lambda: widget.setStyleSheet(base_style + _css_color(end, prop)))
    _hold(widget, animation)
    animation.start()
    return animation


class SlideStack(QStackedWidget):
    """页面切换容器：保守淡入过渡 + 切换锁（2026-09-05 批 3）。
    全页 fade 一次性 240ms（fade_in 自动清 effect）；动画期间锁导航防连续切换。"""
    transitionStarted = Signal()
    transitionFinished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._transition_locked = False

    @property
    def transition_locked(self) -> bool:
        return self._transition_locked

    def slide_to(self, index: int, ms: int = 240) -> bool:
        if self._transition_locked or index == self.currentIndex() or not 0 <= index < self.count():
            return False
        self._transition_locked = True
        self.setCurrentIndex(index)
        self.transitionStarted.emit()
        animation = fade_in(self.currentWidget(), ms, QEasingCurve.Type.OutCubic)
        animation.finished.connect(self._unlock)
        return True

    def _unlock(self) -> None:
        self._transition_locked = False
        self.transitionFinished.emit()


class HoverEngine(QObject):
    """低频 hover 平滑引擎：enter/leave → 颜色插值（注册清单显式、服务关键控件）。
    checked 语义：hover 离开时若控件 isChecked() 恢复 checked 色（防破坏选中态视觉）。
    prop 默认 background-color（QSS :hover 仍是瞬时——引擎只平滑注册项）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rules = {}

    def register(self, widget, normal: QColor, hovered: QColor, ms: int = 140,
                 checked: QColor | None = None, prop: str = "background-color",
                 base_style: str = "") -> None:
        self._rules[widget] = (normal, hovered, checked, ms, prop, base_style)
        widget.installEventFilter(self)

    def eventFilter(self, watched, event):
        rule = self._rules.get(watched)
        if rule is not None and event.type() in (event.Type.Enter, event.Type.Leave):
            normal, hovered, checked, ms, prop, base = rule
            enter = event.type() == event.Type.Enter
            target = hovered if enter else (
                checked if (checked is not None and watched.isChecked()) else normal)
            # 起点取当前内联色（连续往返也平滑），取不到用反态色
            current = _parse_css_color(watched.styleSheet(), prop)
            start = current if current is not None else (normal if enter else hovered)
            animate_color(watched, start, target, ms, prop, base)
        return super().eventFilter(watched, event)
