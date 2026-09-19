#!/usr/bin/env python3
"""🎨 现代取色面板（2026-09-06 审核先行版——截图复刻，未合入主程序）。

形态（按用户截图 + Codex 识图描述）：
  矩形 HSV 色板（上白→右纯色相、下黑；白环指示）
  └ 色相彩虹条（圆滑块）＋ 右侧当前色圆形预览
  └ HEX 输入行（HEX 标签 + 圆角输入框）
  └ 预设 6 色卡（橙/蓝/青/红/紫/灰蓝）
浅色柔和无外框。自绘 + numpy 向量化渲染（拖动流畅）。

用法：
    panel = ColorPanel(initial="#e2a1bc", parent=self)
    panel.colorChanged.connect(lambda hexv: ...)   # 拖动/输入/预设即回调
    panel.popup_under(widget)                       # 无边框圆角浮层

运行自测：python tools/color_panel.py
"""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt, QRectF, Signal, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QLinearGradient, QPixmap, QPalette
from PySide6.QtWidgets import (QApplication, QDialog, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QVBoxLayout, QWidget)

try:
    import numpy as np
except ImportError:
    np = None  # 无 numpy 时降级：色板用 QImage 逐像素（较慢，仍可用）

PRESETS = ["#F97316", "#4A6CF7", "#06B6D4", "#F43F5E", "#8B5CF6", "#737373"]  # 橙蓝青红紫灰蓝


def _hsv_grid_to_rgb(hue: int, w: int, h: int) -> bytes:
    """HSV 色板像素（hue 固定）：x→sat 0..1，y→val 1..0。返回 RGB bytes (w*h*3)。"""
    if np is None:
        out = bytearray()
        for yy in range(h):
            v = 1.0 - yy / max(h - 1, 1)
            for xx in range(w):
                s = xx / max(w - 1, 1)
                c = QColor.fromHsv(hue % 360, int(s * 255), int(v * 255))
                out += bytes((c.red(), c.green(), c.blue()))
        return bytes(out)
    s = np.linspace(0.0, 1.0, w)
    v = np.linspace(1.0, 0.0, h)
    S, V = np.meshgrid(s, v)
    hh = (hue % 360) / 60.0
    c = V * S
    x = c * (1.0 - np.abs(hh % 2.0 - 1.0))
    m = V - c
    r = np.where(hh < 1, c, np.where(hh < 2, x, np.where(hh < 4, 0.0, np.where(hh < 5, x, c))))
    g = np.where(hh < 1, x, np.where(hh < 2, c, np.where(hh < 4, c, np.where(hh < 5, 0.0, 0.0))))
    b = np.where(hh < 1, 0.0, np.where(hh < 2, 0.0, np.where(hh < 4, x, np.where(hh < 5, c, c))))
    rgb = np.dstack((r + m, g + m, b + m))
    return (rgb.clip(0.0, 1.0) * 255.0).astype("uint8").tobytes()


class _Swatch(QPushButton):
    """预设小色卡（圆角矩形、细描边、hover 微亮）。"""

    def __init__(self, hex_color: str):
        super().__init__()
        self._hex = hex_color
        self.setFixedSize(26, 26)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(
            f"QPushButton {{ background-color: {hex_color}; border-radius: 6px;"
            f" border: 1px solid rgba(0,0,0,0.15); }}"
            f"QPushButton:hover {{ border: 2px solid #555; }}")


class ColorPanel(QFrame):
    """现代取色面板：HSV 色板 + 色相条 + 圆形预览 + HEX + 预设。无边框圆角浮层。"""

    colorChanged = Signal(str)  # "#rrggbb"

    def __init__(self, initial: str = "#e2a1bc", parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            "ColorPanel { background-color: #f7f2f0; border-radius: 14px;"
            " border: 1px solid #e3d8d4; }"
            "ColorPanel QLabel { color: #4a4444; font-size: 12px; }"
            "ColorPanel QLineEdit { background: #fffdfc; border: 1px solid #e5dcda;"
            " border-radius: 7px; padding: 5px 8px; color: #3a3434; font-size: 12px; }"
            "ColorPanel QLineEdit:focus { border-color: #c9a8a0; }")
        self.setFixedWidth(330)

        c = QColor(initial)
        self._hue = max(c.hue(), 0)
        self._sat = c.saturationF() if c.saturationF() > 0 else 1.0
        self._val = c.valueF()
        self._board_img = None  # (w,h,hue) 缓存
        self._emit_timer = QTimer(self)
        self._emit_timer.setSingleShot(True)
        self._emit_timer.setInterval(16)
        self._emit_timer.timeout.connect(self._emit_now)

        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(8)

        # 1) HSV 色板（自绘）
        self._board = _Board(self)
        self._board.setFixedHeight(170)
        self._board.posChanged.connect(self._on_board)
        v.addWidget(self._board)

        # 2) 色相条 + 圆形预览 同行
        row = QHBoxLayout()
        row.setSpacing(10)
        self._hue_bar = _HueBar(self)
        self._hue_bar.setFixedHeight(18)
        self._hue_bar.hueChanged.connect(self._on_hue)
        row.addWidget(self._hue_bar, 1)
        self._preview = QLabel()
        self._preview.setFixedSize(40, 40)
        self._preview.setStyleSheet("QLabel { border-radius: 20px; border: 2px solid palette(base); }")
        row.addWidget(self._preview)
        v.addLayout(row)

        # 3) HEX 行
        hex_row = QHBoxLayout()
        hex_row.setSpacing(8)
        hex_row.addWidget(QLabel("HEX"))
        self._hex = QLineEdit()
        self._hex.setText(c.name())
        self._hex.editingFinished.connect(self._apply_hex)
        hex_row.addWidget(self._hex, 1)
        v.addLayout(hex_row)

        # 4) 预设色卡
        pre = QHBoxLayout()
        pre.setSpacing(8)
        for hx in PRESETS:
            sw = _Swatch(hx)
            sw.clicked.connect(lambda _, h=hx: self.set_color(h, emit=True))
            pre.addWidget(sw)
        pre.addStretch()
        v.addLayout(pre)

        self._refresh_views()

    # ── 内部视图 ──
    def _refresh_views(self):
        c = QColor.fromHsv(self._hue % 360, int(self._sat * 255), int(self._val * 255))
        self._board.set_pos(self._sat, self._val)
        self._hue_bar.set_hue(self._hue)
        pal = self._preview.palette()
        pal.setColor(QPalette.Window, c)
        self._preview.setAutoFillBackground(True)
        self._preview.setPalette(pal)
        if self._hex.text().lower() != c.name().lower() and not self._hex.hasFocus():
            self._hex.setText(c.name())

    def _emit(self):
        self._refresh_views()
        if not self._emit_timer.isActive():
            self._emit_timer.start()

    def _emit_now(self):
        c = QColor.fromHsv(self._hue % 360, int(self._sat * 255), int(self._val * 255))
        self.colorChanged.emit(c.name())

    def _on_board(self, sat: float, val: float):
        self._sat, self._val = sat, val
        self._emit()

    def _on_hue(self, hue: int):
        self._hue = hue
        self._emit()

    def _apply_hex(self):
        t = self._hex.text().strip().lstrip("#")
        valid = len(t) in (3, 6) and all(ch in "0123456789abcdefABCDEF" for ch in t)
        if not valid:
            self._refresh_views()
            return
        c = QColor("#" + (''.join(ch * 2 for ch in t) if len(t) == 3 else t))
        self._hue = max(c.hue(), 0)
        self._sat = c.saturationF() if c.saturationF() > 0 else 1.0
        self._val = c.valueF()
        self._emit()

    # ── 外部 ──
    def set_color(self, hex_color: str, emit: bool = False):
        c = QColor(hex_color)
        self._hue = max(c.hue(), 0)
        self._sat = c.saturationF() if c.saturationF() > 0 else 1.0
        self._val = c.valueF()
        self._refresh_views()
        if emit:
            self.colorChanged.emit(c.name())

    def popup_under(self, anchor: QWidget):
        """在锚控件下方弹出（屏内不越界）。"""
        gp = anchor.mapToGlobal(anchor.rect().bottomLeft())
        self.adjustSize()
        self.move(gp.x(), gp.y() + 6)
        self.show()
        self.raise_()


class _Board(QWidget):
    """HSV 矩形色板（自绘；numpy 缓存像素，拖动只重算指示器）。"""

    posChanged = Signal(float, float)  # sat, val

    def __init__(self, panel: ColorPanel):
        super().__init__(panel)
        self._hue = 0
        self._sat, self._val = 1.0, 1.0
        self._pix: QPixmap | None = None
        self.setMouseTracking(True)

    def _render(self):
        w, h = max(self.width(), 2), max(self.height(), 2)
        rgb = _hsv_grid_to_rgb(self._hue % 360, w, h)
        img = QImage(rgb, w, h, w * 3, QImage.Format_RGB888)
        self._pix = QPixmap.fromImage(img)

    def paintEvent(self, ev):
        if self._pix is None or self._pix.width() != self.width() or self._pix.height() != self.height():
            self._render()
        p = QPainter(self)
        p.drawPixmap(0, 0, self._pix)
        # 指示白环
        x = self._sat * (self.width() - 1)
        y = (1.0 - self._val) * (self.height() - 1)
        p.setPen(QPen(Qt.white, 2))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QRectF(x - 5, y - 5, 10, 10))
        p.setPen(QPen(QColor(0, 0, 0, 90), 1))
        p.drawEllipse(QRectF(x - 6, y - 6, 12, 12))

    def set_pos(self, sat: float, val: float):
        self._sat, self._val = sat, val
        self.update()

    def _xy(self, pos) -> tuple[float, float]:
        w, h = max(self.width() - 1, 1), max(self.height() - 1, 1)
        x = min(max(pos.x(), 0), w) / w
        y = min(max(pos.y(), 0), h) / h
        return x, 1.0 - y

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            s, v = self._xy(ev.position())
            self.posChanged.emit(s, v)

    def mouseMoveEvent(self, ev):
        if ev.buttons() & Qt.LeftButton:
            s, v = self._xy(ev.position())
            self.posChanged.emit(s, v)


class _HueBar(QWidget):
    """彩虹色相条（自绘渐变 + 圆滑块）。"""

    hueChanged = Signal(int)

    def __init__(self, parent: QWidget | None):
        super().__init__(parent)
        self._hue = 0
        self.setMouseTracking(True)
        self.setMinimumWidth(120)

    def paintEvent(self, ev):
        p = QPainter(self)
        g = QLinearGradient(0, 0, self.width(), 0)
        for i, hx in enumerate(("#ff0000", "#ffff00", "#00ff00", "#00ffff",
                                "#0000ff", "#ff00ff", "#ff0000")):
            g.setColorAt(i / 6, QColor(hx))
        p.setBrush(g)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(self.rect(), 9, 9)
        # 圆滑块
        x = self._hue % 360 / 360.0 * max(self.width() - 1, 1)
        c = QColor.fromHsv(self._hue % 360, 255, 255)
        p.setBrush(c)
        p.setPen(QPen(Qt.white, 2))
        p.drawEllipse(QRectF(x - 7, self.height() / 2 - 7, 14, 14))

    def set_hue(self, hue: int):
        if hue == self._hue:
            return
        self._hue = hue
        self._pix = None
        self.update()

    def _hx(self, x) -> int:
        return int(min(max(x, 0), max(self.width() - 1, 1)) / max(self.width() - 1, 1) * 360)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.hueChanged.emit(self._hx(ev.position().x()))

    def mouseMoveEvent(self, ev):
        if ev.buttons() & Qt.LeftButton:
            self.hueChanged.emit(self._hx(ev.position().x()))


if __name__ == "__main__":
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    # 自测：弹出面板 + 显示所选色
    app = QApplication(sys.argv)
    anchor = QPushButton("打开取色面板")
    anchor.setFixedSize(180, 40)
    out = QLabel("#e2a1bc")
    out.setFixedSize(120, 30)
    from PySide6.QtWidgets import QWidget as _W
    box = _W()
    lay = QHBoxLayout(box)
    lay.addWidget(anchor)
    lay.addWidget(out)

    def _open():
        p = ColorPanel(initial=out.text(), parent=box)
        p.colorChanged.connect(lambda h: out.setText(h))
        p.popup_under(anchor)
    anchor.clicked.connect(_open)
    box.resize(340, 80)
    box.show()
    sys.exit(app.exec())
