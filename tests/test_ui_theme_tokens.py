def test_theme_tokens_are_module_level_and_referenced():
    source = open("糖糖控制台_qt.py", encoding="utf-8").read()
    assert "RADIUS_CONTROL = 6" in source
    assert "RADIUS_CARD = 10" in source
    assert "HIGHLIGHT_BORDER" in source
    assert "{radius_card}px" in source  # 2026-09-06 Aether：几何令牌按主题派生


def test_four_state_contract_pressed_focus_disabled():
    """四态规范闸门（2026-09-06 令牌表 §4.2）：主要按钮类必须都有 pressed，
    focus 键盘环不得依赖权重不足的通用规则，disabled 不得被 id 规则顶掉。"""
    source = open("糖糖控制台_qt.py", encoding="utf-8").read()
    for sel in ("QPushButton#pinkBtn:pressed", "QPushButton#greenBtn:pressed",
                "QPushButton#dangerBtn:pressed", "QPushButton#actionBtn:pressed",
                "QPushButton#subtleBtn:pressed", "QPushButton#catBtn:pressed",
                "QPushButton#navBtn:pressed", "QPushButton#logPill:pressed"):
        assert sel in source, f"缺失 pressed 态: {sel}"
    # id 级 focus（通用 :focus 权重 11 压不过 id 规则 100）
    assert "QPushButton#catBtn:focus" in source
    assert "QPushButton#logPill:focus" in source
    # id 级 disabled（否则 pinkBtn 在 disabled 时仍是 pink 底）
    assert "QPushButton#pinkBtn:disabled" in source
    assert "QPushButton#dangerBtn:disabled" in source
    # catBtn 边框占位 1px transparent——focus 只变色不位移
    assert 'border: 1px solid transparent;  /* focus 只变色不位移 */' in source


def test_darken_helper():
    from 糖糖控制台_qt import _darken  # noqa: E402
    assert _darken("#ffffff", 0.85) == "#d9d9d9"
    assert _darken("#000000", 0.5) == "#000000"
    assert _darken("#ff0000", 0.5) == "#800000"
    assert _darken("not-hex", 0.5) == "not-hex"


def test_20260906_ui_batch_contract():
    """五项 UI 改动闸门（2026-09-06 主人需求）：横线/分页/滚动/navBtn 灰/侧栏伸缩。"""
    source = open("糖糖控制台_qt.py", encoding="utf-8").read()
    # 1. 统计横线（自绘 HDivider + 主题刷新钩子）
    assert "class HDivider(QFrame)" in source
    assert "self._stat_divider = HDivider" in source
    assert "_stat_divider" in source.split("def _apply_theme")[1][:2000]  # 2026-09-06 Aether 接线加长，窗口放宽
    # 2. 分页：全量可翻页（500/页），禁止回归「仅显示前 500」
    assert "PAGE_SIZE = 500" in source
    assert "def _render_page" in source and "def _page_nav" in source
    assert "仅显示前" not in source
    # 3. 滚动平滑：ScrollPerPixel 双轴 + 列宽 Interactive
    assert "setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)" in source
    assert "setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)" in source
    assert "h.setSectionResizeMode(QHeaderView.Interactive)" in source
    # 4. navBtn 悬浮 = 灰底（与功能内按钮一致），不得回粉底
    nav_seg = source.split("QPushButton#navBtn:hover")[1][:220]
    assert "hover_bg" in nav_seg
    # 5. 侧栏伸缩：点击收缩/悬浮展开/图标态
    assert "def _set_sidebar_collapsed" in source
    # 按内容定位侧栏 eventFilter（模块级新增类可能排在前面，锚定第一个即可漂移）
    _sidebar_ef = next(s for s in source.split("def eventFilter(self, obj, ev):") if "_sidebar" in s[:300])
    assert "QEvent.Enter" in _sidebar_ef[:300]
    assert 'QPushButton#navBtn[rail="true"]' in source
    assert "self._set_sidebar_collapsed(True)" in source  # 点击导航收缩


def test_20260906_qfluent_extract_contract():
    """qfluentwidgets 源码提取工程闸门（2026-09-06）：
    1) 日志胶囊必须自绘（QSS 圆角病不剪裁背景，禁止回归 QSS 填充底）
    2) 主滚动面必须挂 OverlayScrollBar（12 处），禁止只留原生条
    3) ThemeToggle 四态微态在位"""
    source = open("糖糖控制台_qt.py", encoding="utf-8").read()
    # 1. 自绘胶囊：类 + 接入 + QSS 移交底/边框（只留文字色）
    assert "class PillButton(QPushButton)" in source
    assert 'btn = PillButton(cat)' in source
    assert 'self._log_show_all_btn = PillButton("全部")' in source
    logpill_seg = source.split("QPushButton#logPill {{")[1][:400]
    assert "background: transparent" in logpill_seg
    assert "_log_pill_on" in source.split("def _apply_theme")[1][:1800]  # 主题喂色钩子
    # 2. 自绘滚动条：类 + 挂载助手 + 12 处接入（日志/数据表/设置/列表）
    assert "class OverlayScrollBar(QWidget)" in source
    assert "def _attach_overlay_bar" in source
    assert source.count("_attach_overlay_bar(") >= 12
    for anchor in ("self._attach_overlay_bar(self._log_area)",
                   "self._attach_overlay_bar(self._data_table)",
                   "self._attach_overlay_bar(self._data_table, Qt.Horizontal)",
                   "self._attach_overlay_bar(self._settings_scroll)"):
        assert anchor in source, f"缺失滚动条接入: {anchor}"
    # 3. ThemeToggle 四态：hover 提亮 / pressed 压暗（对齐 SwitchButton 语言）
    assert "track_color = _darken(track_color.name(), 1.12)" in source
    assert "track_color = _darken(track_color.name(), 0.88)" in source
    assert "self._pressed = True" in source.split("class ThemeToggle")[1].split("class PillButton")[0]


def test_20260906_direct_reuse_contract():
    """直接复用工程闸门（2026-09-06 主人拍板）：丝滑滚轮 / 自绘 tooltip / 自绘勾选框。"""
    source = open("糖糖控制台_qt.py", encoding="utf-8").read()
    # 1. 丝滑滚轮：引擎在位 + GPL 版权头不可丢 + OverlayScrollBar 集成
    assert "class SmoothWheelEngine(QObject)" in source
    assert "zhiyiYo / PyQt-Fluent-Widgets" in source  # 直接复用的 GPL 版权声明
    assert "SmoothWheelEngine(parent.viewport(), self._partner, orient)" in source
    assert "self._smooth.wheel(ev)" in source
    # 滚轮失效修复（2026-09-06 真机事故）：分数累积器直驱值；禁止回放合成滚轮（取整吞 delta）
    assert "self._px_accum" in source
    assert "滚轮失效修复" in source
    assert "sendEvent(self._bar" not in source
    # 2. 自绘 tooltip：全局过滤器挂载 + 原生 QToolTip 引用清零（方角替换）
    assert "class GlobalToolTipFilter(QObject)" in source
    assert "class FluentTip(QWidget)" in source
    assert "installEventFilter(self._tooltip_filter)" in source
    assert "QToolTip {{" not in source  # 原生 QSS 块已移除（自绘接管）
    assert "QToolTip(" not in source  # 无原生 tooltip 构造调用
    # 3. 自绘勾选框：类 + 两处接入 + QSS 指示器透明（QSS 圆角病）
    assert "class FluentCheckBox(QCheckBox)" in source
    assert "widget = FluentCheckBox()" in source
    assert 'FluentCheckBox("后台静默运行")' in source
    ind_seg = source.split("QCheckBox::indicator {{")[1][:200]
    assert "background: transparent" in ind_seg


def test_20260906_aether_contract():
    """Aether 设计系统移植闸门（2026-09-06，UI参考 bad-dodo-83 system.css 令牌映射）：
    浅色 = 珍珠底/炭黑墨/胶囊几何/玻璃 bevel，深色几何零改动；主色按钮浅色描边化（QSS 不剪裁填充）。"""
    source = open("糖糖控制台_qt.py", encoding="utf-8").read()
    # 1. 色板：浅色 Aether 令牌（pearl/ink/hairline），深色不动
    pal = open("agent/theme_palettes.py", encoding="utf-8").read()
    light_seg = pal.split('"light": {')[1].split("},")[0]
    assert "#eef1f6" in light_seg      # pearl
    assert "#1d1d1f" in light_seg      # ink 炭黑锚点
    assert "#54545a" in light_seg      # slate
    assert "#86868b" in light_seg      # mist
    assert "rgba(15, 23, 42, 0.08)" in light_seg  # hairline
    # 2. 几何主题派生：浅色胶囊/24px 卡；深色回模块常量
    assert 'radius_control = "999" if not _dk else str(RADIUS_CONTROL)' in source
    assert 'radius_card = "24" if not _dk else str(RADIUS_CARD)' in source
    # 3. 主色按钮：浅色表面改由 PillProxyStyle 自绘（2026-09-17 修复接线后生效，
    #    见 test_20260917_light_theme_fix_contract）——QSS 块只留文字/布局属性
    assert "_pink_bg = pink if _dk else \"transparent\"" in source
    assert "_pink_bdr = \"1px solid transparent\" if _dk else f\"1px solid {pink}\"" in source
    assert "{_body_pink}" in source.split("QPushButton#pinkBtn {{")[1][:400]
    # 4. 玻璃 bevel + Aether 排版（标题 600 不上 bold、数字 mono、Inter 链）
    assert "_bevel = \"rgba(255, 255, 255, 0.9)\" if not _dk else HIGHLIGHT_BORDER" in source
    assert "font-weight: 600;" in source.split("QLabel#heading {{")[1][:300]
    assert "JetBrains Mono" in source
    assert '"Inter", "Microsoft YaHei UI"' in source
    # 5. 标签页浅色=炭黑激活芯片；勾选框浅色 checked=炭黑
    assert '_tab_on = "#1d1d1f" if not _dk else "transparent"' in source
    assert '_cb_accent = ("#1d1d1f" if self._pal.get("theme_mode") != "dark"' in source


def test_20260906_aether_round2_contract():
    """Aether 二批闸门（2026-09-06 主人拍板按文档风格执行）：aurora 洗底 / 全局胶囊代理 / 指挥坞。"""
    source = open("糖糖控制台_qt.py", encoding="utf-8").read()
    # 1. AuroraWidget：自绘洗底 + setAlpha 修复（Qt 8 位 hex 是 AARRGGBB 非 CSS 语义——防黄绿回归）
    assert "class AuroraWidget(QWidget)" in source
    assert "setCentralWidget(self._aurora)" in source
    assert "setAlpha(0)" in source
    assert "AARRGGBB" in source  # 坑位注释不可丢
    assert "_base_bg = \"transparent\" if not _dk else main_bg" in source
    # 2. PillProxyStyle：全局胶囊自绘（QSS 圆角病无法实心胶囊）；深色关闭零改动
    assert "class PillProxyStyle(QProxyStyle)" in source
    assert "self._pill_style = _proxy" in source   # 2026-09-17：持有实例装载（见 20260917 闸门）
    assert "def _feed_pill_style" in source
    assert '"pill_on", ink' in source          # navBtn/catBtn 炭黑激活芯片
    assert "isinstance(btn, PillButton)" in source  # 自绘类不双绘
    # 3. 指挥坞：侧栏浮动壳（10px 呼吸边）+ 浅色 24px 玻璃容器
    assert "self._sidebar_shell = QWidget()" in source
    assert "shell_lay.setContentsMargins(10, 10, 10, 10)" in source
    assert '_sb_radius = "24px" if not _dk else "0px"' in source
    # 4. 深色零回归：代理深色关闭
    assert "style.set_enabled(light)" in source
    assert "if not light:\n            return" in source


def test_20260917_light_theme_fix_contract():
    """浅色修复闸门（2026-09-17 主人反馈「深色完好、浅色组件缺失」）：

    ① 危险色语义：浅色前景/描边/洗色用强色 pal["red"]（danger_bg 是填充色，
       白底上不可见——图库 × 删除按钮"消失"的根因）；danger_bg 只作自绘芯片底
    ② 自绘胶囊接线：持有代理实例直接 set_enabled（app.style() 顶层恒为
       QStyleSheetStyle，旧 isinstance 守卫永远失配 → 代理从未开启）
    ③ 委托契约：浅色下被代理接管的按钮规则不得含 border 声明（QStyleSheetStyle
       只在无 border 声明时把 CE_PushButtonBevel 委托给 base style——最小复现）
    ④ 深色零回归：生成 QSS 核对深色仍为实心声明
    """
    import re
    from 糖糖控制台_qt import _build_stylesheet  # noqa: E402
    src = open("糖糖控制台_qt.py", encoding="utf-8").read()
    # ① 危险色语义
    assert "_danger_accent = str(pal.get(\"red\"" in src
    assert "_dgr_fg = DANGER_TEXT if _dk else _danger_accent" in src
    assert "dgr_fill = _qcolor(str(self._pal.get(\"danger_bg\"" in src
    # ② 代理接线
    assert "self._pill_style = _proxy" in src
    assert "style = getattr(self, \"_pill_style\", None)" in src
    # ③④ 生成两套 QSS 核对（行为断言，非源码字面）
    def _qss(mode: str) -> str:
        # 最小 cfg 直构（load_config 会 from main import → 拉进 bot 主模块污染 pytest 捕获）
        return _build_stylesheet({"appearance": {"theme_mode": mode}})

    light, dark = _qss("light"), _qss("dark")
    headers = {"QPushButton", "QPushButton:hover", "QPushButton:focus"}
    for sel in ("QPushButton#pinkBtn", "QPushButton#greenBtn", "QPushButton#dangerBtn",
                "QPushButton#navBtn", "QPushButton#catBtn"):
        headers |= {sel + s for s in ("", ":hover", ":pressed", ":checked", ":focus")}
    seen = set()
    for m in re.finditer(r"(?m)^([^{}\n]+?)\{([^}]*)\}", light):
        header = m.group(1).strip()
        if header not in headers:
            continue
        seen.add(header)
        assert not re.search(r"(?m)^\s*border", m.group(2)), \
            f"浅色 {header} 块出现 border 声明——会杀死 bevel 委托"
    assert "QPushButton#pinkBtn" in seen and "QPushButton#navBtn:checked" in seen
    # 深色零回归：QSS 仍直接画实心面（accent 随配色，只钉存在性与固定中性值）
    dark_pink = dark.split("QPushButton#pinkBtn {")[1].split("}")[0]
    assert "background-color: " in dark_pink and "border: 1px solid transparent;" in dark_pink
    dark_dgr = dark.split("QPushButton#dangerBtn {")[1].split("}")[0]
    assert "background-color: #3a2229;" in dark_dgr      # 深色危险实心底（中性色板固定值）
    assert "border: 1px solid transparent;" in dark.split("QPushButton {")[1][:200]
    # 浅色：全局块已交给代理，不再有 border 声明
    assert "border: 1px solid transparent;" not in light.split("QPushButton {")[1][:200]
