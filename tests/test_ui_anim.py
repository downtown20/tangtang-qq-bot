import ast
from pathlib import Path


def test_ui_anim_exports_and_cleanup_contract():
    tree = ast.parse(Path("agent/ui_anim.py").read_text(encoding="utf-8"))
    names = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    assert {"fade_in", "animate_prop", "SlideStack", "HoverEngine"} <= names
    source = Path("agent/ui_anim.py").read_text(encoding="utf-8")
    assert "setGraphicsEffect(None)" in source
    assert "_transition_locked" in source


def test_ui_theme_tokens_exist():
    source = Path("糖糖控制台_qt.py").read_text(encoding="utf-8")
    for token in ("RADIUS_CONTROL", "RADIUS_CARD", "HEIGHT_CONTROL", "SPACING_UNIT"):
        assert token in source


def _console_source() -> str:
    return Path("糖糖控制台_qt.py").read_text(encoding="utf-8")


def test_navigate_uses_slide_to_not_bare_switch():
    """批 3 接入契约：_navigate 必须走 slide_to（防退回硬切）。
    解析 _navigate 函数体 AST：不得直接调 _stack.setCurrentIndex。"""
    source = _console_source()
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_navigate")
    calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call)]
    assert any("slide_to" in ast.unparse(c.func) for c in calls), "_navigate 未调用 slide_to"
    for c in calls:
        unparsed = ast.unparse(c.func)
        assert "setCurrentIndex" not in unparsed or "_stack" not in unparsed, \
            f"_navigate 存在裸 setCurrentIndex: {unparsed}"


def test_dashboard_intro_gate_and_no_replay():
    """批 4 契约：门闩 _intro_played 存在；_refresh_stats 路径不含入场 fade。"""
    source = _console_source()
    assert "_intro_played" in source  # 门闩定义与初始化
    tree = ast.parse(source)
    refresh = next(n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == "_refresh_stats")
    calls = [ast.unparse(c.func) for c in ast.walk(refresh) if isinstance(c, ast.Call)]
    assert not any("fade_in" in c for c in calls), "_refresh_stats 不应触发入场 fade（会重播）"


def test_window_fade_and_status_color_animation_wired():
    """批 3 契约：窗口启动淡入接入；状态 label 换色统一走 _set_status_color（无硬编码残留）。"""
    source = _console_source()
    assert "setWindowOpacity(0.0)" in source and "windowOpacity" in source
    assert "animate_prop(self, b\"windowOpacity\"" in source
    assert "_set_status_color(" in source
    # 状态色换色点不得残留硬编码内联（logo 静态粉除外——允许 1 处非状态调用）
    import re
    leftovers = re.findall(
        r"setStyleSheet\(f\"font-size: 1[28]px[^\"]*color: \{(GREEN|ORANGE|RED|TEXT_MUTED)\}",
        source)
    assert not leftovers, f"状态换色残留硬编码: {leftovers}"