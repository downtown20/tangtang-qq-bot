#!/usr/bin/env python3
"""🎨 外观设置独立预览器（2026-09-06 审核先行版——未合入主控制台）。

主人审这一版的外观交互与配色：通过了再把同样式合入控制台设置页。
- 左：实时预览区（模拟控制台骨架：侧栏/卡片/按钮/输入/状态）
- 右：三段控制——主题分段按钮 / 10 配色色卡 / 自定义（一个色卡位 → Qt 色板）

运行：python tools/外观预览器.py
数据源：agent/theme_palettes.py（与主程序同一事实源，改这里两边都生效）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import (QApplication, QWidget, QFrame, QLabel, QPushButton,
                               QVBoxLayout, QHBoxLayout, QButtonGroup, QColorDialog,
                               QDialog, QComboBox, QLineEdit, QCheckBox)
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QColor

from agent.theme_palettes import (CUSTOM_ID, DEFAULT_CUSTOM, SCHEMES, SCHEME_ORDER,
                                  derive_accent, resolve_palette)


def build_qss(pal: dict, font_size: int = 13) -> str:
    """模拟控制台 QSS 骨架（与主程序同族样式，供预览主题效果）。"""
    p = pal
    return f"""
QWidget {{ font-family: "Microsoft YaHei UI"; font-size: {font_size}px;
    color: {p['text_primary']}; background-color: {p['main_bg']}; }}
QFrame#sidebar {{ background-color: {p['sidebar_bg']}; border: none;
    border-top: 1px solid {p['highlight']}; }}
QFrame#card {{ background-color: {p['card_bg']}; border: 1px solid {p['card_border']};
    border-top: 1px solid {p['highlight']}; border-radius: 10px; padding: 12px; }}
QLabel#title {{ font-size: 24px; font-weight: bold; color: {p['accent']}; }}
QLabel#muted {{ font-size: 11px; color: {p['text_muted']}; }}
QLabel#val {{ font-size: 17px; font-weight: bold; color: {p['text_primary']}; }}
QPushButton {{ background-color: {p['card_bg']}; color: {p['text_primary']};
    border: 1px solid {p['card_border']}; border-radius: 6px; padding: 7px 16px; }}
QPushButton:hover {{ border-color: {p['accent']}; background-color: {p['hover_bg']}; }}
QPushButton#primary {{ background-color: {p['accent']}; color: {p['accent_on']};
    border: none; font-weight: bold; }}
QPushButton#primary:hover {{ background-color: {p['accent_hover']}; border: none; }}
QPushButton#okBtn {{ background-color: {p['green']}; color: white; border: none; }}
QPushButton#dangerBtn {{ background-color: {p['danger_bg']}; color: {p['danger_text']}; border: none; }}
QPushButton#navBtn {{ background-color: transparent; color: {p['text_secondary']};
    text-align: left; padding: 9px 14px; border-radius: 8px; border: none; }}
QPushButton#navBtn:hover {{ background-color: {p['hover_bg']}; color: {p['text_primary']}; }}
QPushButton#navBtn:checked {{ background-color: {p['accent_soft']};
    color: {p['accent']}; border-left: 3px solid {p['accent']}; }}
QLineEdit, QComboBox {{ background-color: {p['input_bg']}; border: 1px solid {p['card_border']};
    border-radius: 6px; padding: 5px 8px; color: {p['text_primary']}; }}
QLineEdit:focus {{ border-color: {p['accent']}; }}
QScrollBar:vertical {{ background: transparent; width: 8px; }}
QScrollBar::handle:vertical {{ background: {p['card_border']}; border-radius: 4px; }}
"""


class AppearancePreview(QWidget):
    """独立外观审核窗口：左预览右控制。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("🎨 外观预览器（审核版——通过后合入控制台）")
        self.resize(860, 560)
        self._theme = "dark"
        self._scheme = "graphite"
        self._custom = DEFAULT_CUSTOM
        self._pal = resolve_palette(self._theme, self._scheme, self._custom)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_preview(), 3)
        root.addWidget(self._build_controls(), 2)

    # ─────────── 左：预览 ───────────
    def _build_preview(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)
        h = QHBoxLayout()
        h.setSpacing(0)
        # 侧栏（图标示范：lucide 线性图标 + 文字，无 emoji）
        sb = QFrame()
        sb.setObjectName("sidebar")
        sb.setFixedWidth(160)
        sv = QVBoxLayout(sb)
        sv.setContentsMargins(10, 14, 10, 14)
        sv.addWidget(QLabel("小糖糖", objectName="title"))
        sv.addWidget(QLabel("QQ 群智能体控制台", objectName="muted"))
        sv.addSpacing(10)
        self._nav = []
        self._icon_btns: list[tuple[object, str, str]] = []  # (btn, icon_name, role)
        for t, ic in (("仪表盘", "layout-dashboard"), ("设置", "settings"), ("图库", "image")):
            b = QPushButton(t)
            b.setObjectName("navBtn")
            b.setCheckable(True)
            b.setIconSize(QSize(15, 15))
            # 颜色/背景由全局 QSS #navBtn 管理（内联会覆盖主题，深浅切换失效——2026-09-06）
            sv.addWidget(b)
            self._nav.append(b)
            self._icon_btns.append((b, ic, "text_secondary"))
        self._nav[0].setChecked(True)
        sv.addStretch()
        h.addWidget(sb)
        # 主区
        main = QFrame()
        main.setObjectName("card")
        mv = QVBoxLayout(main)
        mv.setContentsMargins(16, 14, 16, 14)
        mv.setSpacing(10)
        mv.addWidget(QLabel("控制", objectName="muted"))
        row = QHBoxLayout()
        self._btn_primary = QPushButton("启动小糖糖")
        self._btn_primary.setObjectName("primary")
        self._btn_primary.setIconSize(QSize(16, 16))
        self._icon_btns.append((self._btn_primary, "play", "accent_on"))
        row.addWidget(self._btn_primary)
        b_stop = QPushButton("停止")
        b_stop.setIconSize(QSize(15, 15))
        self._icon_btns.append((b_stop, "square", "text_secondary"))
        row.addWidget(b_stop)
        b_restart = QPushButton("重启")
        b_restart.setIconSize(QSize(15, 15))
        self._icon_btns.append((b_restart, "rotate-cw", "text_secondary"))
        row.addWidget(b_restart)
        row.addStretch()
        mv.addLayout(row)
        # 输入行
        inp = QHBoxLayout()
        inp.addWidget(QLabel("模型名", objectName="muted"))
        edit = QLineEdit("deepseek-v4-pro")
        inp.addWidget(edit, 1)
        mv.addLayout(inp)
        # 状态行（成功/警告/危险）
        st = QHBoxLayout()
        self._st_ok = QLabel("● 在线")
        self._st_warn = QLabel("● 待处理")
        self._st_err = QLabel("● 故障")
        for lbl in (self._st_ok, self._st_warn, self._st_err):
            st.addWidget(lbl)
        st.addStretch()
        mv.addLayout(st)
        h.addWidget(main, 1)
        lay.addLayout(h)
        # 底部一条：示例卡片行
        cards = QHBoxLayout()
        for name, lbl in (("SnowLuma", "已连接"), ("LLM", "DeepSeek")):
            c = QFrame()
            c.setObjectName("card")
            cv = QVBoxLayout(c)
            cv.addWidget(QLabel(name, objectName="muted"))
            cv.addWidget(QLabel(lbl, objectName="val"))
            cards.addWidget(c)
        lay.addLayout(cards)
        lay.addStretch()
        return page

    def _apply(self):
        self._pal = resolve_palette(self._theme, self._scheme, self._custom)
        qss = build_qss(self._pal)
        QApplication.instance().setStyleSheet(qss)
        # 状态色
        self._st_ok.setStyleSheet(f"color: {self._pal['green']};")
        self._st_warn.setStyleSheet(f"color: {self._pal['orange']};")
        self._st_err.setStyleSheet(f"color: {self._pal['red']};")
        # lucide 图标随主题重新染色（导航选中态用 accent 与文字一致）
        from agent.icons import clear_cache, icon_with_palette
        clear_cache()
        for btn, name, role in getattr(self, "_icon_btns", []):
            if btn.objectName() == "navBtn" and btn.isChecked():
                role = "accent"
            btn.setIcon(icon_with_palette(name, self._pal, role, size=18))
        self._update_checks()

    # ─────────── 右：控制 ───────────
    def _build_controls(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("sidebar")
        panel.setFixedWidth(300)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(20, 20, 20, 16)
        lay.setSpacing(14)
        lay.addWidget(QLabel("🎨 外观（审核版）", objectName="title"))

        # 1. 主题——两段式大按钮
        lay.addWidget(QLabel("主题", objectName="muted"))
        seg = QHBoxLayout()
        seg.setSpacing(6)
        self._theme_group = QButtonGroup(self)
        self._theme_group.setExclusive(True)
        for val, txt in (("dark", "🌙 深色"), ("light", "☀️ 浅色")):
            b = QPushButton(txt)
            b.setCheckable(True)
            b.setFixedHeight(34)
            b.clicked.connect(lambda _, v=val: self._set_theme(v))
            self._theme_group.addButton(b)
            seg.addWidget(b)
            if val == self._theme:
                b.setChecked(True)
        lay.addLayout(seg)

        # 2. 配色色卡（大圆点 + 名字小字在下）
        lay.addWidget(QLabel("配色", objectName="muted"))
        grid = QVBoxLayout()
        grid.setSpacing(4)
        self._dot_group = QButtonGroup(self)
        self._dot_group.setExclusive(True)
        self._dots: dict[str, QPushButton] = {}
        for i in range(0, len(SCHEME_ORDER), 5):
            row = QHBoxLayout()
            row.setSpacing(6)
            for sid in SCHEME_ORDER[i:i + 5]:
                cell = QVBoxLayout()
                cell.setSpacing(2)
                seed = SCHEMES[sid]["seed"]
                d = QPushButton()
                d.setFixedSize(30, 30)
                d.setCheckable(True)
                d.setCursor(Qt.PointingHandCursor)
                d.setToolTip(SCHEMES[sid]["name"])
                lum = 0.299 * int(seed[1:3], 16) + 0.587 * int(seed[3:5], 16) + 0.114 * int(seed[5:7], 16)
                on = "#ffffff" if lum < 160 else "#1b1b1f"
                d.setStyleSheet(
                    f"QPushButton {{ background-color: {seed}; border-radius: 15px;"
                    f" border: 2px solid transparent; color: {on}; font-weight: bold; font-size: 12px; }}"
                    f"QPushButton:hover {{ border-color: #a1a1aa; }}"
                    f"QPushButton:checked {{ border: 2px solid {on}; }}")
                d.clicked.connect(lambda _, s=sid: self._set_scheme(s))
                self._dot_group.addButton(d)
                self._dots[sid] = d
                nm = QLabel(SCHEMES[sid]["name"])
                nm.setObjectName("muted")
                nm.setAlignment(Qt.AlignHCenter)
                cell.addWidget(d, 0, Qt.AlignHCenter)
                cell.addWidget(nm, 0, Qt.AlignHCenter)
                row.addLayout(cell)
            grid.addLayout(row)
        lay.addLayout(grid)

        # 3. 自定义——一个彩虹色卡位 → Qt 色板
        lay.addWidget(QLabel("自定义", objectName="muted"))
        cust_row = QHBoxLayout()
        cust_row.setSpacing(10)
        self._cus_btn = QPushButton("🎨")
        self._cus_btn.setFixedSize(30, 30)
        self._cus_btn.setCheckable(True)
        self._cus_btn.setCursor(Qt.PointingHandCursor)
        self._cus_btn.setToolTip("自定义颜色——点击打开色板")
        self._cus_btn.setStyleSheet(
            "QPushButton { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,"
            "stop:0 #ff6b6b, stop:0.25 #ffd93d, stop:0.5 #6bcb77, stop:0.75 #4d96ff, stop:1 #b983ff);"
            " border-radius: 15px; border: 2px solid transparent; font-size: 10px; }"
            "QPushButton:hover { border-color: #a1a1aa; }"
            "QPushButton:checked { border: 2px solid #ffffff; }")
        self._cus_btn.clicked.connect(self._pick_custom)
        cust_row.addWidget(self._cus_btn)
        cus_lbl = QLabel("自定义色板")
        cus_lbl.setObjectName("muted")
        cust_row.addWidget(cus_lbl)
        cust_row.addStretch()
        lay.addLayout(cust_row)

        lay.addStretch()
        # 底部当前色值信息
        self._info = QLabel("")
        self._info.setObjectName("muted")
        self._info.setWordWrap(True)
        lay.addWidget(self._info)
        return panel

    # ─────────── 行为 ───────────
    def _set_theme(self, val: str):
        self._theme = val
        self._apply()

    def _set_scheme(self, sid: str):
        self._scheme = sid
        self._custom = DEFAULT_CUSTOM
        self._cus_btn.setChecked(sid == CUSTOM_ID)
        self._apply()

    def _pick_custom(self):
        """点自定义彩虹卡 → 弹出截图同款现代取色面板（tools/color_panel.py）。"""
        from agent.color_panel import ColorPanel  # 审核版工具（通过后入 agent/）
        panel = ColorPanel(initial=self._custom if self._scheme == CUSTOM_ID else "#e2a1bc",
                           parent=self)

        def _chosen(hx: str):
            self._custom = hx
            self._scheme = CUSTOM_ID
            self._cus_btn.setChecked(True)
            self._apply()
        panel.colorChanged.connect(_chosen)
        panel.popup_under(self._cus_btn)

    def _update_checks(self):
        """同步选中态与信息行。"""
        for sid, d in self._dots.items():
            d.setChecked(sid == self._scheme and self._scheme != CUSTOM_ID)
            # checked 打勾（scheme 模式）
            d.setText("✓" if d.isChecked() else "")
        theme_name = {"dark": "深色", "light": "浅色"}[self._pal["theme_mode"]]
        scheme = SCHEMES.get(self._scheme, {}).get("name", "自定义")
        self._info.setText(
            f"当前：{theme_name} · {scheme}\n"
            f"强调色 {self._pal['accent']} · 底 {self._pal['main_bg']}\n"
            f"审核 OK 后告诉我，样式合入控制台设置页。")


def main():
    app = QApplication(sys.argv)
    w = AppearancePreview()
    w._apply()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
