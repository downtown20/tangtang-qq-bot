#!python3.10
# ⚠️ 上面这行是 py launcher 的 shebang——只能有 #!python3.10，不能带行内注释（launcher 会把 "#" 当文件打开报错）。
# 用途：机器上有 3.14 时 py 默认指向新版本，而 RVC 依赖(av/faiss)只在 3.10 环境。
"""
🍬 小糖糖桌面控制台 v3 — PySide6 版

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📖 给小白的使用说明
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【仪表盘】— 启动/停止糖糖，查看数据统计
  ・点「启动 SnowLuma」→ 弹出 QQ 登录窗口 → 扫码登录
  ・点「启动小糖糖」→ 连接 SnowLuma 后糖糖上线
  ・下方统计卡片可点击展开详细数据浏览

【设置】— 修改所有配置，左侧 15 个分类可切换
  ・改完点「保存设置」→ 写入 config.yaml
  ・群管理和黑名单支持增删改

【图库】— 管理本地图片库（share_images/ 文件夹）
  ・上传图片 + 编辑配文 → 糖糖自动分享
  ・拖拽图片可调整播放顺序
  ・分类筛选只看某个文件夹的图

【日志】— 查看糖糖运行日志
  ・点「诊断」自动分析问题和性能

【🎨 外观】（在设置页最下面）— 自定义界面
  ・点色块弹出取色器，可调颜色和透明度
  ・背景图 + 不透明度滑条
  ・字体大小调节
  ・「实时预览」即时看效果，「保存设置」持久化

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🏗 技术架构：PySide6 (Qt for Python)
  ・QMainWindow + QStackedWidget 四页切换
  ・QSS 样式表实现暗色主题
  ・QProcess 管理糖糖进程
  ・QSystemTrayIcon 系统托盘
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import time
from collections import deque
from pathlib import Path

from dotenv import load_dotenv, set_key

# 项目内模块
from agent.paths import find_python310
from agent.theme_palettes import (CUSTOM_ID, DEFAULT_CUSTOM, DEFAULT_SCHEME,
                                  SCHEMES, SCHEME_ORDER, resolve_palette)
from agent.ui_anim import SlideStack, HoverEngine, animate_color, animate_prop, fade_in
from agent.icons import icon as lucide, icon_with_palette as lucide_pal, clear_cache as lucide_clear

import yaml
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QFrame, QLabel, QPushButton, QAbstractButton, QCheckBox,
    QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox,
    QScrollArea, QStackedLayout, QListWidget, QListWidgetItem,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QComboBox, QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout,
    QButtonGroup, QFileDialog, QInputDialog, QMessageBox, QDialog,
    QDialogButtonBox, QMenu, QSystemTrayIcon, QApplication, QSizePolicy,
    QAbstractItemView, QSplitter, QStyle, QStyleOptionButton, QProxyStyle,
)
from PySide6.QtCore import (
    Qt, QTimer, QThread, Signal, QSize, QProcess, QMimeData, QUrl, QEventLoop,
    QSharedMemory, QRect, QRectF, QPointF, QPoint, QVariantAnimation, QEasingCurve, QEvent,
    QDateTime, QObject,
)
from PySide6.QtGui import (
    QFont, QIcon, QPixmap, QPainter, QColor, QPen, QBrush, QLinearGradient,
    QAction, QFontDatabase, QDesktopServices, QWheelEvent, QFontMetrics, QPainterPath,
    QRadialGradient,
)

# ═══════════════════════════════════════════════════════════
# 路径（兼容 exe 打包和源码运行）
# ═══════════════════════════════════════════════════════════
def _get_base() -> Path:
    """获取项目根目录。exe 打包后同目录，源码运行时为脚本所在目录。"""
    if getattr(sys, 'frozen', False):
        # PyInstaller 打包后运行
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE = _get_base()
CONFIG_PATH = BASE / "config.yaml"
BACKUP_PATH = BASE / "config.yaml.bak"
SHARE_DIR = BASE / "share_images"
_SL_DIRS = sorted((BASE / "SnowLuma").glob("SnowLuma-v*"), key=lambda p: [int(x) for x in p.name.replace("SnowLuma-v", "").split("-")[0].split(".")], reverse=True)
_SL_DIR = _SL_DIRS[0] if _SL_DIRS else (BASE / "SnowLuma")
SNOWLUMA_EXE = _SL_DIR / "launcher.bat"
SNOWLUMA_DIR = _SL_DIR

TASK_GATE_DEFAULTS = {
    "tasks.text_action_outbox_enabled": True,
    "tasks.media_action_outbox_enabled": False,
}
_PIPELINE_DOWNLOADS = {
    # kind: "dl" = 有在线下载地址（url 空 = 作者待提供）| "env" = 需安装器安装 | "pkg" = 随项目/RVC 自带（缺失属异常）
    "HuTao 模型": ("Retrieval-based-Voice-Conversion-WebUI/assets/weights/hutao.pth",
                   "", "dl"),   # 随发布「歌唱模型包」附件分发；Release 地址发布后填入
    "HuTao 索引": ("Retrieval-based-Voice-Conversion-WebUI/assets/weights/hutao.index",
                   "", "dl"),   # 同上（附件包内，本机已有 55M+301M）
    "HuBERT 模型": ("Retrieval-based-Voice-Conversion-WebUI/assets/hubert/hubert_base.pt",
                    "https://github.com/lj1995/VoiceConversionWebUI/raw/master/assets/hubert/hubert_base.pt", "dl"),
    "RVC infer_cli.py": ("Retrieval-based-Voice-Conversion-WebUI/tools/infer_cli.py", "", "pkg"),
    "Demucs 环境": ("venv_demucs/Scripts/python.exe", "", "env"),
    "人声分离脚本": ("tools/separate_vocals.py", "", "pkg"),
}

# 「打开目录」失败时的解释表。
#
# 2026-09-19 主人实机反馈后加：原来只弹「找不到目录：<路径>」——用户拿到一个路径，
# 不知道**为什么会缺**、也不知道下一步做什么。他当时的原话是「我选的模式是基础的文字
# 对话，是基础的文字对话没有这个功能嘛？确认清楚，如果是就给足提示」。
#
# 口径：每个按钮都要能回答「这个目录属于哪个功能、怎么才能有」。
# 键用页面上看得懂的功能名，不用内部代号。查询走 _missing_dir_message（纯函数，可测）。
_MISSING_DIR_HINT = {
    "voice": (
        "语音模型还没装",
        "这个目录属于「语音」功能（让糖糖开口说话）。\n\n"
        "安装时如果没勾「发语音+听懂语音消息」，就不会有它——模型约 6.5G，"
        "是可选下载项，没有随包附带。\n\n"
        "补装：重跑 安装糖糖.bat，勾上那一项，装完这里就有了。",
    ),
    "rvc": (
        "翻唱制作组件不在发布包里",
        "这个目录属于「歌唱工作室」——把一首歌换成糖糖声线的**制作工具**，"
        "和点歌播放不是一回事。\n\n"
        "它需要 RVC 运行环境 + HuTao 模型，体积很大，没有随发布包附带。\n"
        "想听糖糖唱歌**不需要**它：41 首预录成品已经随包自带，点歌即播。",
    ),
}


def _missing_dir_message(path, feature=None) -> tuple[str, str]:
    """目录缺失时该说什么。纯函数——**弹窗里显示什么，测试就断言什么**。

    刻意不放进方法里：放进 GUI 就得把窗口开起来才能测，那条测试迟早被跳过。
    """
    return _MISSING_DIR_HINT.get(feature, ("目录不存在", "本机没有这个目录。"))


# RVC 索引必须用**相对路径**传给 infer_cli.py，且 cwd 必须是 RVC 目录。
#
# 2026-09-19 实测（faiss 1.7.4 + 中文 Windows）：faiss 的 C++ 端用窄字符 fopen 打开索引，
# 绝对路径里的中文（项目装在中文名目录下时必然如此）会被按 ANSI 代码页解释成乱码：
#
#     绝对路径（含中文）   RuntimeError: FileIOReader ... could not open
#     相对路径（纯 ASCII） ntotal=95555   ← 正常
#
# 而且 RVC 对索引加载失败是**静默降级**的——照样出音频，只是音色相似度更低，
# 界面上一个错都不报。开发机上因此一直跑在无索引模式，没人发现。
RVC_INDEX_REL = "assets/weights/hutao.index"

PROJECT_GITHUB_URL = "https://github.com/downtown20/tangtang-qq-bot" # Star 引导跳转（2026-09-05）
# SnowLuma 官方发布页：糖糖连 QQ 必需，但它是第三方协议端，EULA 5.4 禁止
# 「并入第三方安装包」与「通过自动化脚本部署」——本项目不能随包发、也不能代下，
# 只能把用户引到官方页由他自己下载一次（2026-09-18 核实 EULA 原文）
SNOWLUMA_RELEASE_URL = "https://github.com/SnowLuma/SnowLuma/releases"

# LLM 提供商 → 显示名（仪表盘/侧栏跟随设置；custom 显示 base_url 主机便于辨识）
PROVIDER_NAMES = {"deepseek": "DeepSeek", "xai": "xAI Grok", "anthropic": "Claude",
                  "openai": "OpenAI", "custom": "自定义"}


def _provider_label(provider: str) -> str:
    """提供商值 → 界面显示名（未知名保持原值）。"""
    return PROVIDER_NAMES.get(provider, provider or "?")

# ═══════════════════════════════════════════════════════════
# 配色 —「雾玫瑰·石墨紫」主题（2026-09-05 发布初始配色 v2）
# 低饱和莫兰迪玫瑰 × 石墨微紫底：安静沉稳，粉只作点缀——优雅而非糖果甜。
# 主色以「点缀」用：大面积留给石墨层次，accent 用在导航选中/hover/按钮。
# 注意：发布初始 config.example 的 appearance 与此套对齐（tools/准备发布.py）。
# ═══════════════════════════════════════════════════════════
PINK = "#e2a1bc"        # 雾玫瑰（主色调/点缀）
PINK_HOVER = "#eab6cd"  # 提亮一档（悬停）
RED = "#d0677d"          # 收敛玫红（删除/警告）
SIDEBAR_BG = "#100d13"  # 石墨微紫（侧边栏，最深）
MAIN_BG = "#16131a"     # 石墨微紫（主背景）
CARD_BG = "#1e1a24"     # 浅石墨紫（卡片）
CARD_BORDER = "#2b2433" # 石墨紫灰（卡片边框）
TEXT_PRIMARY = "#ece7ec" # 暖白（主文字）
TEXT_SECONDARY = "#a79aa6" # 灰紫（次要文字）
TEXT_MUTED = "#655c6a"   # 深灰紫（提示文字）
GREEN = "#6fbf9e"        # 薄荷绿（成功）
ORANGE = "#e0a868"        # 暖橘（警告）
LOG_BG = "#0e0a10"       # 极深紫（日志背景）
LOG_TEXT = "#dfb3c6"      # 雾玫瑰浅粉（日志文字）
GREEN_HOVER = "#7fc9ac"  # 薄荷绿提亮（greenBtn hover）
DANGER_BG = "#3d1f28"    # 深玫（dangerBtn 底，与 RED 同族收敛）
DANGER_HOVER = "#52303a" # 深玫提亮（dangerBtn hover）
DANGER_TEXT = "#e58a9a"  # 雾玫亮（dangerBtn 字）

# ── 设计令牌（2026-09-05 UI 质感升级批 1）──────────────
# Fluent 式 tokens：控件圆角/容器圆角/标准控件高/间距基数/暗色高光描边
RADIUS_CONTROL = 6         # 控件级圆角（按钮/输入框/下拉）
RADIUS_CARD = 10           # 容器级圆角（卡片/面板/导航项）
HEIGHT_CONTROL = 32        # 标准控件高（Fluent 32px）
SPACING_UNIT = 4           # 间距基数（布局走 4 的倍数）
HIGHLIGHT_BORDER = "rgba(255, 255, 255, 0.07)" # 暗色 1px 顶部高光（代替阴影分层）

# ═══════════════════════════════════════════════════════════
# QSS 动态生成
def _system_is_dark() -> bool:
    """跟随系统模式：读系统深浅色（Qt 6.5+ QStyleHints；异常回落 False=浅色）。"""
    try:
        app = QApplication.instance()
        return app.styleHints().colorScheme() == Qt.ColorScheme.Dark if app else False
    except Exception:
        return False


def _resolve_appearance(cfg: dict) -> dict:
    """cfg → 外观色板（单一决策源）。旧逐色键存在且无新键时按旧 accent 映射为 custom。"""
    a = cfg.get("appearance", {}) if cfg else {}
    legacy = ("accent_color" in a and "color_scheme" not in a
              and "theme_mode" not in a)  # 任一新键出现即退出 legacy（2026-09-06）
    if legacy:
        theme_mode = "dark" # 旧键无深浅概念——按深色渲染延续观感
        scheme_id = CUSTOM_ID
        custom = str(a.get("accent_color", DEFAULT_CUSTOM))
    else:
        theme_mode = str(a.get("theme_mode", "dark"))
        scheme_id = str(a.get("color_scheme", DEFAULT_SCHEME))
        custom = str(a.get("custom_color", DEFAULT_CUSTOM))
    if not custom.startswith("#"):
        custom = DEFAULT_CUSTOM
    pal = resolve_palette(theme_mode, scheme_id, custom, is_system_dark=_system_is_dark())
    pal["_legacy"] = legacy
    return pal


# ═══════════════════════════════════════════════════════════
def _build_stylesheet(cfg: dict) -> str:
    """根据配置动态生成 QSS（外观模型 2026-09-06：主题模式 + 配色方案 → 色板）。

    模板内颜色一律经同名局部遮蔽取 palette 值（模板本身零改动）；
    RADIUS_* 几何令牌 2026-09-06 起按主题派生（浅色=Aether 胶囊/24px，深色=模块常量），
    HEIGHT_CONTROL 仍为全局常量。
    """
    a = cfg.get("appearance", {}) if cfg else {}
    font_size = int(a.get("font_size", 13))
    pal = _resolve_appearance(cfg)

    # ── palette → 模板同名局部（遮蔽模块常量）──────────────
    pink = pal["accent"]
    pink_hover = pal["accent_hover"]
    sidebar_bg = pal["sidebar_bg"]
    main_bg = pal["main_bg"]
    card_bg = pal["card_bg"]
    card_border = pal["card_border"]
    hover_bg = pal["hover_bg"]
    text_primary = pal["text_primary"]
    log_bg = pal["log_bg"]
    log_text = pal["log_text"]
    input_bg = pal["input_bg"]
    TEXT_PRIMARY = pal["text_primary"]
    TEXT_SECONDARY = pal["text_secondary"]
    TEXT_MUTED = pal["text_muted"]
    GREEN = pal["green"]
    GREEN_HOVER = pal["green_hover"]
    DANGER_BG = pal["danger_bg"]
    DANGER_HOVER = pal["danger_hover"]
    DANGER_TEXT = pal["danger_text"]
    action_bg = pal["hover_bg"]
    action_hover = pal["card_border"]

    # ── 日志页控件语义色（浅/深两态，2026-09-06：浅色主题黑块修复）──
    _dk = pal["theme_mode"] == "dark"
    log_pill_on_bg, log_pill_on_text, log_pill_on_border = _log_pill_on(pal)
    log_tgt_blue = "#82cfff" if _dk else "#1664b3"
    log_tgt_orange = "#ffa07a" if _dk else "#b4551a"
    log_excl_text = "#f48771" if _dk else "#b33c0e"
    _cat_on = "rgba(" + _hex_to_rgba(pink, 18) + ")"
    _cat_hover = hover_bg

    # ── 四态规范派生色（2026-09-06 令牌表 §4.2：pressed = 填充下压一档）──
    pink_pressed = _darken(pink, 0.85)
    green_pressed = _darken(GREEN, 0.85)
    danger_pressed = _darken(DANGER_BG, 0.88)
    action_pressed = _darken(action_hover, 0.85)
    pressed_fill = _darken(card_bg, 0.96)      # 透明底按钮的 pressed 填充
    pressed_rgba = _hex_to_rgba(pink, 14)      # navBtn/catBtn 的 pressed 档（hover 8% → 14%）

    # ── Aether 设计系统令牌（2026-09-06，UI参考 bad-dodo-83 system.css 映射；
    #    浅色 = 胶囊几何/炭黑锚点/玻璃描边，深色 = 既有 6/10 几何零改动）──
    radius_control = "999" if not _dk else str(RADIUS_CONTROL)   # 交互件：浅色全胶囊
    radius_card = "24" if not _dk else str(RADIUS_CARD)          # 容器：浅色 24px（rounded.xl）
    _cb_size = 22 if not _dk else 16                             # 勾选框 22px（Aether checkbox）
    _bevel = "rgba(255, 255, 255, 0.9)" if not _dk else HIGHLIGHT_BORDER
    _input_pad = "6px 18px" if not _dk else "6px 10px"           # 胶囊输入内边距
    # 主色按钮浅色描边化（QSS 填充不剪裁圆角——胶囊+实心填充=方角穿帮，描边语言安全）
    _pink_bg = pink if _dk else "transparent"
    _pink_fg = "white" if _dk else "#1d1d1f"   # 浅色=实心粉胶囊配墨字（自绘接管，见 _srf_pink）
    _pink_bdr = "1px solid transparent" if _dk else f"1px solid {pink}"
    _pink_hov = pink_hover if _dk else f"rgba({_hex_to_rgba(pink, 12)})"
    _pink_prs = pink_pressed if _dk else f"rgba({_hex_to_rgba(pink, 22)})"
    _green_bg = GREEN if _dk else "transparent"
    _green_fg = "white" if _dk else "#1d1d1f"
    _green_bdr = "1px solid transparent" if _dk else f"1px solid {GREEN}"
    _green_hov = GREEN_HOVER if _dk else f"rgba({_hex_to_rgba(GREEN, 12)})"
    _green_prs = green_pressed if _dk else f"rgba({_hex_to_rgba(GREEN, 22)})"
    # 危险色语义修正（2026-09-17 主人反馈「浅色 × 删除按钮消失」）：
    # DANGER_BG = pal["danger_bg"] 是「浅底填充色」（浅色 #ffe9e7），当字色/描边用
    # 在白底上等于隐形。浅色前景/描边/状态洗色一律用强色 pal["red"]；
    # danger_bg 只作填充底（自绘芯片用，见 _feed_pill_style）。
    _danger_accent = str(pal.get("red", "#ff453a"))
    _dgr_bg = DANGER_BG if _dk else "transparent"
    _dgr_fg = DANGER_TEXT if _dk else _danger_accent
    _dgr_bdr = "1px solid transparent" if _dk else f"1px solid {_danger_accent}"
    _dgr_hov = DANGER_HOVER if _dk else f"rgba({_hex_to_rgba(_danger_accent, 12)})"
    _dgr_prs = danger_pressed if _dk else f"rgba({_hex_to_rgba(_danger_accent, 22)})"
    # 标签页（Aether tabs：浅色=玻璃胶囊容器+炭黑激活芯片；深色=既有下划线语言）
    _tab_fg = TEXT_MUTED if not _dk else TEXT_SECONDARY
    _tab_bb = "none" if not _dk else "2px solid transparent"
    _tab_on = "#1d1d1f" if not _dk else "transparent"
    _tab_on_fg = "#f5f5f7" if not _dk else pink
    _tab_bb_on = "none" if not _dk else pink
    # 浅色 aurora 洗底：页面透明让 AuroraWidget 透出；深色保持实底
    _base_bg = "transparent" if not _dk else main_bg
    # 侧栏指挥坞（浅色浮动玻璃容器；深色保持贴边）
    _sb_border = f"1px solid {card_border}" if not _dk else "none"
    _sb_radius = "24px" if not _dk else "0px"
    # 导航/分类激活语言：浅色=炭黑芯片白字（PillProxyStyle 自绘胶囊）；深色=accent 语言
    _nav_on_fg = "#f5f5f7" if not _dk else pink
    _nav_bar = "none" if not _dk else f"3px solid {pink}"
    # ── 浅色自绘胶囊接管（2026-09-17 修复「浅色实心胶囊层失效」）──
    # PillProxyStyle 拦截 CE_PushButtonBevel 自绘真胶囊（绕开 PySide6 6.11 QSS 圆角
    # 不渲染）。QStyleSheetStyle 只在规则里「没有 border 声明」时才把 bevel 委托给
    # base style（2026-09-17 最小复现：带 border → 委托 0 次；只有 background 或
    # 无 surface 声明 → 正常委托）。所以浅色下这 5 类按钮的 QSS 只留文字/布局属性，
    # 表面交给代理；深色分支一字不改（仍由 QSS 画实心色）。
    _srf_note = "/* 浅色：表面由 PillProxyStyle 自绘（勿加 border——会杀死 bevel 委托） */"
    # 深色保持原声明顺序（background → color → border），逐字节零回归
    _body_pink = (f"background-color: {_pink_bg};\n    color: {_pink_fg};\n    border: {_pink_bdr};"
                  if _dk else f"{_srf_note}\n    color: {_pink_fg};")
    _body_green = (f"background-color: {_green_bg};\n    color: {_green_fg};\n    border: {_green_bdr};"
                   if _dk else f"{_srf_note}\n    color: {_green_fg};")
    _body_dgr = (f"background-color: {_dgr_bg};\n    color: {_dgr_fg};\n    border: {_dgr_bdr};"
                 if _dk else f"{_srf_note}\n    color: {_dgr_fg};")
    _btn_srf = (f"border: 1px solid transparent;\n    border-radius: {radius_control}px;"
                if _dk else _srf_note)
    _btn_hover_bdr = (f"border-color: {pink};" if _dk
                      else "/* 浅色：通用 hover 边框环停用（border 声明会杀死自绘委托） */")
    _btn_focus_bdr = (f"border-color: {pink};  /* 键盘焦点环——与 hover 同语言，鼠标点击后不突兀（2026-09-06 四态规范） */"
                      if _dk else "/* 浅色：通用 focus 边框环停用（同上） */")
    _disabled_guard = ("" if _dk else
                       "\n    border: 1px solid transparent;  /* 浅色：留住 border 关闭委托——disabled 交回 QSS 画 */")
    _nav_radius = f"border-radius: {radius_card}px;" if _dk else ""
    _nav_checked_bar = f"border-left: {_nav_bar};" if _dk else ""
    _nav_hover_bdr = ("border-color: transparent;  /* 导航已有背景反馈，去通用 hover 边框 */"
                      if _dk else "")
    _cat_radius = "border-radius: 6px;" if _dk else ""
    _cat_placeholder = ("border: 1px solid transparent;  /* focus 只变色不位移 */"
                        if _dk else "")
    _cat_focus = (f"border-color: {pink};  /* id 级 focus（通用 :focus 权重压不过 id 规则） */"
                  if _dk else f"color: {pink};  /* 浅色：焦点只变色（border 会杀死代理委托） */")

    return f"""
QMainWindow {{
    background-color: {main_bg};
}}
QWidget {{
    font-family: "Inter", "Microsoft YaHei UI";   /* Aether sans 链（无 Inter 时回落雅黑） */
    font-size: {font_size}px;
    color: {text_primary};
    background-color: {_base_bg};   /* 浅色透明让 AuroraWidget aurora 洗底透出；深色实底 */
}}
QLabel {{ background: transparent; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px; }}
QScrollBar::handle:vertical {{ background: {card_border}; border-radius: 5px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {pink}; }}
QTabWidget::pane {{ background: {card_bg}; border: 1px solid {card_border}; border-radius: {radius_card}px; }}
QTabBar::tab {{
    background: transparent;
    color: {_tab_fg};
    padding: 9px 16px;
    border: none;
    border-bottom: {_tab_bb};
    border-radius: {radius_control}px;
}}
QTabBar::tab:selected {{
    background: {_tab_on};            /* 浅色=Aether 炭黑激活芯片；深色=既有下划线语言 */
    color: {_tab_on_fg};
    border-bottom-color: {_tab_bb_on};
}}
QListWidget, QTableWidget {{ background-color: {card_bg}; border: 1px solid {card_border}; border-radius: {radius_card}px; padding: 4px; }}
QTableWidget#dataTable {{ background: {card_bg}; alternate-background-color: {sidebar_bg}; color: {text_primary}; border: 1px solid {card_border}; gridline-color: {card_border}; border-radius: {radius_card}px; }}
QTableWidget#dataTable QHeaderView::section {{ background-color: {hover_bg}; color: {pink}; padding: 6px 8px; border: none; font-weight: bold; }}
QListWidget::item:selected, QTableWidget::item:selected {{ background-color: rgba({_hex_to_rgba(pink, 20)}); color: {TEXT_PRIMARY}; }}
QComboBox {{ background-color: {input_bg}; border: 1px solid {card_border}; border-radius: {radius_control}px; padding: {_input_pad}; min-height: {HEIGHT_CONTROL}px; }}
QComboBox:focus {{ border-color: {pink}; }}
QCheckBox::indicator {{ width: {_cb_size}px; height: {_cb_size}px; background: transparent; border: none; }}  /* FluentCheckBox 自绘 */
QFrame#sidebar {{
    background-color: {sidebar_bg};
    border: {_sb_border};
    border-top: 1px solid {_bevel};
    border-radius: {_sb_radius};   /* 浅色=指挥坞浮动玻璃容器（24px）；深色贴边 */
}}
QFrame#card {{
    background-color: {card_bg};
    border: 1px solid {card_border};
    border-top: 1px solid {_bevel};
    border-radius: {radius_card}px;
    padding: 14px;
}}
QFrame#statItem {{
    background-color: {card_bg};
    border: 1px solid {card_border};
    border-top: 1px solid {_bevel};
    border-radius: {radius_card}px;
    padding: 8px;
}}
QFrame#statItem:hover {{
    border-color: {pink};
    background-color: rgba({_hex_to_rgba(pink, 8)});
}}
QPushButton {{
    {_btn_srf}
    padding: 8px 16px;
    font-weight: bold;
    min-height: {HEIGHT_CONTROL}px;
}}
QPushButton:hover {{
    {_btn_hover_bdr}
}}
QPushButton:focus {{
    {_btn_focus_bdr}
}}
QPushButton:pressed {{
    background-color: {pressed_fill};  /* 无 id 类按钮下压；id 类各自覆盖 */
}}
QPushButton:disabled {{
    color: {TEXT_MUTED};
    background-color: {main_bg};
    border-color: {card_border};
}}
QPushButton#pinkBtn:disabled, QPushButton#greenBtn:disabled,
QPushButton#dangerBtn:disabled, QPushButton#actionBtn:disabled {{
    background-color: {input_bg};
    color: {TEXT_MUTED};{_disabled_guard}
}}
QPushButton#logPill:disabled, QPushButton#logTarget:disabled,
QPushButton#logTargetBlack:disabled {{
    color: {TEXT_MUTED};
}}
QPushButton#navBtn {{
    background: transparent;
    color: {TEXT_SECONDARY};
    text-align: left;
    padding: 12px 18px;
    {_nav_radius}
    font-size: {font_size + 1}px;
    min-height: 0px;
}}
QPushButton#navBtn:checked {{
    background-color: rgba({_hex_to_rgba(pink, 18)});
    color: {_nav_on_fg};
    {_nav_checked_bar}
}}
QPushButton#navBtn:hover {{
    background-color: {hover_bg};  /* 2026-09-06 主人定：与功能内按钮（catBtn 等）悬浮一致——灰底而非粉底 */
    {_nav_hover_bdr}
}}
QPushButton#navBtn:pressed {{
    background-color: {pressed_fill};
}}
QPushButton#navBtn[rail="true"] {{
    padding: 12px 0px;  /* 收缩态：图标居中（2026-09-06 伸缩侧栏，主人定案居中） */
    text-align: center;
}}
QPushButton#catBtn {{
    background: transparent;
    color: {TEXT_SECONDARY};
    text-align: left;
    padding: 8px 12px;
    {_cat_radius}
    font-size: 12px;
    {_cat_placeholder}
}}
QPushButton#catBtn:checked {{
    background-color: {_cat_on};
    color: {_nav_on_fg};   /* 浅色=炭黑芯片白字（PillProxyStyle 自绘）；深色=accent */
}}
QPushButton#catBtn:hover {{
    background-color: {_cat_hover};
}}
QPushButton#catBtn:pressed {{
    background-color: rgba({pressed_rgba});
}}
QPushButton#catBtn:focus {{
    {_cat_focus}
}}
QPushButton#logPill {{
    background: transparent; border: none;
    color: {TEXT_SECONDARY};
    padding: 1px 10px; font-size: 11px;
}}
QPushButton#logPill:checked {{
    color: {log_pill_on_text};
}}
QPushButton#logPill:hover {{ background: transparent; }}  /* 底/边框由 PillButton 自绘（QSS 圆角病） */
QPushButton#logPill:pressed {{ background: transparent; }}  /* 同上 */
QPushButton#logPill:focus, QPushButton#logTarget:focus,
QPushButton#logTargetBlack:focus {{ border-color: {pink}; }}
QPushButton#logTarget, QPushButton#logTargetBlack {{
    background: {input_bg}; border: 1px solid {card_border};
    border-radius: 4px; padding: 2px 10px; font-size: 11px; text-align: left;
}}
QPushButton#logTarget {{ color: {log_tgt_blue}; }}
QPushButton#logTargetBlack {{ color: {log_tgt_orange}; }}
QPushButton#logTarget:hover, QPushButton#logTargetBlack:hover {{ border-color: {pink}; }}
QPushButton#logTarget:pressed, QPushButton#logTargetBlack:pressed {{ border-color: {pink}; }}
QLineEdit#logExclude {{
    background: {input_bg}; color: {log_excl_text}; border: 1px solid {card_border};
    border-radius: 4px; padding: 2px 6px; font-size: 11px;
}}
QPushButton#pinkBtn {{
    {_body_pink}
    padding: 10px 20px;
    font-size: {font_size}px;
}}
QPushButton#pinkBtn:hover {{
    background-color: {_pink_hov};
}}
QPushButton#pinkBtn:pressed {{
    background-color: {_pink_prs};
}}
QPushButton#greenBtn {{
    {_body_green}
    padding: 10px 20px;
}}
QPushButton#greenBtn:hover {{
    background-color: {_green_hov};
}}
QPushButton#greenBtn:pressed {{
    background-color: {_green_prs};
}}
QPushButton#actionBtn {{
    background-color: {action_bg};
    color: {text_primary};
    padding: 6px 14px;
}}
QPushButton#actionBtn:hover {{
    background-color: {action_hover};
}}
QPushButton#actionBtn:pressed {{
    background-color: {action_pressed};
}}
QPushButton#dangerBtn {{
    {_body_dgr}
    padding: 4px 10px;
}}
QPushButton#dangerBtn:hover {{
    background-color: {_dgr_hov};
}}
QPushButton#dangerBtn:pressed {{
    background-color: {_dgr_prs};
}}
QPushButton#subtleBtn {{
    background-color: transparent;
    color: {TEXT_MUTED};
    padding: 4px 8px;
    min-height: 0px;
    font-size: {font_size - 2}px;
}}
QPushButton#subtleBtn:hover {{
    color: {text_primary};
    background-color: {card_bg};
    border-color: transparent;  /* 弱按钮无需边框反馈 */
}}
QPushButton#subtleBtn:pressed {{
    color: {text_primary};
    background-color: {pressed_fill};
}}
QLabel#title {{
    font-size: {font_size + 11}px;
    font-weight: bold;
    color: {pink};
}}
QLabel#heading {{
    font-size: {font_size + 11}px;   /* Aether h2 24px（基准 13 + 11） */
    font-weight: 600;                /* Aether：标题不超过 600 */
    letter-spacing: -0.3px;          /* tracking-heading ≈ -0.012em */
    color: {text_primary};
}}
QLabel#body {{
    font-size: {font_size}px;
    color: {text_primary};
}}
QLabel#muted {{
    font-size: {font_size - 1}px;
    color: {TEXT_MUTED};
}}
QLabel#cardValue {{
    font-family: "JetBrains Mono", "Consolas";   /* Aether：数字走 mono 等宽 */
    font-size: {font_size + 5}px;
    font-weight: 600;
    color: {text_primary};
}}
QLabel#heroTitle {{
    font-size: {font_size + 17}px;
    font-weight: bold;
    color: {pink};
}}
QPushButton#starBtn {{
    background-color: transparent;
    border: 1px solid {card_border};
    border-radius: {radius_card}px;
    font-size: {font_size + 8}px;
    min-width: {HEIGHT_CONTROL + 6}px;
    min-height: {HEIGHT_CONTROL + 6}px;
    padding: 0px;
}}
QPushButton#starBtn:hover {{
    background-color: rgba({_hex_to_rgba(pink, 14)});
    border-color: {pink};
}}
QPushButton#starBtn:pressed {{
    background-color: rgba({_hex_to_rgba(pink, 24)});
}}
QMenu {{
    background-color: {card_bg};
    border: 1px solid {card_border};
    border-radius: 10px;
    padding: 6px;
}}
QMenu::item {{
    padding: 6px 22px 6px 14px;
    border-radius: 6px;
    color: {text_primary};
}}
QMenu::item:selected {{
    background-color: {hover_bg};
    color: {pink};
}}
QComboBox QAbstractItemView {{
    background-color: {card_bg};
    border: 1px solid {card_border};
    border-radius: 8px;
    padding: 4px;
    selection-background-color: rgba({_hex_to_rgba(pink, 18)});
    selection-color: {text_primary};
}}
QLabel#heroSub {{
    font-size: {font_size + 2}px;   /* Aether body 15px */
    letter-spacing: -0.3px;
    color: {TEXT_SECONDARY};
}}
QPlainTextEdit#logArea {{
    background-color: {log_bg};
    color: {log_text};
    font-family: "Consolas";
    font-size: {font_size - 1}px;
    border: 1px solid {card_border};
    border-radius: 6px;
}}
QPlainTextEdit#diagArea {{
    background-color: {log_bg};
    color: {TEXT_SECONDARY};
    font-family: "Consolas";
    font-size: {font_size - 1}px;
    border: 1px solid {card_border};
    border-radius: 6px;
}}
QTextEdit {{
    background-color: {input_bg};
    color: {text_primary};
    border: 1px solid {card_border};
    border-radius: {radius_control}px;
    padding: 5px;
    font-size: {font_size}px;
}}
QLineEdit {{
    background-color: {input_bg};
    color: {text_primary};
    border: 1px solid {card_border};
    border-radius: {radius_control}px;
    padding: {_input_pad};
    min-height: {HEIGHT_CONTROL}px;
    font-size: {font_size}px;
}}
QLineEdit:focus {{
    border-color: {pink};
}}
QSpinBox, QDoubleSpinBox {{
    background-color: {input_bg};
    color: {text_primary};
    border: 1px solid {card_border};
    border-radius: {radius_control}px;
    padding: {_input_pad};
    min-height: {HEIGHT_CONTROL}px;
    font-size: {font_size}px;
}}
QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {pink};
}}
QCheckBox {{
    spacing: 8px;
    color: {TEXT_SECONDARY};
    font-size: {font_size - 1}px;
}}
QCheckBox::indicator {{
    width: {_cb_size}px;
    height: {_cb_size}px;
    background: transparent;  /* 指示器由 FluentCheckBox 自绘覆绘（QSS 圆角病不剪裁填充） */
    border: none;
}}
QComboBox {{
    background-color: {input_bg};
    color: {text_primary};
    border: 1px solid {card_border};
    border-radius: {radius_control}px;
    padding: {_input_pad};
    min-height: {HEIGHT_CONTROL}px;
    font-size: {font_size}px;
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
}}
QComboBox QAbstractItemView {{
    background-color: {card_bg};
    color: {text_primary};
    selection-background-color: {pink};
    border: 1px solid {card_border};
    outline: none;
}}
QScrollBar:vertical {{
    background: {main_bg};
    width: 8px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {card_border};
    border-radius: 4px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: {pink};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    height: 8px;
    background: {main_bg};
}}
QScrollBar::handle:horizontal {{
    background: {card_border};
    border-radius: 4px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {pink};
}}
QListWidget {{
    background-color: transparent;
    border: none;
    outline: none;
}}
QListWidget::item {{
    background-color: {card_bg};
    border-radius: 4px;
    margin: 1px 0;
    padding: 0;
}}
QListWidget::item:alternate {{
    background-color: {sidebar_bg};
}}
QListWidget::item:selected {{
    background-color: rgba({_hex_to_rgba(pink, 25)});
    border: 1px solid {pink};
}}
QSplitter::handle {{
    background-color: {card_border};
    width: 1px;
}}
QMenu {{
    background-color: {card_bg};
    color: {text_primary};
    border: 1px solid {card_border};
    border-radius: 6px;
    padding: 4px;
}}
QMenu::item {{
    padding: 8px 32px;
    border-radius: 4px;
}}
QMenu::item:selected {{
    background-color: {pink};
}}
QMenu::separator {{
    height: 1px;
    background: {card_border};
    margin: 4px 8px;
}}
QDialog {{
    background-color: {main_bg};
}}
"""


def _hex_to_rgba(hex_color: str, opacity_pct: int) -> str:
    """将 #rrggbb 转换为 rgba(r, g, b, opacity%)"""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        return hex_color
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    alpha = max(0, min(100, opacity_pct)) / 100.0
    return f"{r}, {g}, {b}, {alpha:.2f}"


def _darken(hex_color: str, factor: float) -> str:
    """#rrggbb 按 factor 压暗（0.85 = 明度×0.85）——pressed 态派生色（2026-09-06 四态规范）。"""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        return hex_color
    r, g, b = (round(int(hex_color[i:i + 2], 16) * factor) for i in (0, 2, 4))
    return "#%02x%02x%02x" % (min(r, 255), min(g, 255), min(b, 255))


_RGBA_RE = re.compile(
    r"^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})"
    r"(?:\s*,\s*([\d.]+%?))?\s*\)$", re.IGNORECASE)


def _qcolor(value, fallback: str = "#000000") -> QColor:
    """宽容取色：hex / 颜色名交给 QColor；CSS rgb()/rgba() 手动解析。

    2026-09-17 修复「浅色黑线」：PySide6 6.11 的 QColor 不认 CSS rgb()/rgba()
    （返回 invalid，画出来是纯黑）。浅色配色板的 border/border_hi 是 rgba()
    字符串（深色是十六进制，所以只在浅色暴露），自绘控件直接 QColor(调色板
    字符串) 就会画出黑线。凡自绘取色一律走本函数，勿改回裸 QColor()。"""
    if isinstance(value, QColor):          # 已是 QColor（代理 pill_* 属性喂入）直通
        return value
    s = str(value).strip()
    m = _RGBA_RE.match(s)
    if m:
        r, g, b = (min(int(m.group(i)), 255) for i in (1, 2, 3))
        a = 255
        av = m.group(4)
        if av:
            a = (int(round(float(av[:-1]) * 255 / 100)) if av.endswith("%")
                 else int(round(float(av) * 255)))
        return QColor(r, g, b, max(0, min(a, 255)))
    c = QColor(s)
    return c if c.isValid() else QColor(fallback)


def _log_pill_on(pal: dict) -> tuple:
    """日志胶囊 checked 语义色 (bg, text, border)——浅/深双态，
    _build_stylesheet 与 _apply_theme（PillButton 自绘喂色）共用单一事实源。"""
    _dk = pal.get("theme_mode") == "dark"
    return ("#3a2a4e", "#c8a0e0", "#7b5ea7") if _dk else ("#f1e9fb", "#6d4a9e", "#c3a8e8")

# ═══════════════════════════════════════════════════════════
# 字体
# ═══════════════════════════════════════════════════════════
def _font(size: int, bold: bool = False) -> QFont:
    f = QFont("Microsoft YaHei UI", size)
    f.setBold(bold)
    return f

def _mono(size: int = 12) -> QFont:
    return QFont("Consolas", size)


# ═══════════════════════════════════════════════════════════
# 配置读写（不变）
# ═══════════════════════════════════════════════════════════
def load_config():
    load_dotenv()
    # 首启自举（2026-09-05）：发布包只带 config.example.yaml（脱敏模板），
    # 用户第一次启动控制台时自动复制成 config.yaml，避免 FileNotFoundError 闪退。
    if not CONFIG_PATH.exists():
        example = BASE / "config.example.yaml"
        if example.exists():
            shutil.copy2(example, CONFIG_PATH)
            print("📄 首次启动：已从 config.example.yaml 创建 config.yaml —— 请到设置页填写机器人 QQ、主人 QQ 与 API Key")
        else:
            print(f"⚠️  缺少配置文件: {CONFIG_PATH}（复制 config.example.yaml 为 config.yaml 后重启）")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    # 递归替换 ${VAR_NAME} 为环境变量值
    from main import _resolve_env_vars
    return _resolve_env_vars(raw)


def save_config_file(data):
    # UI 只持有已接入的字段；保存前从磁盘配置递归补回未接入字段，
    # 防止旧版/并行控制台把 tasks、扩展 provider 等配置静默删除。
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            existing = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        existing = {}

    def _merge_missing(dst, src):
        if not isinstance(dst, dict) or not isinstance(src, dict):
            return
        for key, value in src.items():
            if key not in dst:
                dst[key] = value
            elif isinstance(dst[key], dict) and isinstance(value, dict):
                _merge_missing(dst[key], value)

    _merge_missing(data, existing)
    shutil.copy2(CONFIG_PATH, BACKUP_PATH)
    # 2026-08-10 防物化：保存前把仍等于环境变量值的密钥字段恢复为 ${VAR} 占位符——
    # 否则 UI 保存会把原本的 ${DEEPSEEK_KEY} 这类占位符物化成明文密钥写回 config.yaml
    _RESTORE_ENV = [
        (("llm", "api_key"), "DEEPSEEK_KEY"),
        (("llm", "seductive_api_key"), "SEDUCTIVE_KEY"),
        (("llm", "vision", "api_key"), "QWEN_KEY"),
        (("napcat", "access_token"), "SNOWLUMA_TOKEN"),
    ]
    for path, env_name in _RESTORE_ENV:
        d = data
        ok = True
        for key in path[:-1]:
            if not isinstance(d, dict) or key not in d:
                ok = False
                break
            d = d[key]
        if ok and isinstance(d, dict) and path[-1] in d:
            env_val = os.environ.get(env_name, "")
            if env_val and d[path[-1]] == env_val:
                d[path[-1]] = "${" + env_name + "}"
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


class RoundDotButton(QAbstractButton):
    """纯自绘圆盘色卡按钮：实心圆 + 悬停/选中细环。

    实测 PySide6 6.11.1 的 QSS border-radius 不再剪裁 background-color（四角恒方、
    各控件类型与半径均复现），且全局 QPushButton min-height 会顶掉 setFixedSize——
    色卡自绘是唯一稳定纯圆路径。勿改回 QSS 实现（2026-09-06 像素验证定案）。"""

    def __init__(self, color: str | None = None, gradient_stops: list | None = None,
                 ring_hover="#a1a1aa", ring_checked="#ffffff", parent=None):
        super().__init__(parent)
        self._color = color
        self._stops = gradient_stops or []
        self._ring_hover = QColor(ring_hover)
        self._ring_checked = QColor(ring_checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)

    def _fill_brush(self) -> QBrush:
        if self._stops:
            g = QLinearGradient(0, 0, self.width(), self.height())
            for pos, col in self._stops:
                g.setColorAt(pos, QColor(col))
            return QBrush(g)
        return QBrush(QColor(self._color))

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        side = min(self.width(), self.height()) - 4
        disc = QRect(0, 0, side, side)
        disc.moveCenter(self.rect().center())
        p.setPen(Qt.NoPen)
        p.setBrush(self._fill_brush())
        p.drawEllipse(disc)
        if self.isChecked():
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(self._ring_checked, 2))
            p.drawEllipse(disc.adjusted(1, 1, -1, -1))
        elif self.underMouse():
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(self._ring_hover, 1.5))
            p.drawEllipse(disc.adjusted(1, 1, -1, -1))
        p.end()

    def sizeHint(self) -> QSize:
        return QSize(32, 32)


class HDivider(QFrame):
    """圆角分隔横条（2026-09-06 主人需求：卡片标题下的分隔线，圆角长方形）。

    自绘原因见 [[qss-radius-broken-pyside611]]——QSS background 圆角不剪裁。"""

    def __init__(self, color: str = "#333338", parent=None):
        super().__init__(parent)
        self._c = _qcolor(color)
        self.setFixedHeight(3)

    def set_color(self, color: str):
        self._c = _qcolor(color)
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(self._c)
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, 2), 1, 1)
        p.end()


class ThemeToggle(QWidget):
    """自绘拨杆开关（主人选定形态 A——2026-09-06 市场模板对照后拍板）。

    免疫 PySide6 6.11.1 QSS 圆角剪裁失效（轨道/圆钮均 paintEvent 绘制）。
    风格对齐 qfluentwidgets SwitchButton：胶囊轨道 + 白圆钮滑入。
    信号 theme_changed(bool)：True=深色。勿改回 QSlider/QSS 实现。"""

    theme_changed = Signal(bool)

    def __init__(self, dark: bool, accent: str, track_off: str, track_border: str,
                 parent=None):
        super().__init__(parent)
        self._dark = dark
        self._acc = _qcolor(accent)
        self._off = _qcolor(track_off)
        self._bord = _qcolor(track_border)
        self._x = 1.0 if dark else 0.0  # 圆钮位置 0..1
        self._hovered = False
        self._pressed = False
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)
        self.setFixedSize(52, 28)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName("主题开关：深色浅色")
        self.setToolTip("切换深色 / 浅色主题")

    def _on_anim(self, v):
        self._x = float(v)
        self.update()

    def _toggle(self):
        self._dark = not self._dark
        self._anim.stop()
        self._anim.setStartValue(self._x)
        self._anim.setEndValue(1.0 if self._dark else 0.0)
        self._anim.start()
        self.theme_changed.emit(self._dark)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._pressed = True
            self.update()
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._pressed = False
        self.update()
        if ev.button() == Qt.LeftButton and self.rect().contains(ev.position().toPoint()):
            self._toggle()
        else:
            super().mouseReleaseEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self._toggle()
        else:
            super().keyPressEvent(ev)

    def enterEvent(self, ev):
        self._hovered = True
        self.update()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hovered = False
        self.update()
        super().leaveEvent(ev)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = self.rect()
        track = QRectF(r.x() + 1.5, r.y() + 3.5, r.width() - 3, r.height() - 7)
        # 四态微态（对齐 qfluentwidgets SwitchButton：hover 提亮 / pressed 压暗）
        track_color = self._acc if self._dark else self._off
        if self._pressed:
            track_color = _darken(track_color.name(), 0.88)
        elif self._hovered:
            track_color = _darken(track_color.name(), 1.12)
        p.setPen(QPen(self._bord, 1))
        p.setBrush(QColor(track_color))
        p.drawRoundedRect(track, track.height() / 2, track.height() / 2)
        d = r.height() - 8
        x = 3 + self._x * (r.width() - d - 6)
        p.setPen(QPen(self._bord, 1))
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QPointF(x + d / 2, r.center().y()), d / 2, d / 2)
        # Fluent 风格的状态图标与焦点环：让拨杆在低对比配色下仍有明确语义
        p.setPen(QPen(self._acc if self._dark else self._off, 1.5))
        glyph = "☾" if self._dark else "☀"
        p.drawText(QRectF(x, r.y() + 1, d, d), Qt.AlignCenter, glyph)
        if self.hasFocus() or self.underMouse():
            p.setPen(QPen(QColor(self._acc), 1.2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(track.adjusted(-2, -2, 2, 2), track.height() / 2 + 2, track.height() / 2 + 2)
        p.end()


class PillButton(QPushButton):
    """自绘胶囊按钮——PySide6 6.11.1 QSS border-radius 不剪裁背景（真机像素实测 2026-09-06），
    圆角胶囊底/边框必须 paintEvent 自绘；文字仍走 QSS（logPill 规则只留 color/padding）。
    四态语言对齐 qfluentwidgets PillButton：checked hover 提亮 / pressed 压暗（_darken 派生）。
    颜色由 _apply_theme 经 set_pill_colors 喂入（与 HDivider.set_color 同模式，随主题刷新）。
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._p_bg = "#1f1f23"            # 常态填充（pal input_bg）
        self._p_border = "#333338"        # 常态边框（pal card_border）
        self._p_hover_border = "#f28db7"  # hover 边框（pal accent）
        self._p_on = "#3a2a4e"            # checked 填充（_log_pill_on 深色档）
        self._hovered = False
        self.setAttribute(Qt.WA_Hover, True)

    def set_pill_colors(self, bg: str, border: str, hover_border: str, on: str):
        self._p_bg, self._p_border, self._p_hover_border, self._p_on = bg, border, hover_border, on
        self.update()

    def enterEvent(self, ev):
        self._hovered = True
        self.update()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hovered = False
        self.update()
        super().leaveEvent(ev)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        r = rect.height() / 2
        if self.isChecked():
            fill = _darken(self._p_on, 0.88) if self.isDown() else (
                _darken(self._p_on, 1.12) if self._hovered else self._p_on)
            border = fill
        else:
            fill = _darken(self._p_bg, 0.96) if self.isDown() else self._p_bg
            border = self._p_hover_border if (self._hovered or self.isDown()) else self._p_border
        p.setPen(QPen(_qcolor(border), 1))
        p.setBrush(_qcolor(fill))
        p.drawRoundedRect(rect, r, r)
        p.end()
        super().paintEvent(ev)  # QSS 只画文字（logPill 规则 background: transparent; border: none）


class SmoothWheelEngine(QObject):
    """滚轮平滑引擎——移植自 qfluentwidgets `common/smooth_scroll.py` 的
    FixedStepSmoothScrollEngine（GPL-3.0，© zhiyiYo / PyQt-Fluent-Widgets，
    https://github.com/zhiyiYo/PyQt-Fluent-Widgets。2026-09-06 主人拍板直接复用，
    保留原作者版权声明）。

    一格滚轮拆成 24 帧（60fps × 400ms）线性缓动窗口，模拟惯性丝滑；
    触控板像素滚动（delta 非 120 倍数）不拦截，直通原生处理。
    """

    def __init__(self, viewport, scrollbar, orient: Qt.Orientation):
        super().__init__(viewport)
        self._viewport = viewport
        self._bar = scrollbar
        self._orient = orient
        self._fps = 60
        self._duration = 400
        self._step_ratio = 1.0
        self._acceleration = 1
        self._stamps = deque()
        self._steps_queue = deque()
        self._px_accum = 0.0  # 分数位移累积器——不足 1px 的帧位移攒够再挪（防取整吞掉）
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._smooth_move)

    def wheel(self, e: QWheelEvent) -> bool:
        """处理滚轮事件；返回 True=已接管（走平滑动画），False=直通。"""
        d = e.angleDelta()
        if self._orient == Qt.Vertical:
            if d.y() == 0:
                return False
            delta = d.y()
        else:
            if d.x() == 0:
                return False
            delta = d.x()
        if abs(delta) % 120 != 0:
            return False  # 触控板像素滚动直通

        now = QDateTime.currentDateTime().toMSecsSinceEpoch()
        self._stamps.append(now)
        while now - self._stamps[0] > 500:
            self._stamps.popleft()

        # 未处理事件频率 → 加速比（连滚越急越快）
        accel_ratio = min(len(self._stamps) / 15, 1)

        delta = delta * self._step_ratio
        if self._acceleration > 0:
            delta += delta * self._acceleration * accel_ratio

        self._steps_queue.append([delta, self._fps * self._duration / 1000])
        self._timer.start(int(1000 / self._fps))
        return True

    def _smooth_move(self):
        if not self._steps_queue:
            self._timer.stop()
            return

        total_delta = 0
        for item in self._steps_queue:
            total_delta += self._sub_delta(item[0], item[1], self._fps * self._duration / 1000)
            item[1] -= 1
        while self._steps_queue and self._steps_queue[0][1] == 0:
            self._steps_queue.popleft()

        if total_delta != 0:
            self._send(total_delta)
        if not self._steps_queue:
            self._timer.stop()

    def _sub_delta(self, delta, steps_left, steps_total):
        """线性缓动窗口：中点峰值、两端归零（ease in-out）。"""
        m = steps_total / 2
        x = abs(steps_total - steps_left - m)
        return 2 * delta / steps_total * (m - x) / m

    def _send(self, total_delta):
        """把插值增量换算成滚动条位移，经分数累积器直驱 setValue（2026-09-06 滚轮失效修复）：
        合成滚轮事件携带不足一格的小 delta 会被滚动条整数取整吞掉（真机实测），
        逐帧 round 同理归零——故按原生每格位移 = delta/120 × wheelScrollLines × singleStep
        累积分数，攒够整像素再挪，总位移与原生逐格完全一致。"""
        self._px_accum += total_delta / 120.0 * QApplication.wheelScrollLines() * self._bar.singleStep()
        step = int(self._px_accum)
        if step:
            self._bar.setValue(self._bar.value() - step)
            self._px_accum -= step


class OverlayScrollBar(QWidget):
    """自绘覆盖式滚动条（2026-09-06，架构参考 qfluentwidgets ScrollBar——只学思路，代码重写）。

    QSS `QScrollBar::handle` 的 border-radius 不剪裁背景（真机像素实测），圆头滑块必须自绘。
    - partner = 原滚动区的原生滚动条（AlwaysOff 隐藏）作真值源（range/value）
    - 本控件 overlay 在滚动区边缘：滑块常态 4px 圆头胶囊；悬停后（200ms 延迟）groove 淡入
      150ms + 滑块扩到 6px；点击槽跳页、拖拽滑块、滚轮转发 viewport
    - 滚轮经 SmoothWheelEngine 平滑（2026-09-06 主人点名丝滑动效）
    - 颜色由 _apply_theme 经 set_colors 喂入（与 HDivider/PillButton 同模式）
    """

    def __init__(self, orient, parent):
        """parent: QAbstractScrollArea（QTableWidget/QPlainTextEdit/QScrollArea/QListWidget 皆可）"""
        super().__init__(parent)
        self._orient = orient
        self._padding = 14
        self._min_handle = 30
        self._opacity = 0.0        # groove 淡入进度（同时驱动滑块 4→6px 展开）
        self._hovered = False
        self._pressed = False
        self._press_off = 0
        self._c_handle = "#333338"   # 滑块常态（pal card_border）
        self._c_hover = "#f28db7"    # 滑块悬停（pal accent）
        self._c_groove = "#27272b"   # groove（pal hover_bg）
        if orient == Qt.Vertical:
            self._partner = parent.verticalScrollBar()
            parent.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        else:
            self._partner = parent.horizontalScrollBar()
            parent.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._partner.rangeChanged.connect(self._sync)
        self._partner.valueChanged.connect(self._sync)
        parent.installEventFilter(self)
        # 丝滑滚轮（2026-09-06）：viewport 上的滚轮事件经平滑引擎逐帧插值
        self._smooth = SmoothWheelEngine(parent.viewport(), self._partner, orient)
        parent.viewport().installEventFilter(self)
        self._fade = QVariantAnimation(self)
        self._fade.setDuration(150)
        self._fade.valueChanged.connect(self._on_fade)
        self._adjust_pos(parent.size())
        self._sync()
        self.raise_()

    # ── 外部接口 ──────────────────────────────────────────
    def set_colors(self, handle: str, hover: str, groove: str):
        self._c_handle, self._c_hover, self._c_groove = handle, hover, groove
        self.update()

    # ── 与 partner 同步 ────────────────────────────────────
    def _sync(self, *_a):
        self.setVisible(self._partner.maximum() > 0)
        self.update()

    def _slide_len(self):
        return max(self._groove_len() - self._handle_len(), 1)

    def _handle_len(self):
        p = self.parent()
        if self._orient == Qt.Vertical:
            total = self._partner.maximum() - self._partner.minimum() + p.height()
            s = int(self._groove_len() * p.height() / max(total, 1))
            return max(self._min_handle, min(s, self._groove_len()))
        total = self._partner.maximum() - self._partner.minimum() + p.width()
        s = int(self._groove_len() * p.width() / max(total, 1))
        return max(self._min_handle, min(s, self._groove_len()))

    def _groove_len(self):
        if self._orient == Qt.Vertical:
            return self.height() - 2 * self._padding
        return self.width() - 2 * self._padding

    def _adjust_pos(self, size):
        if self._orient == Qt.Vertical:
            self.resize(12, size.height() - 2)
            self.move(size.width() - 13, 1)
        else:
            self.resize(size.width() - 2, 12)
            self.move(1, size.height() - 13)

    def eventFilter(self, obj, ev):
        if obj is getattr(self.parent(), "viewport", lambda: None)() and ev.type() == QEvent.Wheel:
            # 平滑接管（True=吃掉原生处理；False=触控板/另一轴直通）
            if self._smooth.wheel(ev):
                return True
            return False
        if obj is self.parent() and ev.type() == QEvent.Resize:
            self._adjust_pos(ev.size())
        return super().eventFilter(obj, ev)

    # ── 悬停淡入/淡出 ─────────────────────────────────────
    def enterEvent(self, ev):
        self._hovered = True
        QTimer.singleShot(200, self._expand)
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hovered = False
        QTimer.singleShot(200, self._collapse)
        super().leaveEvent(ev)

    def _expand(self):
        if not self._hovered or self._pressed:
            return
        self._fade.stop()
        self._fade.setStartValue(self._opacity)
        self._fade.setEndValue(1.0)
        self._fade.start()

    def _collapse(self):
        if self._hovered or self._pressed:
            return
        self._fade.stop()
        self._fade.setStartValue(self._opacity)
        self._fade.setEndValue(0.0)
        self._fade.start()

    def _on_fade(self, v):
        self._opacity = float(v)
        self.update()

    # ── 交互 ──────────────────────────────────────────────
    def _handle_rect(self):
        """滑块几何（垂直：全宽内缩后 4+2*opacity 宽；水平同构）"""
        r = self.rect()
        if self._orient == Qt.Vertical:
            hw = 4 + int(2 * self._opacity)  # 4→6px 展开
            x = (r.width() - hw) // 2
            delta = int(self._partner.value() / max(self._partner.maximum(), 1) * self._slide_len())
            return QRect(x, self._padding + delta, hw, self._handle_len())
        hw = 4 + int(2 * self._opacity)
        y = (r.height() - hw) // 2
        delta = int(self._partner.value() / max(self._partner.maximum(), 1) * self._slide_len())
        return QRect(self._padding + delta, y, self._handle_len(), hw)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        # groove（悬停淡入）
        if self._opacity > 0.01:
            p.setOpacity(self._opacity * 0.75)
            p.setPen(Qt.NoPen)
            p.setBrush(_qcolor(self._c_groove))
            p.drawRoundedRect(QRectF(self.rect()), 6, 6)
        # 滑块（圆头胶囊，始终可见）
        p.setOpacity(1.0)
        p.setPen(Qt.NoPen)
        p.setBrush(_qcolor(self._c_hover if self._hovered else self._c_handle))
        hr = self._handle_rect()
        r = hr.width() / 2 if self._orient == Qt.Vertical else hr.height() / 2
        p.drawRoundedRect(QRectF(hr), r, r)
        p.end()

    def mousePressEvent(self, ev):
        self._pressed = True
        hr = self._handle_rect()
        if hr.contains(ev.pos()):
            self._press_off = (ev.pos().y() if self._orient == Qt.Vertical else ev.pos().x()) - (
                hr.y() if self._orient == Qt.Vertical else hr.x())
            return
        # 点击槽：跳到对应位置（滑块中点对准点击处）
        if self._orient == Qt.Vertical:
            pos = ev.pos().y() - self._padding - self._handle_len() / 2
        else:
            pos = ev.pos().x() - self._padding - self._handle_len() / 2
        v = int(pos / self._slide_len() * self._partner.maximum())
        self._partner.setValue(max(0, min(v, self._partner.maximum())))
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if not self._pressed:
            return
        if self._orient == Qt.Vertical:
            pos = ev.pos().y() - self._press_off - self._padding
        else:
            pos = ev.pos().x() - self._press_off - self._padding
        v = int(pos / self._slide_len() * self._partner.maximum())
        self._partner.setValue(max(0, min(v, self._partner.maximum())))
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._pressed = False
        super().mouseReleaseEvent(ev)

    def wheelEvent(self, ev):
        QApplication.sendEvent(self.parent().viewport(), ev)


class FluentTip(QWidget):
    """自绘圆角 tooltip 浮窗——原生 QToolTip 走 QSS 圆角病（方角），故自绘
    （参考 qfluentwidgets ToolTip：无边框置顶 + 淡入 + 定时隐藏，代码重写）。
    颜色由 _apply_theme 经 set_colors 喂入。"""

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint |
                         Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._text = ""
        self._bg = "#272733"
        self._fg = "#f7f7fb"
        self._bord = "#f28db7"
        self._fade = QVariantAnimation(self)
        self._fade.setDuration(150)
        self._fade.valueChanged.connect(self.setWindowOpacity)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    def set_colors(self, bg: str, fg: str, border: str):
        self._bg, self._fg, self._bord = bg, fg, border
        self.update()

    def show_text(self, text: str, pos_global, duration: int = 3000):
        self._text = text
        fm = QFontMetrics(self.font())
        bound = fm.boundingRect(QRect(0, 0, 380, 1000), Qt.TextWordWrap, text)
        self.resize(bound.width() + 26, max(bound.height() + 16, 30))
        screen = QApplication.primaryScreen().availableGeometry()
        x = max(screen.left() + 4, min(pos_global.x() + 14, screen.right() - self.width() - 4))
        y = max(screen.top() + 4, min(pos_global.y() + 18, screen.bottom() - self.height() - 4))
        self.move(x, y)
        self._fade.stop()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()
        self.show()
        if duration > 0:
            self._hide_timer.start(duration)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(_qcolor(self._bord), 1))
        p.setBrush(_qcolor(self._bg))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
        p.setPen(_qcolor(self._fg))
        p.drawText(self.rect().adjusted(13, 8, -13, -8), Qt.TextWordWrap | Qt.AlignVCenter, self._text)
        p.end()


class GlobalToolTipFilter(QObject):
    """应用级 ToolTip 拦截（移植 qfluentwidgets ToolTipFilter 思路，代码重写）：
    QEvent.ToolTip 一律吃掉原生 QToolTip，300ms 延迟后显示 FluentTip 自绘浮窗。
    挂载：`QApplication.instance().installEventFilter(GlobalToolTipFilter())`。"""

    def __init__(self):
        super().__init__()
        self._tip = FluentTip()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._show)
        self._pending = None  # (text, global_pos)

    def set_colors(self, bg: str, fg: str, border: str):
        self._tip.set_colors(bg, fg, border)

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.ToolTip:
            # 向父级冒泡找 tooltip 文本（子控件常有空 tooltip）
            w = obj
            text = ""
            while isinstance(w, QWidget):
                text = w.toolTip()
                if text:
                    break
                w = w.parentWidget()
            if not text:
                return False
            self._pending = (text, ev.globalPos())
            self._timer.start(300)
            return True  # 吃掉原生（方角）tooltip
        if ev.type() in (QEvent.Leave, QEvent.MouseButtonPress, QEvent.Wheel):
            self._timer.stop()
            self._tip.hide()
        return super().eventFilter(obj, ev)

    def _show(self):
        if self._pending is None:
            return
        text, pos = self._pending
        self._tip.show_text(text, pos)


class FluentCheckBox(QCheckBox):
    """自绘圆角勾选框——QSS ::indicator 的 checked 填充圆角病（方角，真机实测），
    参考 qfluentwidgets CheckBox：super() 只画文字（QSS 指示器已透明），
    paintEvent 覆绘圆角指示器 + 勾（四态：hover 亮边 / pressed 压暗 / checked accent）。
    颜色由 _apply_theme 经 set_check_colors 喂入。"""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._c_border = "#333338"   # pal card_border
        self._c_fill = "#1f1f23"     # pal input_bg
        self._c_accent = "#f28db7"   # pal accent
        self._c_check = "#ffffff"
        self._hovered = False
        self._pressed = False

    def set_check_colors(self, border: str, fill: str, accent: str, check: str = "#ffffff"):
        self._c_border, self._c_fill, self._c_accent, self._c_check = border, fill, accent, check
        self.update()

    def mousePressEvent(self, ev):
        self._pressed = True
        self.update()
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._pressed = False
        self.update()
        super().mouseReleaseEvent(ev)

    def enterEvent(self, ev):
        self._hovered = True
        self.update()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hovered = False
        self.update()
        super().leaveEvent(ev)

    def paintEvent(self, ev):
        super().paintEvent(ev)  # 文字 + 状态（QSS 指示器已透明）
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        opt = QStyleOptionButton()
        opt.initFrom(self)
        rect = self.style().subElementRect(QStyle.SE_CheckBoxIndicator, opt, self)
        rect.adjust(0, 0, -1, -1)
        if self.isChecked():
            fill = self._c_accent
            if self._pressed:
                fill = _darken(fill, 0.88)
            elif self._hovered:
                fill = _darken(fill, 1.12)
            border = fill
        else:
            fill = _darken(self._c_fill, 0.96) if self._pressed else self._c_fill
            border = self._c_accent if self._hovered else self._c_border
        p.setPen(QPen(_qcolor(border), 1))
        p.setBrush(_qcolor(fill))
        p.drawRoundedRect(QRectF(rect), rect.height() / 3, rect.height() / 3)  # Aether 22px≈7px 半径
        if self.isChecked():
            p.setPen(QPen(_qcolor(self._c_check), 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            path = QPainterPath()
            path.moveTo(rect.x() + 3.2, rect.y() + rect.height() / 2)
            path.lineTo(rect.x() + rect.width() * 0.44, rect.y() + rect.height() - 3.4)
            path.lineTo(rect.x() + rect.width() - 3, rect.y() + 3.2)
            p.drawPath(path)
        p.end()


class PillProxyStyle(QProxyStyle):
    """Aether 决策（2026-09-06）：浅色主题全局按钮胶囊自绘。

    QSS border-radius 不剪裁填充（圆角病真机实测）——实心胶囊按钮无法用 QSS 实现。
    代理样式拦截 CE_PushButtonBevel：按 widget 属性（pill_fill/pill_border/pill_on）
    自绘胶囊填充与描边；文字仍由 QStyleSheetStyle 绘制（QSS 文字色照常）。
    深色主题 set_enabled(False) 整体直通——深色零改动。
    颜色由 _apply_theme 经 _feed_pill_style 按 objectName 喂入；PillButton 自绘类直通不拦截。
    """

    def __init__(self):
        super().__init__()  # 包裹当前 app style（QStyleSheetStyle）
        self._enabled = False

    def set_enabled(self, on: bool):
        self._enabled = on

    def drawControl(self, element, opt, painter, widget=None):
        if (element == QStyle.CE_PushButtonBevel and self._enabled
                and isinstance(widget, QPushButton)
                and not isinstance(widget, PillButton)
                and widget.property("pill_fill") is not None):
            self._draw_pill(opt, painter, widget)
            return
        super().drawControl(element, opt, painter, widget)

    def _draw_pill(self, opt, painter, widget):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(opt.rect).adjusted(0.5, 0.5, -0.5, -0.5)
        r = rect.height() / 2
        checked = bool(opt.state & QStyle.State_On)
        hover = bool(opt.state & QStyle.State_MouseOver)
        down = bool(opt.state & QStyle.State_Sunken)
        fill = _qcolor(widget.property("pill_fill"))
        if checked and widget.property("pill_on") is not None:
            fill = _qcolor(widget.property("pill_on"))         # 激活芯片色（炭黑/accent-soft）
        if down:
            # 透明底按钮按下不压暗（透明/invalid 被压会变纯黑）——保持透明
            fill = _qcolor(_darken(fill.name(), 0.88)) if fill.alpha() > 0 else fill
        elif hover:
            hf = widget.property("pill_hover_fill")            # 透明底按钮的悬浮玻璃洗色
            if hf is not None:
                fill = _qcolor(hf)
            elif fill.alpha() > 0:
                fill = _qcolor(_darken(fill.name(), 1.06))     # 实心按钮悬浮微提亮
        border = fill
        if widget.property("pill_border") is not None and not checked:
            border = _qcolor(widget.property("pill_border"))
        elif checked and widget.property("pill_on_border") is not None:
            border = _qcolor(widget.property("pill_on_border"))
        painter.setPen(QPen(border, 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, r, r)
        painter.restore()


class AuroraWidget(QWidget):
    """Aether aurora 洗底（2026-09-06）：珍珠底 + 三团径向极光（system.css body 背景映射，
    cx 12%/-10% #c9d6ff、95%/10% #e3d3ff、55%/110% #d8e4ff，60% 处透明）。
    浅色绘制；深色=纯 main_bg 实色。_apply_theme 经 set_theme 切换。"""

    def __init__(self):
        super().__init__()
        self._light = True
        self._dark_bg = QColor("#17171a")

    def set_theme(self, light: bool, main_bg: str):
        self._light = light
        self._dark_bg = QColor(main_bg)
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        w, h = self.width(), self.height()
        if not self._light:
            p.fillRect(self.rect(), self._dark_bg)
            p.end()
            return
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor("#eef1f6"))  # pearl

        def wash(cx, cy, rad, color):
            g = QRadialGradient(QPointF(w * cx, h * cy), max(w, h) * rad)
            g.setColorAt(0.0, QColor(color))
            c = QColor(color)
            c.setAlpha(0)   # ⚠️ Qt 8 位 hex 是 #AARRGGBB（非 CSS 的 #RRGGBBAA），必须 setAlpha
            g.setColorAt(0.6, c)
            p.fillRect(self.rect(), QBrush(g))

        wash(0.12, -0.10, 0.55, "#c9d6ff")
        wash(0.95, 0.10, 0.48, "#e3d3ff")
        wash(0.55, 1.10, 0.42, "#d8e4ff")
        p.end()


# ═══════════════════════════════════════════════════════════
# 主窗口
# ═══════════════════════════════════════════════════════════
class TangTangQtConsole(QMainWindow):
    # 记忆快照打包完成信号（后台线程 → 主线程）
    _pack_done_sig = Signal(bool)

    def __init__(self):
        super().__init__()
        self._pack_done_sig.connect(self._on_pack_done)
        # 单实例锁：防止多次启动导致托盘图标堆积
        self._instance_lock = QSharedMemory("TangTangConsole")
        if self._instance_lock.attach():
            # 已有实例在运行
            QMessageBox.warning(None, "已在运行", "糖糖控制台已经在运行了～\n请查看系统托盘或任务栏。")
            sys.exit(0)
        if not self._instance_lock.create(1):
            # 创建失败——可能是上次异常退出残留，先清理再试
            self._instance_lock.detach()
            self._instance_lock.create(1)
        self.setWindowTitle("🍬 小糖糖 控制台")
        self.resize(1100, 760)
        self.setMinimumSize(900, 620)

        self.cfg = load_config()
        self._sugar_process: QProcess | None = None
        self._manual_stop: bool = False  # 手动停止标记——区别"定时重启自动退出"与"用户主动停止"
        self._packing: bool = False  # 打包进行中标记（防重复触发）
        self._exiting: bool = False  # 关闭流程进行中标记（打包完成后退出）
        self._intro_played: bool = False  # 2026-09-05 批 4：仪表盘首屏入场门闩（定时刷新不重播）
        self._snowluma_proc = None
        self._diag_worker: DiagnosticWorker | None = None
        self._overlay_bars: list[OverlayScrollBar] = []  # 自绘覆盖滚动条（_apply_theme 喂色）
        self._tooltip_filter = GlobalToolTipFilter()  # 全局自绘 tooltip（原生方角替换，2026-09-06）
        QApplication.instance().installEventFilter(self._tooltip_filter)

        # ── 记忆自动同步闭环 ──
        # 开机 5 秒后自动解包（快照比本地新才执行，糖糖未启动时安全）
        QTimer.singleShot(5000, self._auto_unpack_memory)
        # 🔔 SnowLuma 更新检查（2026-08-16 QQ 9.9.33 注入事故——OIDB 挂，
        # 等 SnowLuma 新版适配；GitHub API 可达，开机后台查一次）
        QTimer.singleShot(8000, self._check_snowluma_update)
        # 每 30 分钟自动打包一次——关机打包可能来不及，定时打包保证快照不旧于 30 分钟
        self._pack_timer = QTimer(self)
        self._pack_timer.timeout.connect(self._auto_pack_memory)
        self._pack_timer.start(30 * 60 * 1000)
        # Windows 关机/注销前尽力打包一次
        app = QApplication.instance()
        if app is not None:
            app.commitDataRequest.connect(self._on_commit_data)

        self._apply_theme()  # 先设 QSS（UI 未建，背景图跳过）
        self._build_ui()
        self._apply_theme()  # UI 已建，设置背景图
        self._setup_tray()
        self._navigate("dashboard")
        self._refresh_stats()

        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._sync_snowluma_status)
        self._status_timer.start(5000)
        self._sync_snowluma_status()

        # 2026-09-05 批 3：窗口启动淡入（windowOpacity 走合成器，无离屏代价）
        self.setWindowOpacity(0.0)
        animate_prop(self, b"windowOpacity", 0.0, 1.0, 250)
        # 批 4：仪表盘首屏卡片交错入场（一次性，_refresh_stats 不重播）
        QTimer.singleShot(80, self._play_dashboard_intro)

    # ═══════════════════════════════════════════════════════
    # UI 骨架
    # ═══════════════════════════════════════════════════════
    def _build_ui(self):
        # Aether aurora 洗底（2026-09-06）：AuroraWidget 作中央底层，内容透明浮于其上
        self._aurora = AuroraWidget()
        self.setCentralWidget(self._aurora)
        aurora_lay = QVBoxLayout(self._aurora)
        aurora_lay.setContentsMargins(0, 0, 0, 0)
        aurora_lay.setSpacing(0)
        central = QWidget()
        aurora_lay.addWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._build_sidebar(root)
        self._build_pages(root)

    # ═══════════════════════════════════════════════════════
    # 侧边栏
    # ═══════════════════════════════════════════════════════
    def _build_sidebar(self, root: QHBoxLayout):
        self._sidebar = QFrame()
        self._sidebar.setObjectName("sidebar")
        self._sidebar.setFixedWidth(200)
        self._sidebar.installEventFilter(self)  # 悬浮展开（2026-09-06 伸缩侧栏）
        self._sidebar_collapsed = False
        self._sidebar_layout = QVBoxLayout(self._sidebar)
        self._sidebar_layout.setContentsMargins(16, 24, 16, 16)
        self._sidebar_layout.setSpacing(0)

        # Logo
        logo_row = QHBoxLayout()
        logo_icon = QLabel("🍬")
        logo_icon.setFont(QFont("Segoe UI Emoji", 36))
        logo_row.addWidget(logo_icon)
        self._sb_logo_text = QLabel("小糖糖")
        self._sb_logo_text.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {PINK};")
        logo_row.addWidget(self._sb_logo_text)
        logo_row.addStretch()
        self._sidebar_layout.addLayout(logo_row)

        self._sb_subtitle = QLabel("QQ 群智能体控制台")
        self._sb_subtitle.setObjectName("muted")
        self._sb_subtitle.setStyleSheet("margin-bottom: 16px;")
        self._sidebar_layout.addWidget(self._sb_subtitle)

        # 导航按钮
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._nav_btns: dict[str, QPushButton] = {}

        nav_tips = {
            "dashboard": "启动/停止糖糖、查看数据统计",
            "settings": "修改机器人配置（人设、AI、群管理、黑名单等）",
            "gallery": "管理本地图片库：上传、配文、拖拽排序",
            "log": "查看运行日志、诊断问题",
        }
        self._nav_labels: dict[str, str] = {}
        for icon_name, label, key in [
            ("layout-dashboard", "仪表盘", "dashboard"),
            ("settings", "设置", "settings"),
            ("image", "图库", "gallery"),
            ("scroll-text", "日志", "log"),
        ]:
            btn = QPushButton(f" {label}")
            btn.setObjectName("navBtn")
            btn.setCheckable(True)
            btn.setIconSize(QSize(16, 16))
            btn.setIcon(lucide(icon_name, "#a1a1aa", 16))
            btn._lucide_name = icon_name  # 主题刷新重染用
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(nav_tips.get(key, ""))
            btn.clicked.connect(lambda checked, k=key: (self._navigate(k), self._set_sidebar_collapsed(True)))
            self._nav_group.addButton(btn)
            self._nav_btns[key] = btn
            self._nav_labels[key] = f" {label}"
            self._sidebar_layout.addWidget(btn)
            self._sidebar_layout.addSpacing(2)

        self._sidebar_layout.addStretch()

        # 状态指示
        self._sidebar_status = QLabel("● 离线")
        self._sidebar_status.setObjectName("muted")
        self._sidebar_layout.addWidget(self._sidebar_status)

        self._llm_info = QLabel(f"LLM: {_provider_label(self.cfg.get('llm', {}).get('provider', ''))}")
        self._llm_info.setObjectName("muted")
        self._sidebar_layout.addWidget(self._llm_info)

        # 2026-09-05 批 2：导航 hover 平滑过渡。
        # checked 语义：hover 离开恢复选中色（alpha 46 ≈ QSS rgba(粉,18%)）。
        # 2026-09-06 主人定：hover 与功能内按钮一致为灰底——叠层改中性灰（alpha 22 ≈ hover_bg 明度）。
        self._hover_engine = HoverEngine(self)
        for _btn in self._nav_btns.values():
            _c_hover = QColor("#9a9aa2")
            _c_hover.setAlpha(22)
            _c_checked = QColor(PINK)
            _c_checked.setAlpha(46)
            self._hover_engine.register(_btn, QColor(0, 0, 0, 0), _c_hover, 140, _c_checked)

        # Aether 指挥坞（2026-09-06）：侧栏浮动于窗口内（壳留 10px 呼吸边），伸缩行为不变
        self._sidebar_shell = QWidget()
        shell_lay = QVBoxLayout(self._sidebar_shell)
        shell_lay.setContentsMargins(10, 10, 10, 10)
        shell_lay.setSpacing(0)
        shell_lay.addWidget(self._sidebar)
        root.addWidget(self._sidebar_shell)

    # ═══════════════════════════════════════════════════════
    # QStackedWidget 页面容器
    # ═══════════════════════════════════════════════════════
    def _build_pages(self, root: QHBoxLayout):
        self._stack = SlideStack(self)  # 2026-09-05 批 3：页面切换淡入过渡
        self._stack.addWidget(self._build_dashboard())
        self._stack.addWidget(self._build_settings())
        self._stack.addWidget(self._build_gallery())
        self._stack.addWidget(self._build_log())
        root.addWidget(self._stack, 1)

    def _navigate(self, page: str):
        idx = {"dashboard": 0, "settings": 1, "gallery": 2, "log": 3}
        if page in idx:
            self._stack.slide_to(idx[page], ms=240)  # 批 3：淡入过渡（锁防连点）
        # 更新导航按钮 checked 状态（QSS #navBtn:checked 自动处理样式）
        for key, btn in self._nav_btns.items():
            btn.setChecked(key == page)
        self._refresh_icons()  # 选中项图标随 checked 变色（2026-09-06）

    def _set_sidebar_collapsed(self, collapsed: bool):
        """侧栏伸缩（2026-09-06 主人需求）：收缩态只显示图标（48px），展开态 200px。
        点击导航按钮 → 收缩；悬浮侧栏 → 展开（eventFilter 触发）。宽度动画过渡。"""
        if getattr(self, "_sidebar_collapsed", False) == collapsed:
            return
        self._sidebar_collapsed = collapsed
        target = 56 if collapsed else 200
        anim = QVariantAnimation(self)
        anim.setDuration(200)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(self._sidebar.width())
        anim.setEndValue(target)
        anim.valueChanged.connect(lambda v: self._sidebar.setFixedWidth(int(v)))
        anim.start(QVariantAnimation.DeleteWhenStopped)
        # 收缩态只留图标：隐藏文字控件 + 按钮清文字 + 紧凑边距 + rail 属性（QSS 居中 padding）
        for w in (self._sb_logo_text, self._sb_subtitle, self._sidebar_status, self._llm_info):
            w.setVisible(not collapsed)
        for key, btn in self._nav_btns.items():
            btn.setText("" if collapsed else self._nav_labels[key])
            btn.setProperty("rail", collapsed)
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        self._sidebar_layout.setContentsMargins(8 if collapsed else 16, 24, 8 if collapsed else 16, 16)

    def eventFilter(self, obj, ev):
        """侧栏悬浮展开（2026-09-06 伸缩侧栏——QEvent.Enter 触发）"""
        if obj is getattr(self, "_sidebar", None) and ev.type() == QEvent.Enter:
            self._set_sidebar_collapsed(False)
        return super().eventFilter(obj, ev)

    def _refresh_icons(self):
        """主题/配色/选中态变化后重染 lucide 图标（2026-09-06 emoji 退役工程）。
        所有带 _lucide_name 的按钮/控件在此统一按当前 pal 染色。"""
        lucide_clear()
        pal = getattr(self, "_pal", None)
        if pal is None:
            return
        sec = pal.get("text_secondary", "#a1a1aa")
        acc = pal.get("accent", "#e2a1bc")
        for btn in self.findChildren(QPushButton):
            name = getattr(btn, "_lucide_name", None)
            if name:
                role = getattr(btn, "_lucide_role", "text_secondary")
                if btn.isChecked():
                    color = acc
                elif role == "accent_on":
                    color = pal.get("accent_on", "#ffffff")
                elif role == "accent":
                    color = acc
                else:
                    color = sec
                btn.setIcon(lucide(name, color, 16))
                btn.setIconSize(QSize(16, 16))

    def _open_github(self):
        """Star 引导：默认浏览器打开项目仓库（2026-09-05）。"""
        QDesktopServices.openUrl(QUrl(PROJECT_GITHUB_URL))

    def _play_dashboard_intro(self):
        """批 4：仪表盘首屏卡片交错淡入（一次性）。
        交错间隔 70ms 保证同屏 QGraphicsOpacityEffect ≤2（性能铁律，见 ui_anim）。
        门闩 _intro_played：_refresh_stats 5s 定时刷新不得重播。"""
        if self._intro_played or not getattr(self, "_intro_cards", None):
            return
        self._intro_played = True
        cards = list(self._intro_cards)

        def _play(i: int) -> None:
            if i >= len(cards):
                return
            fade_in(cards[i], 200)
            QTimer.singleShot(70, lambda: _play(i + 1))

        _play(0)

    # ═══════════════════════════════════════════════════════
    # 1. Dashboard 页
    # ═══════════════════════════════════════════════════════
    def _build_dashboard(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.viewport().setMouseTracking(True)
        self._attach_overlay_bar(scroll)

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(16)

        # 批 4：首屏入场卡片收集（hero + 状态卡 + 统计帧，串行淡入）
        self._intro_cards: list[QFrame] = []

        # Hero：大标题 + Star 引导（2026-09-05 主人定稿——点方形 ⭐ 跳 GitHub）
        hero = QFrame()
        hero.setObjectName("card")
        hl = QVBoxLayout(hero)
        hl.setContentsMargins(24, 20, 24, 20)
        title = QLabel("🍬 小糖糖")
        title.setObjectName("heroTitle")
        hl.addWidget(title)
        star_row = QHBoxLayout()
        star_row.setSpacing(10)
        star_btn = QPushButton("⭐")
        star_btn.setObjectName("starBtn")
        star_btn.setToolTip("去 GitHub 给糖糖点个 Star ⭐")
        star_btn.setCursor(Qt.PointingHandCursor)
        star_btn.clicked.connect(self._open_github)
        star_row.addWidget(star_btn)
        star_lbl = QLabel("喜欢她，就点个 Star 让她被更多人遇见吧")
        star_lbl.setObjectName("heroSub")
        star_row.addWidget(star_lbl)
        star_row.addStretch()
        hl.addLayout(star_row)
        layout.addWidget(hero)
        self._intro_cards.append(hero)

        # 状态卡片行
        cards_row = QHBoxLayout()
        cards_row.setSpacing(12)
        self._card_status = self._make_status_card("SnowLuma", "检测中...")
        self._card_llm = self._make_status_card("LLM", _provider_label(self.cfg.get("llm", {}).get("provider", "")))
        self._card_online = self._make_status_card("🍬 小糖糖", "未启动")
        cards_row.addWidget(self._card_status)
        cards_row.addWidget(self._card_llm)
        cards_row.addWidget(self._card_online)
        layout.addLayout(cards_row)
        self._intro_cards.extend([self._card_status, self._card_llm, self._card_online])

        # 快速统计
        stats_frame = QFrame()
        stats_frame.setObjectName("card")
        sf = QVBoxLayout(stats_frame)
        sf.setContentsMargins(16, 12, 16, 12)
        sf.addWidget(QLabel("快速统计", objectName="heading"))
        sf.addSpacing(6)
        self._stat_divider = HDivider(color=str(getattr(self, "_pal", {}).get("card_border", "#333338")))
        sf.addWidget(self._stat_divider)
        sf.addSpacing(8)
        grid = QGridLayout()
        grid.setSpacing(8)
        self._stat_labels = {}
        for i, (icon, key, label) in enumerate([
            ("", "people", "群友"), ("🧠", "memories", "记忆"),
            ("💬", "chats", "聊天"), ("🖼", "stickers", "表情包"),
        ]):
            item = QFrame()
            item.setObjectName("statItem")
            item.setCursor(Qt.PointingHandCursor)
            il = QVBoxLayout(item)
            il.setContentsMargins(12, 8, 12, 8)
            il.setSpacing(2)
            il.addWidget(QLabel(f"{icon} {label}", objectName="muted"))
            val = QLabel("...")
            val.setObjectName("cardValue")
            il.addWidget(val)
            self._stat_labels[key] = val
            item.setToolTip(f"点击查看{label}详细数据")
            item.mousePressEvent = lambda e, k=key: self._toggle_data_panel(k)
            grid.addWidget(item, 0, i)
        sf.addLayout(grid)
        layout.addWidget(stats_frame)
        self._intro_cards.append(stats_frame)

        # 数据浏览面板（点击统计卡片展开，点击 ✕ 或再点卡片关闭）
        self._data_panel = QFrame()
        self._data_panel.setObjectName("card")
        self._data_panel.setVisible(False)
        dpl = QVBoxLayout(self._data_panel)
        dpl.setContentsMargins(16, 12, 16, 12)
        dp_header = QHBoxLayout()
        self._data_title = QLabel("数据浏览")
        self._data_title.setObjectName("heading")
        dp_header.addWidget(self._data_title)
        self._data_count = QLabel("")
        self._data_count.setObjectName("muted")
        dp_header.addWidget(self._data_count)
        dp_header.addStretch()
        self._data_extra_btn = QPushButton("重打标签")
        self._data_extra_btn.setObjectName("actionBtn")
        self._data_extra_btn.clicked.connect(self._run_retag)
        self._data_extra_btn.setVisible(False)
        dp_header.addWidget(self._data_extra_btn)
        dp_header.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("subtleBtn")
        close_btn.clicked.connect(lambda: self._data_panel.setVisible(False))
        dp_header.addWidget(close_btn)
        dpl.addLayout(dp_header)
        # 搜索行 + 分页（2026-09-06：全量数据翻页浏览）
        filter_row = QHBoxLayout()
        self._data_search = QLineEdit()
        self._data_search.setPlaceholderText("输入关键词过滤...")
        self._data_search.textChanged.connect(self._filter_data)
        filter_row.addWidget(self._data_search)
        filter_row.addSpacing(8)
        self._data_prev_btn = QPushButton("◀ 上一页")
        self._data_prev_btn.setObjectName("subtleBtn")
        self._data_prev_btn.clicked.connect(lambda: self._page_nav(-1))
        filter_row.addWidget(self._data_prev_btn)
        self._data_next_btn = QPushButton("下一页 ▶")
        self._data_next_btn.setObjectName("subtleBtn")
        self._data_next_btn.clicked.connect(lambda: self._page_nav(1))
        filter_row.addWidget(self._data_next_btn)
        dpl.addLayout(filter_row)
        # 表格视图
        self._data_table = QTableWidget()
        self._data_table.setObjectName("dataTable")
        self._data_table.setMinimumHeight(300)
        self._data_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._data_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._data_table.horizontalHeader().setStretchLastSection(True)
        self._data_table.verticalHeader().setVisible(False)
        self._data_table.setAlternatingRowColors(True)
        # 像素级滚动——ScrollPerItem 默认一格一格跳，是「一卡一卡」的元凶之一（2026-09-06）
        self._data_table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._data_table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._attach_overlay_bar(self._data_table)
        self._attach_overlay_bar(self._data_table, Qt.Horizontal)
        dpl.addWidget(self._data_table)
        layout.addWidget(self._data_panel)

        # 控制区
        ctrl = QFrame()
        ctrl.setObjectName("card")
        cl = QVBoxLayout(ctrl)
        cl.setContentsMargins(16, 12, 16, 12)
        cl.addWidget(QLabel("控制", objectName="heading"))

        # SnowLuma 行：启动 QQ 协议适配器
        row1 = QHBoxLayout()
        self._btn_snowluma = QPushButton("启动 SnowLuma")
        self._btn_snowluma.setObjectName("actionBtn")
        self._btn_snowluma.setToolTip("启动 SnowLuma QQ 客户端，需先扫码登录才能使用糖糖")
        self._btn_snowluma.clicked.connect(self._start_snowluma)
        row1.addWidget(self._btn_snowluma)
        self._lbl_snowluma_hint = QLabel("启动后自动登录")
        self._lbl_snowluma_hint.setObjectName("muted")
        row1.addWidget(self._lbl_snowluma_hint)
        row1.addStretch()
        cl.addLayout(row1)

        # 糖糖行：启动/停止/重启 AI 机器人（lucide 图标——2026-09-06 emoji 退役）
        row2 = QHBoxLayout()
        self._btn_sugar = QPushButton(" 启动小糖糖")
        self._btn_sugar.setObjectName("pinkBtn")
        self._btn_sugar.setIconSize(QSize(16, 16))
        self._btn_sugar._lucide_name = "play"
        self._btn_sugar._lucide_role = "accent_on"
        self._btn_sugar.setToolTip("启动糖糖 AI 机器人，连接 SnowLuma 后自动上线")
        self._btn_sugar.clicked.connect(self._start_sugar)
        row2.addWidget(self._btn_sugar)

        stop_btn = QPushButton(" 停止")
        stop_btn.setObjectName("actionBtn")
        stop_btn.setIconSize(QSize(15, 15))
        stop_btn._lucide_name = "square"
        stop_btn.setToolTip("停止糖糖进程")
        stop_btn.clicked.connect(self._stop_sugar)
        row2.addWidget(stop_btn)

        restart_btn = QPushButton(" 重启")
        restart_btn.setObjectName("actionBtn")
        restart_btn.setIconSize(QSize(15, 15))
        restart_btn._lucide_name = "rotate-cw"
        restart_btn.setToolTip("停止后重新启动糖糖")
        restart_btn.clicked.connect(self._restart_sugar)
        row2.addWidget(restart_btn)
        row2.addStretch()
        cl.addLayout(row2)

        # 底部行：静默模式（诊断入口在日志页——2026-09-05 仪表盘不再重复）
        row3 = QHBoxLayout()
        self._silent_check = FluentCheckBox("后台静默运行")
        self._silent_check.setToolTip("关闭窗口时最小化到系统托盘而不是退出")
        self._silent_check.setChecked(False)
        row3.addWidget(self._silent_check)
        row3.addStretch()
        cl.addLayout(row3)

        layout.addWidget(ctrl)
        layout.addStretch()

        scroll.setWidget(page)
        return scroll

    def _make_status_card(self, title: str, value: str) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        l = QVBoxLayout(card)
        l.setContentsMargins(16, 12, 16, 12)
        l.setSpacing(4)
        l.addWidget(QLabel(title, objectName="muted"))
        val_lbl = QLabel(value)
        val_lbl.setObjectName("cardValue")
        l.addWidget(val_lbl)
        card._val_label = val_lbl  # 保存引用以便更新
        return card

    def _set_status_color(self, label: QLabel, target_hex: str):
        """状态 label 换色平滑（2026-09-05 批 3）：180ms 颜色插值过渡。
        保留内联非 color 段（字号/粗细等）；颜色相同直接落定无动画。"""
        ss = label.styleSheet()
        base = re.sub(r"color:\s*(#[0-9a-fA-F]{6}|rgba\([^)]*\))\s*;?", "", ss)
        end = QColor(target_hex)
        m = re.search(r"color:\s*(#[0-9a-fA-F]{6})", ss)
        start = QColor(m.group(1)) if m else end
        if start == end:
            label.setStyleSheet(base + f"color: {target_hex};")
            return
        animate_color(label, start, end, 180, "color", base)

    def _refresh_llm_display(self):
        """设置保存后刷新 LLM 显示：仪表盘卡 + 侧栏标签跟随提供商（2026-09-05）。"""
        provider = str(self.cfg.get("llm", {}).get("provider", ""))
        label = _provider_label(provider)
        if getattr(self, "_llm_info", None):
            self._llm_info.setText(f"LLM: {label}")
        if getattr(self, "_card_llm", None):
            self._card_llm._val_label.setText(label)

    def _refresh_stats(self):
        """刷新仪表盘统计数字"""
        try:
            import sqlite3, json as _json
            db_path = BASE / "memory.db"
            if db_path.exists():
                conn = sqlite3.connect(str(db_path))
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM people")
                self._stat_labels["people"].setText(str(cur.fetchone()[0]))
                cur.execute("SELECT COUNT(*) FROM memories")
                self._stat_labels["memories"].setText(str(cur.fetchone()[0]))
                cur.execute("SELECT COUNT(*) FROM chat_log")
                self._stat_labels["chats"].setText(str(cur.fetchone()[0]))
                conn.close()
            else:
                for k in ["people", "memories", "chats"]:
                    self._stat_labels[k].setText("—")
        except Exception:
            for k in ["people", "memories", "chats"]:
                self._stat_labels[k].setText("?")

        # 表情包数量
        try:
            meta = BASE / "stickers" / "metadata.json"
            if meta.exists():
                with open(meta, "r", encoding="utf-8") as f:
                    self._stat_labels["stickers"].setText(str(len(_json.load(f))))
            else:
                self._stat_labels["stickers"].setText("N/A")
        except Exception:
            self._stat_labels["stickers"].setText("?")

    def _toggle_data_panel(self, key: str):
        if self._data_panel.isVisible():
            self._data_panel.setVisible(False)
            return
        self._data_panel.setVisible(True)
        # sticker 不需要搜索框
        self._data_search.setVisible(key != "stickers")
        # sticker 显示重打标签按钮
        self._data_extra_btn.setVisible(key == "stickers")
        self._load_data(key)

    def _load_data(self, key: str):
        """从 SQLite 加载数据到表格"""
        tips = {"people": "按QQ号/昵称过滤", "memories": "按QQ号/关键词/内容过滤",
                "chats": "按QQ号/消息内容过滤", "stickers": ""}
        self._data_search.setPlaceholderText(tips.get(key, "输入关键词过滤..."))
        try:
            import sqlite3, json as _json
            db_path = BASE / "memory.db"
            if not db_path.exists():
                self._data_table.clear()
                self._data_table.setRowCount(1); self._data_table.setColumnCount(1)
                self._data_table.setHorizontalHeaderLabels([""])
                self._data_table.setItem(0, 0, QTableWidgetItem("还没有数据。启动糖糖后会自动创建数据库。"))
                self._data_count.setText("")
                return
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            if key == "people":
                cur.execute("SELECT qq_id, nickname, intimacy, total_chats FROM people ORDER BY total_chats DESC")
                self._populate_table(["QQ号", "昵称", "亲密度", "发言数"], cur.fetchall())
            elif key == "memories":
                cur.execute("SELECT qq_id, key, value, importance, timestamp FROM memories ORDER BY importance DESC")
                self._populate_table(["QQ号", "关键词", "内容", "重要性", "时间"], cur.fetchall())
            elif key == "chats":
                cur.execute("SELECT qq_id, message, timestamp FROM chat_log ORDER BY timestamp DESC")
                self._populate_table(["QQ号", "消息内容", "时间"], cur.fetchall())
            elif key == "stickers":
                stickers_dir = BASE / "stickers"
                meta_file = stickers_dir / "metadata.json"
                count = 0
                if meta_file.exists():
                    with open(meta_file, "r", encoding="utf-8") as f:
                        count = len(_json.load(f))
                self._data_table.clear()
                self._data_table.setRowCount(1); self._data_table.setColumnCount(1)
                self._data_table.setHorizontalHeaderLabels(["表情包"])
                self._data_table.setItem(0, 0, QTableWidgetItem(f"共 {count} 张    位置: stickers/"))
                self._data_count.setText("")
                conn.close()
                return
            conn.close()
        except Exception as e:
            self._data_table.clear()
            self._data_table.setRowCount(1); self._data_table.setColumnCount(1)
            self._data_table.setHorizontalHeaderLabels([""])
            self._data_table.setItem(0, 0, QTableWidgetItem(f"加载失败: {e}"))
            self._data_count.setText("")

    PAGE_SIZE = 500

    def _populate_table(self, headers: list, rows: list):
        """填充表格数据（2026-09-06 分页改造：全量数据可翻页浏览，每页 500，搜索在全量上过滤）"""
        self._all_rows = rows
        self._all_headers = headers
        self._page = 0
        self._pages = max(1, (len(rows) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self._data_table.clear()
        self._data_table.setColumnCount(len(headers))
        self._data_table.setHorizontalHeaderLabels(headers)
        if not rows:
            self._data_table.setRowCount(1)
            hint = QTableWidgetItem("（无匹配结果）")
            hint.setForeground(QColor(TEXT_MUTED))
            self._data_table.setItem(0, 0, hint)
            self._data_count.setText("共 0 条")
            self._sync_page_controls()
            return
        self._render_page()

    def _render_page(self):
        """渲染当前页（2026-09-06 分页）"""
        start = self._page * self.PAGE_SIZE
        display = self._all_rows[start:start + self.PAGE_SIZE]
        self._data_table.setRowCount(len(display))
        for r, row in enumerate(display):
            for c, val in enumerate(row):
                self._data_table.setItem(r, c, QTableWidgetItem(str(val)))
        total = len(self._all_rows)
        if self._pages > 1:
            self._data_count.setText(f"共 {total} 条 · 第 {self._page + 1}/{self._pages} 页")
        else:
            self._data_count.setText(f"共 {total} 条")
        # 列宽：一次性按内容计算后保持 Interactive——ResizeToContents 会在每次滚动时
        # 重算全部行宽，是横向滚动卡顿的元凶（2026-09-06 修复）
        h = self._data_table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.Interactive)
        h.resizeSections(QHeaderView.ResizeToContents)
        for c in range(self._data_table.columnCount()):
            if h.sectionSize(c) > 320:
                h.resizeSection(c, 320)
        self._sync_page_controls()

    def _sync_page_controls(self):
        """分页按钮可用态（2026-09-06）"""
        if hasattr(self, "_data_prev_btn"):
            self._data_prev_btn.setEnabled(self._page > 0)
            self._data_next_btn.setEnabled(self._page < self._pages - 1)

    def _page_nav(self, delta: int):
        """翻页（-1 上一页 / +1 下一页）"""
        self._page = max(0, min(self._pages - 1, self._page + delta))
        self._render_page()

    def _filter_data(self, text: str):
        """搜索过滤表格行（全量数据上过滤，命中可跨页浏览）"""
        if not hasattr(self, '_all_rows') or not hasattr(self, '_all_headers'):
            return
        search = text.strip().lower()
        if not search:
            self._populate_table(self._all_headers, self._all_rows)
            return
        filtered = [r for r in self._all_rows
                    if search in " ".join(str(c).lower() for c in r)]
        self._populate_table(self._all_headers, filtered)

    def _run_retag(self):
        """运行统一贴图标注工具（控制台识图方式配置优先，另一后端兜底）"""
        script = BASE / "tools" / "标注贴图情绪.py"
        if not script.exists():
            QMessageBox.warning(self, "未找到", f"未找到 标注贴图情绪.py")
            return
        reply = QMessageBox.question(
            self, "确认", "将用识图模型扫描全部贴图目录打情绪标签（按控制台「识图方式」选后端，不可用自动切换）。\n已打标签的图片会跳过。\n\n确定开始？"
        )
        if reply != QMessageBox.Yes:
            return
        self._navigate("log")
        self._log("🏷 开始重新打标签...")
        try:
            import subprocess
            proc = subprocess.Popen(
                [sys.executable, str(script)],
                cwd=str(BASE),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace"
            )
            import threading
            def _read():
                for line in proc.stdout:
                    if line.strip():
                        self._log(f"🏷 {line.strip()}")
                self._log("重打标签完成")
                self._refresh_stats()
            threading.Thread(target=_read, daemon=True).start()
            self._log("🏷 retag_stickers.py 已在后台运行，输出见上方")
        except Exception as e:
            self._log(f"❌ 启动失败: {e}")

    # ═══════════════════════════════════════════════════════
    # 2. Settings 页
    # ═══════════════════════════════════════════════════════
    def _build_settings(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 顶栏
        header = QFrame()
        header.setObjectName("card")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 12, 20, 12)
        hl.addWidget(QLabel("设置 — 修改配置后点「保存设置」生效，部分需重启糖糖", objectName="heading"))
        hl.addStretch()
        save_btn = QPushButton(" 保存设置")
        save_btn.setObjectName("pinkBtn")
        save_btn.setIconSize(QSize(16, 16))
        save_btn._lucide_name = "save"
        save_btn._lucide_role = "accent_on"
        save_btn.clicked.connect(self._save_config)
        hl.addWidget(save_btn)
        layout.addWidget(header)

        # 主体：左侧分类 + 右侧内容
        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        # 左侧分类栏（可滚动）
        cats_frame = QFrame()
        cats_frame.setObjectName("sidebar")
        cats_layout = QVBoxLayout(cats_frame)
        cats_layout.setContentsMargins(8, 8, 8, 8)
        cats_layout.setSpacing(2)

        cats_scroll = QScrollArea()
        cats_scroll.setFixedWidth(165)
        cats_scroll.setWidgetResizable(True)
        cats_scroll.setFrameShape(QFrame.NoFrame)
        cats_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        cats_scroll.setWidget(cats_frame)
        self._attach_overlay_bar(cats_scroll)

        self._settings_cats: dict[str, QPushButton] = {}
        self._settings_sections: dict[str, QWidget] = {}
        self._settings_fields: dict[str, list] = {}

        # 分组结构
        groups = [
            ("核心配置", ["bot", "napcat", "groups", "blacklist", "memory"]),
            ("AI 引擎",  ["llm", "vision", "web_search"]),
            ("人格行为", ["personality", "behavior", "mood"]),
            ("扩展功能", ["tasks", "image_share", "voice", "songs", "daily_report", "daily_like", "image_gen"]),
            ("外观定制", ["appearance"]),
            ("密钥管理", ["secrets"]),
        ]
        labels = {
            "bot": "机器人", "napcat": "SnowLuma", "groups": "群管理",
            "blacklist": "黑名单", "memory": "记忆", "llm": "AI模型",
            "vision": "识图", "web_search": "搜索",
            "personality": "人格", "behavior": "行为", "mood": "情绪",
            "image_share": "图片分享", "voice": "语音", "songs": "曲库",
            "tasks": "⏰ 提醒可靠性",
            "daily_report": "每日播报", "daily_like": "每日点赞", "image_gen": "AI画图",
            "appearance": "外观", "secrets": "密钥管理",
        }

        for group_title, keys in groups:
            title_lbl = QLabel(group_title)
            title_lbl.setObjectName("muted")
            title_lbl.setStyleSheet(f"padding: 10px 12px 4px 12px; font-size: 10px; color: {TEXT_MUTED};")
            cats_layout.addWidget(title_lbl)
            for key in keys:
                btn = QPushButton(labels.get(key, key))
                btn.setCheckable(True)
                btn.setCursor(Qt.PointingHandCursor)
                btn.setObjectName("catBtn")  # 样式随主题（2026-09-06 浅色黑块修复，勿改回 inline）
                # 直接连接 clicked，绕过 QButtonGroup 的独占选中逻辑
                btn.clicked.connect(lambda checked, k=key: self._on_cat_clicked(k))
                self._settings_cats[key] = btn
                cats_layout.addWidget(btn)

        cats_layout.addStretch()
        body_layout.addWidget(cats_scroll)

        # 右侧内容区：QScrollArea → QStackedLayout → section 容器
        self._settings_container = QWidget()
        self._settings_layout = QStackedLayout()
        self._settings_layout.setContentsMargins(0, 0, 0, 0)
        self._settings_container.setLayout(self._settings_layout)

        self._settings_scroll = QScrollArea()
        self._settings_scroll.setWidgetResizable(True)
        self._settings_scroll.setFrameShape(QFrame.NoFrame)
        self._settings_scroll.setWidget(self._settings_container)
        self._attach_overlay_bar(self._settings_scroll)
        body_layout.addWidget(self._settings_scroll, 1)

        layout.addWidget(body, 1)

        self._build_all_sections()
        for key, container in self._settings_sections.items():
            self._settings_layout.addWidget(container)

        self._on_cat_clicked("bot")

        return page

    def _build_all_sections(self):
        """预建 14 个设置的 section widget"""
        cfg = self.cfg

        def _add_section(key: str, title: str, fields_def: list):
            """fields_def: list of (config_path, label, field_type, description_or_choices)"""
            container = QWidget()
            layout = QVBoxLayout(container)
            layout.setContentsMargins(24, 16, 24, 16)
            layout.setSpacing(12)
            layout.addWidget(QLabel(title, objectName="heading"))

            field_list = []
            form = QFormLayout()
            form.setSpacing(10)
            form.setLabelAlignment(Qt.AlignRight)

            for field_def in fields_def:
                if len(field_def) == 5:
                    path, label, ftype, extra, hint = field_def
                elif len(field_def) == 4:
                    path, label, ftype, extra = field_def
                    hint = ""
                else:
                    path, label, ftype = field_def
                    extra = ""
                    hint = ""

                widget = None
                if ftype == "str":
                    widget = QLineEdit()
                    widget.setText(str(_get_nested(cfg, path, "")))
                    if extra:
                        widget.setPlaceholderText(str(extra))
                        widget.setToolTip(str(extra))
                elif ftype == "int":
                    widget = QSpinBox()
                    widget.setRange(0, 999999)
                    widget.setValue(int(_get_nested(cfg, path, 0)))
                    if extra:
                        widget.setToolTip(str(extra))
                elif ftype == "float":
                    widget = QDoubleSpinBox()
                    widget.setRange(0, 9999.0)
                    widget.setDecimals(3)
                    widget.setSingleStep(0.1)
                    widget.setValue(float(_get_nested(cfg, path, 0)))
                    if extra:
                        widget.setToolTip(str(extra))
                elif ftype == "bool":
                    widget = FluentCheckBox()
                    widget.setChecked(bool(_get_nested(
                        cfg, path, TASK_GATE_DEFAULTS.get(path, False),
                    )))
                    if extra:
                        widget.setToolTip(str(extra))
                elif ftype == "text":
                    widget = QTextEdit()
                    widget.setAcceptRichText(False)  # 纯文本，粘贴不带格式
                    widget.setMaximumHeight(120)
                    val = _get_nested(cfg, path, "")
                    if isinstance(val, list):
                        widget.setPlainText("\n".join(val))
                    else:
                        widget.setPlainText(str(val))
                    if extra:
                        widget.setToolTip(str(extra))
                elif ftype == "list":
                    widget = QTextEdit()
                    widget.setAcceptRichText(False)  # 纯文本，粘贴不带格式
                    widget.setMaximumHeight(100)
                    val = _get_nested(cfg, path, [])
                    if isinstance(val, list):
                        widget.setPlainText("\n".join(val))
                    else:
                        widget.setPlainText(str(val))
                    if extra:
                        widget.setToolTip(str(extra))
                elif ftype == "choice":
                    # extra 格式: ["选项1", "选项2", ...] 或 "选项1/选项2/..."
                    choices = extra if isinstance(extra, list) else str(extra).split("/")
                    widget = QComboBox()
                    widget.addItems([c.strip() for c in choices if c.strip()])
                    cur = str(_get_nested(cfg, path, choices[0].strip() if choices else ""))
                    if cur in [widget.itemText(i) for i in range(widget.count())]:
                        widget.setCurrentText(cur)
                    elif widget.count() > 0:
                        widget.setCurrentIndex(0)
                    if hint:
                        widget.setToolTip(str(hint))
                    # provider 自动填充 model + base_url
                    if path == "llm.provider":
                        widget.currentTextChanged.connect(lambda val, w=widget: self._on_provider_changed(val))
                elif ftype == "label":
                    widget = QLabel(str(extra))
                    widget.setWordWrap(True)
                    widget.setObjectName("muted")
                    widget.setStyleSheet("padding: 4px 0;")

                if widget:
                    widget.setProperty("config_path", path)
                    widget.setProperty("field_type", ftype)
                    if ftype == "label":
                        # label 类型：只显示文本，不要标签列和提示
                        widget.setContentsMargins(0, 4, 0, 4)
                        form.addRow(widget)
                    else:
                        lbl = QLabel(label)
                        lbl.setObjectName("body")
                        lbl.setFixedWidth(80)
                        # tooltip: hint 优先（说明文字），extra 次之（参数格式）
                        tt = hint or (extra if ftype != "choice" else "") or ""
                        if tt:
                            lbl.setToolTip(str(tt))
                            widget.setToolTip(str(tt))
                        form.addRow(lbl, widget)
                    # 如果有说明，在字段下方显示灰色小字（label 类型跳过）
                    # hint 优先于 extra——extra 是参数格式（如 choice 的选项列表），不是说明文字
                    display_hint = hint or (extra if ftype != "choice" else "")
                    if display_hint and ftype != "label":
                        hint_lbl = QLabel(str(display_hint))
                        hint_lbl.setObjectName("muted")
                        hint_lbl.setWordWrap(True)
                        hint_lbl.setStyleSheet("font-size: 10px; padding-left: 84px; margin-bottom: 4px;")
                        form.addRow(hint_lbl)
                    field_list.append((path, widget, ftype))

            layout.addLayout(form)
            layout.addStretch()
            self._settings_sections[key] = container
            self._settings_fields[key] = field_list

        # ── Bot ──
        _add_section("bot", "机器人设置 — 糖糖的基本身份", [
            ("bot.qq_id", "QQ号", "str", "机器人的QQ号，就是糖糖登录的那个号"),
            ("bot.owner_qq", "主人QQ", "str", "主人的QQ号（最高权限，能执行任何命令）"),
            ("bot.name", "名字", "str", "机器人的名字，显示在日志里"),
            ("bot.nicknames", "昵称列表", "list", "群友叫这些名字糖糖也会回应。一行一个，建议2-5个"),
        ])

        # ── LLM ──
        _add_section("llm", "AI模型 — 控制糖糖的「大脑」", [
            ("llm.provider", "提供商", "choice",
             ["deepseek", "xai", "anthropic", "openai", "custom"],
             "选择模型提供商，自动填入接口地址和模型名。选 custom 可自由填写"),
            ("llm.model", "模型", "str", "模型名。选提供商后自动填入，也可手动改"),
            ("_api_key_hint", "", "label",
             "🔑 API Key 已迁移至「🔑 密钥管理」分类，在左侧列表底部。"),
            ("llm.base_url", "接口地址", "str", "选提供商后自动填入，也可手动改"),
            ("llm.max_tokens", "最大Token", "int", "回复最大长度。建议 512-2048，越大回复越长但也越贵"),
            ("llm.temperature", "温度", "float", "0=死板 2=疯癫。建议 0.7-1.0，值越高回复越随机"),
        ])

        # ── Personality（已迁移至 role_card.md）──
        _add_section("personality", "人格 — 糖糖的性格和行为准则（由 role_card.md 管理）", [
            ("_rolecard_note", "", "label",
             "💡 糖糖的人格内容已全部迁移至项目根目录的 role_card.md。\n"
             "编辑 role_card.md → 重启糖糖 或 发送 /人格 重载 即可生效。\n"
             "config.yaml 中的 personality 节仅作兜底（role_card.md 不存在时才会用到）。"),
        ])

        # ── Behavior ──
        _add_section("behavior", "行为 — 控制糖糖什么时候说话、怎么说", [
            ("behavior.reply_only_to", "仅回复这些人", "list", "白名单模式：只回复这里列的QQ号。一行一个QQ号，不限制数量。留空=回复所有人"),
            ("behavior.testing_mode", "测试模式", "bool", "开启后拦截所有群消息（只回私聊）。调试用，正常使用请关闭"),
            ("behavior.active_interjection", "主动插话", "bool", "糖糖看到感兴趣的话题会主动加入。关闭后只有@或叫名字才回"),
            ("behavior.private_interjection", "私聊插话", "bool", "糖糖会主动给私聊用户发关心消息（心理陪伴用户/心情告警优先）。关闭后私聊只被动回复"),
            ("behavior.interjection_thirst", "插话饥渴度", "float", "0=沉默 1=话痨。值越高越容易插话。建议 0.3-0.8"),
            ("behavior.interjection_cooldown", "插话冷却", "int", "两次插话之间最少间隔多少秒。防止刷屏"),
            ("behavior.sticker_steal", "自动偷图", "bool", "开启后糖糖会自动保存群里发的表情包"),
            ("behavior.welcome_new_members", "入群欢迎", "bool", "新人入群时自动发送个性化欢迎消息"),
            ("behavior.welcome_message", "欢迎文案", "str", "自定义欢迎语，留空让AI自己说。写 [QQ号] 会自动替换成新朋友的QQ号"),
        ])

        # ── Mood 情绪 ──
        _add_section("mood", "情绪 — 糖糖三维心情模型（精力/心情/耐心）", [
            ("mood.decay_energy_per_hour", "精力消耗/时", "int",
             "每小时自然消耗多少精力。每次回复额外扣1-3点。\n"
             "例：设1 → 聊2小时20句 ≈ 精力-42（不易困）\n"
             "例：设5 → 聊2小时20句 ≈ 精力-50（容易累）\n"
             "建议 1-5，默认3"),
            ("mood.mood_neutral", "心情中性值", "int",
             "心情的自然平衡点，被夸会涨、被骂会跌，空闲时自动向这个值靠拢。\n"
             "例：设60 → 被骂到40后，每小时恢复4点，5小时回到60\n"
             "建议 50-70，默认60"),
            ("mood.decay_mood_per_hour", "心情回归速度", "int",
             "心情偏离中性值后，每小时恢复多少。\n"
             "例：设1 → 被骂掉了20点，需要20小时才恢复（记仇）\n"
             "例：设5 → 被骂掉了20点，4小时就恢复了（不记仇）\n"
             "建议 1-5，默认1"),
            ("mood.decay_patience_per_hour", "耐心恢复/时", "int",
             "每小时自然恢复多少耐心。被连续追问会消耗耐心。\n"
             "例：设2 → 耐心耗光后需要约35小时回满\n"
             "例：设10 → 耐心耗光后约7小时回满\n"
             "建议 2-10，默认2"),
            ("mood.energy_high", "精力充沛阈值", "int",
             "精力≥此值 → 活泼，回复兴致高。\n"
             "例：设50 → 精力50以上就活泼\n"
             "例：设85 → 只有精力85以上才活泼（大部分时间正常）\n"
             "建议 50-85，默认70"),
            ("mood.energy_normal", "精力正常阈值", "int",
             "精力在[此值,high)之间 → 正常聊天。精力<此值 → 开始变懒。\n"
             "例：设20 → 精力跌到20才会累\n"
             "例：设60 → 精力跌到60就觉得累了\n"
             "建议 20-60，默认40"),
            ("mood.energy_low", "精力低迷阈值", "int",
             "精力<此值 → 非常疲惫，能短就短。\n"
             "例：设5 → 几乎见底才很累\n"
             "例：设30 → 精力还剩30就累得不行了\n"
             "建议 5-30，默认15"),
        ])

        # ── Mood 预设 ──
        self._build_mood_presets()

        # ── Groups 编辑器 ──
        self._build_groups_section()

        # ── Blacklist 编辑器 ──
        self._build_blacklist_section()

        # ── SnowLuma ──
        _add_section("napcat", "SnowLuma — QQ 协议连接", [
            ("napcat.http_url", "HTTP地址", "str", "SnowLuma 的 HTTP API 地址，默认 http://127.0.0.1:3000"),
            ("_napcat_token_hint", "", "label",
             "🔑 SnowLuma Token 已迁移至「🔑 密钥管理」分类。"),
        ])

        # ── Memory ──
        _add_section("memory", "记忆 — 糖糖记住的东西", [
            ("memory.db_path", "数据库路径", "str", "SQLite 数据库文件路径。不要删除这个文件！"),
            ("memory.short_term_size", "短期记忆条数", "int", "每个群缓存的最近消息数。建议 100-500：太小记不住上下文，太大浪费内存。默认 250"),
        ])

        # ── Reminder reliability / rollback gates ──
        _add_section("tasks", "⏰ 提醒可靠性 — ActionPlan 上线与回滚闸门", [
            ("tasks.text_action_outbox_enabled", "领取文本任务", "bool",
             "关闭后到期纯文本提醒保持 pending，不调用 LLM、不创建新发送计划。安全回滚时先关闭此项。"),
            ("tasks.media_action_outbox_enabled", "领取媒体任务", "bool",
             "统一贴图/语音提醒的 canary 闸门。关闭期间到期的媒体提醒只跳过、不补发；确认新 boot 与 outbox worker 正常后再开启。"),
        ])

        # ── Vision ──
        _add_section("vision", "识图 — 看懂群友发的图片", [
            ("llm.vision.enabled", "启用识图", "bool", "开启后糖糖能看懂图片内容并评价"),
            ("llm.vision.provider", "识图方式", "choice", ["local", "api"],
             "local=本地MiniCPM-V模型(RTX加速) | api=云端千问VL(不占GPU)"),
            ("llm.vision.model", "API 模型（仅 API 方式）", "str",
             "填入你的视觉模型名（OpenAI 兼容格式，与你接入的 API 对应）"),
            ("_vision_key_hint", "", "label",
             "🔑 API 方式的 Key 在「🔑 密钥管理」分类配置。本地方式无需 Key。"),
            ("llm.vision.base_url", "API 地址（仅 API 方式）", "str",
             "填入你的 OpenAI 兼容 API 地址（通常以 /v1 结尾）"),
        ])

        # ── Voice ──
        _add_section("voice", "语音 — GPT-SoVITS 文字转语音 / 声音克隆", [
            ("voice.enabled", "启用语音", "bool", "开启后糖糖可以把文字转成语音发到群里"),
            ("voice.provider", "语音引擎", "label", "GPT-SoVITS（固定）"),
            ("voice.cache_dir", "缓存目录", "str", "语音文件缓存位置"),
        ])

        self._build_gpt_sovits_panel()

        # ── Web Search ──
        _add_section("web_search", "搜索 — 糖糖上网查资料", [
            ("web_search.enabled", "启用搜索", "bool", "开启后糖糖会自动搜索网络回答实时问题"),
            ("web_search.timeout", "超时(秒)", "int", "搜索请求超时时间。网络不好可以调大"),
            ("web_search.max_results", "最大结果数", "int", "每次搜索返回几条结果。太多会干扰回答"),
        ])

        # ── Image Share ──
        _add_section("image_share", "图片分享 — 糖糖定时发图到群里", [
            ("image_share.enabled", "启用分享", "bool", "开启后糖糖会定时从图库选图发到群里"),
            ("image_share.interval_minutes", "间隔(分钟)", "float", "多久发一张图。例如120=每2小时。0=不自动发"),
            ("image_share.max_per_day", "每日上限", "int", "每天最多发几张。防止刷屏"),
            ("image_share.use_local_only", "仅本地图库", "bool", "只发 share_images/ 里的图，不爬花瓣网"),
            ("image_share.local_dir", "图库路径", "str", "本地图片文件夹。默认 ./share_images"),
            ("image_share.caption", "配文模式", "str", "random=随机配文 / llm=AI生成配文 / none=不配文"),
            ("image_share.play_mode", "播放模式", "str", "random=随机选图 / sequential=按顺序播放"),
            ("image_share.play_category", "播放分类", "str", "只播某个文件夹的图。留空=所有。如：美图"),
        ])

        # ── Songs ──
        self._build_songs_section()
        self._build_singing_panel()

        # ── Daily Report ──
        _add_section("daily_report", "每日播报 — 糖糖每天早上发群活跃日报", [
            ("daily_report.enabled", "启用播报", "bool", "开启后每天定时发送群活跃日报"),
            ("daily_report.time", "播报时间", "str", "每天几点发送，格式 HH:MM（如 9:00）"),
            ("daily_report.include_weather", "包含天气", "bool", "播报中附带当日天气信息"),
            ("daily_report.include_stats", "包含统计", "bool", "播报中附带群数据（人数/消息数/记忆数）"),
        ])

        # ── 每日点赞 ──
        _add_section("daily_like", "每日定时点赞 — 每天固定给指定的人点赞（每人N次）", [
            ("daily_like.enabled", "启用", "bool",
             "开启后每天定时给指定的人点赞"),
            ("daily_like.time", "点赞时间", "str",
             "每天几点执行，格式 HH:MM（如 10:00）"),
            ("daily_like.targets", "目标QQ号", "list",
             "每行一个QQ号，糖糖每天会给这些人各点 N 次赞。"
             "修改后需重启糖糖进程生效"),
            ("daily_like.count_per_person", "每人点赞数", "int",
             "每个目标每天点赞的次数（默认10，受QQ限制建议≤20）"),
        ])

        # ── Image Gen（🔒 已关闭，待重做）──
        _add_section("image_gen", "AI画图 — 🔒 功能已暂时关闭", [
            ("_img_gen_note", "", "label",
             "🔒 AI画图功能已暂时关闭。\n"
             "原因：正则匹配太宽（'画饼''画画'也触发），容易误触发大量 API 调用。\n"
             "计划：改用 LLM 意图识别替代正则 + 加频率限制 + 加 @需明确点名 后才画。\n"
             "详见 docs/开发规划/ROADMAP.md"),
        ])

        # ── Appearance ──
        self._build_appearance_section()

        # ── Secrets ──
        self._build_secrets_section()

    def _build_groups_section(self):
        """群管理编辑器"""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.setSpacing(12)
        layout.addWidget(QLabel("群管理", objectName="heading"))
        hint = QLabel("糖糖加入的群自动出现在这里。你只需要为需要特殊设置的群配群主/管理/场景。")
        hint.setObjectName("muted"); hint.setWordWrap(True)
        layout.addWidget(hint)
        self._groups_list = QListWidget()
        self._groups_list.setMaximumHeight(250)
        self._groups_list.setToolTip("糖糖加入的群（自动发现）。有⚙标记的群已配置群主/管理/场景。")
        self._attach_overlay_bar(self._groups_list)
        layout.addWidget(self._groups_list)
        btn_row = QHBoxLayout()
        edit_btn = QPushButton("✎ 配置选中群"); edit_btn.setObjectName("actionBtn")
        edit_btn.setToolTip("为选中群设置群主、管理员、场景")
        edit_btn.clicked.connect(self._edit_group_dialog); btn_row.addWidget(edit_btn)
        del_btn = QPushButton("清除设置"); del_btn.setObjectName("dangerBtn")
        del_btn.setToolTip("清除该群的手动配置（群主/管理/场景恢复默认）")
        del_btn.clicked.connect(self._delete_group); btn_row.addWidget(del_btn)
        refresh_btn = QPushButton("刷新"); refresh_btn.setObjectName("actionBtn")
        refresh_btn.setToolTip("重新从数据库读取群列表")
        refresh_btn.clicked.connect(self._refresh_groups_list); btn_row.addWidget(refresh_btn)
        btn_row.addStretch(); layout.addLayout(btn_row)

        # 🆕 私聊场景目标用户（不在任何群的纯私聊好友）
        layout.addWidget(QLabel("私聊场景目标用户（一行一个: QQ号 空格 场景名）:", objectName="body"))
        _avail = []
        try:
            import yaml
            sd = BASE / "scenarios"
            if sd.exists():
                for f in sorted(sd.glob("*.yaml")):
                    d = yaml.safe_load(f.read_text(encoding="utf-8"))
                    if isinstance(d, dict) and "display" in d:
                        _avail.append(d["display"])
        except Exception:
            pass
        _avail_str = "、".join(_avail) if _avail else "无"
        pm_hint = QLabel(f"为纯私聊好友（不在任何群的人）单独指定场景。可用场景：{_avail_str}。格式：QQ号 空格 场景名。")
        pm_hint.setObjectName("muted"); pm_hint.setWordWrap(True)
        layout.addWidget(pm_hint)
        self._private_targets_input = QTextEdit()
        self._private_targets_input.setMaximumHeight(70)
        self._private_targets_input.setPlaceholderText("10001  心理陪伴\n10002  活动管理者")
        # 显式暗色样式（QScrollArea 内 QTextEdit 有时不受全局 QSS 控制）
        self._private_targets_input.setStyleSheet(
            f"QTextEdit {{ background-color: {self._pal['input_bg']}; color: {self._pal['text_primary']};"
            f" border: 1px solid {self._pal['card_border']}; border-radius: 4px; padding: 5px; }}"
        )
        layout.addWidget(self._private_targets_input)

        layout.addStretch()
        self._settings_sections["groups"] = container
        self._settings_fields["groups"] = []
        self._refresh_groups_list()
        self._load_private_targets()

    def _load_private_targets(self):
        """加载全局 scenario_targets 到私聊输入框"""
        targets = self.cfg.get("scenario_targets", {})
        if isinstance(targets, dict) and targets:
            lines = [f"{qq}  {sn}" if sn else f"{qq}  " for qq, sn in targets.items()]
            self._private_targets_input.setPlainText("\n".join(lines))

    def _save_private_targets(self):
        """保存私聊场景目标到 config"""
        text = self._private_targets_input.toPlainText().strip()
        targets = {}
        if text:
            for line in text.split("\n"):
                line = line.strip()
                if not line:
                    continue
                parts = line.split(None, 1)
                qq = parts[0].strip()
                sn = parts[1].strip() if len(parts) > 1 else ""
                if qq:
                    targets[qq] = sn
        self.cfg["scenario_targets"] = targets

    def _build_songs_section(self):
        """曲库管理：列表 + 编辑器"""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.setSpacing(8)
        layout.addWidget(QLabel("曲库管理", objectName="heading"))

        self._songs_dir = BASE / "songs"
        self._songs_dir.mkdir(parents=True, exist_ok=True)

        # 工具栏
        tb = QHBoxLayout()
        add_btn = QPushButton("➕ 新建歌曲")
        add_btn.setObjectName("pinkBtn")
        add_btn.clicked.connect(self._song_new)
        tb.addWidget(add_btn)
        del_btn = QPushButton("删除选中")
        del_btn.setIconSize(QSize(15, 15))
        del_btn._lucide_name = "trash"
        del_btn.setObjectName("dangerBtn")
        del_btn.clicked.connect(self._song_delete)
        tb.addWidget(del_btn)
        save_btn = QPushButton(" 保存当前")
        save_btn.setIconSize(QSize(16, 16))
        save_btn._lucide_name = "save"
        save_btn._lucide_role = "accent_on"
        save_btn.setObjectName("actionBtn")
        save_btn.clicked.connect(self._song_save)
        tb.addWidget(save_btn)
        tb.addStretch()
        count_lbl = QLabel("")
        count_lbl.setObjectName("muted")
        tb.addWidget(count_lbl)
        self._song_count_lbl = count_lbl
        layout.addLayout(tb)

        # 主区域：左列表 + 右编辑
        body = QHBoxLayout()
        self._song_list = QListWidget()
        self._song_list.setFixedWidth(180)
        # ⚠️ 用 blockSignals 防止 refresh 期间的 None 选中触发 _song_select
        self._song_list.currentItemChanged.connect(self._song_select)
        self._attach_overlay_bar(self._song_list)
        body.addWidget(self._song_list)

        # 脏状态追踪
        self._song_dirty = False
        self._song_current_path: Path | None = None

        edit_panel = QWidget()
        ep = QVBoxLayout(edit_panel)
        ep.setContentsMargins(0, 0, 0, 0); ep.setSpacing(6)
        ep.addWidget(QLabel("歌名"))
        self._song_name = QLineEdit()
        self._song_name.setPlaceholderText("歌名（即文件名）")
        ep.addWidget(self._song_name)
        ep.addWidget(QLabel("歌手"))
        self._song_artist = QLineEdit()
        self._song_artist.setPlaceholderText("歌手名（第一行）")
        ep.addWidget(self._song_artist)
        ep.addWidget(QLabel("歌词"))
        self._song_lyrics = QTextEdit()
        self._song_lyrics.setPlaceholderText("歌词内容（每句一行）")
        ep.addWidget(self._song_lyrics, 1)
        body.addWidget(edit_panel, 1)
        layout.addLayout(body, 1)

        # 编辑器变化 → 标记脏（必须在控件创建之后连接）
        self._song_name.textChanged.connect(self._song_mark_dirty)
        self._song_artist.textChanged.connect(self._song_mark_dirty)
        self._song_lyrics.textChanged.connect(self._song_mark_dirty)

        self._settings_sections["songs"] = container
        self._settings_fields["songs"] = []
        self._song_refresh_list()

    def _song_refresh_list(self):
        """刷新歌曲列表（阻止信号避免中间态触发 _song_select(None)）"""
        self._song_list.blockSignals(True)
        self._song_list.clear()
        files = sorted(self._songs_dir.glob("*.txt"), key=lambda f: f.stem)
        for f in files:
            item = QListWidgetItem(f.stem)
            item.setData(Qt.UserRole, str(f))
            self._song_list.addItem(item)
        self._song_count_lbl.setText(f"共 {len(files)} 首")
        self._song_list.blockSignals(False)

    def _song_select(self, item: QListWidgetItem | None):
        """选中歌曲 → 先自动保存当前编辑，再加载新歌"""
        if item is None:
            return

        # ⚠️ 切换前自动保存当前歌曲（含改名处理）
        need_refresh = False
        if self._song_dirty and self._song_current_path:
            new_name = self._song_name.text().strip()
            if new_name and new_name != self._song_current_path.stem:
                # 歌名改了 → 保存到新文件，删旧文件
                new_path = self._songs_dir / f"{new_name}.txt"
                if self._song_do_save(new_path):
                    if self._song_current_path.exists():
                        try:
                            self._song_current_path.unlink()
                        except OSError:
                            pass
                    self._song_current_path = new_path
                    need_refresh = True
            elif self._song_current_path.exists():
                self._song_do_save(self._song_current_path)
            self._song_dirty = False

        path = Path(item.data(Qt.UserRole))
        # 自动保存改名后旧文件已删除 → 用编辑器里的新歌名重新定位
        if not path.exists() and need_refresh:
            new_path = self._songs_dir / f"{self._song_name.text().strip()}.txt"
            if new_path.exists():
                path = new_path
        if not path.exists():
            if need_refresh:
                self._song_refresh_list()
            return
        self._song_current_path = path
        self._song_dirty = False
        # 阻止 textChanged 信号触发 dirty 标记
        self._song_name.blockSignals(True)
        self._song_artist.blockSignals(True)
        self._song_lyrics.blockSignals(True)
        lines = path.read_text(encoding="utf-8").strip().split("\n", 1)
        self._song_name.setText(path.stem)
        self._song_artist.setText(lines[0] if lines else "")
        self._song_lyrics.setPlainText(lines[1] if len(lines) > 1 else "")
        self._song_name.blockSignals(False)
        self._song_artist.blockSignals(False)
        self._song_lyrics.blockSignals(False)

        # 自动保存改过名 → 刷新列表并重新选中当前歌曲
        if need_refresh:
            cur_name = path.stem
            self._song_refresh_list()
            for j in range(self._song_list.count()):
                if self._song_list.item(j).text() == cur_name:
                    self._song_list.setCurrentRow(j)
                    break

    def _song_new(self):
        """新建歌曲"""
        name = "新歌曲"
        i = 1
        while (self._songs_dir / f"{name}{i}.txt").exists():
            i += 1
        name = f"{name}{i}"
        path = self._songs_dir / f"{name}.txt"
        path.write_text("未知歌手\n", encoding="utf-8")
        self._song_refresh_list()
        # 选中新建的
        for j in range(self._song_list.count()):
            if self._song_list.item(j).text() == name:
                self._song_list.setCurrentRow(j)
                break
        self._song_current_path = path
        self._song_dirty = False

    def _song_save(self):
        """保存当前编辑的歌曲（用户手动点击）"""
        item = self._song_list.currentItem()
        if item is None:
            QMessageBox.warning(self, "保存失败", "请先选中一首歌曲。")
            return
        new_name = self._song_name.text().strip()
        if not new_name:
            QMessageBox.warning(self, "保存失败", "歌名不能为空。")
            return
        old_path = Path(item.data(Qt.UserRole))
        new_path = self._songs_dir / f"{new_name}.txt"

        # 写入文件
        if not self._song_do_save(new_path):
            return  # _song_do_save 已弹错误提示

        # 处理改名
        if old_path != new_path:
            if old_path.exists():
                try:
                    old_path.unlink()
                except OSError:
                    pass  # 旧文件删不掉不致命
            self._song_refresh_list()
            for j in range(self._song_list.count()):
                if self._song_list.item(j).text() == new_name:
                    self._song_list.setCurrentRow(j)
                    break
        else:
            item.setText(new_name)
            item.setData(Qt.UserRole, str(new_path))

        self._song_current_path = new_path
        self._song_dirty = False

    def _song_do_save(self, path: Path) -> bool:
        """执行文件写入，返回 True 成功 / False 失败（已弹错误框）"""
        artist = self._song_artist.text().strip()
        lyrics = self._song_lyrics.toPlainText().strip()
        # 空歌手用占位符，防止下次加载时第一句歌词被当成歌手
        if not artist:
            artist = "未知歌手"
        content = f"{artist}\n{lyrics}"
        try:
            path.write_text(content, encoding="utf-8")
            return True
        except OSError as e:
            QMessageBox.warning(self, "保存失败", f"写入文件失败：\n{e}")
            return False

    def _song_mark_dirty(self):
        """编辑器内容变化时标记未保存"""
        self._song_dirty = True

    def _song_delete(self):
        """删除选中歌曲"""
        item = self._song_list.currentItem()
        if item is None:
            return
        path = Path(item.data(Qt.UserRole))
        reply = QMessageBox.question(self, "确认删除", f"删除《{path.stem}》？")
        if reply == QMessageBox.Yes:
            path.unlink(missing_ok=True)
            self._song_refresh_list()

    def _refresh_groups_list(self):
        self._groups_list.clear()
        config_groups = self.cfg.get("groups", {})

        # 从数据库读取糖糖实际加入的群（group_info 表由 refresh_group_info 填充）
        db_groups = {}  # {group_id: group_name}
        try:
            import sqlite3
            db_path = BASE / "memory.db"
            if db_path.exists():
                conn = sqlite3.connect(str(db_path))
                rows = conn.execute(
                    "SELECT group_id, group_name FROM group_info ORDER BY group_name"
                ).fetchall()
                conn.close()
                db_groups = {str(r[0]): r[1] for r in rows if r[0]}
        except Exception:
            pass

        # 合并数据库 + config.yaml——数据库有群名，config 有群配置
        # 两边各可能漏掉一些群，合并后不遗漏
        all_gids = set(db_groups.keys()) | set(str(g) for g in config_groups.keys())

        if not all_gids:
            self._groups_list.addItem("（暂无群数据——糖糖启动后会自动发现所在群聊）")
            return

        for gid in sorted(all_gids):
            gname = db_groups.get(gid, "")
            info = config_groups.get(gid, {})
            if isinstance(info, dict):
                owner = info.get("owner", "")
                admins = info.get("admins", [])
                scenario = info.get("scenario", "")
                targets = info.get("scenario_targets", {})
            else:
                owner, admins, scenario, targets = "", [], "", {}

            # 构建显示行
            name_str = f" {gname}" if gname else ""
            line = f"群 {gid}{name_str}"

            # 配置标记
            flags = []
            if owner:
                flags.append(f"群主:{owner}")
            if admins:
                flags.append(f"管理{len(admins)}人")
            if scenario:
                s_icon = {"psychology": "", "event_manager": ""}.get(scenario, "")
                flags.append(f"{s_icon}{scenario}")
            if targets:
                flags.append(f"+{len(targets)}人精准")

            if flags:
                line += " |  ⚙ " + " |  ".join(flags)
            elif gid in db_groups:
                line += " |  （默认设置）"

            self._groups_list.addItem(line)

    def _add_group_dialog(self):
        gid, ok = QInputDialog.getText(self, "添加群", "群号：")
        if not ok or not gid.strip():
            return
        gid = gid.strip()
        if "groups" not in self.cfg:
            self.cfg["groups"] = {}
        self.cfg["groups"][gid] = {"owner": "", "admins": []}
        self._refresh_groups_list()

    def _edit_group_dialog(self):
        items = self._groups_list.selectedItems()
        if not items:
            return
        gid = items[0].text().split()[1]
        dlg = GroupEditDialog(self, gid, self.cfg.get("groups", {}).get(gid, {}))
        if dlg.exec() == QDialog.Accepted:
            self.cfg["groups"][gid] = dlg.result
            self._refresh_groups_list()

    def _delete_group(self):
        items = self._groups_list.selectedItems()
        if not items:
            return
        gid = items[0].text().split()[1]
        reply = QMessageBox.question(self, "确认删除", f"确定删除群 {gid}？")
        if reply == QMessageBox.Yes:
            if "groups" in self.cfg and gid in self.cfg["groups"]:
                del self.cfg["groups"][gid]
            self._refresh_groups_list()

    def _build_blacklist_section(self):
        """黑名单编辑器"""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.setSpacing(12)
        layout.addWidget(QLabel("黑名单 — 阻止特定群或用户", objectName="heading"))
        hint = QLabel("黑名单里的群/用户，糖糖完全不会回复。支持空格或逗号分隔批量输入多个号码。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # 群黑名单
        layout.addWidget(QLabel("群黑名单", objectName="body"))
        bl_row1 = QHBoxLayout()
        self._bl_group_input = QLineEdit()
        self._bl_group_input.setPlaceholderText("输入群号，回车或点按钮添加（支持空格/逗号分隔多个）")
        self._bl_group_input.setToolTip("输入QQ群号，支持一次输入多个（用空格或逗号隔开）")
        self._bl_group_input.returnPressed.connect(self._add_bl_group)
        bl_row1.addWidget(self._bl_group_input)
        add_bl_g = QPushButton("加入黑名单")
        add_bl_g.setObjectName("dangerBtn")
        add_bl_g.setToolTip("把上面输入的群号加入黑名单")
        add_bl_g.clicked.connect(self._add_bl_group)
        bl_row1.addWidget(add_bl_g)
        layout.addLayout(bl_row1)

        self._bl_groups_list = QListWidget()
        self._bl_groups_list.setMaximumHeight(80)
        self._bl_groups_list.setToolTip("已拉黑的群列表")
        self._attach_overlay_bar(self._bl_groups_list)
        layout.addWidget(self._bl_groups_list)

        del_bl_g = QPushButton("移除选中群")
        del_bl_g.setObjectName("subtleBtn")
        del_bl_g.setToolTip("选中上方列表中的群，点击解除黑名单")
        del_bl_g.clicked.connect(self._remove_bl_group)
        layout.addWidget(del_bl_g)

        # 私聊黑名单
        layout.addSpacing(8)
        layout.addWidget(QLabel("私聊黑名单", objectName="body"))
        bl_row2 = QHBoxLayout()
        self._bl_user_input = QLineEdit()
        self._bl_user_input.setPlaceholderText("输入QQ号，回车或点按钮添加（支持空格/逗号分隔多个）")
        self._bl_user_input.setToolTip("输入QQ号，支持一次输入多个（用空格或逗号隔开）")
        self._bl_user_input.returnPressed.connect(self._add_bl_user)
        bl_row2.addWidget(self._bl_user_input)
        add_bl_u = QPushButton("加入黑名单")
        add_bl_u.setObjectName("dangerBtn")
        add_bl_u.setToolTip("把上面输入的QQ号加入私聊黑名单")
        add_bl_u.clicked.connect(self._add_bl_user)
        bl_row2.addWidget(add_bl_u)
        layout.addLayout(bl_row2)

        self._bl_users_list = QListWidget()
        self._bl_users_list.setMaximumHeight(80)
        self._bl_users_list.setToolTip("已拉黑的用户列表")
        self._attach_overlay_bar(self._bl_users_list)
        layout.addWidget(self._bl_users_list)

        del_bl_u = QPushButton("移除选中用户")
        del_bl_u.setObjectName("subtleBtn")
        del_bl_u.setToolTip("选中上方列表中的用户，点击解除黑名单")
        del_bl_u.clicked.connect(self._remove_bl_user)
        layout.addWidget(del_bl_u)

        layout.addStretch()

        self._settings_sections["blacklist"] = container
        self._settings_fields["blacklist"] = []  # 黑名单直接操作 self.cfg，不走 fields
        self._refresh_blacklist()

    def _refresh_blacklist(self):
        self._bl_groups_list.clear()
        for g in self.cfg.get("blacklist", {}).get("groups", []):
            self._bl_groups_list.addItem(str(g))
        self._bl_users_list.clear()
        for u in self.cfg.get("blacklist", {}).get("private_users", []):
            self._bl_users_list.addItem(str(u))

    def _add_bl_group(self):
        gid = self._bl_group_input.text().strip()
        if gid:
            if "blacklist" not in self.cfg:
                self.cfg["blacklist"] = {}
            if "groups" not in self.cfg["blacklist"]:
                self.cfg["blacklist"]["groups"] = []
            if gid not in self.cfg["blacklist"]["groups"]:
                self.cfg["blacklist"]["groups"].append(gid)
                self._refresh_blacklist()
            self._bl_group_input.clear()

    def _remove_bl_group(self):
        for item in self._bl_groups_list.selectedItems():
            gid = item.text()
            if gid in self.cfg.get("blacklist", {}).get("groups", []):
                self.cfg["blacklist"]["groups"].remove(gid)
        self._refresh_blacklist()

    def _add_bl_user(self):
        uid = self._bl_user_input.text().strip()
        if uid:
            if "blacklist" not in self.cfg:
                self.cfg["blacklist"] = {}
            if "private_users" not in self.cfg["blacklist"]:
                self.cfg["blacklist"]["private_users"] = []
            if uid not in self.cfg["blacklist"]["private_users"]:
                self.cfg["blacklist"]["private_users"].append(uid)
                self._refresh_blacklist()
            self._bl_user_input.clear()

    def _remove_bl_user(self):
        for item in self._bl_users_list.selectedItems():
            uid = item.text()
            if uid in self.cfg.get("blacklist", {}).get("private_users", []):
                self.cfg["blacklist"]["private_users"].remove(uid)
        self._refresh_blacklist()

    # ═══════════════════════════════════════════════════════
    def _build_gpt_sovits_panel(self):
        """展示 GPT-SoVITS 情绪参考音频与当前模型档案。"""
        container = self._settings_sections.get("voice")
        if not container:
            return
        layout = container.layout()
        # 2026-09-05：容器尾部是 _add_section 的 addStretch——先摘掉弹性空白再插入
        # 面板（否则面板被 stretch 推到远端，与字段区之间出现大空隙），收尾补回。
        while layout.count() and layout.itemAt(layout.count() - 1).spacerItem():
            layout.takeAt(layout.count() - 1)
        # 语音区字段少、内容紧凑——收紧上下间距（默认 12 → 6）
        layout.setSpacing(6)
        title = QLabel("🎙️ GPT-SoVITS 声线与参考音频", objectName="heading")
        layout.addWidget(title)
        hint = QLabel("各情绪参考音频位于 gpt-sovits/speakers/<情绪>/ref.wav；缺失项会明确标出。", objectName="muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        refs = []
        speaker_dir = BASE / "gpt-sovits" / "speakers"
        # 真相源 = 磁盘目录（与 voice.py _SPEAKER_PROMPT 一致，硬编码列表会漂移漏项）
        if speaker_dir.is_dir():
            for emotion in sorted(p.name for p in speaker_dir.iterdir() if p.is_dir()):
                path = speaker_dir / emotion / "ref.wav"
                refs.append(f"{'✅' if path.exists() else '⚠️'} {emotion}: {path.relative_to(BASE)}")
        else:
            refs.append(f"⚠️ 目录不存在: {speaker_dir}")
        layout.addWidget(QLabel("\n".join(refs), objectName="muted"))
        layout.addWidget(QLabel(
            "当前说话声线：murasame（丛雨参考音）\n"
            "模型档案：\n"
            " v4（基础底模）：gpt-sovits/GPT_SoVITS/pretrained_models/\n"
            " michele（米雪儿微调）：gpt-sovits/models/michele/",
            objectName="muted",
        ))

        folder_row = QHBoxLayout()
        finetune_btn = QPushButton("微调声线（michele）")
        finetune_btn.setIconSize(QSize(15, 15))
        finetune_btn._lucide_name = "folder-open"
        finetune_btn.setToolTip("角色微调模型：gpt-sovits/models/michele/")
        finetune_btn.clicked.connect(
            lambda _checked=False: self._open_dir(
                BASE / "gpt-sovits" / "models" / "michele", feature="voice")
        )
        folder_row.addWidget(finetune_btn)
        base_btn = QPushButton("底模（V4 等）")
        base_btn.setIconSize(QSize(15, 15))
        base_btn._lucide_name = "folder-open"
        base_btn.setToolTip("GPT-SoVITS 官方底模：gpt-sovits/GPT_SoVITS/pretrained_models/")
        base_btn.clicked.connect(
            lambda _checked=False: self._open_dir(
                BASE / "gpt-sovits" / "GPT_SoVITS" / "pretrained_models", feature="voice")
        )
        folder_row.addWidget(base_btn)
        speaker_btn = QPushButton("参考音频")
        speaker_btn.setIconSize(QSize(15, 15))
        speaker_btn._lucide_name = "folder-open"
        speaker_btn.clicked.connect(
            lambda _checked=False: self._open_dir(
                BASE / "gpt-sovits" / "speakers", feature="voice")
        )
        folder_row.addWidget(speaker_btn)
        folder_row.addStretch()
        layout.addLayout(folder_row)
        layout.addStretch()  # 补回弹性空白（与其它 section 尾部一致）

    # 外观设置
    # ═══════════════════════════════════════════════════════
    # 情绪预设按钮

    def _build_mood_presets(self):
        """在情绪 section 下方添加预设按钮，一键切换糖糖的情绪风格"""
        mood_container = self._settings_sections.get("mood")
        if not mood_container:
            return
        layout = mood_container.layout()
        # 去掉最后的 stretch
        stretch = layout.takeAt(layout.count() - 1) if layout.count() > 0 else None

        preset_label = QLabel("📌 一键预设：")
        preset_label.setObjectName("body")
        layout.addWidget(preset_label)

        # 预设定义
        presets = [
            ("活泼猫娘", {
                "mood.decay_energy_per_hour": 1,
                "mood.mood_neutral": 70,
                "mood.decay_mood_per_hour": 5,
                "mood.decay_patience_per_hour": 10,
                "mood.energy_high": 50,
                "mood.energy_normal": 25,
                "mood.energy_low": 10,
            }, "精力充沛不易困，心情好恢复快，适合想让糖糖一直元气满满的场景"),
            ("温和日常", {
                "mood.decay_energy_per_hour": 3,
                "mood.mood_neutral": 60,
                "mood.decay_mood_per_hour": 1,
                "mood.decay_patience_per_hour": 2,
                "mood.energy_high": 60,
                "mood.energy_normal": 30,
                "mood.energy_low": 15,
            }, "默认平衡状态，有活力也会累，中规中矩"),
            ("慵懒猫娘", {
                "mood.decay_energy_per_hour": 5,
                "mood.mood_neutral": 50,
                "mood.decay_mood_per_hour": 1,
                "mood.decay_patience_per_hour": 2,
                "mood.energy_high": 75,
                "mood.energy_normal": 45,
                "mood.energy_low": 25,
            }, "容易困、经常犯懒、说话软绵绵。适合想让糖糖更'猫'一点的场景"),
        ]

        btn_row = QHBoxLayout()
        for name, values, tip in presets:
            btn = QPushButton(name)
            btn.setToolTip(f"{tip}\n点击即可一键设置所有情绪参数")
            btn.clicked.connect(lambda checked, v=values: self._apply_mood_preset(v))
            btn_row.addWidget(btn)
        layout.addLayout(btn_row)

        if stretch:
            layout.addItem(stretch)

    def _apply_mood_preset(self, values: dict):
        """应用情绪预设：更新 UI 控件 + 直接写入 config"""
        for path, widget, ftype in self._settings_fields.get("mood", []):
            if path not in values:
                continue
            val = values[path]
            try:
                if ftype == "int":
                    widget.setValue(val)
                elif ftype == "float":
                    widget.setValue(float(val))
                elif ftype == "str":
                    widget.setText(str(val))
                # 同步写入 config
                _set_nested(self.cfg, path, val)
            except Exception:
                pass
        # 直接保存到文件
        save_config_file(self.cfg)
        self.cfg = load_config()
        preset_names = {
            "40,15,3": "活泼猫娘",
            "70,40,15": "温和日常",
            "85,60,30": "慵懒猫娘",
        }
        key = f"{values['mood.energy_high']},{values['mood.energy_normal']},{values['mood.energy_low']}"
        name = preset_names.get(key, "自定义")
        self._log(f"✅ 情绪预设「{name}」已应用并保存")

    def _studio_availability_hint(self) -> QLabel:
        """歌唱工作室顶部状态：组件齐不齐、缺的是什么、要不要管。

        判据直接用 `_PIPELINE_DOWNLOADS`——**「一键全流程」判断缺什么用的就是这张表**，
        这里再抄一份必然漂移。单一真相源。
        """
        missing = [label for label, (rel, _url, _kind) in _PIPELINE_DOWNLOADS.items()
                   if not (BASE / rel).exists()]
        if not missing:
            # 只说「文件在位」——这里查的就是文件。写「可以转换」就越界了：
            # 2026-09-19 实测开发机文件全在、RVC 却因 faiss 与 NumPy 2 的 ABI 冲突
            # 根本 import 不了。一句过头的绿字比不写更坏。
            text = "✅ 翻唱组件文件已就位（RVC 推理脚本、HuTao 模型、人声分离环境）。"
        else:
            text = ("⚠ 这里是把歌**做成**糖糖声线的制作工具，需要额外组件，"
                    f"当前缺 {len(missing)} 项：{'、'.join(missing)}。\n"
                    "　▸ 只是想听糖糖唱歌？**不需要**这里——41 首预录成品已随包自带，"
                    "群里点歌即播。")
        lbl = QLabel(text)
        lbl.setObjectName("muted")
        lbl.setWordWrap(True)
        return lbl

    def _build_singing_panel(self):
        """歌唱工作室（保留）"""
        songs_container = self._settings_sections.get("songs")
        if not songs_container:
            return
        layout = songs_container.layout()
        self._studio_list = QListWidget()
        self._studio_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._attach_overlay_bar(self._studio_list)
        layout.addWidget(QLabel("🎙 歌唱工作室", objectName="heading"))
        # 先把「这块能不能用、缺什么」说在前面，别等用户点了才弹错误。
        # 2026-09-19 主人实机反馈：他点了「模型」只拿到一句「找不到目录」，
        # 只能反过来问「是基础文字对话没有这个功能吗」——说明缺的从来不是那个弹窗文案，
        # 而是**事前就没告诉他这里需要额外组件**。
        layout.addWidget(self._studio_availability_hint())
        layout.addWidget(self._studio_list)
        self._studio_add_btn = QPushButton("添加翻唱")
        self._studio_add_btn.setIconSize(QSize(15, 15))
        self._studio_add_btn._lucide_name = "download"
        self._studio_add_btn.clicked.connect(self._studio_add_cover)
        # 2026-09-05：按钮不拉全宽，靠左排列（与下方一键全流程按钮对齐）
        layout.addWidget(self._studio_add_btn, 0, Qt.AlignLeft)
        self._studio_batch_btn = QPushButton("一键全流程")
        self._studio_batch_btn.setIconSize(QSize(15, 15))
        self._studio_batch_btn._lucide_name = "rocket"
        self._studio_batch_btn.setObjectName("pinkBtn")
        self._studio_batch_btn.setToolTip(
            "一键全流程：人声分离(demucs) → RVC 音色转换(HuTao) → 切除静音 → 输出到 songs/audio\n"
            "依赖：RVC WebUI(HuTao 模型/索引/HuBERT 底模)、venv_demucs 环境、separate_vocals.py\n"
            "缺失时会列出清单并可一键下载；每首约 3-10 分钟；已有音频会被覆盖"
        )
        self._studio_batch_btn.clicked.connect(self._studio_full_pipeline)
        # 一键全流程 + 模型文件夹按钮（水平并排；2026-09-05 主人需求）
        flow_row = QHBoxLayout()
        flow_row.addWidget(self._studio_batch_btn)
        self._studio_model_btn = QPushButton("模型")
        self._studio_model_btn.setIconSize(QSize(15, 15))
        self._studio_model_btn._lucide_name = "folder"
        self._studio_model_btn.setToolTip(
            "打开 RVC 音色模型文件夹（assets/weights/）——一键全流程使用的模型文件"
            "（HuTao 模型/索引）所在；缺失时可从发布附件「歌唱模型包」获取")
        self._studio_model_btn.clicked.connect(
            lambda _checked=False: self._open_dir(
                BASE / "Retrieval-based-Voice-Conversion-WebUI" / "assets" / "weights",
                feature="rvc"))
        flow_row.addWidget(self._studio_model_btn)
        flow_row.addStretch()
        layout.addLayout(flow_row)
        self._studio_status = QLabel("")
        layout.addWidget(self._studio_status)
        self._studio_refresh()

    def _open_dir(self, path, feature=None):
        """安全打开目录。目录不存在时，说明它属于哪个功能、怎么才能有。

        `feature` 取 `_MISSING_DIR_HINT` 的键；不传则退化成只报路径
        （文案里明说「这是个内部目录」，免得又变成一句没有出路的提示）。
        """
        if not path.is_dir():
            title, why = _missing_dir_message(path, feature)
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle(title)
            box.setText(why)
            box.setInformativeText(f"要找的位置：\n{path}")
            box.exec()
            return
        try:
            os.startfile(str(path))
        except OSError as e:
            QMessageBox.warning(self, "打开失败", f"无法打开目录：\n{path}\n\n{e}")

    # ── 唱歌面板方法 ──

    # ═══════════════════════════════════════════════════════
    # 歌唱工作室：人声分离 + RVC 音色转换
    # ═══════════════════════════════════════════════════════

    def _studio_set_busy(self, busy: bool):
        """防止重复点击：忙碌时禁用操作按钮"""
        for btn in [self._studio_batch_btn]:
            btn.setEnabled(not busy)
        self._studio_add_btn.setEnabled(not busy)

    def _studio_refresh(self):
        """刷新翻唱列表"""
        self._studio_list.clear()
        covers_dir = BASE / "songs" / "covers"
        if not covers_dir.exists():
            return
        htdemucs_dir = covers_dir / "htdemucs"
        audio_dir = BASE / "songs" / "audio"

        files = sorted(
            [f for f in covers_dir.glob("*") if f.suffix.lower() in (".wav", ".mp3", ".m4a")],
            key=lambda f: f.stem,
        )
        for f in files:
            name = f.stem
            # 检查状态
            vocal_path = htdemucs_dir / name / "vocals.wav"
            out_path = audio_dir / f"{name}.wav"
            has_vocal = vocal_path.exists()
            has_output = out_path.exists() and out_path.stat().st_size > 10000

            if has_output:
                icon = "✅"
                status = "已转换"
            elif has_vocal:
                icon = "🔇"
                status = "已分离人声"
            else:
                icon = "⬜"
                status = "待处理"

            size_mb = f.stat().st_size / 1024 / 1024
            item = QListWidgetItem(f"{icon} {name}  [{status}]  ({size_mb:.1f}MB)")
            item.setData(Qt.UserRole, str(f))
            self._studio_list.addItem(item)

        self._studio_status.setText(
            f"共 {len(files)} 首翻唱 | "
            f"已分离: {sum(1 for f in files if (htdemucs_dir / f.stem / 'vocals.wav').exists())} | "
            f"已转换: {sum(1 for f in files if (audio_dir / f'{f.stem}.wav').exists())}"
        )

    def _studio_add_cover(self):
        """多选翻唱文件添加到 covers 目录"""
        from PySide6.QtWidgets import QFileDialog
        covers_dir = str(BASE / "songs" / "covers")
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择翻唱音频（Ctrl+A 全选 / Ctrl+点击多选）", covers_dir,
            "音频文件 (*.mp3 *.wav *.m4a *.flac);;所有文件 (*.*)",
        )
        if not paths:
            return

        # 显示确认列表
        names = "\n".join(f" {i+1}. {Path(p).name}" for i, p in enumerate(paths))
        reply = QMessageBox.question(
            self, f"确认添加 {len(paths)} 首",
            f"将添加以下翻唱到 covers：\n\n{names}",
            QMessageBox.Ok | QMessageBox.Cancel,
        )
        if reply != QMessageBox.Ok:
            return

        import shutil
        added, skipped, replaced = 0, 0, 0
        for path in paths:
            src = Path(path)
            dst = BASE / "songs" / "covers" / src.name
            # 已经在 covers 目录里了，跳过
            if src.resolve() == dst.resolve():
                skipped += 1
                continue
            if dst.exists():
                reply = QMessageBox.question(self, "文件已存在", f"{src.name} 已存在，覆盖？",
                                             QMessageBox.Yes | QMessageBox.No)
                if reply != QMessageBox.Yes:
                    skipped += 1
                    continue
                replaced += 1
            shutil.copy2(str(src), str(dst))
            added += 1
        parts = [f"✅ 已添加 {added} 首"]
        if replaced:
            parts.append(f"覆盖 {replaced} 首")
        if skipped:
            parts.append(f"跳过 {skipped} 首")
        self._studio_status.setText("，".join(parts))
        self._studio_refresh()

    def _studio_vocal_sep(self):
        """人声分离：对选中的所有翻唱逐个执行"""
        if hasattr(self, '_studio_worker') and self._studio_worker and self._studio_worker.isRunning():
            QMessageBox.information(self, "忙碌中", "当前有操作正在执行，请等待完成后再试。")
            return
        rows = self._studio_list.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "提示", "请先在列表中选中翻唱（Ctrl/Shift 多选）")
            return

        tasks = []
        covers_dir = str(BASE / "songs" / "covers")
        names = []
        for idx in rows:
            item = self._studio_list.item(idx.row())
            name = Path(item.data(Qt.UserRole)).stem
            cover_path = str(Path(item.data(Qt.UserRole)))
            vocal_path = Path(covers_dir) / "htdemucs" / name / "vocals.wav"
            tasks.append((name, cover_path, vocal_path.exists()))
            names.append(name)

        self._log(f"🔇 人声分离: {len(tasks)} 首 → {names}")
        self._studio_status.setText(f"🔇 正在分离人声: 1/{len(tasks)} ...")
        self._studio_set_busy(True)

        from PySide6.QtCore import QThread, Signal
        class BatchSepThread(QThread):
            progress_sig = Signal(str)
            finished_sig = Signal(bool, str)
            def __init__(self, tasks, covers_dir):
                super().__init__()
                self.tasks = tasks
                self.covers_dir = covers_dir
            def run(self):
                import subprocess, traceback as _tb
                ok = 0
                for idx, (name, cover_path, has_vocal) in enumerate(self.tasks):
                    tag = f"[{idx+1}/{len(self.tasks)}]"
                    self.progress_sig.emit(f"🔇 {tag} {name} ...")
                    try:
                        cmd = (
                            "import torchaudio, soundfile;"
                            "torchaudio.save = lambda p, w, sample_rate, **kw: soundfile.write(str(p), w.numpy().T if hasattr(w, 'numpy') and w.ndim==2 else w.numpy() if hasattr(w, 'numpy') else w, sample_rate);"
                            "import sys; sys.argv = ['x', '--two-stems', 'vocals', '-d', 'cpu', "
                            f"r'{cover_path}', '-o', r'{self.covers_dir}'];"
                            "from demucs.separate import main; main()"
                        )
                        r = subprocess.run(
                            [sys.executable, "-c", cmd],
                            capture_output=True, text=True, timeout=600, cwd=self.covers_dir,
                        )
                        if r.returncode != 0:
                            self.progress_sig.emit(f"❌ {tag} {name}: {r.stderr[-200:]}")
                        else:
                            ok += 1
                            self.progress_sig.emit(f"✅ {tag} {name} 完成")
                    except subprocess.TimeoutExpired:
                        self.progress_sig.emit(f"⏰ {tag} {name}: 超时")
                    except Exception as e:
                        self.progress_sig.emit(f"❌ {tag} {name}: {e}")
                self.finished_sig.emit(ok > 0, f"{ok}/{len(self.tasks)} 完成")

        self._studio_worker = BatchSepThread(tasks, covers_dir)
        self._studio_worker.progress_sig.connect(
            lambda msg: (self._studio_status.setText(msg), self._log(msg))
        )
        self._studio_worker.finished_sig.connect(self._studio_on_sep_done)
        self._studio_worker.start()

    def _studio_on_sep_done(self, ok, msg):
        try:
            if ok:
                self._studio_status.setText(f"✅ {msg}")
                self._log(f"✅ 人声分离完成: {msg}")
            else:
                self._studio_status.setText(f"❌ {msg}")
                self._log(f"❌ 人声分离失败: {msg}")
            self._studio_refresh()
        except Exception as e:
            self._log(f"⚠ 人声分离回调异常: {e}")
        finally:
            self._studio_set_busy(False)

    def _studio_rvc_convert(self):
        """RVC 音色转换：对选中的所有翻唱逐个执行"""
        if hasattr(self, '_studio_worker') and self._studio_worker and self._studio_worker.isRunning():
            QMessageBox.information(self, "忙碌中", "当前有操作正在执行，请等待完成后再试。")
            return
        rows = self._studio_list.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "提示", "请先在列表中选中翻唱（Ctrl/Shift 多选）")
            return

        covers_dir = BASE / "songs" / "covers"
        audio_dir = BASE / "songs" / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        rvc_dir = str(BASE / "Retrieval-based-Voice-Conversion-WebUI")
        infer_script = str(BASE / "Retrieval-based-Voice-Conversion-WebUI" / "tools" / "infer_cli.py")
        index_path = RVC_INDEX_REL          # 相对路径——原因见 RVC_INDEX_REL 的注释

        names = []
        tasks = []
        for idx in rows:
            item = self._studio_list.item(idx.row())
            names.append(Path(item.data(Qt.UserRole)).stem)
            name = Path(item.data(Qt.UserRole)).stem
            vocal = covers_dir / "htdemucs" / name / "vocals.wav"
            out = audio_dir / f"{name}.wav"
            if vocal.exists():
                tasks.append((name, str(vocal), str(out)))

        self._log(f"🎤 RVC 转换: 选中 {len(rows)} 首 → {names}，有分离人声的 {len(tasks)} 首")
        if not tasks:
            QMessageBox.information(self, "提示", "选中的翻唱都还没有分离人声，请先执行「人声分离」")
            return

        self._studio_status.setText(f"🎤 正在 RVC 转换: 1/{len(tasks)} ...")
        self._studio_set_busy(True)

        from PySide6.QtCore import QThread, Signal
        class BatchRvcThread(QThread):
            progress_sig = Signal(str)
            finished_sig = Signal(bool, str)
            def __init__(self, tasks, infer_script, index_path, rvc_dir):
                super().__init__()
                self.tasks = tasks
                self.infer_script = infer_script
                self.index_path = index_path
                self.rvc_dir = rvc_dir
            def run(self):
                import subprocess, traceback as _tb
                py = sys.executable
                ok = 0
                for idx, (name, vocal, out) in enumerate(self.tasks):
                    tag = f"[{idx+1}/{len(self.tasks)}]"
                    self.progress_sig.emit(f"🎤 {tag} {name} ...")
                    try:
                        r = subprocess.run([
                            py, self.infer_script,
                            "--input_path", vocal, "--opt_path", out,
                            "--model_name", "hutao", "--index_path", self.index_path,
                            "--f0method", "fcpe", "--rms_mix_rate", "0.7",
                        ], capture_output=True, text=True, timeout=600, cwd=self.rvc_dir)
                        if Path(out).exists() and Path(out).stat().st_size > 50000:
                            ok += 1
                            self.progress_sig.emit(f"✅ {tag} {name} 完成")
                        else:
                            self.progress_sig.emit(f"❌ {tag} {name}: {r.stderr[-200:] if r.stderr else '无输出'}")
                    except subprocess.TimeoutExpired:
                        self.progress_sig.emit(f"⏰ {tag} {name}: 超时")
                    except Exception as e:
                        self.progress_sig.emit(f"❌ {tag} {name}: {e}")
                self.finished_sig.emit(ok > 0, f"{ok}/{len(self.tasks)} 完成")

        self._studio_worker = BatchRvcThread(tasks, infer_script, index_path, rvc_dir)
        self._studio_worker.progress_sig.connect(
            lambda msg: (self._studio_status.setText(msg), self._log(msg))
        )
        self._studio_worker.finished_sig.connect(self._studio_on_rvc_done)
        self._studio_worker.start()

    def _studio_on_rvc_done(self, ok, msg):
        try:
            if ok:
                self._studio_status.setText(f"✅ {msg}")
                self._log(f"✅ RVC 转换完成: {msg}")
            else:
                self._studio_status.setText(f"❌ {msg}")
                self._log(f"❌ RVC 转换失败: {msg}")
            self._studio_refresh()
        except Exception as e:
            self._log(f"⚠ RVC 回调异常: {e}")
        finally:
            self._studio_set_busy(False)

    def _studio_denoise(self):
        """DeepFilterNet3 降噪：对全曲 WAV 进行人声降噪"""
        item = self._studio_list.currentItem()
        if item is None:
            # 没有选中翻唱 → 对全部全曲 WAV 执行
            reply = QMessageBox.question(
                self, "批量降噪",
                "没有选中翻唱。要对 songs/audio/ 下所有全曲 WAV 批量降噪吗？",
            )
            if reply != QMessageBox.Yes:
                return
            wavs = sorted((BASE / "songs" / "audio").glob("*.wav"))
            if not wavs:
                QMessageBox.information(self, "提示", "songs/audio/ 下没有全曲 WAV 文件")
                return
            songs_to_denoise = [w.stem for w in wavs]
        else:
            path = Path(item.data(Qt.UserRole))
            name = path.stem
            full_wav = BASE / "songs" / "audio" / f"{name}.wav"
            if not full_wav.exists():
                QMessageBox.warning(self, "错误", f"没有找到全曲 WAV：\n{full_wav}\n\n请先完成 RVC 转换生成全曲音频。")
                return
            songs_to_denoise = [name]

        self._log(f"🎵 降噪启动: {', '.join(songs_to_denoise)}")
        self._studio_status.setText(f"🎵 正在降噪: {len(songs_to_denoise)} 首...")

        from PySide6.QtCore import QThread, Signal

        class DenoiseThread(QThread):
            finished_sig = Signal(bool, str)

            def __init__(self, song_names):
                super().__init__()
                self.song_names = song_names

            def run(self):
                import subprocess, traceback as _tb
                try:
                    denoise_script = str(BASE / "tools" / "denoise.py")
                    for name in self.song_names:
                        r = subprocess.run(
                            ["python", denoise_script, "--song", name],
                            capture_output=True, text=True, timeout=300,
                            cwd=str(BASE),
                        )
                        if r.returncode != 0:
                            self.finished_sig.emit(False, f"{name}: {r.stderr[-200:]}")
                            return
                    total = len(self.song_names)
                    self.finished_sig.emit(True, f"{total} 首降噪完成")
                except subprocess.TimeoutExpired:
                    self.finished_sig.emit(False, "⏰ 超时（>5分钟）")
                except Exception as e:
                    self.finished_sig.emit(False, f"异常: {e}\n{_tb.format_exc()}")

        self._studio_set_busy(True)
        self._studio_worker = DenoiseThread(songs_to_denoise)
        self._studio_worker.finished_sig.connect(self._studio_on_denoise_done)
        self._studio_worker.start()

    def _studio_on_denoise_done(self, ok, msg):
        try:
            if ok:
                self._studio_status.setText(f"✅ {msg}")
                self._log(f"✅ 降噪完成: {msg}")
            else:
                self._studio_status.setText(f"❌ {msg}")
                self._log(f"❌ 降噪失败: {msg}")
            self._studio_refresh()
        except Exception as e:
            self._log(f"⚠ 降噪回调异常: {e}")
        finally:
            self._studio_set_busy(False)

    def _studio_trim_silence(self):
        """切除全曲音频的前奏/尾奏静音"""
        item = self._studio_list.currentItem()
        if item is None:
            # 没有选中翻唱 → 对全部已存在的全曲 WAV 执行
            reply = QMessageBox.question(
                self, "批量切除静音",
                "没有选中翻唱。要对 songs/audio/ 下所有全曲 WAV 批量切除静音吗？",
            )
            if reply != QMessageBox.Yes:
                return
            wavs = sorted((BASE / "songs" / "audio").glob("*.wav"))
            if not wavs:
                QMessageBox.information(self, "提示", "songs/audio/ 下没有全曲 WAV 文件")
                return
            songs_to_trim = [w.stem for w in wavs]
        else:
            path = Path(item.data(Qt.UserRole))
            name = path.stem
            # 找到对应的全曲 WAV
            full_wav = BASE / "songs" / "audio" / f"{name}.wav"
            if not full_wav.exists():
                QMessageBox.warning(self, "错误", f"没有找到全曲 WAV：\n{full_wav}\n\n请先完成 RVC 转换生成全曲音频。")
                return
            songs_to_trim = [name]

        self._log(f"✂️ 切除静音启动: {', '.join(songs_to_trim)}")
        self._studio_status.setText(f"✂️ 正在切除静音: {len(songs_to_trim)} 首...")

        from PySide6.QtCore import QThread, Signal

        class TrimThread(QThread):
            finished_sig = Signal(bool, str)

            def __init__(self, song_names):
                super().__init__()
                self.song_names = song_names

            def run(self):
                import subprocess, traceback as _tb
                try:
                    trim_script = str(BASE / "tools" / "trim_silence.py")
                    for name in self.song_names:
                        r = subprocess.run(
                            ["python", trim_script, "--song", name],
                            capture_output=True, text=True, timeout=120,
                            cwd=str(BASE),
                        )
                        if r.returncode != 0:
                            self.finished_sig.emit(False, f"{name}: {r.stderr[-200:]}")
                            return
                    total = len(self.song_names)
                    self.finished_sig.emit(True, f"{total} 首切除完成")
                except subprocess.TimeoutExpired:
                    self.finished_sig.emit(False, "⏰ 超时")
                except Exception as e:
                    self.finished_sig.emit(False, f"异常: {e}\n{_tb.format_exc()}")

        self._studio_set_busy(True)
        self._studio_worker = TrimThread(songs_to_trim)
        self._studio_worker.finished_sig.connect(self._studio_on_trim_done)
        self._studio_worker.start()

    def _studio_on_trim_done(self, ok, msg):
        try:
            if ok:
                self._studio_status.setText(f"✅ {msg}")
                self._log(f"✅ 切除静音完成: {msg}")
            else:
                self._studio_status.setText(f"❌ {msg}")
                self._log(f"❌ 切除静音失败: {msg}")
            self._studio_refresh()
        except Exception as e:
            self._log(f"⚠ 切除静音回调异常: {e}")
        finally:
            self._studio_set_busy(False)

    # ── 流水线缺失文件下载（2026-09-05 L1 补丁：接「下载缺失文件」按钮）──
    def _download_missing_pipeline(self, missing):
        """下载缺失的流水线依赖（kind=dl 且带 URL 的项）。

        env/pkg 类无下载地址，弹窗已分别提示去向（安装器/随包）；此处只处理
        真正可在线下载的模型文件。子线程逐项落位，完成后可重跑一键全流程。
        """
        from PySide6.QtCore import QThread, Signal

        jobs = [(label, path, url) for label, path, url, kind in missing if kind == "dl" and url]
        if not jobs:
            QMessageBox.information(self, "提示", "缺少的文件均无在线下载地址：\n"
                                    "• HuTao 模型/索引：待作者提供下载地址后重试\n"
                                    "• Demucs 环境：运行「安装糖糖.bat」→ 安装唱歌组件\n"
                                    "• 随包文件：重新解压或更新版本")
            return

        class _PipelineDownloadThread(QThread):
            progress_sig = Signal(str)
            finished_sig = Signal(bool, str)

            def __init__(self, jobs):
                super().__init__()
                self._jobs = jobs

            def run(self):
                import urllib.request
                ok_list, fail_list = [], []
                for label, path, url in self._jobs:
                    try:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        tmp = path.with_name(path.name + ".part")
                        req = urllib.request.Request(url, headers={"User-Agent": "TangTangConsole/1.0"})
                        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as f:
                            while True:
                                chunk = resp.read(65536)
                                if not chunk:
                                    break
                                f.write(chunk)
                        tmp.replace(path)
                        ok_list.append(label)
                        self.progress_sig.emit(f"✅ 已下载 {label}")
                    except Exception as e:
                        fail_list.append(f"{label}（{e}）")
                        self.progress_sig.emit(f"❌ {label} 下载失败: {e}")
                self.finished_sig.emit(not fail_list, "；".join(fail_list))

        self._log("📥 开始下载缺失的流水线文件…")

        def _on_progress(msg):
            self._studio_status.setText(f"📥 {msg}")

        def _on_finished(ok, fails):
            self._studio_set_busy(False)
            if ok:
                self._studio_status.setText("✅ 缺失文件下载完成，可重新运行一键全流程")
                self._log("✅ 缺失文件下载完成")
                QMessageBox.information(self, "下载完成", "缺失文件已就位，可以重新点「一键全流程」。")
            else:
                self._studio_status.setText(f"❌ 下载未完成: {fails}")
                self._log(f"❌ 下载未完成: {fails}")
                QMessageBox.warning(self, "下载未完成", f"下载失败：\n{fails}")

        self._studio_set_busy(True)
        self._download_worker = _PipelineDownloadThread(jobs)
        self._download_worker.progress_sig.connect(_on_progress)
        self._download_worker.finished_sig.connect(_on_finished)
        self._download_worker.start()

    def _studio_full_pipeline(self):
        """一键全流程：四步流水线分离 → RVC → 切除静音"""
        if hasattr(self, '_studio_worker') and self._studio_worker and self._studio_worker.isRunning():
            QMessageBox.information(self, "忙碌中", "当前有操作正在执行，请等待完成后再试。")
            return
        covers_dir = BASE / "songs" / "covers"
        audio_dir = BASE / "songs" / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        rvc_dir = BASE / "Retrieval-based-Voice-Conversion-WebUI"
        infer_script = rvc_dir / "tools" / "infer_cli.py"
        # 相对路径（cwd 是 rvc_dir）——同 RVC_INDEX_REL 的注释，绝对路径带中文会让 faiss 打不开
        index_path = RVC_INDEX_REL
        sep_script = BASE / "tools" / "separate_vocals.py"
        venv_python = BASE / "venv_demucs" / "Scripts" / "python.exe"

        missing = [(label, BASE / rel, url, kind)
                   for label, (rel, url, kind) in _PIPELINE_DOWNLOADS.items()
                   if not (BASE / rel).exists()]
        if missing:
            lines = []
            for label, path, url, kind in missing:
                note = {"dl": "（可下载）", "env": "（需运行安装器装唱歌组件）",
                        "pkg": "（文件异常——随包/随 RVC 自带，请重装）"}.get(kind, "")
                if kind == "dl" and not url:
                    note = "（暂无下载地址）"
                lines.append(f"• {label}: {path} {note}")
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("流水线依赖缺失")
            box.setText(f"一键全流程暂不可用，缺少：\n" + "\n".join(lines))
            dl_btn = box.addButton("下载缺失文件", QMessageBox.AcceptRole)
            box.addButton("取消", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is dl_btn:
                self._download_missing_pipeline(missing)
            return

        # 构建任务列表
        rows = self._studio_list.selectionModel().selectedRows()
        if rows:
            # 有选中 → 只处理选中的
            names = []
            for idx in rows:
                item = self._studio_list.item(idx.row())
                names.append(Path(item.data(Qt.UserRole)).stem)
        else:
            # 没选中 → 全部
            names = sorted(set(
                f.stem for f in covers_dir.glob("*")
                if f.is_file() and f.suffix.lower() in (".mp3", ".wav", ".flac", ".m4a", ".ogg")
            ))
        tasks = []
        for name in names:
            cover_files = list(covers_dir.glob(f"{name}.*"))
            if not cover_files:
                continue
            cover_path = cover_files[0]
            vocal_path = covers_dir / "htdemucs" / name / "vocals.wav"
            out_path = audio_dir / f"{name}.wav"
            tasks.append((name, cover_path, vocal_path, out_path))

        if not tasks:
            QMessageBox.information(self, "提示", "covers/ 下没有翻唱文件。\n请先点击「添加翻唱」上传歌曲。")
            return

        # 确认弹窗
        preview = "\n".join(f" • {n}" for n in names[:15])
        more = f"\n  ...等共 {len(names)} 首" if len(names) > 15 else ""
        reply = QMessageBox.question(
            self, "一键全流程",
            f"将对以下 {len(tasks)} 首翻唱执行全流程：\n\n{preview}{more}\n\n"
            f"⚠️ 已有音频的歌曲将被覆盖重新生成。\n"
            f"预计每首 3-10 分钟。\n\n确认开始？",
        )
        if reply != QMessageBox.Yes:
            return

        total = len(tasks)
        plural = "首" if total > 1 else ""
        self._log(f"🎙 一键全流程启动: {total} {plural}翻唱")
        self._studio_status.setText(f"一键全流程: 共 {total} 首 ...（每首约3-10分钟）")

        from PySide6.QtCore import QThread, Signal

        class FullPipelineThread(QThread):
            progress_sig = Signal(str)
            finished_sig = Signal(bool, str)

            def __init__(self, _tasks):
                super().__init__()
                self._tasks = _tasks

            def run(self):
                import subprocess as _sp, traceback as _tb, os as _os
                py = sys.executable
                ok_count = 0
                fail_list: list[str] = []

                def _run_cmd(cmd, cwd, timeout, desc):
                    """实时输出的 subprocess 包装"""
                    proc = _sp.Popen(
                        cmd, stdout=_sp.PIPE, stderr=_sp.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                        cwd=str(cwd), bufsize=1,
                    )
                    try:
                        for line in proc.stdout:
                            line = line.rstrip()
                            if line:
                                self.progress_sig.emit(f"   {desc}: {line[:120]}")
                        proc.wait(timeout=timeout)
                    except _sp.TimeoutExpired:
                        proc.kill()
                        return False, "超时"
                    return proc.returncode == 0, ""

                for idx, (name, cover_path, vocal_path, out_path) in enumerate(self._tasks):
                    tag = f"[{idx+1}/{len(self._tasks)}]"
                    try:
                        # Step 1: 四步流水线人声分离
                        sep_output = covers_dir / "separated" / f"{name}_FINAL.wav"
                        if not sep_output.exists():
                            self.progress_sig.emit(f"🔇 {tag} Step 1/3 分离: {name}")
                            ok, err = _run_cmd(
                                [str(venv_python), str(sep_script), str(cover_path),
                                 "-o", str(covers_dir / "separated")],
                                BASE, 1200, f"{name}"
                            )
                            if not ok or not sep_output.exists():
                                fail_list.append(f"{name}: 分离失败 {err}")
                                continue
                        else:
                            self.progress_sig.emit(f"🔇 {tag} 分离已完成，跳过: {name}")

                        # Step 2: RVC 转换
                        self.progress_sig.emit(f"🎤 {tag} Step 2/3 RVC: {name}")
                        out_path.parent.mkdir(parents=True, exist_ok=True)
                        rvc_input = sep_output if sep_output.exists() else vocal_path
                        if not rvc_input.exists():
                            fail_list.append(f"{name}: 找不到人声文件")
                            continue
                        ok, err = _run_cmd([
                            py, str(infer_script),
                            "--input_path", str(rvc_input),
                            "--opt_path", str(out_path),
                            "--model_name", "hutao",
                            "--index_path", str(index_path),
                            "--f0method", "fcpe",
                            "--rms_mix_rate", "0.7",
                        ], rvc_dir, 600, f"{name} RVC")
                        if not (out_path.exists() and out_path.stat().st_size > 50000):
                            fail_list.append(f"{name}: RVC 转换失败")
                            continue

                        # Step 3: 切除静音
                        self.progress_sig.emit(f"✂️ {tag} Step 3/3 切除静音: {name}")
                        trim_script = str(BASE / "tools" / "trim_silence.py")
                        ok, err = _run_cmd(
                            [py, trim_script, "--song", name],
                            BASE, 120, f"{name} 静音"
                        )
                        if ok:
                            ok_count += 1
                        else:
                            fail_list.append(f"{name}: 切除静音失败")

                    except Exception as e:
                        fail_list.append(f"{name}: {e}")

                # 清理空目录
                sep_dir = covers_dir / "separated"
                if sep_dir.exists():
                    for d in sep_dir.iterdir():
                        if d.is_dir() and not any(d.iterdir()):
                            try: d.rmdir()
                            except: pass

                # 汇总结果
                summary_parts = [f"✅ {ok_count}/{len(self._tasks)} 首完成"]
                if fail_list:
                    summary_parts.append(f"❌ {len(fail_list)} 首失败: " + "; ".join(fail_list[:5]))
                    if len(fail_list) > 5:
                        summary_parts[-1] += f" ...等{len(fail_list)}首"
                self.finished_sig.emit(len(fail_list) == 0, "\n".join(summary_parts))

        self._studio_set_busy(True)
        self._studio_worker = FullPipelineThread(tasks)
        self._studio_worker.progress_sig.connect(
            lambda msg: (self._studio_status.setText(msg), self._log(msg))
        )
        self._studio_worker.finished_sig.connect(self._studio_on_full_done)
        self._studio_worker.start()

    def _studio_on_full_done(self, ok, msg):
        try:
            if ok:
                self._studio_status.setText(f"✅ {msg}")
                self._log(f"✅ 全流程完成: {msg}")
            else:
                self._studio_status.setText(f"❌ {msg}")
                self._log(f"❌ 全流程失败: {msg}")
            self._studio_refresh()
        except Exception as e:
            self._log(f"⚠ 全流程回调异常: {e}")
        finally:
            self._studio_set_busy(False)

    # ═══════════════════════════════════════════════════════
    def _build_appearance_section(self):
        """外观（2026-09-06 定稿极简三项）：主题拨杆 → 圆形配色 → 自定义。"""
        from agent.color_panel import ColorPanel

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(20, 12, 20, 12)
        layout.setSpacing(10)
        layout.addWidget(QLabel("外观", objectName="heading"))

        a = self.cfg.get("appearance", {}) or {}
        _theme = str(a.get("theme_mode", "dark"))
        _sid = str(a.get("color_scheme", "graphite"))
        if "accent_color" in a and "color_scheme" not in a and "theme_mode" not in a:
            _sid = CUSTOM_ID
        field_list = []  # 三项均自管理写 cfg；无传统表单字段

        # ═══════ 1. 主题（拨杆开关——主人选定形态 A，2026-09-06）═══════
        theme_row = QHBoxLayout()
        theme_row.setSpacing(10)
        theme_lbl = QLabel("主题", objectName="body")
        theme_lbl.setFixedWidth(64)
        theme_row.addWidget(theme_lbl)
        _pal = getattr(self, "_pal", None) or {}
        theme_toggle = ThemeToggle(
            dark=_theme == "dark",
            accent=str(_pal.get("accent", "#e2a1bc")),
            track_off=str(_pal.get("hover_bg", "#3f3f46")),
            track_border=str(_pal.get("card_border", "#52525b")),
        )
        theme_row.addWidget(theme_toggle)
        theme_tag = QLabel("深色" if _theme == "dark" else "浅色")
        theme_tag.setObjectName("muted")
        theme_tag.setFixedWidth(30)
        theme_row.addWidget(theme_tag)
        theme_row.addStretch()
        layout.addLayout(theme_row)

        def _on_theme(dark: bool):
            val = "dark" if dark else "light"
            self.cfg.setdefault("appearance", {})["theme_mode"] = val
            theme_tag.setText("深色" if dark else "浅色")
            self._apply_theme()
        theme_toggle.theme_changed.connect(_on_theme)

        # ═══════ 2. 配色方案（纯圆色卡 5×2 带名）═══════
        sc_lbl = QLabel("配色方案", objectName="body")
        sc_lbl.setFixedWidth(64)
        layout.addWidget(sc_lbl)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        self._scheme_group = QButtonGroup(self)
        self._scheme_group.setExclusive(True)
        self._scheme_btns: dict[str, RoundDotButton] = {}
        for _i, _sid_ in enumerate(SCHEME_ORDER):
            _seed = SCHEMES[_sid_]["seed"]
            cell = QVBoxLayout()
            cell.setSpacing(3)
            _lum = (0.299 * int(_seed[1:3], 16) + 0.587 * int(_seed[3:5], 16)
                    + 0.114 * int(_seed[5:7], 16))
            _on = "#ffffff" if _lum < 150 else "#1b1b1f"
            d = RoundDotButton(color=_seed, ring_hover="#a1a1aa", ring_checked=_on)
            d.setFixedSize(32, 32)
            d.setCheckable(True)
            d.clicked.connect(lambda _, s=_sid_: self._choose_scheme(s))
            self._scheme_group.addButton(d)
            self._scheme_btns[_sid_] = d
            nm = QLabel(SCHEMES[_sid_]["name"])
            nm.setObjectName("muted")
            nm.setAlignment(Qt.AlignHCenter)
            cell.addWidget(d, 0, Qt.AlignHCenter)
            cell.addWidget(nm, 0, Qt.AlignHCenter)
            grid.addLayout(cell, _i // 5, _i % 5)
            if _sid_ == _sid:
                d.setChecked(True)
        for _ci in range(5):
            grid.setColumnStretch(_ci, 1)
        layout.addLayout(grid)

        # ═══════ 3. 自定义（彩虹圆卡 → 现代取色面板）═══════
        cus_row = QHBoxLayout()
        cus_row.setSpacing(10)
        cus_lbl = QLabel("自定义颜色", objectName="body")
        cus_lbl.setFixedWidth(64)
        cus_row.addWidget(cus_lbl)
        self._cus_btn = RoundDotButton(
            gradient_stops=[(0.0, "#ff6b6b"), (0.25, "#ffd93d"), (0.5, "#6bcb77"),
                            (0.75, "#4d96ff"), (1.0, "#b983ff")])
        self._cus_btn.setFixedSize(32, 32)
        self._cus_btn.setCheckable(True)
        self._cus_btn.clicked.connect(self._pick_custom_panel)

        cus_row.addWidget(self._cus_btn)
        cus_row.addStretch()
        layout.addLayout(cus_row)
        if _sid == CUSTOM_ID:
            self._cus_btn.setChecked(True)

        layout.addStretch()

        self._settings_sections["appearance"] = container
        self._settings_fields["appearance"] = field_list

    def _pick_custom_panel(self):
        """自定义 → 现代取色面板（agent/color_panel.py）"""
        from agent.color_panel import ColorPanel
        app_cfg = self.cfg.setdefault("appearance", {})
        cur = str(app_cfg.get("custom_color", DEFAULT_CUSTOM))
        sid = str(app_cfg.get("color_scheme", ""))
        if sid in SCHEMES:
            cur = SCHEMES[sid]["seed"]
        panel = ColorPanel(initial=cur, parent=self)
        panel.setAttribute(Qt.WA_DeleteOnClose, True)
        apply_timer = QTimer(self)
        apply_timer.setSingleShot(False)
        # 约 30fps 合并刷新：颜色连续变化，同时避免每个鼠标事件都重建 QSS
        apply_timer.setInterval(33)
        apply_timer.timeout.connect(lambda: self._apply_theme(refresh_icons=False))
        panel.destroyed.connect(lambda: (apply_timer.stop(), self._apply_theme(refresh_icons=True)))

        def _chosen(hx: str):
            app_cfg["custom_color"] = hx
            # 让外观页的色卡即时反映拖动中的自定义颜色
            self._cus_btn._stops = []
            self._cus_btn._color = hx
            self._cus_btn.update()
            app_cfg["color_scheme"] = CUSTOM_ID
            self._cus_btn.setChecked(True)
            # 合并连续拖动事件，避免每帧重建全局 QSS 和图标
            apply_timer.start()
        panel.colorChanged.connect(_chosen)
        panel.popup_under(self._cus_btn)

    def _choose_scheme(self, scheme_id: str):
        """色卡/自定义选中：写 cfg（即时预览），同步选中态。"""
        app_cfg = self.cfg.setdefault("appearance", {})
        app_cfg["color_scheme"] = scheme_id
        app_cfg.pop("accent_color", None)
        for sid, btn in self._scheme_btns.items():
            btn.setChecked(sid == scheme_id)
        self._cus_btn.setChecked(scheme_id == CUSTOM_ID)
        self._apply_theme()

    def _build_secrets_section(self):
        """🔑 密钥管理：读取/编辑 .env 文件中的 API Key 和 Token"""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.setSpacing(12)
        layout.addWidget(QLabel("密钥管理", objectName="heading"))
        hint = QLabel("这些密钥存储在 .env 文件中（不会提交到 Git）。修改后点保存即可。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._secrets_fields: dict[str, QLineEdit] = {}
        self._secrets_labels: dict[str, QLabel] = {}

        # LLM provider → key name + help text
        _provider_key_info = {
            "deepseek":  ("DEEPSEEK_KEY", "从 platform.deepseek.com → API Keys 获取"),
            "xai":       ("XAI_KEY", "从 console.x.ai → API Keys → Create API Key 获取"),
            "anthropic": ("ANTHROPIC_KEY", "从 console.anthropic.com → API Keys 获取"),
            "openai":    ("OPENAI_KEY", "从 platform.openai.com → API Keys 获取"),
            "custom":    ("CUSTOM_LLM_KEY", "填入你的 API Key"),
        }
        provider = self.cfg.get("llm", {}).get("provider", "deepseek")
        key_name, key_hint = _provider_key_info.get(provider, _provider_key_info["deepseek"])
        self._current_llm_key_name = key_name  # 供 _save_secrets_to_env 使用

        secrets = [
            ("llm_key", f"LLM API Key（当前: {provider}）", key_name, key_hint),
            # 标签按**用户想做的事**命名，不按厂商命名（2026-09-19 主人实机反馈）。
            # 原来叫「千问 VL API Key」会误导：识图有两种方式，选「本地 MiniCPM-V」
            # 根本不需要这个 Key——可那个名字看起来像「用识图就得先弄个千问的号」。
            # 厂商信息移进说明里，它仍然有用（去哪儿申请），但不该占据标题。
            # ⚠ 环境变量名 `QWEN_KEY` 不能改——用户的 .env 里就是这个键，改了会失效。
            ("QWEN_KEY", "识图 API Key",
             "识图选「云端千问 VL」时才需要，从阿里云 DashScope 获取；"
             "选「本地 MiniCPM-V」不需要填"),
            ("SNOWLUMA_TOKEN", "SnowLuma Token", "SnowLuma QQ 的访问令牌，在 SnowLuma WebUI 查看"),
        ]
        for item in secrets:
            if len(item) == 4:
                key, label, env_key, tooltip = item
            else:
                key, label, tooltip = item
                env_key = key
            row = QHBoxLayout()
            lbl = QLabel(label)
            lbl.setFixedWidth(150)
            row.addWidget(lbl)
            field = QLineEdit()
            field.setEchoMode(QLineEdit.Password)
            field.setPlaceholderText(f"输入 {label}...")
            val = os.environ.get(env_key, "")
            if val:
                field.setText(val)
            field.setToolTip(tooltip)
            row.addWidget(field)
            show_btn = QPushButton("")
            show_btn.setIconSize(QSize(15, 15))
            show_btn._lucide_name = "eye"
            show_btn._lucide_role = "text_muted"
            show_btn.setFixedWidth(36)
            show_btn.setCheckable(True)
            show_btn.toggled.connect(lambda checked, f=field: f.setEchoMode(
                QLineEdit.Normal if checked else QLineEdit.Password))
            row.addWidget(show_btn)
            layout.addLayout(row)
            self._secrets_fields[key] = field
            self._secrets_labels[key] = lbl

        layout.addStretch()
        self._settings_sections["secrets"] = container
        self._settings_fields["secrets"] = []

    def _save_secrets_to_env(self):
        """将密钥管理表单的内容写入 .env 文件"""
        env_path = Path(__file__).parent / ".env"
        # UI key → .env key 映射（根据当前选择的 provider 动态确定）
        _key_to_env = {"llm_key": getattr(self, '_current_llm_key_name', 'DEEPSEEK_KEY')}
        lines = []
        if env_path.exists():
            lines = env_path.read_text(encoding="utf-8").split("\n")
        # 只更新已有 key，不删其他行
        updated = set()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if "=" in stripped and not stripped.startswith("#"):
                env_key = stripped.split("=", 1)[0].strip()
                # 检查是否有 UI key 映射到这个 env key
                matched_ui_key = None
                for ui_key, mapped_env in _key_to_env.items():
                    if mapped_env == env_key and ui_key in self._secrets_fields:
                        matched_ui_key = ui_key
                        break
                if env_key in self._secrets_fields or matched_ui_key:
                    ui_key = matched_ui_key or env_key
                    new_val = self._secrets_fields[ui_key].text().strip()
                    new_lines.append(f"{env_key}={new_val}")
                    # 也更新 os.environ 让当前进程立即生效
                    os.environ[env_key] = new_val
                    updated.add(ui_key)
                    continue
            new_lines.append(line)
        # 追加新增的 key
        for ui_key, field in self._secrets_fields.items():
            if ui_key not in updated:
                env_key = _key_to_env.get(ui_key, ui_key)
                new_lines.append(f"{env_key}={field.text().strip()}")
                os.environ[env_key] = field.text().strip()
        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        # 重载环境变量让 config 解析器生效
        load_dotenv(override=True)

    # LLM 提供商预设
    _PROVIDER_PRESETS = {
        "deepseek":  {"model": "deepseek-v4-pro",       "base_url": "https://api.deepseek.com"},
        "xai":       {"model": "grok-3-mini",        "base_url": "https://api.x.ai/v1"},
        "anthropic": {"model": "claude-sonnet-4-6",  "base_url": "https://api.anthropic.com"},
        "openai":    {"model": "gpt-4o",             "base_url": "https://api.openai.com/v1"},
        "custom":    {"model": "",                   "base_url": ""},
    }

    def _on_provider_changed(self, provider: str):
        """提供商下拉改变时，自动填入对应 model 和 base_url"""
        preset = self._PROVIDER_PRESETS.get(provider, {})
        if "llm" not in self._settings_fields:
            return
        for path, widget, ftype in self._settings_fields["llm"]:
            if path == "llm.model" and preset.get("model"):
                if hasattr(widget, 'setText'):
                    widget.setText(preset["model"])
            elif path == "llm.base_url" and preset.get("base_url"):
                if hasattr(widget, 'setText'):
                    widget.setText(preset["base_url"])
        # 同步更新密钥管理的标签
        if hasattr(self, '_secrets_labels') and "llm_key" in self._secrets_labels:
            self._secrets_labels["llm_key"].setText(f"LLM API Key（当前: {provider}）")

    def _preview_appearance(self):
        """读取外观表单当前值 → 临时更新 self.cfg → 应用主题（不保存文件）"""
        if "appearance" not in self._settings_fields:
            return
        for path, widget, ftype in self._settings_fields["appearance"]:
            try:
                if ftype == "str":
                    val = widget.text().strip()
                elif ftype == "int":
                    val = widget.value() if hasattr(widget, 'value') else int(widget.text().strip())
                elif ftype == "float":
                    val = widget.value()
                else:
                    continue
                _set_nested(self.cfg, path, val)
            except Exception:
                pass
        self._apply_theme()

    def _apply_theme(self, refresh_icons: bool = True):
        """（重新）应用 QSS 主题 + 背景图"""
        if not hasattr(self, 'cfg'):
            return
        self._pal = _resolve_appearance(self.cfg)  # 2026-09-06：当前色板缓存（python 内联样式/状态色用）
        qss = _build_stylesheet(self.cfg)
        QApplication.instance().setStyleSheet(qss)
        # Aether 胶囊代理（2026-09-06；2026-09-17 修复接线）：setStyleSheet 会把
        # QStyleSheetStyle 置顶（实测 metaObject=QStyleSheetStyle），代理只能挂在它的
        # base 上——旧 isinstance 守卫恒假 → set_enabled 从未被调用 → 代理从未开启。
        # 现在持有实例直接喂色（每次重建，旧的由 Qt 回收）。
        _proxy = PillProxyStyle()
        QApplication.instance().setStyle(_proxy)
        self._pill_style = _proxy
        self._feed_pill_style(self._pal.get("theme_mode") != "dark")
        if hasattr(self, "_aurora"):
            self._aurora.set_theme(self._pal.get("theme_mode") != "dark",
                                   str(self._pal.get("main_bg", "#17171a")))
        if refresh_icons:
            self._refresh_icons()  # lucide 图标随主题重染（2026-09-06）
        if hasattr(self, "_stat_divider"):
            self._stat_divider.set_color(str(self._pal.get("card_border", "#333338")))
        if hasattr(self, "_log_pill_buttons"):
            on, _, _ = _log_pill_on(self._pal)
            for btn in self._log_pill_buttons:
                btn.set_pill_colors(
                    str(self._pal.get("input_bg", "#1f1f23")),
                    str(self._pal.get("card_border", "#333338")),
                    str(self._pal.get("accent", "#f28db7")),
                    on)
        if hasattr(self, "_overlay_bars"):
            for bar in self._overlay_bars:
                bar.set_colors(
                    str(self._pal.get("card_border", "#333338")),
                    str(self._pal.get("accent", "#f28db7")),
                    str(self._pal.get("hover_bg", "#27272b")))
        if hasattr(self, "_tooltip_filter"):
            _dk = self._pal.get("theme_mode") == "dark"
            self._tooltip_filter.set_colors(
                "#272733" if _dk else "#ffffff",
                "#f7f7fb" if _dk else "#202124",
                str(self._pal.get("accent", "#f28db7")) if _dk else str(self._pal.get("card_border", "#333338")))
        # FluentCheckBox 指示器喂色（findChildren 覆盖设置页 + 仪表盘所有勾选框）
        # Aether：浅色 checked=炭黑墨色（#1d1d1f）+ 白勾；深色保持 accent
        _cb_accent = ("#1d1d1f" if self._pal.get("theme_mode") != "dark"
                      else str(self._pal.get("accent", "#f28db7")))
        for cb in self.findChildren(QCheckBox):
            if hasattr(cb, "set_check_colors"):
                cb.set_check_colors(
                    str(self._pal.get("card_border", "#333338")),
                    str(self._pal.get("input_bg", "#1f1f23")),
                    _cb_accent)

    def _attach_overlay_bar(self, area, orient=Qt.Vertical) -> OverlayScrollBar:
        """为滚动区挂自绘覆盖滚动条（隐藏原生条）——2026-09-06 自绘滚动条工程。"""
        bar = OverlayScrollBar(orient, area)
        self._overlay_bars.append(bar)  # 防 GC
        return bar

    def _feed_pill_style(self, light: bool):
        """Aether 胶囊按钮喂色（2026-09-06）：浅色主题下按 objectName 给 QPushButton
        写 pill_* 属性，PillProxyStyle 据此自绘胶囊填充（QSS 圆角病无法实现实心胶囊）。
        深色代理关闭，零改动。"""
        style = getattr(self, "_pill_style", None)
        if style is None:
            _st = QApplication.instance().style()
            style = _st if isinstance(_st, PillProxyStyle) else None
        if style is not None:
            style.set_enabled(light)   # 必须是持有的实例——app.style() 恒为 QStyleSheetStyle
        if not light:
            return
        pink = _qcolor(str(self._pal.get("accent", "#f28db7")))
        green = _qcolor(str(self._pal.get("green", "#34c759")))
        red = _qcolor(str(self._pal.get("red", "#ff453a")))
        dgr_fill = _qcolor(str(self._pal.get("danger_bg", "#ffe9e7")))  # 浅底芯片底（danger_bg 本义）
        ink = QColor("#1d1d1f")
        glass = _qcolor("rgba(255, 255, 255, 0.5)")          # 坞芯片 hover 玻璃洗色
        wash = QColor(str(self._pal.get("hover_bg", "#e9edf3")))  # catBtn hover 洗色
        for btn in self.findChildren(QPushButton):
            if isinstance(btn, PillButton):
                continue
            n = btn.objectName()
            if n == "pinkBtn":
                btn.setProperty("pill_fill", pink)
                btn.setProperty("pill_border", pink)
            elif n == "greenBtn":
                btn.setProperty("pill_fill", green)
                btn.setProperty("pill_border", green)
            elif n == "dangerBtn":
                btn.setProperty("pill_fill", dgr_fill)
                btn.setProperty("pill_border", red)
            elif n == "navBtn":
                btn.setProperty("pill_fill", QColor("transparent"))
                btn.setProperty("pill_border", QColor("transparent"))
                btn.setProperty("pill_on", ink)
                btn.setProperty("pill_on_border", ink)
                btn.setProperty("pill_hover_fill", glass)
            elif n == "catBtn":
                btn.setProperty("pill_fill", QColor("transparent"))
                btn.setProperty("pill_border", QColor("transparent"))
                btn.setProperty("pill_on", ink)
                btn.setProperty("pill_on_border", ink)
                btn.setProperty("pill_hover_fill", wash)

    def _on_cat_clicked(self, key: str):
        """处理分类按钮点击：管理独占选中 + 切换内容区"""
        if key == getattr(self, '_current_settings_key', None):
            return

        for btn in self._settings_cats.values():
            btn.blockSignals(True)
            btn.setChecked(btn is self._settings_cats.get(key))
            btn.blockSignals(False)

        self._current_settings_key = key
        container = self._settings_sections.get(key)
        if container is None:
            return

        self._settings_layout.setCurrentWidget(container)

    def _save_config(self):
        """遍历所有 settings section fields，收集值，写回 config"""
        # 先保存私聊场景目标（不走 fields 系统）
        self._save_private_targets()
        for section_key, fields in self._settings_fields.items():
            for path, widget, ftype in fields:
                try:
                    if ftype == "str":
                        val = widget.text().strip()
                    elif ftype == "int":
                        val = widget.value()
                    elif ftype == "float":
                        val = widget.value()
                    elif ftype == "bool":
                        val = widget.isChecked()
                    elif ftype == "text":
                        val = widget.toPlainText().strip()
                    elif ftype == "list":
                        lines = widget.toPlainText().strip()
                        val = [l.strip() for l in lines.split("\n") if l.strip()] if lines else []
                    elif ftype == "choice":
                        val = widget.currentText().strip()
                    else:
                        continue
                    _set_nested(self.cfg, path, val)
                except Exception as e:
                    self._log(f"⚠ 保存字段 {path} 失败: {e}")

        # 保存 .env 密钥
        if hasattr(self, '_secrets_fields') and self._secrets_fields:
            self._save_secrets_to_env()
        save_config_file(self.cfg)
        self.cfg = load_config()
        self._apply_theme()  # 重新应用主题
        self._refresh_llm_display()  # 仪表盘/侧栏 LLM 跟随提供商（2026-09-05）
        self._log("✅ 配置已保存 → config.yaml（备份: config.yaml.bak）")
        QMessageBox.information(self, "保存完成", "配置已保存到 config.yaml")

    # ═══════════════════════════════════════════════════════
    # 3. Gallery 页
    # ═══════════════════════════════════════════════════════
    def _build_gallery(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 工具栏
        toolbar = QFrame()
        toolbar.setObjectName("card")
        tl = QHBoxLayout(toolbar)
        tl.setContentsMargins(16, 10, 16, 10)
        tl.setSpacing(8)

        tl.addWidget(QLabel("本地图库", objectName="heading"))

        self._gallery_cat_combo = QComboBox()
        self._gallery_cat_combo.setMinimumWidth(150)
        self._gallery_cat_combo.setToolTip("筛选分类文件夹，选「全部」显示所有图片")
        self._gallery_cat_combo.currentIndexChanged.connect(self._gallery_refresh)
        tl.addWidget(self._gallery_cat_combo)

        upload_btn = QPushButton("上传")
        upload_btn.setObjectName("actionBtn")
        upload_btn.setToolTip("从电脑选择图片添加到图库")
        upload_btn.clicked.connect(self._gallery_upload)
        tl.addWidget(upload_btn)

        new_folder_btn = QPushButton("新建分类")
        new_folder_btn.setObjectName("actionBtn")
        new_folder_btn.setToolTip("创建新的分类文件夹（如：美图、参考图）")
        new_folder_btn.clicked.connect(self._gallery_new_folder)
        tl.addWidget(new_folder_btn)

        tl.addStretch()

        save_all_btn = QPushButton("保存全部配文")
        save_all_btn.setObjectName("pinkBtn")
        save_all_btn.setToolTip("将所有配文写入 .txt 文件，糖糖发图时会读取")
        save_all_btn.clicked.connect(self._gallery_save_all)
        tl.addWidget(save_all_btn)

        layout.addWidget(toolbar)

        # 提示：拖拽排序
        hint = QLabel("💡 拖拽图片可调整播放顺序 · 点击 ✎ 改名 · 📂 移动分类 · ✕ 删除")
        hint.setObjectName("muted")
        layout.addWidget(hint)

        # 图库列表
        self._gallery_list = QListWidget()
        self._gallery_list.setDragDropMode(QAbstractItemView.InternalMove)
        self._gallery_list.setDefaultDropAction(Qt.MoveAction)
        self._gallery_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self._gallery_list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._gallery_list.model().rowsMoved.connect(self._on_gallery_reordered)
        self._attach_overlay_bar(self._gallery_list)
        layout.addWidget(self._gallery_list, 1)

        # 初始化
        self._gallery_refresh()
        return page

    def _gallery_refresh(self):
        """重建图库列表"""
        self._gallery_list.clear()
        exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

        # 保存当前选中分类
        prev_cat = self._gallery_cat_combo.currentData()
        # 如果还没初始化（首次调用），prev_cat 可能是 None
        if prev_cat is None:
            prev_cat = ""

        # 更新分类下拉
        self._gallery_cat_combo.blockSignals(True)
        self._gallery_cat_combo.clear()
        self._gallery_cat_combo.addItem("📂 全部", "")
        img_dir = SHARE_DIR
        img_dir.mkdir(parents=True, exist_ok=True)
        folders = sorted([d.name for d in img_dir.iterdir() if d.is_dir()])
        for fname in folders:
            self._gallery_cat_combo.addItem(f"📁 {fname}", fname)

        # 恢复之前选中的分类
        for i in range(self._gallery_cat_combo.count()):
            if self._gallery_cat_combo.itemData(i) == prev_cat:
                self._gallery_cat_combo.setCurrentIndex(i)
                break
        self._gallery_cat_combo.blockSignals(False)

        # 获取当前选中的分类
        cat = self._gallery_cat_combo.currentData()

        # 获取图片
        if cat:
            search_dir = img_dir / cat
            search_dir.mkdir(parents=True, exist_ok=True)
            images = self._get_ordered(search_dir, exts)
        else:
            # 全部图片（按修改时间排序）
            image_files = [f for f in img_dir.rglob("*") if f.suffix.lower() in exts and f.is_file()]
            images = sorted(image_files, key=lambda f: f.stat().st_mtime, reverse=True)

        if not images:
            placeholder = QListWidgetItem("还没有图片 — 点「上传」添加")
            placeholder.setFlags(Qt.NoItemFlags)
            self._gallery_list.addItem(placeholder)
            return

        for i, img_path in enumerate(images):
            item = QListWidgetItem()
            item.setFlags(item.flags() | Qt.ItemIsDragEnabled)
            item.setData(Qt.UserRole, str(img_path))
            self._gallery_list.addItem(item)

            row = QWidget()
            row.setProperty("img_path", str(img_path))
            rl = QHBoxLayout(row)
            rl.setContentsMargins(6, 3, 6, 3)
            rl.setSpacing(4)

            # 缩略图
            thumb = QLabel()
            thumb.setFixedSize(48, 36)
            thumb.setScaledContents(True)
            thumb.setStyleSheet(f"background-color: {self._pal['input_bg']}; border-radius: 3px;")
            pixmap = QPixmap(str(img_path))
            if not pixmap.isNull():
                thumb.setPixmap(pixmap.scaled(48, 36, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            rl.addWidget(thumb)

            # 文件名
            name_lbl = QLabel(img_path.name[:30])
            name_lbl.setFixedWidth(170)
            name_lbl.setObjectName("body")
            rl.addWidget(name_lbl)

            # 大小
            size_kb = img_path.stat().st_size / 1024
            size_lbl = QLabel(f"{size_kb:.0f}KB")
            size_lbl.setFixedWidth(50)
            size_lbl.setObjectName("muted")
            rl.addWidget(size_lbl)

            # 分类
            cat_name = img_path.parent.name if img_path.parent != img_dir else "-"
            cat_lbl = QLabel(cat_name[:8])
            cat_lbl.setFixedWidth(55)
            cat_lbl.setObjectName("muted")
            rl.addWidget(cat_lbl)

            # 配文
            txt_path = img_path.with_suffix(".txt")
            current = txt_path.read_text(encoding="utf-8").strip() if txt_path.exists() else ""
            cap_entry = QLineEdit()
            cap_entry.setText(current)
            cap_entry.setPlaceholderText("配文...")
            cap_entry.setProperty("txt_path", str(txt_path))
            cap_entry.editingFinished.connect(lambda e=cap_entry: self._gallery_save_caption(e))
            rl.addWidget(cap_entry, 1)

            # 操作按钮
            rename_btn = QPushButton("✎")
            rename_btn.setFixedSize(28, 26)
            rename_btn.setObjectName("subtleBtn")
            rename_btn.setToolTip("改名")
            rename_btn.clicked.connect(lambda checked, p=img_path: self._gallery_rename(p))
            rl.addWidget(rename_btn)

            move_btn = QPushButton("")
            move_btn.setIconSize(QSize(15, 15))
            move_btn._lucide_name = "folder-open"
            move_btn._lucide_role = "text_secondary"
            move_btn.setFixedSize(28, 26)
            move_btn.setObjectName("subtleBtn")
            move_btn.setToolTip("移动")
            move_btn.clicked.connect(lambda checked, p=img_path: self._gallery_move(p))
            rl.addWidget(move_btn)

            del_btn = QPushButton("")
            del_btn.setIconSize(QSize(14, 14))
            del_btn._lucide_name = "x"
            del_btn._lucide_role = "text_muted"
            del_btn.setFixedSize(28, 26)
            del_btn.setObjectName("dangerBtn")
            del_btn.setToolTip("删除")
            del_btn.clicked.connect(lambda checked, p=img_path: self._gallery_delete(p))
            rl.addWidget(del_btn)

            self._gallery_list.setItemWidget(item, row)
            item.setSizeHint(QSize(0, 44))

    def _get_ordered(self, directory: Path, exts: set) -> list[Path]:
        """按 _order.json 排序，新文件自动追加"""
        order_file = directory / "_order.json"
        ordered = []
        if order_file.exists():
            try:
                ordered = json.loads(order_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        current = {f.name for f in directory.iterdir() if f.suffix.lower() in exts and f.is_file()}
        valid = [n for n in ordered if n in current]
        new_files = sorted(current - set(valid))
        final = valid + new_files
        if final != ordered:
            try:
                order_file.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                pass
        return [directory / n for n in final if (directory / n).exists()]

    def _on_gallery_reordered(self, parent, start, end, destination, row):
        """拖拽排序后同步 _order.json"""
        cat = self._gallery_cat_combo.currentData()
        img_dir = SHARE_DIR
        if cat:
            directory = img_dir / cat
        else:
            # 全部模式 — 检测第一张图片的父目录
            first_item = self._gallery_list.item(0)
            if first_item and first_item.data(Qt.UserRole):
                first_path = Path(first_item.data(Qt.UserRole))
                directory = first_path.parent
            else:
                return

        new_order = []
        for i in range(self._gallery_list.count()):
            item = self._gallery_list.item(i)
            img_path_str = item.data(Qt.UserRole)
            if img_path_str:
                new_order.append(Path(img_path_str).name)

        if new_order:
            order_file = directory / "_order.json"
            try:
                order_file.write_text(json.dumps(new_order, ensure_ascii=False, indent=2), encoding="utf-8")
                self._log(f"📋 排序已更新: {directory.name}/_order.json")
            except OSError:
                pass

    def _gallery_save_caption(self, entry: QLineEdit):
        """保存单张配文"""
        txt_path_str = entry.property("txt_path")
        if not txt_path_str:
            return
        txt_path = Path(txt_path_str)
        caption = entry.text().strip()
        try:
            if caption:
                txt_path.write_text(caption, encoding="utf-8")
            elif txt_path.exists():
                txt_path.unlink()
        except OSError:
            pass

    def _gallery_save_all(self):
        """保存所有配文"""
        saved = 0
        for i in range(self._gallery_list.count()):
            item = self._gallery_list.item(i)
            widget = self._gallery_list.itemWidget(item)
            if widget:
                entries = widget.findChildren(QLineEdit)
                for entry in entries:
                    txt_path_str = entry.property("txt_path")
                    if txt_path_str:
                        txt_path = Path(txt_path_str)
                        caption = entry.text().strip()
                        try:
                            if caption:
                                txt_path.write_text(caption, encoding="utf-8")
                            elif txt_path.exists():
                                txt_path.unlink()
                            saved += 1
                        except OSError:
                            pass
        self._log(f"💾 已保存 {saved} 个配文")
        QMessageBox.information(self, "保存完成", f"已保存 {saved} 个配文")

    def _gallery_upload(self):
        """上传图片"""
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择图片", "",
            "图片 (*.jpg *.jpeg *.png *.gif *.webp *.bmp)"
        )
        if not files:
            return
        cat = self._gallery_cat_combo.currentData()
        dest = SHARE_DIR
        if cat:
            dest = dest / cat
        dest.mkdir(parents=True, exist_ok=True)

        added = 0
        for src in files:
            src_path = Path(src)
            target = dest / src_path.name
            if target.exists():
                target = dest / f"{src_path.stem}_{src_path.stat().st_mtime:.0f}{src_path.suffix}"
            shutil.copy2(str(src_path), str(target))
            added += 1
        self._log(f"📤 已添加 {added} 张图片")
        self._gallery_refresh()

    def _gallery_new_folder(self):
        """新建分类"""
        name, ok = QInputDialog.getText(self, "新建分类", "分类名：")
        if ok and name.strip():
            folder = SHARE_DIR / name.strip()
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "_order.json").write_text("[]", encoding="utf-8")
            self._gallery_refresh()
            # 选中新建的分类
            for i in range(self._gallery_cat_combo.count()):
                if self._gallery_cat_combo.itemData(i) == name.strip():
                    self._gallery_cat_combo.setCurrentIndex(i)
                    break

    def _gallery_rename(self, img_path: Path):
        """重命名图片"""
        new_name, ok = QInputDialog.getText(
            self, "重命名", "新文件名（不含扩展名）：",
            text=img_path.stem
        )
        if ok and new_name.strip() and new_name.strip() != img_path.stem:
            new_path = img_path.with_name(new_name.strip() + img_path.suffix)
            if new_path.exists():
                QMessageBox.warning(self, "错误", "该文件名已存在")
                return
            img_path.rename(new_path)
            old_txt = img_path.with_suffix(".txt")
            if old_txt.exists():
                old_txt.rename(new_path.with_suffix(".txt"))
            self._gallery_refresh()

    def _gallery_move(self, img_path: Path):
        """移动图片到其他分类"""
        img_dir = SHARE_DIR
        folders = sorted([d.name for d in img_dir.iterdir() if d.is_dir() and d != img_path.parent])
        if img_path.parent != img_dir:
            folders.insert(0, "（根目录）")

        if not folders:
            QMessageBox.information(self, "提示", "还没有其他分类文件夹")
            return

        # 使用 QInputDialog 选择目标
        target_name, ok = QInputDialog.getItem(
            self, "移动到...", f"移动「{img_path.name[:20]}」到：",
            folders, 0, False
        )
        if not ok:
            return

        if target_name == "（根目录）":
            target_dir = img_dir
        else:
            target_dir = img_dir / target_name

        new_path = target_dir / img_path.name
        if new_path.exists():
            new_path = target_dir / f"{img_path.stem}_{int(time.time())}{img_path.suffix}"
        shutil.move(str(img_path), str(new_path))
        old_txt = img_path.with_suffix(".txt")
        if old_txt.exists():
            shutil.move(str(old_txt), str(new_path.with_suffix(".txt")))
        self._gallery_refresh()

    def _gallery_delete(self, img_path: Path):
        """删除图片"""
        reply = QMessageBox.question(
            self, "确认删除",
            f"确定要删除 {img_path.name} 吗？"
        )
        if reply == QMessageBox.Yes:
            img_path.unlink(missing_ok=True)
            txt = img_path.with_suffix(".txt")
            if txt.exists():
                txt.unlink()
            self._gallery_refresh()

    # ═══════════════════════════════════════════════════════
    # 4. Log 页
    # ═══════════════════════════════════════════════════════
    def _build_log(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        toolbar = QFrame()
        toolbar.setObjectName("card")
        tl = QHBoxLayout(toolbar)
        tl.setContentsMargins(16, 10, 16, 10)
        tl.addWidget(QLabel("日志", objectName="heading"))
        tl.addStretch()

        clear_btn = QPushButton("清空")
        clear_btn.setObjectName("actionBtn")
        clear_btn.clicked.connect(self._clear_log)
        tl.addWidget(clear_btn)
        analyze_btn = QPushButton(" 分析")
        analyze_btn.setIconSize(QSize(15, 15))
        analyze_btn._lucide_name = "search"
        analyze_btn.setObjectName("pinkBtn")
        analyze_btn.setToolTip("分析对话质量，检测公式化开头、回复率、跑题等问题")
        analyze_btn.clicked.connect(self._run_quality_analysis)
        tl.addWidget(analyze_btn)
        diag_btn = QPushButton("诊断")
        diag_btn.setObjectName("pinkBtn")
        diag_btn.clicked.connect(self._run_diagnostic)
        tl.addWidget(diag_btn)
        layout.addWidget(toolbar)

        # ── 内容过滤器：分类按钮 ──
        filter_bar = QFrame()
        filter_bar.setObjectName("card")
        filter_bar.setStyleSheet("QFrame#card { border-top: none; padding-top: 0; }")
        fl = QHBoxLayout(filter_bar)
        fl.setContentsMargins(16, 4, 16, 8)
        fl.setSpacing(6)

        fl.addWidget(QLabel("内容:", styleSheet="color: #888; font-size: 11px;"))

        self._log_content_categories = {
            "消息":   (["💬", "📩", "📤", "📨", "发送", "回复"],
                       "群聊和私聊的收发消息，点此只看对话"),
            "LLM":    (["LLM", "DeepSeek", "Claude", "调用LLM"],
                       "AI 调用记录：请求/响应/耗时"),
            "搜索":   (["搜索", "WebSearch", "Bing", "搜"],
                       "联网搜索相关：Bing/DuckDuckGo 查询与结果"),
            "技能":   (["技能", "Skil", "执行技能"],
                       "技能调用：计算、翻译、天气、时间等"),
            "记忆":   (["记忆", "Memor", "recall", "remember"],
                       "记忆系统：提取、召回、存储"),
            "语音":   (["语音", "voice", "tts", "TTS", "Cosy"],
                       "语音合成与播放记录"),
            "协议":   (["RAW", "heartbeat", "meta_event", "notice"],
                       "SnowLuma 底层协议数据：原始JSON、心跳、戳一戳等（调试用，通常可排除）"),
            "HTTP":   (["httpx", "HTTP"],
                       "HTTP 请求详情（API调用底层日志）"),
            "系统":   (["启动", "停止", "连接", "SnowLuma", "诊断", "配置"],
                       "系统事件：启动/停止/连接/诊断"),
        }
        self._log_content_btns: dict[str, QPushButton] = {}
        self._log_pill_buttons: list[PillButton] = []  # _apply_theme 喂色（自绘胶囊）
        for cat, (keywords, tooltip) in self._log_content_categories.items():
            btn = PillButton(cat)  # 自绘胶囊（QSS 圆角病不剪裁背景，2026-09-06 实测）
            btn.setCheckable(True)
            btn.setToolTip(tooltip)
            btn.setFixedHeight(22)
            btn.setObjectName("logPill")  # 文字色随主题（QSS）
            btn.clicked.connect(self._on_content_filter_changed)
            fl.addWidget(btn)
            self._log_content_btns[cat] = btn
            self._log_pill_buttons.append(btn)

        # 全部/重置按钮
        self._log_show_all_btn = PillButton("全部")
        self._log_show_all_btn.setCheckable(True)
        self._log_show_all_btn.setChecked(True)  # 默认显示全部
        self._log_show_all_btn.setFixedHeight(22)
        self._log_show_all_btn.setObjectName("logPill")
        self._log_show_all_btn.clicked.connect(self._on_show_all_clicked)
        fl.addWidget(self._log_show_all_btn)
        self._log_pill_buttons.append(self._log_show_all_btn)

        fl.addSpacing(12)
        fl.addWidget(QLabel("排除:", styleSheet="color: #888; font-size: 11px;"))
        self._log_blacklist_input = QLineEdit()
        self._log_blacklist_input.setPlaceholderText("隐藏含这些词的日志")
        self._log_blacklist_input.setFixedWidth(150)
        self._log_blacklist_input.setObjectName("logExclude")  # 样式随主题
        self._log_blacklist_input.textChanged.connect(self._on_log_filter_changed)
        fl.addWidget(self._log_blacklist_input)
        layout.addWidget(filter_bar)

        # ── 目标过滤器：下拉菜单选择群/人 ──
        target_bar = QFrame()
        target_bar.setObjectName("card")
        target_bar.setStyleSheet("QFrame#card { border-top: none; padding-top: 0; }")
        tfl = QHBoxLayout(target_bar)
        tfl.setContentsMargins(16, 0, 16, 8)
        tfl.setSpacing(6)

        tfl.addWidget(QLabel("目标:", styleSheet="color: #888; font-size: 11px;"))

        self._log_target_white_btn = QPushButton("▾ 显示全部")
        self._log_target_white_btn.setFixedHeight(24)
        self._log_target_white_btn.setObjectName("logTarget")  # 样式随主题
        self._log_target_white_btn.clicked.connect(self._show_target_white_menu)
        tfl.addWidget(self._log_target_white_btn)

        self._log_target_black_btn = QPushButton("▾ 不过滤")
        self._log_target_black_btn.setFixedHeight(24)
        self._log_target_black_btn.setObjectName("logTargetBlack")  # 样式随主题
        self._log_target_black_btn.clicked.connect(self._show_target_black_menu)
        tfl.addWidget(self._log_target_black_btn)

        tfl.addStretch()
        self._log_filter_count_label = QLabel("")
        self._log_filter_count_label.setStyleSheet("color: #555; font-size: 10px;")
        tfl.addWidget(self._log_filter_count_label)
        layout.addWidget(target_bar)

        # 过滤状态
        self._log_filter_active = False
        self._log_filter_count = 0
        self._log_target_white_ids: set[str] = set()
        self._log_target_black_ids: set[str] = set()

        # 诊断卡片（初始隐藏）
        self._diag_card = QFrame()
        self._diag_card.setObjectName("card")
        self._diag_card.setVisible(False)
        dcl = QVBoxLayout(self._diag_card)
        dcl.setContentsMargins(16, 12, 16, 12)
        dc_header = QHBoxLayout()
        dc_header.addWidget(QLabel("诊断报告", objectName="heading"))
        dc_header.addStretch()
        close_diag = QPushButton("关闭")
        close_diag.setObjectName("subtleBtn")
        close_diag.clicked.connect(lambda: self._diag_card.setVisible(False))
        dc_header.addWidget(close_diag)
        dcl.addLayout(dc_header)
        self._diag_summary = QLabel("")
        self._diag_summary.setObjectName("muted")
        dcl.addWidget(self._diag_summary)
        self._diag_text = QPlainTextEdit()
        self._diag_text.setReadOnly(True)
        self._diag_text.setMaximumHeight(250)
        self._diag_text.setObjectName("diagArea")
        self._attach_overlay_bar(self._diag_text)
        dcl.addWidget(self._diag_text)
        layout.addWidget(self._diag_card)

        # 日志区
        self._log_area = QPlainTextEdit()
        self._log_area.setReadOnly(True)
        self._log_area.setObjectName("logArea")
        self._log_area.setMaximumBlockCount(5000)
        self._attach_overlay_bar(self._log_area)
        layout.addWidget(self._log_area, 1)

        return page

    # ── 目标过滤器：下拉菜单 ──

    def _get_target_choices(self) -> list[tuple[str, str]]:
        """从数据库 + config.yaml 获取可选的目标列表：(标签, ID)"""
        choices = []
        # 1. 从数据库读糖糖实际加入的群（含群名）
        db_groups = {}  # {gid: group_name}
        try:
            import sqlite3
            db_path = BASE / "memory.db"
            if db_path.exists():
                conn = sqlite3.connect(str(db_path))
                rows = conn.execute(
                    "SELECT group_id, group_name FROM group_info ORDER BY group_name"
                ).fetchall()
                conn.close()
                db_groups = {str(r[0]): r[1] for r in rows if r[0]}
        except Exception:
            pass
        # 2. config.yaml 的群配置（群主/管理/场景）
        config_groups = self.cfg.get("groups", {})
        # 合并：数据库 + config，两边都不遗漏
        all_gids = set(db_groups.keys()) | set(str(g) for g in config_groups.keys())
        for gid in sorted(all_gids):
            info = config_groups.get(gid, {})
            gname = db_groups.get(gid, "")
            owner = info.get("owner", "") if isinstance(info, dict) else ""
            label = f"群 {gid}"
            if gname:
                label += f"「{gname}」"
            if owner:
                label += f" (主:{owner})"
            choices.append((label, gid))
            if owner and owner not in [c[1] for c in choices]:
                choices.append((f" └ 群主:{owner}", owner))
        owner_qq = self.cfg.get("bot", {}).get("owner_qq", "")
        if owner_qq and owner_qq not in [c[1] for c in choices]:
            choices.insert(0, (f"⭐ 主人:{owner_qq}", owner_qq))
        return choices

    def _show_target_white_menu(self):
        menu = QMenu(self)
        for label, gid in self._get_target_choices():
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(gid in self._log_target_white_ids)
            action.setData(gid)
        menu.triggered.connect(self._on_target_white_toggled)
        menu.exec(self._log_target_white_btn.mapToGlobal(self._log_target_white_btn.rect().bottomLeft()))

    def _on_target_white_toggled(self, action: QAction):
        gid = action.data()
        if not gid:
            return
        if action.isChecked():
            self._log_target_white_ids.add(gid)
            self._log_target_black_ids.discard(gid)
        else:
            self._log_target_white_ids.discard(gid)
        self._update_target_btn_labels()
        self._on_log_filter_changed()

    def _show_target_black_menu(self):
        menu = QMenu(self)
        for label, gid in self._get_target_choices():
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(gid in self._log_target_black_ids)
            action.setData(gid)
        menu.triggered.connect(self._on_target_black_toggled)
        menu.exec(self._log_target_black_btn.mapToGlobal(self._log_target_black_btn.rect().bottomLeft()))

    def _on_target_black_toggled(self, action: QAction):
        gid = action.data()
        if not gid:
            return
        if action.isChecked():
            self._log_target_black_ids.add(gid)
            self._log_target_white_ids.discard(gid)
        else:
            self._log_target_black_ids.discard(gid)
        self._update_target_btn_labels()
        self._on_log_filter_changed()

    def _update_target_btn_labels(self):
        if self._log_target_white_ids:
            ids = ",".join(sorted(self._log_target_white_ids))
            self._log_target_white_btn.setText(f"▾ 只看: {ids}")
        else:
            self._log_target_white_btn.setText("▾ 显示全部")
        if self._log_target_black_ids:
            ids = ",".join(sorted(self._log_target_black_ids))
            self._log_target_black_btn.setText(f"▾ 排除: {ids}")
        else:
            self._log_target_black_btn.setText("▾ 不过滤")

    # ── 内容过滤器 ──

    def _on_content_filter_changed(self):
        # 如果点了具体分类，取消「全部」选中
        if self._log_show_all_btn.isChecked():
            self._log_show_all_btn.setChecked(False)
        self._on_log_filter_changed()

    def _on_show_all_clicked(self):
        # 点击「全部」→ 取消所有分类按钮，显示全部日志
        for btn in self._log_content_btns.values():
            btn.setChecked(False)
        self._log_show_all_btn.setChecked(True)
        self._on_log_filter_changed()

    def _get_active_content_keywords(self) -> list[str]:
        """收集所有被选中内容分类的关键词"""
        kw = []
        for cat, btn in self._log_content_btns.items():
            if btn.isChecked():
                keywords, _ = self._log_content_categories.get(cat, ([], ""))
                kw.extend(keywords)
        return kw

    # ── 通用过滤逻辑 ──

    def _clear_log(self):
        self._log_area.clear()
        self._log_filter_count = 0
        self._update_filter_count_label()

    def _on_log_filter_changed(self):
        active_cats = any(btn.isChecked() for btn in self._log_content_btns.values())
        has_blacklist = bool(self._log_blacklist_input.text().strip())
        has_targets = bool(self._log_target_white_ids or self._log_target_black_ids)
        self._log_filter_active = active_cats or has_blacklist or has_targets
        self._log_filter_count = 0
        self._update_filter_count_label()
        # 同步「全部」按钮状态
        if not self._log_filter_active:
            self._log_show_all_btn.setChecked(True)

    def _update_filter_count_label(self):
        if self._log_filter_active:
            active_cats = [cat for cat, btn in self._log_content_btns.items() if btn.isChecked()]
            desc = ", ".join(active_cats) if active_cats else ""
            extra = []
            if self._log_blacklist_input.text().strip():
                extra.append("排除")
            if self._log_target_white_ids:
                extra.append("目标白名单")
            if self._log_target_black_ids:
                extra.append("目标黑名单")
            if extra:
                desc = (desc + " + " if desc else "") + " + ".join(extra)
            self._log_filter_count_label.setText(f"🔍 过滤中：{desc}" if desc else "🔍 过滤中")
            self._log_filter_count_label.setStyleSheet("color: #f0a060; font-size: 10px;")
        elif self._log_filter_count > 0:
            self._log_filter_count_label.setText(f"已过滤 {self._log_filter_count} 条")
            self._log_filter_count_label.setStyleSheet("color: #555; font-size: 10px;")
        else:
            self._log_filter_count_label.setText("")
            self._log_filter_count_label.setStyleSheet("color: #555; font-size: 10px;")

    def _extract_ids(self, msg: str) -> set[str]:
        import re
        ids = set()
        for m in re.finditer(r'群[:：]?\s*(\d{5,15})', msg):
            ids.add(m.group(1))
        for m in re.finditer(r'(?:qq|QQ)[:：]?\s*(\d{5,15})', msg):
            ids.add(m.group(1))
        for m in re.finditer(r'被\s*(\d{5,15})\s*戳', msg):
            ids.add(m.group(1))
        return ids

    def _match_filter(self, msg: str) -> bool:
        if not self._log_filter_active:
            return True
        # 1. 内容白名单（选中分类的关键词）
        active_kw = self._get_active_content_keywords()
        if active_kw:
            if not any(k in msg for k in active_kw):
                return False
        # 2. 内容黑名单
        b = self._log_blacklist_input.text().strip()
        if b:
            keywords = [k.strip() for k in b.split(",") if k.strip()]
            if keywords and any(k in msg for k in keywords):
                return False
        # 3. 目标白名单：只显示勾选的群/人，无 ID 的日志（系统/LLM等）放行
        if self._log_target_white_ids:
            msg_ids = self._extract_ids(msg)
            if msg_ids and not msg_ids.intersection(self._log_target_white_ids):
                return False
        # 4. 目标黑名单：隐藏勾选的群/人
        if self._log_target_black_ids:
            msg_ids = self._extract_ids(msg)
            if msg_ids.intersection(self._log_target_black_ids):
                return False
        return True

    def _log(self, msg: str):
        if not self._match_filter(msg):
            self._log_filter_count += 1
            # 每 20 条更新一次计数（避免频繁更新 UI）
            if self._log_filter_count % 20 == 0:
                self._update_filter_count_label()
            return

        ts = time.strftime("%H:%M:%S")
        # 只有用户在底部时才自动滚到底部——翻上去看历史日志时不被新消息打断
        sb = self._log_area.verticalScrollBar()
        was_at_bottom = sb.value() >= sb.maximum() - 10
        self._log_area.appendPlainText(f"[{ts}] {msg}")
        if was_at_bottom:
            sb.setValue(sb.maximum())

    # ═══════════════════════════════════════════════════════
    # SnowLuma 管理
    # ═══════════════════════════════════════════════════════
    def _check_snowluma_status(self) -> bool:
        """检查 SnowLuma 是否在运行 — 检测 OneBot HTTP API 端口"""
        import socket
        try:
            s = socket.create_connection(('127.0.0.1', 3000), timeout=0.2)
            s.close()
            return True
        except (OSError, ConnectionRefusedError):
            return False

    def _sync_snowluma_status(self):
        # 🔄 检查自动重启信号（main.py 检测到 QQ 离线后写入）
        signal_file = BASE / ".trigger_restart_snowluma"
        if signal_file.exists():
            self._log("🔄 收到糖糖的自动重启信号...")
            try:
                signal_file.unlink()
            except Exception:
                pass
            # 杀掉旧 SnowLuma → 等 3 秒 → 重新启动
            import subprocess
            # 用 QProcess 避免阻塞 UI：taskkill 在后台完成
            proc = QProcess()
            proc.setProgram("taskkill")
            proc.setArguments(["/f", "/im", "SnowLuma.exe"])
            proc.setProcessChannelMode(QProcess.SeparateChannels)
            proc.start()
            # 等待最多 10 秒（事件循环保持活跃）
            _loop = QEventLoop()
            proc.finished.connect(_loop.quit)
            QTimer.singleShot(10000, _loop.quit)
            _loop.exec()
            # 等进程完全退出后用定时器异步启动——不阻塞 UI 线程
            QTimer.singleShot(3000, lambda: (
                self._start_snowluma(),
                self._log("✅ SnowLuma 已自动重启")
            ))
            return

        alive = self._check_snowluma_status()
        if alive:
            self._btn_snowluma.setText("✅ SnowLuma 在线")
            self._btn_snowluma.setStyleSheet(
                f"QPushButton {{ background-color: {GREEN}; color: white; padding: 10px 20px; "
                f"font-size: 13px; font-weight: bold; border: none; border-radius: 6px; }}"
            )
            self._lbl_snowluma_hint.setText("已检测到 SnowLuma · 可启动糖糖")
            self._card_status._val_label.setText("已连接")
            self._set_status_color(self._card_status._val_label, self._pal['green'])
        else:
            self._btn_snowluma.setText("启动 SnowLuma")
            self._btn_snowluma.setObjectName("actionBtn")
            self._btn_snowluma.setStyleSheet("")
            self._lbl_snowluma_hint.setText("启动后自动登录")
            self._card_status._val_label.setText("离线")
            self._set_status_color(self._card_status._val_label, self._pal['text_muted'])

    def _ask_download_snowluma(self):
        """没装 SnowLuma 时给出可执行的下一步，而不是丢一个路径就完事。

        SnowLuma 是第三方 QQ 协议端，其 EULA 第 5.4 条明确禁止「将其并入第三方
        安装包」与「通过自动化脚本部署」——所以本项目既不能随包分发、也不能让
        安装器代下。这里只能把用户引到官方发布页，由他自己下载一次。
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("还差一步：SnowLuma")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(
            "糖糖需要 <b>SnowLuma</b> 才能连上 QQ，本机还没有。<br><br>"
            "它是第三方 QQ 协议端，许可协议不允许随本项目一起分发，"
            "所以要你下载一次：<br><br>"
            "　1. 打开官方发布页，下载 <b>Windows x64</b> 版<br>"
            "　2. 解压到下面这个目录里<br>"
            f"　　　<code>{SNOWLUMA_DIR.parent}</code><br>"
            "　　　（解压后应得到 <code>SnowLuma-vX.Y.Z-win-x64/</code> 文件夹）<br>"
            "　3. 回到这里再点一次「启动 SnowLuma」"
        )
        open_btn = box.addButton("打开发布页", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("知道了", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl(SNOWLUMA_RELEASE_URL))
            self._log("已在浏览器打开 SnowLuma 发布页——下载 Windows x64 版解压到 "
                      f"{SNOWLUMA_DIR.parent}")

    def _start_snowluma(self):
        if self._check_snowluma_status():
            self._log("ℹ SnowLuma 已在运行中")
            return
        if not SNOWLUMA_EXE.exists():
            self._ask_download_snowluma()
            return
        try:
            import subprocess
            self._snowluma_proc = subprocess.Popen(
                [str(SNOWLUMA_EXE)],
                cwd=str(SNOWLUMA_EXE.parent),
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._log("SnowLuma 已启动 → 屏幕应该弹出了 QQ 登录窗口")
        except Exception as e:
            self._log(f"❌ SnowLuma 启动失败: {e}")

    # ═══════════════════════════════════════════════════════
    # 糖糖进程管理 (QProcess)
    # ═══════════════════════════════════════════════════════
    def _start_sugar(self):
        if self._sugar_process and self._sugar_process.state() == QProcess.Running:
            QMessageBox.information(self, "已经在运行", "小糖糖已经在运行中了")
            return

        self._manual_stop = False  # 真正启动了——下次退出不再是"手动停止"
        self._sugar_process = QProcess(self)
        self._sugar_process.setWorkingDirectory(str(BASE))
        self._sugar_process.setProcessChannelMode(QProcess.MergedChannels)
        self._sugar_process.readyReadStandardOutput.connect(self._read_sugar_output)
        self._sugar_process.finished.connect(self._on_sugar_finished)

        # 优先用 Python 3.10（GPT-SoVITS 依赖完整），不可用则回退当前解释器
        py310 = find_python310()
        python_exe = str(py310) if py310 else sys.executable
        self._sugar_process.start(python_exe, [str(BASE / "main.py")])
        self._log("🍬 小糖糖正在启动...")
        self._update_sugar_ui("start")

    def _stop_sugar(self):
        self._manual_stop = True
        if self._sugar_process and self._sugar_process.state() == QProcess.Running:
            self._log("⏹ 正在停止糖糖…（停止后自动打包记忆快照）")
            self._sugar_process.terminate()
            if not self._sugar_process.waitForFinished(3000):
                self._sugar_process.kill()
            self._log("⏹ 小糖糖已停止")
        # 清理孤儿 GPT-SoVITS（Windows SIGTERM 不可用，子进程可能残留）
        self._kill_port_process(9880)
        self._update_sugar_ui("stop")

    def _on_sugar_finished(self, exit_code: int, exit_status):
        """糖糖进程退出处理：
        - 手动停止 → 什么都不做（_manual_stop 标记由 _stop_sugar 设置）
        - 定时重启（exit=42）→ 3 秒后自动拉起新进程
        - 其他退出 → 记录日志，等待手动启动
        - 任何退出 → 后台自动打包记忆快照（数据未变化自动跳过）
        """
        self._auto_pack_memory()
        if self._manual_stop:
            self._manual_stop = False
            return
        if exit_code == 42:  # 与 handler.py _do_scheduled_restart 的退出码约定
            self._log("🔄 检测到定时重启（exit=42）——自动拉起新进程…")
            QTimer.singleShot(3000, self._start_sugar)
            return
        self._log(f"⚠️ 糖糖进程退出 (code={exit_code})——已停止")
        self._update_sugar_ui("stop")

    def _auto_pack_memory(self):
        """自动打包记忆快照——停止糖糖 / 关闭程序 / 定时重启时都会触发。
        用 subprocess.Popen 保证控制台退出后打包仍会完成；
        daemon 线程等待结束后发信号回主线程提示结果。
        数据没变化时同步记忆.py 的 --if-newer 会自动跳过。"""
        script = BASE / "tools" / "同步记忆.py"
        if not script.exists():
            return
        if getattr(self, '_packing', False):
            self._log("ℹ 记忆快照打包已在进行中，跳过")
            return
        self._packing = True
        try:
            import subprocess as _sp
            import threading as _th
            proc = _sp.Popen(
                [sys.executable, str(script), "打包", "--if-newer"],
                cwd=str(BASE),
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            )

            def _wait_and_notify():
                code = proc.wait()
                self._packing = False
                self._pack_done_sig.emit(code == 0)

            _th.Thread(target=_wait_and_notify, daemon=True).start()
            self._log("💾 正在打包记忆快照…（数据未变化自动跳过）")
        except Exception as e:
            self._packing = False
            self._log(f"⚠️ 自动打包失败: {e}")
            if getattr(self, '_exiting', False):
                QApplication.quit()  # 关闭流程中打包启动失败——不能卡住退出

    def _on_pack_done(self, ok: bool):
        """打包完成提示（主线程）"""
        if ok:
            self._log("✅ 记忆快照打包完成（换机时用「解包记忆」恢复）")
        else:
            self._log("⚠️ 记忆快照打包失败——请双击 安装糖糖.bat → 菜单 2 打包记忆")
        if getattr(self, '_exiting', False):
            self._log("👋 控制台已关闭（SnowLuma 保持运行）")
            QApplication.quit()
            return
        # 后台自动打包（30分钟定时/停止糖糖等）→ 静默执行，仅记日志，不弹窗打扰
        # 关闭控制台时的打包提示在 _do_exit 的确认弹窗里已说明

    def _check_snowluma_update(self):
        """开机后台检查 SnowLuma 新版本（2026-08-16 QQ 9.9.33 注入事故——
        SnowLuma 1.14.8 OIDB 挂；新版可能适配。有新版弹提醒，无则静默）。"""
        try:
            import subprocess
            import sys as _sys
            proc = subprocess.run(
                [_sys.executable, str(BASE / "tools" / "检查SnowLuma更新.py")],
                capture_output=True, text=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.stdout.strip():
                self._log(proc.stdout.strip())
                QMessageBox.information(self, "🔔 SnowLuma 有新版本",
                                        proc.stdout.strip())
        except Exception as e:
            self._log(f"⚠ SnowLuma 更新检查失败: {e}")

    def _auto_unpack_memory(self):
        """开机自动解包：memory_sync/ 里最新快照比本地 memory.db 新 → 自动应用。
        仅在控制台刚启动时执行（糖糖未运行，安全）。本地数据比快照新则跳过（不丢数据）。
        整个逻辑包在 try 里——任何异常都只记日志，不能让控制台崩溃（崩溃会连带杀掉糖糖）。"""
        try:
            sync_dir = BASE / "memory_sync"
            db = BASE / "memory.db"
            if not sync_dir.exists():
                return
            snaps = sorted(sync_dir.glob("memory-*.db"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            if not snaps:
                return
            # 自动解包只考虑「其他机器」的快照——本机快照是本机库的拷贝，
            # 解包它无意义，还会因为本机停止时自动打包而永远"最新"，挡住其他机器的快照
            import os as _os
            my_name = (_os.environ.get("COMPUTERNAME") or "").lower()
            others = [p for p in snaps if my_name not in p.name.lower()]
            if not others:
                self._log("ℹ 开机检查：无其他设备的记忆快照（本机快照不用于自动解包）")
                return
            snap = others[0]
            # 快照比本地新 ≥ 60 秒才解包——防止"刚打包完"的抖动触发无意义覆盖
            if db.exists() and db.stat().st_mtime + 60 >= snap.stat().st_mtime:
                self._log("ℹ 开机检查：本地记忆已是最新，无需解包")
                return
            import time as _time
            self._log(f"📦 检测到新记忆快照 {snap.name}"
                      f"（{_time.strftime('%H:%M', _time.localtime(snap.stat().st_mtime))}）——自动解包…")
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "sync_memory", str(BASE / "tools" / "同步记忆.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            # 分叉检测（2026-09-04 起按 event_key 判定——QQ 同一条消息两台设备
            # 各自写入不同内容才是真并发分叉；自增 id 撞车不算）→ 跳过需人工处理
            fork = mod.count_fork_diff(db, snap)
            if fork > 0:
                self._log(f"⚠️ 检测到并发记忆分叉（{fork} 条同一消息两台设备各自写入）——自动解包已跳过")
                self._log("  请手动处理: 双击 安装糖糖.bat → 菜单 3 解包记忆（确认哪台为准）")
                QMessageBox.warning(
                    self, "📦 记忆同步",
                    f"检测到并发记忆分叉（{fork} 条同一消息两台设备各自写入）\n\n"
                    f"自动解包已跳过，请手动处理：\n"
                    f"双击 安装糖糖.bat → 菜单 3 解包记忆")
                return
            # 方向守卫：本机有快照未包含的新记录（最后时刻没打包）→ 覆盖会静默丢失
            missing = mod.count_local_missing(db, snap)
            if missing > 0:
                self._log(f"⚠️ 本机有 {missing} 条快照未包含的新记录——自动解包已跳过")
                self._log("  请先在本机执行「打包记忆」，再重试解包")
                QMessageBox.warning(
                    self, "📦 记忆同步",
                    f"本机有 {missing} 条快照未包含的新记录（最后时刻的对话没打包）\n\n"
                    f"自动解包已跳过（避免静默丢失）：\n"
                    f"请先在本机执行 安装糖糖.bat → 菜单 2 打包记忆，\n"
                    f"再重试自动/手动解包")
                return
            rc = mod.cmd_unpack(None)  # 自动选最新快照；返回退出码约定：0=成功，非0=失败
            if rc == 0:
                self._log("✅ 开机自动解包完成，糖糖已接上另一台设备的记忆")
                QMessageBox.information(
                    self, "📦 记忆同步",
                    "已自动接上另一台设备的记忆 ✅\n糖糖现在记得另一台机器上的对话。")
            else:
                self._log("⚠️ 开机自动解包失败——请手动检查 tools/同步记忆.py")
                QMessageBox.warning(
                    self, "📦 记忆同步",
                    "自动解包失败 ⚠️\n请双击 安装糖糖.bat → 菜单 3 解包记忆")
        except Exception as e:
            self._log(f"⚠️ 开机自动解包异常: {e}")

    def _on_commit_data(self, *args):
        """Windows 关机/注销前触发——尽力打包记忆快照。
        注意：关机流程可能等不了打包完成（10-30秒），
        真正兜底是每 30 分钟的定时自动打包。"""
        self._log("💾 检测到系统关机/注销——尽力打包记忆快照…")
        self._auto_pack_memory()

    def _kill_port_process(self, port: int):
        """杀掉指定端口的进程（用于清理孤儿子进程）"""
        try:
            import subprocess
            result = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.split("\n"):
                if f"127.0.0.1:{port}" in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=5)
                    self._log(f"🔧 已清理端口 {port} 的残留进程 (PID={pid})")
                    break
        except Exception as e:
            self._log(f"⚠ 清理端口 {port} 失败: {e}")

    def _restart_sugar(self):
        self._stop_sugar()
        QTimer.singleShot(1000, self._start_sugar)

    def _read_sugar_output(self):
        data = self._sugar_process.readAllStandardOutput()
        text = data.data().decode("utf-8", errors="replace")
        for line in text.splitlines():
            if line.strip():
                self._log(line.strip())
                if "SnowLuma 已连接" in line:
                    self._update_sugar_ui("running")
                if "QQ 账号已掉线" in line:
                    self._update_sugar_ui("qq_offline")
                if "QQ 账号已恢复在线" in line:
                    self._update_sugar_ui("running")
                if "小糖糖崩溃了" in line or "Traceback" in line:
                    self._update_sugar_ui("error")

    def _update_sugar_ui(self, state: str):
        if state == "start":
            self._btn_sugar.setText("⏳ 启动中...")
            self._btn_sugar.setStyleSheet(
                f"QPushButton {{ background-color: {ORANGE}; color: white; padding: 10px 20px; "
                f"font-size: 13px; font-weight: bold; border: none; border-radius: 6px; }}"
            )
            self._card_online._val_label.setText("启动中...")
            self._set_status_color(self._card_online._val_label, self._pal['orange'])
            self._sidebar_status.setText("🟡 启动中")
            self._set_status_color(self._sidebar_status, self._pal['orange'])
        elif state == "running":
            self._btn_sugar.setText("✅ 运行中")
            self._btn_sugar.setStyleSheet(
                f"QPushButton {{ background-color: {GREEN}; color: white; padding: 10px 20px; "
                f"font-size: 13px; font-weight: bold; border: none; border-radius: 6px; }}"
            )
            self._card_online._val_label.setText("运行中")
            self._set_status_color(self._card_online._val_label, self._pal['green'])
            self._sidebar_status.setText("🟢 在线")
            self._set_status_color(self._sidebar_status, self._pal['green'])
        elif state == "stop":
            self._btn_sugar.setText("🍬 启动小糖糖")
            self._btn_sugar.setObjectName("pinkBtn")
            self._btn_sugar.setStyleSheet("")
            self._card_online._val_label.setText("已停止")
            self._set_status_color(self._card_online._val_label, self._pal['text_muted'])
            self._sidebar_status.setText("● 离线")
            self._set_status_color(self._sidebar_status, self._pal['text_muted'])
        elif state == "qq_offline":
            # QQ 掉线但 SnowLuma 进程仍在
            self._card_online._val_label.setText("QQ离线")
            self._set_status_color(self._card_online._val_label, self._pal['orange'])
            self._sidebar_status.setText("🟠 QQ离线")
            self._set_status_color(self._sidebar_status, self._pal['orange'])
        elif state == "error":
            self._card_online._val_label.setText("异常")
            self._set_status_color(self._card_online._val_label, self._pal['red'])
            self._sidebar_status.setText("🔴 异常")
            self._set_status_color(self._sidebar_status, self._pal['red'])

    # ═══════════════════════════════════════════════════════
    # 诊断
    # ═══════════════════════════════════════════════════════
    def _run_diagnostic(self):
        self._navigate("log")
        self._log("🔍 开始诊断...")

        self._diag_worker = DiagnosticWorker(BASE)
        self._diag_worker.finished.connect(self._show_diag_card)
        self._diag_worker.start()

    def _show_diag_card(self, s: dict, db: dict):
        lines = []
        lines.append("━" * 40)
        lines.append("🔍 糖糖运行诊断报告")
        lines.append("━" * 40)
        lines.append(f"扫描 {s.get('total', 0)} 行日志 · {db.get('people_count', 0)}群友 {db.get('memory_count', 0)}记忆 {db.get('chat_count', 0)}聊天 {db.get('sticker_count', 0)}表情包")
        lines.append("")

        # ── 阻塞类问题（必须修复） ──
        blockers = []
        if s.get("testing_blocked", 0) > 0:
            blockers.append(f"🔴 测试模式拦截了 {s['testing_blocked']} 条群消息 → config.yaml → testing_mode: false")
        if s.get("whitelist_blocked", 0) > 0:
            blockers.append(f"🔴 白名单拦截了 {s['whitelist_blocked']} 条消息 → 把群号加到 config.yaml → groups")
        if s.get("llm_calls", 0) > 0:
            fail_rate = s["llm_failures"] / s["llm_calls"] * 100
            if fail_rate > 5:
                blockers.append(f"🔴 LLM 失败率 {fail_rate:.0f}% ({s['llm_failures']}/{s['llm_calls']}) → 检查 API Key 和余额")
        if blockers:
            lines.append("🚫 必须修复：")
            lines.extend(blockers)
            lines.append("")

        # ── 回复链路 ──
        called = s.get("at_bot", 0) + s.get("name_mention", 0)
        llm_calls = s.get("llm_calls", 0)
        interj = "🟢 开" if s.get("interjection_on") else "🔴 关"
        lines.append(f"📡 回复链路：收到群消息 {s.get('total_group_msg', 0)} 条 → LLM调用 {llm_calls} 次 → 发送 {s.get('reply_sent', 0)} 次")
        lines.append(f"  主动插话：{interj} | 饥渴度 {s.get('interjection_thirst', '?')} | 冷却 {s.get('interjection_cooldown', '?')}s")
        if called > 0:
            actual = round(llm_calls / called * 100) if called else 0
            lines.append(f"  被呼叫(@+提名字)：{called} 次 → 实际回复：{llm_calls} 次 → 呼叫响应率：{actual}%")
            if actual < 50:
                lines.append(f"  ⚠ 响应率偏低！{called - llm_calls} 次呼叫未回复")
        lines.append("")

        # ── 内容质量 ──
        if db.get("avg_reply_len", 0) > 0:
            lines.append("💬 内容质量：")
            rlen = db['avg_reply_len']
            rlen_icon = "✅" if 50 <= rlen <= 200 else ("⚠ 偏短" if rlen < 50 else "⚠ 偏长(可能啰嗦)")
            div = db.get('opening_diversity', 0)
            div_icon = "✅" if div >= 50 else ("⚠ 模板化" if div < 30 else "⚡ 一般")
            mem = db.get('memory_ref_rate', 0)
            mem_icon = "✅" if mem >= 10 else ("⚠ 偏少" if mem < 5 else "⚡ 尚可")
            overlap = db.get('avg_keyword_overlap', 0)
            ov_icon = "✅" if overlap >= 20 else ("⚠ 跑题" if overlap < 10 else "⚡ 一般")
            follow = db.get('follow_up_rate', 0)
            fw_icon = "✅" if follow >= 40 else ("⚠ 冷场" if follow < 20 else "⚡ 一般")

            lines.append(f"  回复长度：{rlen}字 {rlen_icon}  开头多样性：{div}% {div_icon}")
            lines.append(f"  关键词相关：{overlap}% {ov_icon}  记忆引用：{mem}% {mem_icon}")
            lines.append(f"  对话延续：{follow}% {fw_icon}（糖糖回了之后有人接话的比例）")
            lines.append(f"  提问率：{db.get('question_rate', 0)}% | 回复率（最近500条）：{db.get('reply_rate', 0)}%")

            # 模板化
            if db.get("most_repeated"):
                lines.append(f"  🔄 模板化：同一句话对不同人说了多次 →")
                for msg, cnt in db["most_repeated"][:2]:
                    lines.append(f"     出现{cnt}次：{msg[:50]}...")

            lines.append("")

        # ── 优化建议 ──
        tips = []
        if db.get("opening_diversity", 100) < 30:
            tips.append("💡 开头重复率高 → 人格 can_do 里加「每次回复用不同的开场方式」")
        if db.get("avg_keyword_overlap", 100) < 10:
            tips.append("💡 关键词相关性低 → 系统提示词强调「围绕群友的话题回复，不要自说自话」")
        if db.get("follow_up_rate", 100) < 30:
            tips.append("💡 回复后冷场 → can_do 里加「回复结尾留钩子：提问、邀请回应」")
        if db.get("memory_ref_rate", 100) < 5:
            tips.append("💡 记忆引用少 → can_do 加强调「在对话中自然提及记得的事」")
        if db.get("question_rate", 100) < 15:
            tips.append("💡 很少反问 → can_do 加「每次回复尽量带一个问题」")
        if s.get("busy_skipped", 0) > 10:
            tips.append("💡 忙线跳过频繁 → /冷却 增加冷却时间")
        if s.get("group_send_fail", 0) > 0:
            tips.append("💡 发送失败 → 降低发消息频率，避免被QQ风控")

        if tips:
            lines.append("💡 优化建议：")
            lines.extend(tips)
            lines.append("")
        else:
            lines.append("✅ 运行质量良好，无需调整")
            lines.append("")

        # ── 低质量样本 ──
        if db.get("low_quality_samples"):
            lines.append("📝 跑题回复示例（糖糖没跟上话题）：")
            for i, (human, bot) in enumerate(db["low_quality_samples"][:3], 1):
                lines.append(f"  {i}. 群友: {human[:60]}")
                lines.append(f"     糖糖: {bot[:80]}")
            lines.append("")

        self._diag_summary.setText(f"扫描 {s.get('total', 0)} 行日志")
        self._diag_text.setPlainText("\n".join(lines))
        self._diag_card.setVisible(True)
        self._log("✅ 诊断完成")

    # ═══════════════════════════════════════════════════════
    # 对话质量分析
    # ═══════════════════════════════════════════════════════
    def _run_quality_analysis(self):
        self._navigate("log")
        group_id = next(iter(self._log_target_white_ids), "") if self._log_target_white_ids else ""
        label = f"群 {group_id}" if group_id else "全部"
        self._log(f"📊 开始分析对话质量 ({label})...")
        self._diag_card.setVisible(True)
        self._diag_summary.setText(f"📊 对话质量分析 · {label} · 正在分析...")
        self._diag_text.setPlainText("分析中，请稍候...")
        self._quality_worker = QualityAnalysisWorker(BASE, group_id=group_id)
        self._quality_worker.finished.connect(self._show_quality_result)
        self._quality_worker.start()

    def _show_quality_result(self, text: str):
        self._diag_summary.setText("📊 对话质量分析 · 完成")
        self._diag_text.setPlainText(text)
        self._log("📊 分析完成")

    # ═══════════════════════════════════════════════════════
    # 系统托盘
    # ═══════════════════════════════════════════════════════
    def _setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        # QPainter 画图标
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(PINK))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(4, 4, 56, 56)
        painter.setPen(QColor("white"))
        font = QFont("Microsoft YaHei UI", 28, QFont.Bold)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignCenter, "糖")
        painter.end()

        self._tray_icon = QSystemTrayIcon(self)
        self._tray_icon.setIcon(QIcon(pixmap))
        self._tray_icon.setToolTip("小糖糖")

        menu = QMenu()
        menu.addAction("🍬 显示控制台", self._tray_show)
        menu.addAction("诊断", self._tray_diagnostic)
        menu.addSeparator()
        menu.addAction("❌ 退出", self._tray_exit)
        self._tray_icon.setContextMenu(menu)
        self._tray_icon.activated.connect(self._on_tray_activated)
        self._tray_icon.show()

    def _tray_show(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def _tray_diagnostic(self):
        self.show()
        self.raise_()
        QTimer.singleShot(500, self._run_diagnostic)

    def _tray_exit(self):
        self._tray_icon.hide()
        self._do_exit()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self._tray_show()

    def _do_exit(self):
        if getattr(self, '_exiting', False):
            return  # 关闭流程已在进行
        self._exiting = True
        if self._sugar_process and self._sugar_process.state() == QProcess.Running:
            reply = QMessageBox.question(
                self, "确认关闭",
                "小糖糖还在运行，要停止并退出吗？\n\n"
                "退出前会等待记忆快照打包完成（约 10-30 秒，\n换机时用「解包记忆」恢复）。"
            )
            if reply != QMessageBox.Yes:
                self._exiting = False
                return  # 取消关闭
            self._stop_sugar()
        # 关闭控制台 = 完全可控的退出流程 → 100% 确保打包：
        # 触发打包后不立即退出，等打包完成信号 _on_pack_done → 再退出
        self._log("👋 正在关闭控制台——确保记忆快照打包完成…")
        self._auto_pack_memory()

    def closeEvent(self, event):
        """关闭窗口 → 最小化到托盘（如果托盘可用）"""
        if QSystemTrayIcon.isSystemTrayAvailable() and self._silent_check.isChecked():
            self.hide()
            self._log("📌 控制台最小化到托盘（右键托盘图标可退出）")
            event.ignore()
        else:
            self._do_exit()
            event.accept()


# ═══════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════
def _get_nested(d: dict, path: str, default=None):
    """读取嵌套字典值，如 'llm.vision.enabled'"""
    keys = path.split(".")
    for k in keys:
        if isinstance(d, dict) and k in d:
            d = d[k]
        else:
            return default
    return d


def _set_nested(d: dict, path: str, value):
    """设置嵌套字典值"""
    keys = path.split(".")
    for k in keys[:-1]:
        if k not in d or not isinstance(d[k], dict):
            d[k] = {}
        d = d[k]
    d[keys[-1]] = value


# ═══════════════════════════════════════════════════════════
# 群编辑弹窗
# ═══════════════════════════════════════════════════════════
class GroupEditDialog(QDialog):
    def __init__(self, parent, gid: str, info: dict):
        super().__init__(parent)
        self.setWindowTitle(f"编辑群 {gid}")
        self.setFixedSize(440, 500)
        self.result = {"owner": "", "admins": [], "scenario": "", "scenario_targets": {}}

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        layout.addWidget(QLabel(f"群号: {gid}", objectName="heading"))

        layout.addWidget(QLabel("群主 QQ:", objectName="body"))
        self._owner_input = QLineEdit(info.get("owner", ""))
        layout.addWidget(self._owner_input)

        layout.addWidget(QLabel("管理员 QQ（一行一个）:", objectName="body"))
        self._admins_input = QTextEdit()
        self._admins_input.setMaximumHeight(70)
        admins = info.get("admins", [])
        if isinstance(admins, list):
            self._admins_input.setPlainText("\n".join(admins))
        layout.addWidget(self._admins_input)

        # 场景：群默认
        layout.addWidget(QLabel("群默认场景:", objectName="body"))
        self._scenario_combo = QComboBox()
        self._scenario_combo.setToolTip("群级别默认场景。下面名单没列出的群友都用这个。空=默认闲聊猫娘。")
        self._scenario_combo.addItem("💬 日常闲聊（默认）", "")
        self._load_scenarios()
        current = info.get("scenario", "")
        idx = self._scenario_combo.findData(current)
        if idx >= 0:
            self._scenario_combo.setCurrentIndex(idx)
        layout.addWidget(self._scenario_combo)

        # 场景：按 QQ 号精准指定
        layout.addWidget(QLabel("场景目标用户（一行一个: QQ号 空格 场景名）:", objectName="body"))
        # 动态生成 placeholder：列出实际可用的场景名
        _avail = [self._scenario_combo.itemText(i) for i in range(1, self._scenario_combo.count())]
        _avail_str = "、".join(_avail) if _avail else "无"
        hint = QLabel(f"为特定群友单独指定场景。可用的场景：{_avail_str}。场景名留空=继承群默认。优先级高于群默认。")
        hint.setObjectName("muted"); hint.setWordWrap(True)
        layout.addWidget(hint)
        self._targets_input = QTextEdit()
        self._targets_input.setMaximumHeight(90)
        # 用中文场景名做示例
        _example = "10001  心理陪伴\n10002  活动管理者\n10003              （场景名留空=继承群默认）"
        self._targets_input.setPlaceholderText(_example)
        targets = info.get("scenario_targets", {})
        if isinstance(targets, dict) and targets:
            lines = []
            for qq, sn in targets.items():
                lines.append(f"{qq}  {sn}" if sn else f"{qq}  ")
            self._targets_input.setPlainText("\n".join(lines))
        layout.addWidget(self._targets_input)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _load_scenarios(self):
        """扫描 scenarios/*.yaml 加载可用场景列表"""
        import yaml
        scenarios_dir = BASE / "scenarios"
        if not scenarios_dir.exists():
            return
        for f in sorted(scenarios_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "name" in data:
                    display = data.get("display", data["name"])
                    self._scenario_combo.addItem(f"{display}", data["name"])
            except Exception:
                pass

    def _on_accept(self):
        self.result["owner"] = self._owner_input.text().strip()
        admins_text = self._admins_input.toPlainText().strip()
        self.result["admins"] = [a.strip() for a in admins_text.split("\n") if a.strip()] if admins_text else []
        self.result["scenario"] = self._scenario_combo.currentData()
        # 解析场景目标用户
        targets = {}
        targets_text = self._targets_input.toPlainText().strip()
        if targets_text:
            for line in targets_text.split("\n"):
                line = line.strip()
                if not line:
                    continue
                parts = line.split(None, 1)
                qq = parts[0].strip()
                sn = parts[1].strip() if len(parts) > 1 else ""
                if qq:
                    targets[qq] = sn  # 空字符串 = 继承群默认
        self.result["scenario_targets"] = targets
        self.accept()


class QualityAnalysisWorker(QThread):
    finished = Signal(str)

    def __init__(self, base_path, group_id: str = "", private_qq: str = ""):
        super().__init__()
        self._base = base_path
        self._group_id = group_id
        self._private_qq = private_qq

    def run(self):
        import sys
        sys.path.insert(0, str(self._base))
        from tools.分析对话质量 import load_messages, ReplyQualityAnalyzer
        pairs = load_messages(group_id=self._group_id or None,
                             private_qq=self._private_qq or None,
                             hours=24)
        if self._group_id:
            label = f"群 {self._group_id}"
        elif self._private_qq:
            label = f"私聊 {self._private_qq}"
        else:
            label = "所有群聊 + 私聊"
        label += "（最近 24h）"
        analyzer = ReplyQualityAnalyzer(pairs, label)
        text = analyzer.analyze()
        self.finished.emit(text)


class DiagnosticWorker(QThread):
    finished = Signal(dict, dict)

    def __init__(self, base_dir: Path):
        super().__init__()
        self._base = base_dir

    def run(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("diagnose", str(self._base / "诊断糖糖.py"))
        if not spec or not spec.loader:
            self.finished.emit({"total": 0}, {"people_count": 0, "memory_count": 0, "chat_count": 0})
            return
        diag = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(diag)

        s = {"total": 0}
        db = {"people_count": 0, "memory_count": 0, "chat_count": 0}

        try:
            lines = diag.read_logs()
            if lines:
                s = diag.analyze_logs(lines)
        except Exception:
            pass

        try:
            db = diag.analyze_database()
        except Exception:
            pass

        self.finished.emit(s, db)


# ═══════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════
def main():
    # 静默 ICC 色彩配置文件警告（不影响功能）
    from PySide6.QtCore import QLoggingCategory
    QLoggingCategory.setFilterRules("qt.gui.icc.warning=false")
    app = QApplication(sys.argv)
    app.setStyle("Fusion")  # Fusion 基础风格，QSS 在其上覆盖
    app.setApplicationName("小糖糖控制台")
    console = TangTangQtConsole()
    console.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    print("🍬 控制台启动: 糖糖控制台_qt.py (多选翻唱版 2026-07-03)")
    main()
